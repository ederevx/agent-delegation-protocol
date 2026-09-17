/**
 * SubagentRun — one spawned child's lifecycle
 *
 * Owns everything about a single child `pi` process: its result object,
 * its registry entry, the stdout/stderr stream parsing, the turn-budget
 * enforcement, the throttle window for parent-facing partial updates, and
 * the temp system-prompt file. It is the single writer of its `SingleResult`
 * and its `RunningSubagent` entry from spawn until close; the registry
 * (registry.ts) only owns membership and teardown handles.
 *
 * Parent-facing updates are throttled to at most one send per interval so a
 * fast stream cannot flood the parent with partial results. One timestamp,
 * no timers: emits inside the window are skipped and the next message_end
 * after it elapses carries the latest state (trailing edge). The /subagents
 * live listeners (notifyListeners) stay unthrottled — they only re-render a
 * view. `finishEntry` flushes unconditionally so the final state is never
 * delayed.
 */

import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AssistantMessage, Message } from "@earendil-works/pi-ai";
import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import { killWithEscalation, type RunningSubagent, type SubagentRegistry } from "./registry.ts";
import { getFinalOutput } from "./format.ts";
import type {
	DispatchDefaults,
	OnUpdateCallback,
	SingleResult,
	SubagentDetails,
	SubagentMode,
} from "./types.ts";
import type { AgentConfig } from "./agents.ts";

const PARENT_UPDATE_INTERVAL_MS = 250;

// Same tier heading the delegation enforcer reads from the spawned profile;
// only the generated worker profiles carry a `# <tier> Worker` heading.
const WORKER_TIER_PATTERN = /^#\s+(quick|bulk|balanced|frontier)\s+worker\b/im;

function parseWorkerTier(systemPrompt: string): string | undefined {
	const match = systemPrompt.match(WORKER_TIER_PATTERN);
	return match ? `${match[1]}-worker` : undefined;
}

/**
 * The turn-budget section appended to the worker's system prompt. It goes at
 * the bottom of the profile body so the `# <tier> Worker` heading the enforcer
 * keys on stays first.
 */
function turnBudgetSection(limit: number): string {
	return (
		"## Turn budget\n\n" +
		`This worker has a hard budget of ${limit} agentic turns, enforced by the parent.\n` +
		"The budget is the total number of model rounds in this task, not tool calls. Do not\n" +
		"start new work that cannot finish within the budget; as it nears its end,\n" +
		"deliver your final evidence report as plain text and end."
	);
}

async function writePromptToTempFile(
	agentName: string,
	prompt: string,
): Promise<{ dir: string; filePath: string }> {
	const tmpDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "pi-subagent-"));
	const safeName = agentName.replace(/[^\w.-]+/g, "_");
	const filePath = path.join(tmpDir, `prompt-${safeName}.md`);
	try {
		await withFileMutationQueue(filePath, async () => {
			await fs.promises.writeFile(filePath, prompt, { encoding: "utf-8", mode: 0o600 });
		});
	} catch (error) {
		// The caller only learns about the dir on success; remove it here so
		// a failed write cannot leak the mkdtemp directory.
		try {
			await fs.promises.rmdir(tmpDir);
		} catch {
			/* ignore */
		}
		throw error;
	}
	return { dir: tmpDir, filePath };
}

function getPiInvocation(args: string[]): { command: string; args: string[] } {
	const currentScript = process.argv[1];
	const isBunVirtualScript = currentScript?.startsWith("/$bunfs/root/");
	if (currentScript && !isBunVirtualScript && fs.existsSync(currentScript)) {
		return { command: process.execPath, args: [currentScript, ...args] };
	}

	const execName = path.basename(process.execPath).toLowerCase();
	const isGenericRuntime = /^(node|bun)(\.exe)?$/.test(execName);
	if (!isGenericRuntime) {
		return { command: process.execPath, args };
	}

	return { command: "pi", args };
}

