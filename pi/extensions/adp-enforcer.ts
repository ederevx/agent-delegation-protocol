/**
 * ADP delegation enforcer for Pi.
 *
 * Adapted from the Agent Delegation Protocol host hooks: this extension only
 * adapts Pi lifecycle events to the shared adapter's normalized hook payloads.
 * Classification, state, worker budgets, and ledgers live in the installed
 * Python adapter, called through the delegation-enforcer bridge in the
 * protocol state directory (`.delegation-protocol/`, next to the adapter) so
 * no legacy `hooks/` directory exists and Pi's deprecation warning stays
 * silent. It never spawns subagents itself: delegation evidence is Pi's
 * native `subagent` tool (the official subagent extension), which this file
 * does not depend on or re-implement. When the bridge is absent the extension
 * is inert.
 *
 * Event mapping:
 *   before_agent_start          -> prompt         (classify; append routing policy)
 *   tool_call (any)             -> pre-mutation   (delegation gate / worker budget)
 *   tool_call (subagent, admit) -> worker-start   (Pi has no native start event)
 *   tool_result (subagent)      -> worker-complete
 *   turn_end                    -> turn-stop
 *
 * Worker sessions are recognized from the spawn argv the official subagent
 * extension uses (`--mode json -p --no-session`); the tier is read from the
 * spawned profile's --append-system-prompt file. An identified worker whose
 * tier cannot be read gets the adapter's conservative limit of 16.
 */

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface WorkerIdentity {
	id: string;
	tier?: string;
}

type Payload = Record<string, unknown>;

function agentHome(): string {
	return process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
}

function bridgePath(): string {
	return path.join(agentHome(), ".delegation-protocol", "delegation-enforcer.py");
}

