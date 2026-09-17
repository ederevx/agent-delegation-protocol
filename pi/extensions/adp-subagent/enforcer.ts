/**
 * Delegation enforcer — the ADP half of the adp-subagent extension.
 *
 * Adapted from the Agent Delegation Protocol host hooks: this module only
 * adapts Pi lifecycle events to the shared adapter's normalized hook
 * payloads. Classification, state, worker budgets, and ledgers live in the
 * installed Python adapter, called through the delegation-enforcer bridge
 * in the protocol state directory (`.delegation-protocol/`, next to the
 * adapter) so no legacy `hooks/` directory exists and Pi's deprecation
 * warning stays silent. It never spawns subagents itself: delegation
 * evidence is Pi's native `subagent` tool (registered by this same
 * extension's index.ts) — this module does not depend on or re-implement
 * that tool. When the bridge is absent the module is inert.
 *
 * Event mapping:
 *   before_agent_start          -> prompt         (classify; append routing policy)
 *   tool_call (any)             -> pre-mutation   (delegation gate / worker budget)
 *   tool_call (subagent, admit) -> worker-start   (Pi has no native start event)
 *   tool_result (subagent)      -> worker-complete
 *   turn_end                    -> turn-stop
 *
 * Worker sessions are recognized from the spawn argv the ADP-owned subagent
 * extension uses (`--mode json -p --no-session`); the tier is read from the
 * spawned profile's --append-system-prompt file. An identified worker whose
 * tier cannot be read gets the adapter's conservative limit of 16.
 *
 * Decomposed per the single-responsibility convention: BridgeClient owns
 * the transport, Reporter owns degradation/notification surfaces,
 * TerminalDenyGuard owns the grace-window policy, and AdpEnforcer maps Pi
 * events onto bridge calls. Response decoding and worker detection are
 * pure helpers; state is mutated only by its owning object.
 */

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface WorkerIdentity {
	id: string;
	tier?: string;
}

type Payload = Record<string, unknown>;

// -- pure helpers (no state; env/argv/fs arrive as parameters) ------------

function agentHome(env: NodeJS.ProcessEnv = process.env): string {
	return env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
}

function bridgePath(env: NodeJS.ProcessEnv = process.env): string {
	return path.join(agentHome(env), ".delegation-protocol", "delegation-enforcer.py");
}