export interface SubagentRunOptions {
	registry: SubagentRegistry;
	agents: AgentConfig[];
	defaultCwd: string;
	dispatchDefaults: DispatchDefaults;
	agentName: string;
	task: string;
	cwd?: string;
	step?: number;
	signal?: AbortSignal;
	onUpdate?: OnUpdateCallback;
	makeDetails: (results: SingleResult[]) => SubagentDetails;
	mode: SubagentMode;
	notifySpawn?: (message: string) => void;
}

export class SubagentRun {
	private readonly registry: SubagentRegistry;
	private readonly agent: AgentConfig | undefined;
	private readonly defaultCwd: string;
	private readonly dispatchDefaults: DispatchDefaults;
	private readonly agentName: string;
	private readonly task: string;
	private readonly cwd: string | undefined;
	private readonly step: number | undefined;
	private readonly signal: AbortSignal | undefined;
	private readonly onUpdate: OnUpdateCallback | undefined;
	private readonly makeDetails: (results: SingleResult[]) => SubagentDetails;
	private readonly mode: SubagentMode;
	private readonly notifySpawn: ((message: string) => void) | undefined;

	// Owned lifecycle: this run is the single writer of its result and its
	// registry entry from spawn until the child closes.
	private readonly result: SingleResult;
	private entry: RunningSubagent | null = null;
	private proc: ChildProcess | undefined;
	private lastParentEmitAt = 0;
	private buffer = "";
	private wasAborted = false;
	private budgetStopped = false;
	private tmpPromptDir: string | null = null;
	private tmpPromptPath: string | null = null;

	constructor(options: SubagentRunOptions) {
		this.registry = options.registry;
		this.defaultCwd = options.defaultCwd;
		this.dispatchDefaults = options.dispatchDefaults;
		this.agentName = options.agentName;
		this.task = options.task;
		this.cwd = options.cwd;
		this.step = options.step;
		this.signal = options.signal;
		this.onUpdate = options.onUpdate;
		this.makeDetails = options.makeDetails;
		this.mode = options.mode;
		this.notifySpawn = options.notifySpawn;
		this.agent = options.agents.find((a) => a.name === options.agentName);

		if (this.agent) {
			this.result = {
				agent: this.agentName,
				agentSource: this.agent.source,
				task: this.task,
				exitCode: -1,
				messages: [],
				stderr: "",
				usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
				tier: parseWorkerTier(this.agent.systemPrompt),
				model: this.agent.model ?? options.dispatchDefaults.model,
				step: options.step,
			};
		} else {
			const available = options.agents.map((a) => `"${a.name}"`).join(", ") || "none";
			this.result = {
				agent: this.agentName,
				agentSource: "unknown",
				task: this.task,
				exitCode: 1,
				messages: [],
				stderr: `Unknown agent: "${this.agentName}". Available agents: ${available}.`,
				usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
				step: options.step,
			};
		}
	}

	/** Spawn the child and await its exit; resolves with the final result. */
	async run(): Promise<SingleResult> {
		try {
			if (!this.agent) return this.result;
			const args = this.buildArgs();
			await this.appendSystemPrompt(args);
			args.push(`Task: ${this.task}`);
			this.result.exitCode = await this.spawnChild(args);
			if (this.wasAborted) throw new Error("Subagent was aborted");
			return this.result;
		} finally {
			this.cleanup();
		}
	}

	// ------------------------------------------------------------------
	// Spawn preparation
	// ------------------------------------------------------------------

	private buildArgs(): string[] {
		const agent = this.agent as AgentConfig;
		const args: string[] = ["--mode", "json", "-p", "--no-session"];
		const inheritsDispatchConfig = !agent.model;
		const model = agent.model ?? this.dispatchDefaults.model;
		if (model) args.push("--model", model);
		if (inheritsDispatchConfig && this.dispatchDefaults.thinkingLevel) {
			args.push("--thinking", this.dispatchDefaults.thinkingLevel);
		}
		if (agent.tools && agent.tools.length > 0) args.push("--tools", agent.tools.join(","));
		return args;
	}