function detectWorker(): WorkerIdentity | null {
	const argv = process.argv;
	// The official subagent extension spawns workers as one-shot processes
	// with --no-session; a parent session never carries that flag.
	if (!argv.includes("--no-session")) return null;
	const id = "pi:" + createHash("sha256").update(argv.join("\n")).digest("hex");
	let tier: string | undefined;
	const index = argv.indexOf("--append-system-prompt");
	const profilePath = index >= 0 ? argv[index + 1] : undefined;
	if (typeof profilePath === "string" && profilePath.length > 0) {
		try {
			const profile = fs.readFileSync(profilePath, "utf8");
			const match = profile.match(/^#\s+(quick|bulk|balanced|frontier)\s+worker\b/im);
			if (match) tier = `${match[1].toLowerCase()}-worker`;
		} catch {
			// Missing or unreadable profile: leave the tier unknown; the
			// adapter then pins the conservative limit of 16.
		}
	}
	return { id, tier };
}

function denialReason(response: unknown): string | null {
	const output = (response as any)?.hookSpecificOutput;
	if (output?.permissionDecision === "deny") {
		return output.permissionDecisionReason || "denied by the delegation protocol";
	}
	return null;
}

export default function (pi: ExtensionAPI) {
	const worker = detectWorker();
	const bridge = bridgePath();
	if (!fs.existsSync(bridge)) return; // not installed: stay inert

	let warnedBridgeFailure = false;
	let pythonExecutable = process.env.ADP_PYTHON || "python3";

	// A worker that keeps calling tools after a terminal deny (budget
	// exhausted, unreadable protocol state) spins forever: pi has no
	// native per-worker timeout, and the deny text alone does not
	// reliably stop a spinning model. The adapter marks such denys with
	// hookSpecificOutput.terminal. The first terminal deny buys a grace
	// period to produce the final report; the next one terminates the
	// process so the parent gets its evidence instead of a hang.
	let denyGraceTimer: NodeJS.Timeout | null = null;
	let terminated = false;

	function terminateWorker(reason: string) {
		try {
			process.stderr.write(
				`ADP: worker terminated after a terminal deny: ${reason}\n`,
			);
			process.kill(process.pid, "SIGTERM");
		} catch {
			/* already gone */
		}
	}

	function noteTerminalDeny() {
		if (terminated) return;
		if (denyGraceTimer) {
			clearTimeout(denyGraceTimer);
			denyGraceTimer = null;
			terminated = true;
			terminateWorker("terminal deny");
			return;
		}
		denyGraceTimer = setTimeout(() => {
			denyGraceTimer = null;
			terminated = true;
			terminateWorker("terminal deny grace elapsed");
		}, 60_000);
		if (denyGraceTimer.unref) denyGraceTimer.unref();
	}

	function sessionId(ctx: any): string | undefined {
		try {
			return ctx.sessionManager?.getSessionId?.() || undefined;
		} catch {
			return undefined;
		}
	}

	function callBridge(
		mode: string,
		payload: Payload,
		contextTokens?: number,
	): Promise<any | undefined> {
		return new Promise((resolve) => {
			const env: NodeJS.ProcessEnv = { ...process.env };
			if (!worker && typeof contextTokens === "number" && contextTokens > 0) {
				env.PI_CONTEXT_TOKENS = String(Math.floor(contextTokens));
			}
			execFile(
				pythonExecutable,
				[bridge, mode],
				{ env, timeout: 4000, maxBuffer: 1024 * 1024 },
				(error, stdout) => {
					if (error) {
						resolve(null);
						return;
					}
					try {
						resolve(JSON.parse(stdout || "{}"));
					} catch {
						resolve(null);
					}
				},
			).stdin?.end(JSON.stringify(payload));
		});
	}

	async function invoke(
		ctx: any,
		mode: string,
		payload: Payload,
	): Promise<any | null> {
		const response = await callBridge(mode, payload, ctx?.model?.contextWindow);
		if (response === null) {
			if (!warnedBridgeFailure) {
				warnedBridgeFailure = true;
				try {
					if (worker) {
						// Workers have no UI; make the degraded state visible
						// in the parent's captured stderr instead of silently
						// bypassing enforcement.
						process.stderr.write(
							"ADP: delegation enforcer bridge failed; enforcement is degraded\n",
						);
					} else if (ctx?.hasUI) {
						ctx.ui.notify(
							"ADP: delegation enforcer bridge failed; enforcement is degraded",
							"warning",
						);
					}
				} catch {
					/* no sink available */
				}
			}
		}
		return response;
	}

	pi.on("before_agent_start", async (event, ctx) => {
		if (worker) return; // generated profiles already carry the routing policy
		const response = await invoke(ctx, "prompt", {
			session_id: sessionId(ctx),
			prompt: event.prompt,
		});
		const additional = (response as any)?.hookSpecificOutput?.additionalContext;
		if (additional) {
			return {
				systemPrompt: `${event.systemPrompt}\n\n# Delegation protocol (ADP)\n\n${additional}`,
			};
		}
	});

	pi.on("tool_call", async (event, ctx) => {
		const input =
			event.input && typeof event.input === "object"
				? { ...(event.input as Record<string, unknown>) }
				: {};
		// The adapter reads the requested tier from `subagent_type`; Pi's
		// subagent tool names the parameter `agent`.
		if (event.toolName === "subagent" && typeof input.agent === "string") {
			input.subagent_type = input.agent;
		}
		const payload: Payload = {
			session_id: sessionId(ctx),
			tool_name: event.toolName,
			tool_input: input,
			tool_use_id: event.toolCallId,
			...(worker ? { agent_id: worker.id, agent_type: worker.tier } : {}),
		};
		const response = await invoke(ctx, "pre-mutation", payload);
		const denied =
			(response as any)?.hookSpecificOutput?.permissionDecision === "deny"
				? (response as any).hookSpecificOutput.permissionDecisionReason ||
					"denied by the delegation protocol"
				: null;
		if (denied) {
			try {
				if (ctx?.hasUI) ctx.ui.notify(`ADP: ${denied}`, "warning");
			} catch {
				/* no UI available */
			}
			if (worker && (response as any)?.hookSpecificOutput?.terminal === true) {
				noteTerminalDeny();
			}
			return { block: true, reason: denied };
		}
		// Parent side: an admitted spawn consumes its reservation at once,
		// because Pi has no native SubagentStart event -- the subagent tool
		// call itself is the start. One active slot per subagent tool call;
		// parallel tasks inside one call share that call's slot.
		if (!worker && event.toolName === "subagent") {
			await invoke(ctx, "worker-start", {
				session_id: sessionId(ctx),
				agent_id: event.toolCallId,
				tool_use_id: event.toolCallId,
			});
		}
	});

	pi.on("tool_result", async (event, ctx) => {
		if (worker || event.toolName !== "subagent") return;
		await invoke(ctx, "worker-complete", {
			session_id: sessionId(ctx),
			agent_id: event.toolCallId,
			tool_use_id: event.toolCallId,
		});
	});

	pi.on("turn_end", async (_event, ctx) => {
		if (worker) return;
		const response = await invoke(ctx, "turn-stop", { session_id: sessionId(ctx) });
		// pi has no veto point at turn end; a block decision here (protocol
		// state errors) can only be surfaced, never enforced.
		const decision = (response as any)?.decision;
		if (decision === "block") {
			const reason = (response as any)?.reason || "turn-stop blocked";
			let surfaced = false;
			try {
				if (ctx?.hasUI) {
					ctx.ui.notify(`ADP: ${reason}`, "warning");
					surfaced = true;
				}
			} catch {
				/* no UI available */
			}
			if (!surfaced) {
				try {
					process.stderr.write(`ADP: ${reason}\n`);
				} catch {
					/* no sink available */
				}
			}
		}
	});
}