function detectWorker(
	argv: string[],
	readFile: (path: string) => string = (p) => fs.readFileSync(p, "utf8"),
): WorkerIdentity | null {
	// The ADP-owned subagent extension (vendored from the official example)
	// spawns workers as one-shot processes with --no-session; a parent
	// session never carries that flag.
	if (!argv.includes("--no-session")) return null;
	const id = "pi:" + createHash("sha256").update(argv.join("\n")).digest("hex");
	let tier: string | undefined;
	const index = argv.indexOf("--append-system-prompt");
	const profilePath = index >= 0 ? argv[index + 1] : undefined;
	if (typeof profilePath === "string" && profilePath.length > 0) {
		try {
			const profile = readFile(profilePath);
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

function isTerminalDeny(response: unknown): boolean {
	return (response as any)?.hookSpecificOutput?.terminal === true;
}

function additionalContext(response: unknown): string | undefined {
	return (response as any)?.hookSpecificOutput?.additionalContext;
}

function turnStopBlock(response: unknown): string | null {
	const decision = (response as any)?.decision;
	if (decision === "block") {
		return (response as any)?.reason || "turn-stop blocked";
	}
	return null;
}

function sessionId(ctx: any): string | undefined {
	try {
		return ctx.sessionManager?.getSessionId?.() || undefined;
	} catch {
		return undefined;
	}
}

function buildToolPayload(
	event: { toolName: string; toolCallId: string; input?: unknown },
	sid: string | undefined,
	worker: WorkerIdentity | null,
): Payload {
	const input =
		event.input && typeof event.input === "object"
			? { ...(event.input as Record<string, unknown>) }
			: {};
	// The adapter reads the requested tier from `subagent_type`; Pi's
	// subagent tool names the parameter `agent`.
	if (event.toolName === "subagent" && typeof input.agent === "string") {
		input.subagent_type = input.agent;
	}
	return {
		session_id: sid,
		tool_name: event.toolName,
		tool_input: input,
		tool_use_id: event.toolCallId,
		...(worker ? { agent_id: worker.id, agent_type: worker.tier } : {}),
	};
}

// -- collaborators ---------------------------------------------------------

/** Transport to the adapter bridge: one JSON-over-execFile round trip,
 *  resolved to null on any failure. Owns nothing but its configuration. */
class BridgeClient {
	constructor(
		private readonly bridge: string,
		private readonly pythonExecutable: string,
	) {}

	call(
		mode: string,
		payload: Payload,
		options: { isWorker: boolean; contextTokens?: number },
	): Promise<any | undefined> {
		return new Promise((resolve) => {
			const env: NodeJS.ProcessEnv = { ...process.env };
			if (!options.isWorker && typeof options.contextTokens === "number" && options.contextTokens > 0) {
				env.PI_CONTEXT_TOKENS = String(Math.floor(options.contextTokens));
			}
			execFile(
				this.pythonExecutable,
				[this.bridge, mode],
				{ env, timeout: 4000, maxBuffer: 1024 * 1024 },
				(error, stdout) => {
					if (error) {
						resolve(undefined);
						return;
					}
					try {
						resolve(JSON.parse(stdout || "{}"));
					} catch {
						resolve(undefined);
					}
				},
			).stdin?.end(JSON.stringify(payload));
		});
	}
}

/** The surfaces ADP messages reach, plus the once-only bridge-degradation
 *  warning. Owns the warned flag; nothing else mutates it. */
class Reporter {
	private warnedBridgeFailure = false;

	warnBridgeFailureOnce(ctx: any, isWorker: boolean): void {
		if (this.warnedBridgeFailure) return;
		this.warnedBridgeFailure = true;
		try {
			if (isWorker) {
				// Workers have no UI; make the degraded state visible in
				// the parent's captured stderr instead of silently
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

	notify(ctx: any, text: string): void {
		try {
			if (ctx?.hasUI) ctx.ui.notify(`ADP: ${text}`, "warning");
		} catch {
			/* no UI available */
		}
	}

	surface(ctx: any, text: string): void {
		let surfaced = false;
		try {
			if (ctx?.hasUI) {
				ctx.ui.notify(`ADP: ${text}`, "warning");
				surfaced = true;
			}
		} catch {
			/* no UI available */
		}
		if (!surfaced) {
			try {
				process.stderr.write(`ADP: ${text}\n`);
			} catch {
				/* no sink available */
			}
		}
	}
}

/** Grace-window policy for terminal denys in workers: the first terminal
 *  deny starts the window and orders the worker to produce its final
 *  evidence report; later terminal denies within the window only
 *  re-deliver the deny text as tool feedback and never terminate the
 *  process. The grace kill exists only to stop a worker that never
 *  finishes, not to punish a second deny, so a winding-down worker keeps
 *  its final report instead of being killed mid-summary. */
class TerminalDenyGuard {
	private denyGraceTimer: NodeJS.Timeout | null = null;
	private terminated = false;

	constructor(private readonly reporter: Reporter) {}

	note(): void {
		if (this.terminated || this.denyGraceTimer) return;
		this.denyGraceTimer = setTimeout(() => {
			this.denyGraceTimer = null;
			this.terminated = true;
			this.terminate("terminal deny grace elapsed");
		}, 120_000);
		if (this.denyGraceTimer.unref) this.denyGraceTimer.unref();
		process.stderr.write(
			"ADP: budget exhausted; produce your final evidence report now — the process is decommissioned when the grace window closes.\n",
		);
	}

	private terminate(reason: string): void {
		try {
			process.stderr.write(
				`ADP: worker terminated after a terminal deny: ${reason}\n`,
			);
			process.kill(process.pid, "SIGTERM");
		} catch {
			/* already gone */
		}
	}
}

/** Maps Pi lifecycle events onto the adapter bridge. Owns the worker
 *  identity and its collaborators; the handlers each do one mapping. */
class AdpEnforcer {
	constructor(
		private readonly worker: WorkerIdentity | null,
		private readonly bridge: BridgeClient,
		private readonly reporter: Reporter,
		private readonly denyGuard: TerminalDenyGuard,
	) {}

	async handleAgentStart(
		event: { prompt: string; systemPrompt: string },
		ctx: any,
	): Promise<{ systemPrompt: string } | undefined> {
		if (this.worker) return; // generated profiles already carry the routing policy
		const response = await this.invoke(ctx, "prompt", {
			session_id: sessionId(ctx),
			prompt: event.prompt,
		});
		const additional = additionalContext(response);
		if (additional) {
			return {
				systemPrompt: `${event.systemPrompt}\n\n# Delegation protocol (ADP)\n\n${additional}`,
			};
		}
		return undefined;
	}

	async handleToolCall(
		event: { toolName: string; toolCallId: string; input?: unknown },
		ctx: any,
	): Promise<{ block: boolean; reason: string } | undefined> {
		const payload = buildToolPayload(event, sessionId(ctx), this.worker);
		const response = await this.invoke(ctx, "pre-mutation", payload);
		const denied = denialReason(response);
		if (denied) {
			this.reporter.notify(ctx, denied);
			if (this.worker && isTerminalDeny(response)) {
				this.denyGuard.note();
			}
			return { block: true, reason: denied };
		}
		// Parent side: an admitted spawn consumes its reservation at once,
		// because Pi has no native SubagentStart event -- the subagent tool
		// call itself is the start. Accounting is per task: a parallel
		// `tasks` call takes one active slot per task against the session
		// cap, so an N-task fan-out holds N slots, while a sequential
		// `chain` holds a single slot no matter how many steps it runs.
		if (!this.worker && event.toolName === "subagent") {
			await this.invoke(ctx, "worker-start", {
				session_id: sessionId(ctx),
				agent_id: event.toolCallId,
				tool_use_id: event.toolCallId,
			});
		}
		return undefined;
	}

	async handleToolResult(
		event: { toolName: string; toolCallId: string; input?: unknown },
		ctx: any,
	): Promise<void> {
		// Pi skips afterToolCall for blocked calls, so a tool_result means
		// the call actually executed. Charge the worker's hook-covered
		// budget here; pre-mutation only checks, because Pi fires it
		// before other extensions may block the call, and charging there
		// would spend the ledger on denied retries.
		if (this.worker) {
			const input =
				event.input && typeof event.input === "object"
					? { ...(event.input as Record<string, unknown>) }
					: {};
			await this.invoke(ctx, "post-tool-use", {
				session_id: sessionId(ctx),
				tool_name: event.toolName,
				tool_input: input,
				tool_use_id: event.toolCallId,
				agent_id: this.worker.id,
				agent_type: this.worker.tier,
			});
		}
		// Nested worker spawns must also complete: a worker that fans out
		// reserves per-task slots, and without the completion event those
		// slots leak until the stale sweep. Slot bookkeeping is not a
		// parent obligation, so workers deliver it too.
		if (event.toolName !== "subagent") return;
		await this.invoke(ctx, "worker-complete", {
			session_id: sessionId(ctx),
			agent_id: event.toolCallId,
			tool_use_id: event.toolCallId,
		});
	}

	async handleTurnEnd(_event: unknown, ctx: any): Promise<void> {
		if (this.worker) return;
		const response = await this.invoke(ctx, "turn-stop", {
			session_id: sessionId(ctx),
		});
		// pi has no veto point at turn end; a block decision here (protocol
		// state errors) can only be surfaced, never enforced.
		const reason = turnStopBlock(response);
		if (reason) {
			this.reporter.surface(ctx, reason);
		}
	}

	private async invoke(ctx: any, mode: string, payload: Payload): Promise<any | null> {
		const response = await this.bridge.call(mode, payload, {
			isWorker: this.worker !== null,
			contextTokens: ctx?.model?.contextWindow,
		});
		if (response === undefined || response === null) {
			this.reporter.warnBridgeFailureOnce(ctx, this.worker !== null);
			return null;
		}
		return response;
	}
}

export function registerDelegationEnforcer(pi: ExtensionAPI): void {
	const worker = detectWorker(process.argv);
	const bridgeFile = bridgePath();
	if (!fs.existsSync(bridgeFile)) return; // not installed: stay inert

	const reporter = new Reporter();
	const enforcer = new AdpEnforcer(
		worker,
		new BridgeClient(bridgeFile, process.env.ADP_PYTHON || "python3"),
		reporter,
		new TerminalDenyGuard(reporter),
	);

	pi.on("before_agent_start", async (event, ctx) => {
		return enforcer.handleAgentStart(event, ctx);
	});

	pi.on("tool_call", async (event, ctx) => {
		return enforcer.handleToolCall(event, ctx);
	});

	pi.on("tool_result", async (event, ctx) => {
		await enforcer.handleToolResult(event, ctx);
	});

	pi.on("turn_end", async (event, ctx) => {
		await enforcer.handleTurnEnd(event, ctx);
	});
}