	/**
	 * Stage the system prompt (plus the turn-budget section) in a temp file
	 * and add the `--append-system-prompt` argument. The budget section goes
	 * below the profile body so the `# <tier> Worker` heading the delegation
	 * enforcer keys on stays first.
	 */
	private async appendSystemPrompt(args: string[]): Promise<void> {
		const agent = this.agent as AgentConfig;
		let promptContent = agent.systemPrompt;
		if (agent.maxTurns) {
			promptContent = promptContent.trimEnd()
				? `${promptContent.trimEnd()}\n\n${turnBudgetSection(agent.maxTurns)}\n`
				: `${turnBudgetSection(agent.maxTurns)}\n`;
		}
		if (!promptContent.trim()) return;
		const tmp = await writePromptToTempFile(agent.name, promptContent);
		this.tmpPromptDir = tmp.dir;
		this.tmpPromptPath = tmp.filePath;
		args.push("--append-system-prompt", tmp.filePath);
	}

	// ------------------------------------------------------------------
	// Child process lifecycle
	// ------------------------------------------------------------------

	private spawnChild(args: string[]): Promise<number> {
		const agent = this.agent as AgentConfig;
		return new Promise<number>((resolve) => {
			const invocation = getPiInvocation(args);
			// Register before spawn so /subagents can show the child from the
			// very first moment of its run.
			this.entry = {
				id: this.registry.allocateId(),
				agent: this.agentName,
				tier: this.result.tier,
				task: this.task,
				mode: this.mode,
				startedAt: Date.now(),
				partialText: "",
				result: this.result,
				turnLimit: agent.maxTurns,
				listeners: new Set(),
			};
			this.registry.register(this.entry);

			// Strip the hosting identity: a subagent child inherits the
			// parent's PI_HOSTED/PI_HOSTED_SESSION, and its own rc-background
			// instance would then publish state (idle on start/end) and
			// announce against the PARENT's hosted session — marking a
			// working parent idle whenever a worker runs. Children are
			// plain workers, not hosted sessions.
			const childEnv = { ...process.env };
			delete childEnv.PI_HOSTED;
			delete childEnv.PI_HOSTED_SESSION;
			this.proc = spawn(invocation.command, invocation.args, {
				cwd: this.cwd ?? this.defaultCwd,
				shell: false,
				stdio: ["ignore", "pipe", "pipe"],
				env: childEnv,
			});
			this.entry.proc = this.proc;
			const proc = this.proc;

			// Full informative set the tool row used to show: agent, tier,
			// mode, turn budget, and a task-head preview (the single-agent
			// call slot now renders empty, so this notification is the sole
			// preview carrier). Never the full body. The turn budget is NOT
			// repeated here: the running row's live usage line already shows
			// the same counter (N/limit turns), so it would read twice.
			this.notifySpawn?.(
				`Subagent spawned: ${agent.name}${this.result.tier ? ` (${this.result.tier})` : ""} · ${this.mode}` +
					` — ${this.task.length > 80 ? `${this.task.slice(0, 80)}...` : this.task}`,
			);

			proc.stdout?.on("data", (data) => {
				this.buffer += data.toString();
				const lines = this.buffer.split("\n");
				this.buffer = lines.pop() || "";
				for (const line of lines) this.processLine(line);
			});

			proc.stderr?.on("data", (data) => {
				this.result.stderr += data.toString();
			});

			proc.on("close", (code) => {
				if (this.buffer.trim()) this.processLine(this.buffer);
				this.finishEntry();
				resolve(code ?? 0);
			});

			proc.on("error", () => {
				this.finishEntry();
				resolve(1);
			});

			if (this.signal) {
				const killProc = () => {
					this.wasAborted = true;
					killWithEscalation(proc);
				};
				if (this.signal.aborted) killProc();
				else this.signal.addEventListener("abort", killProc, { once: true });
			}
		});
	}

	// ------------------------------------------------------------------
	// Stream handling
	// ------------------------------------------------------------------

	/** One JSON event line from the child's stdout stream. */
	private processLine(line: string): void {
		if (!line.trim()) return;
		let event: any;
		try {
			event = JSON.parse(line);
		} catch {
			return;
		}

		if (event.type === "message_update") {
			// Stream liveness: accumulate the partial assistant text for the
			// /subagents detail view.
			if (event.assistantMessageEvent?.type === "text_delta" && this.entry) {
				this.entry.partialText += event.assistantMessageEvent.delta ?? "";
				this.notifyListeners();
			}
			return;
		}

		if (event.type === "message_end" && event.message) {
			const msg = event.message as Message;
			// Both assistant turns and tool results (message.role ===
			// "toolResult") arrive as message_end events; the push below
			// collects both. Only the assistant branch counts turns.
			this.result.messages.push(msg);

			if (msg.role === "assistant") {
				this.result.usage.turns++;
				// The completed message supersedes the streamed partial.
				if (this.entry) this.entry.partialText = "";
				this.enforceTurnBudget();
				this.accumulateUsage(msg);
				if (!this.result.model && msg.model) this.result.model = msg.model;
				if (msg.stopReason) this.result.stopReason = msg.stopReason;
				if (msg.errorMessage) this.result.errorMessage = msg.errorMessage;
			}
			this.emitUpdate();
		}

		// Tool results need no extra handling beyond the generic push of
		// message_end events: the former "tool_result_end" branch was dead
		// code (no such pi event) and has been removed.
	}

	/**
	 * Hard turn budget: stop the child without touching the tool-level abort
	 * signal, so the close handler resolves normally and no sibling work is
	 * rejected.
	 */
	private enforceTurnBudget(): void {
		const maxTurns = this.agent?.maxTurns;
		if (!maxTurns || this.budgetStopped || this.result.usage.turns < maxTurns) return;
		this.budgetStopped = true;
		this.result.turnBudgetExhausted = true;
		this.result.turnLimit = maxTurns;
		if (this.proc) killWithEscalation(this.proc);
	}

	private accumulateUsage(msg: AssistantMessage): void {
		const usage = msg.usage;
		if (!usage) return;
		this.result.usage.input += usage.input || 0;
		this.result.usage.output += usage.output || 0;
		this.result.usage.cacheRead += usage.cacheRead || 0;
		this.result.usage.cacheWrite += usage.cacheWrite || 0;
		this.result.usage.cost += usage.cost?.total || 0;
		this.result.usage.contextTokens = usage.totalTokens || 0;
	}

	// ------------------------------------------------------------------
	// Updates
	// ------------------------------------------------------------------

	/** Wake the /subagents live views; unthrottled and error-isolated. */
	private notifyListeners(): void {
		if (!this.entry) return;
		for (const listener of this.entry.listeners) {
			try {
				listener();
			} catch {
				/* listener errors must not break the stream */
			}
		}
	}

	private emitUpdate(): void {
		this.notifyListeners();
		const now = Date.now();
		if (now - this.lastParentEmitAt < PARENT_UPDATE_INTERVAL_MS) return;
		this.lastParentEmitAt = now;
		this.emitParentUpdate();
	}

	private emitParentUpdate(): void {
		if (!this.onUpdate) return;
		this.onUpdate({
			content: [{ type: "text", text: getFinalOutput(this.result.messages) || "(running...)" }],
			details: this.makeDetails([this.result]),
		});
	}

	/**
	 * Close path: retire the entry via the registry, wake any listeners
	 * watching the live result, and flush a final parent update that
	 * bypasses the throttle window.
	 */
	private finishEntry(): void {
		const entry = this.entry;
		if (!entry) return;
		this.entry = null;
		this.registry.finish(entry);
		for (const listener of entry.listeners) {
			try {
				listener();
			} catch {
				/* listener errors must not break the close path */
			}
		}
		this.lastParentEmitAt = 0;
		this.emitParentUpdate();
	}

	/** Safety net for exits without a close event, plus temp file cleanup. */
	private cleanup(): void {
		if (this.entry && !this.entry.completedAt) {
			this.registry.drop(this.entry);
			this.entry = null;
		}
		if (this.tmpPromptPath)
			try {
				fs.unlinkSync(this.tmpPromptPath);
			} catch {
				/* ignore */
			}
		if (this.tmpPromptDir)
			try {
				fs.rmdirSync(this.tmpPromptDir);
			} catch {
				/* ignore */
			}
	}
}
