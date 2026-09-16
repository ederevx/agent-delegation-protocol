/**
 * Subagent Tool - Delegate tasks to specialized agents
 *
 * Spawns a separate `pi` process for each subagent invocation,
 * giving it an isolated context window.
 *
 * Supports three modes:
 *   - Single: { agent: "name", task: "..." }
 *   - Parallel: { tasks: [{ agent: "name", task: "..." }, ...] }
 *   - Chain: { chain: [{ agent: "name", task: "... {previous} ..." }, ...] }
 *
 * Uses JSON mode to capture structured output from subagents.
 *
 * Vendored from: @earendil-works/pi-coding-agent v0.85.1 (examples/extensions/subagent, MIT)
 * Upstream: https://www.npmjs.com/package/@earendil-works/pi-coding-agent
 * Maintained by: Agent Delegation Protocol
 * Local modifications: maxTurns turn budgets; child registry (numeric ids,
 *   recentSubagents ring with proc handles stripped, streamed partialText) +
 *   /subagents command; spawn notifications; tool_result_end dead-branch
 *   removal; expanded renderer restored with turn-budget markers; parallel
 *   fan-out (MAX_PARALLEL_TASKS) and child concurrency (MAX_CONCURRENCY)
 *   raised to 10 to match the ADP active-worker cap; /subagents UI: a
 *   settings-styled selector that groups entries under Active/Inactive
 *   headers (active first) with no window chrome, plus a borderless
 *   full-screen mouse+keyboard detail viewer; throttled parent updates
 *   (≤4/s); steering Enter while workers are active interrupts the
 *   workers and delivers the message non-queued before the next LLM call
 */

import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentToolResult, ThinkingLevel } from "@earendil-works/pi-agent-core";
import type { Message } from "@earendil-works/pi-ai";
import { StringEnum } from "@earendil-works/pi-ai";
import {
	CONFIG_DIR_NAME,
	type ExtensionAPI,
	DynamicBorder,
	getAgentDir,
	getMarkdownTheme,
	getSettingsListTheme,
	withFileMutationQueue,
} from "@earendil-works/pi-coding-agent";
import {
	Container,
	Markdown,
	matchesKey,
	Spacer,
	Text,
	type TuiMouseEvent,
	truncateToWidth,
	visibleWidth,
	wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import { Type } from "typebox";
import { type AgentConfig, type AgentScope, discoverAgents } from "./agents.ts";

const MAX_PARALLEL_TASKS = 10;
const MAX_CONCURRENCY = 10;
const COLLAPSED_ITEM_COUNT = 10;
const PER_TASK_OUTPUT_CAP = 50 * 1024;

function formatTokens(count: number): string {
	if (count < 1000) return count.toString();
	if (count < 10000) return `${(count / 1000).toFixed(1)}k`;
	if (count < 1000000) return `${Math.round(count / 1000)}k`;
	return `${(count / 1000000).toFixed(1)}M`;
}

/**
 * Upstream usage formatter, extended with an optional `turnLimit`: when a
 * turn budget is known, turns print as `used/limit` instead of a bare count.
 */
function formatUsageStats(
	usage: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		cost: number;
		contextTokens?: number;
		turns?: number;
	},
	model?: string,
	turnLimit?: number,
): string {
	const parts: string[] = [];
	if (usage.turns)
		parts.push(
			turnLimit
				? `${usage.turns}/${turnLimit} turns`
				: `${usage.turns} turn${usage.turns > 1 ? "s" : ""}`,
		);
	if (usage.input) parts.push(`↑${formatTokens(usage.input)}`);
	if (usage.output) parts.push(`↓${formatTokens(usage.output)}`);
	if (usage.cacheRead) parts.push(`R${formatTokens(usage.cacheRead)}`);
	if (usage.cacheWrite) parts.push(`W${formatTokens(usage.cacheWrite)}`);
	if (usage.cost) parts.push(`$${usage.cost.toFixed(4)}`);
	if (usage.contextTokens && usage.contextTokens > 0) {
		parts.push(`ctx:${formatTokens(usage.contextTokens)}`);
	}
	if (model) parts.push(model);
	return parts.join(" ");
}

function formatToolCall(
	toolName: string,
	args: Record<string, unknown>,
	themeFg: (color: any, text: string) => string,
	// `full` drops the width-based preview truncation for the /subagents
	// detail view; the caller word-wraps the result at the real width.
	full = false,
): string {
	const shortenPath = (p: string) => {
		const home = os.homedir();
		return p.startsWith(home) ? `~${p.slice(home.length)}` : p;
	};

	switch (toolName) {
		case "bash": {
			const command = (args.command as string) || "...";
			const preview = !full && command.length > 60 ? `${command.slice(0, 60)}...` : command;
			return themeFg("muted", "$ ") + themeFg("toolOutput", preview);
		}
		case "read": {
			const rawPath = (args.file_path || args.path || "...") as string;
			const filePath = shortenPath(rawPath);
			const offset = args.offset as number | undefined;
			const limit = args.limit as number | undefined;
			let text = themeFg("accent", filePath);
			if (offset !== undefined || limit !== undefined) {
				const startLine = offset ?? 1;
				const endLine = limit !== undefined ? startLine + limit - 1 : "";
				text += themeFg("warning", `:${startLine}${endLine ? `-${endLine}` : ""}`);
			}
			return themeFg("muted", "read ") + text;
		}
		case "write": {
			const rawPath = (args.file_path || args.path || "...") as string;
			const filePath = shortenPath(rawPath);
			const content = (args.content || "") as string;
			const lines = content.split("\n").length;
			let text = themeFg("muted", "write ") + themeFg("accent", filePath);
			if (lines > 1) text += themeFg("dim", ` (${lines} lines)`);
			return text;
		}
		case "edit": {
			const rawPath = (args.file_path || args.path || "...") as string;
			return themeFg("muted", "edit ") + themeFg("accent", shortenPath(rawPath));
		}
		case "ls": {
			const rawPath = (args.path || ".") as string;
			return themeFg("muted", "ls ") + themeFg("accent", shortenPath(rawPath));
		}
		case "find": {
			const pattern = (args.pattern || "*") as string;
			const rawPath = (args.path || ".") as string;
			return themeFg("muted", "find ") + themeFg("accent", pattern) + themeFg("dim", ` in ${shortenPath(rawPath)}`);
		}
		case "grep": {
			const pattern = (args.pattern || "") as string;
			const rawPath = (args.path || ".") as string;
			return (
				themeFg("muted", "grep ") +
				themeFg("accent", `/${pattern}/`) +
				themeFg("dim", ` in ${shortenPath(rawPath)}`)
			);
		}
		default: {
			const argsStr = JSON.stringify(args);
			const preview = !full && argsStr.length > 50 ? `${argsStr.slice(0, 50)}...` : argsStr;
			return themeFg("accent", toolName) + themeFg("dim", ` ${preview}`);
		}
	}
}

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

interface RunningSubagent {
	id: number;
	agent: string;
	tier?: string;
	task: string;
	mode: "single" | "parallel-task" | "chain-step";
	// Accumulated assistant text_delta output for the in-flight turn; cleared
	// when the matching assistant message_end arrives.
	partialText: string;
	// Epoch milliseconds.
	startedAt: number;
	completedAt?: Date;
	proc?: ChildProcess;
	// The live SingleResult; mutated in place as stream events arrive.
	result: SingleResult;
	turnLimit?: number;
	listeners: Set<() => void>;
}

// Registry backing /subagents. Entries live from spawn until the child
// closes; on close an entry moves to `recent` (last 10 kept, each with
// completedAt and its final result, and the child-process handle stripped
// so finished entries never pin a proc), so a killed or errored worker never
// leaks a running slot.
const runningSubagents = new Map<number, RunningSubagent>();
const recentSubagents: RunningSubagent[] = [];
const MAX_RECENT_SUBAGENTS = 10;
let nextSubagentId = 1;

export const getRunningSubagents = (): IterableIterator<RunningSubagent> =>
	runningSubagents.values();
export const getRecentSubagents = (): readonly RunningSubagent[] => recentSubagents;

// Registry facade over the module state above; the runtime layout carries
// these as SubagentRegistry methods. The repo registry is module state
// rather than a class instance, so `runningSubagents` stands in for the
// private `running` map and the method bodies are otherwise verbatim.
const registry = {
	/** Running entries that still have a live child-process handle. */
	activeWithProc(): RunningSubagent[] {
		return [...runningSubagents.values()].filter((entry) => entry.proc);
	},

	/** Steering interrupt: SIGTERM every active child (escalating later). */
	interruptActive(): void {
		for (const entry of registry.activeWithProc()) {
			if (entry.proc) killWithEscalation(entry.proc);
		}
	},
};

interface UsageStats {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	cost: number;
	contextTokens: number;
	turns: number;
}

interface SingleResult {
	agent: string;
	agentSource: "user" | "project" | "unknown";
	task: string;
	// -1 while the child is still running; the real code is assigned on close.
	exitCode: number;
	messages: Message[];
	stderr: string;
	usage: UsageStats;
	tier?: string;
	model?: string;
	stopReason?: string;
	errorMessage?: string;
	turnBudgetExhausted?: boolean;
	turnLimit?: number;
	step?: number;
}

interface SubagentDetails {
	mode: "single" | "parallel" | "chain";
	agentScope: AgentScope;
	projectAgentsDir: string | null;
	results: SingleResult[];
}

function getFinalOutput(messages: Message[]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const msg = messages[i];
		if (msg.role === "assistant") {
			for (const part of msg.content) {
				if (part.type === "text") return part.text;
			}
		}
	}
	return "";
}

type DisplayItem =
	| { type: "text"; text: string }
	| { type: "toolCall"; name: string; args: Record<string, any> }
	| { type: "toolResult"; toolName: string; text: string; isError: boolean };

const TOOL_RESULT_PREVIEW_LINES = 10;

// Indented tool-result preview for the /subagents detail transcript, capped
// at a fixed number of lines per result.
function indentPreview(text: string): string {
	return text
		.split("\n")
		.slice(0, TOOL_RESULT_PREVIEW_LINES)
		.map((line) => `  ${line}`)
		.join("\n");
}

// Assistant text, tool calls, and tool results, in stream order; backs the
// /subagents activity view.
function getDisplayItems(messages: Message[]): DisplayItem[] {
	const items: DisplayItem[] = [];
	for (const msg of messages) {
		if (msg.role === "assistant") {
			for (const part of msg.content) {
				if (part.type === "text") items.push({ type: "text", text: part.text });
				else if (part.type === "toolCall")
					items.push({ type: "toolCall", name: part.name, args: part.arguments as Record<string, any> });
			}
		} else if (msg.role === "toolResult") {
			const text = msg.content
				.filter((c) => c.type === "text")
				.map((c) => c.text)
				.join("\n");
			if (text.trim())
				items.push({ type: "toolResult", toolName: msg.toolName, text, isError: msg.isError });
		}
	}
	return items;
}

function isFailedResult(result: SingleResult): boolean {
	// A still-running child (exitCode -1) is neither failed nor finished.
	if (result.exitCode === -1) return false;
	// A budget-exhausted child was terminated by us on purpose, so its exit
	// code and stop reason look like an abort; that is not a task failure.
	if (result.turnBudgetExhausted) return false;
	return result.exitCode !== 0 || result.stopReason === "error" || result.stopReason === "aborted";
}

function getResultOutput(result: SingleResult): string {
	if (isFailedResult(result)) {
		return result.errorMessage || result.stderr || getFinalOutput(result.messages) || "(no output)";
	}
	return getFinalOutput(result.messages) || "(no output)";
}

function truncateParallelOutput(output: string): string {
	const byteLength = Buffer.byteLength(output, "utf8");
	if (byteLength <= PER_TASK_OUTPUT_CAP) return output;

	let truncated = output.slice(0, PER_TASK_OUTPUT_CAP);
	while (Buffer.byteLength(truncated, "utf8") > PER_TASK_OUTPUT_CAP) {
		truncated = truncated.slice(0, -1);
	}
	return `${truncated}\n\n[Output truncated: ${byteLength - Buffer.byteLength(truncated, "utf8")} bytes omitted. Full output preserved in tool details.]`;
}

async function mapWithConcurrencyLimit<TIn, TOut>(
	items: TIn[],
	concurrency: number,
	fn: (item: TIn, index: number) => Promise<TOut>,
): Promise<TOut[]> {
	if (items.length === 0) return [];
	const limit = Math.max(1, Math.min(concurrency, items.length));
	const results: TOut[] = new Array(items.length);
	let nextIndex = 0;
	const workers = new Array(limit).fill(null).map(async () => {
		while (true) {
			const current = nextIndex++;
			if (current >= items.length) return;
			results[current] = await fn(items[current], current);
		}
	});
	await Promise.all(workers);
	return results;
}

async function writePromptToTempFile(agentName: string, prompt: string): Promise<{ dir: string; filePath: string }> {
	const tmpDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), "pi-subagent-"));
	const safeName = agentName.replace(/[^\w.-]+/g, "_");
	const filePath = path.join(tmpDir, `prompt-${safeName}.md`);
	await withFileMutationQueue(filePath, async () => {
		await fs.promises.writeFile(filePath, prompt, { encoding: "utf-8", mode: 0o600 });
	});
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

/**
 * SIGTERM the child, escalating to SIGKILL after five seconds if it is still
 * running. `subprocess.killed` is true the moment kill() is called, so the
 * escalation checks the exit code and signal instead.
 */
function killWithEscalation(proc: ChildProcess): void {
	proc.kill("SIGTERM");
	const timer = setTimeout(() => {
		if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
	}, 5000);
	timer.unref();
}

type OnUpdateCallback = (partial: AgentToolResult<SubagentDetails>) => void;

interface DispatchDefaults {
	model?: string;
	thinkingLevel?: ThinkingLevel;
}

async function runSingleAgent(
	defaultCwd: string,
	dispatchDefaults: DispatchDefaults,
	agents: AgentConfig[],
	agentName: string,
	task: string,
	cwd: string | undefined,
	step: number | undefined,
	signal: AbortSignal | undefined,
	onUpdate: OnUpdateCallback | undefined,
	makeDetails: (results: SingleResult[]) => SubagentDetails,
	mode: "single" | "parallel-task" | "chain-step",
	notifySpawn: ((message: string) => void) | undefined,
): Promise<SingleResult> {
	const agent = agents.find((a) => a.name === agentName);

	if (!agent) {
		const available = agents.map((a) => `"${a.name}"`).join(", ") || "none";
		return {
			agent: agentName,
			agentSource: "unknown",
			task,
			exitCode: 1,
			messages: [],
			stderr: `Unknown agent: "${agentName}". Available agents: ${available}.`,
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
			step,
		};
	}

	const args: string[] = ["--mode", "json", "-p", "--no-session"];
	const inheritsDispatchConfig = !agent.model;
	const model = agent.model ?? dispatchDefaults.model;
	if (model) args.push("--model", model);
	if (inheritsDispatchConfig && dispatchDefaults.thinkingLevel) {
		args.push("--thinking", dispatchDefaults.thinkingLevel);
	}
	if (agent.tools && agent.tools.length > 0) args.push("--tools", agent.tools.join(","));

	let tmpPromptDir: string | null = null;
	let tmpPromptPath: string | null = null;

	const tier = parseWorkerTier(agent.systemPrompt);
	const currentResult: SingleResult = {
		agent: agentName,
		agentSource: agent.source,
		task,
		exitCode: -1,
		messages: [],
		stderr: "",
		usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
		tier,
		model,
		step,
	};

	// Registry entry and listener notification. The entry carries the live
	// result object, so a /subagents-style consumer sees stream updates.
	let entry: RunningSubagent | null = null;
	// Parent-facing onUpdate is throttled to at most one send per interval so
	// a fast stream cannot flood the parent with partial results. One
	// timestamp, no timers: emits inside the window are skipped and the next
	// message_end after it elapses carries the latest state (trailing edge).
	// The /subagents live listeners (notifyListeners) stay unthrottled — they
	// only re-render an overlay. finishEntry flushes unconditionally so the
	// final state is never delayed.
	const PARENT_UPDATE_INTERVAL_MS = 250;
	let lastParentEmitAt = 0;
	const emitParentUpdate = () => {
		if (!onUpdate) return;
		onUpdate({
			content: [{ type: "text", text: getFinalOutput(currentResult.messages) || "(running...)" }],
			details: makeDetails([currentResult]),
		});
	};
	// Close/error path: move the entry out of `running` into the recent ring,
	// stamp completion time, strip the child-process handle, and wake any
	// listeners watching the live result.
	const finishEntry = () => {
		if (!entry) return;
		runningSubagents.delete(entry.id);
		entry.completedAt = new Date();
		entry.proc = undefined;
		recentSubagents.push(entry);
		if (recentSubagents.length > MAX_RECENT_SUBAGENTS)
			recentSubagents.splice(0, recentSubagents.length - MAX_RECENT_SUBAGENTS);
		for (const listener of entry.listeners) {
			try {
				listener();
			} catch {
				/* listener errors must not break the close path */
			}
		}
		entry = null;
		// Immediate final flush, bypassing the throttle window.
		lastParentEmitAt = 0;
		emitParentUpdate();
	};
	const notifyListeners = () => {
		if (!entry) return;
		for (const listener of entry.listeners) {
			try {
				listener();
			} catch {
				/* listener errors must not break the stream */
			}
		}
	};

	const emitUpdate = () => {
		notifyListeners();
		const now = Date.now();
		if (now - lastParentEmitAt < PARENT_UPDATE_INTERVAL_MS) return;
		lastParentEmitAt = now;
		emitParentUpdate();
	};

	try {
		// The turn-budget section is appended to the same temp profile file,
		// below the profile body, so the `# <tier> Worker` heading stays first
		// for the delegation enforcer.
		let promptContent = agent.systemPrompt;
		if (agent.maxTurns) {
			promptContent = promptContent.trimEnd()
				? `${promptContent.trimEnd()}\n\n${turnBudgetSection(agent.maxTurns)}\n`
				: `${turnBudgetSection(agent.maxTurns)}\n`;
		}
		if (promptContent.trim()) {
			const tmp = await writePromptToTempFile(agent.name, promptContent);
			tmpPromptDir = tmp.dir;
			tmpPromptPath = tmp.filePath;
			args.push("--append-system-prompt", tmpPromptPath);
		}

		args.push(`Task: ${task}`);
		let wasAborted = false;
		let budgetStopped = false;

		const exitCode = await new Promise<number>((resolve) => {
			const invocation = getPiInvocation(args);
			// Register before spawn so /subagents can show the child from the
			// very first moment of its run.
			entry = {
				id: nextSubagentId++,
				agent: agentName,
				tier,
				task,
				mode,
				startedAt: Date.now(),
				partialText: "",
				result: currentResult,
				turnLimit: agent.maxTurns,
				listeners: new Set(),
			};
			runningSubagents.set(entry.id, entry);

			const proc = spawn(invocation.command, invocation.args, {
				cwd: cwd ?? defaultCwd,
				shell: false,
				stdio: ["ignore", "pipe", "pipe"],
			});
			entry.proc = proc;

			// Agent, tier, and a short task-head preview only; mode and turn
			// budget ride along when known at spawn time. Never the full body.
			notifySpawn?.(
				`Subagent spawned: ${agent.name}${tier ? ` (${tier})` : ""} · ${mode}` +
					(agent.maxTurns ? ` · turns≤${agent.maxTurns}` : "") +
					` — ${task.length > 60 ? `${task.slice(0, 60)}...` : task}`,
			);

			let buffer = "";

			const processLine = (line: string) => {
				if (!line.trim()) return;
				let event: any;
				try {
					event = JSON.parse(line);
				} catch {
					return;
				}

				if (event.type === "message_update") {
					// Stream liveness: accumulate the partial assistant text for
					// the /subagents detail view.
					if (event.assistantMessageEvent?.type === "text_delta" && entry) {
						entry.partialText += event.assistantMessageEvent.delta ?? "";
						notifyListeners();
					}
					return;
				}

				if (event.type === "message_end" && event.message) {
					const msg = event.message as Message;
					// Both assistant turns and tool results (message.role ===
					// "toolResult") arrive as message_end events; the push below
					// collects both. Only the assistant branch increments turns.
					currentResult.messages.push(msg);

					if (msg.role === "assistant") {
						currentResult.usage.turns++;
						// The completed message supersedes the streamed partial.
						if (entry) entry.partialText = "";
						// Hard turn budget: stop the child without touching the
						// tool-level abort signal, so the close handler resolves
						// normally and no sibling work is rejected.
						if (agent.maxTurns && !budgetStopped && currentResult.usage.turns >= agent.maxTurns) {
							budgetStopped = true;
							currentResult.turnBudgetExhausted = true;
							currentResult.turnLimit = agent.maxTurns;
							killWithEscalation(proc);
						}
						const usage = msg.usage;
						if (usage) {
							currentResult.usage.input += usage.input || 0;
							currentResult.usage.output += usage.output || 0;
							currentResult.usage.cacheRead += usage.cacheRead || 0;
							currentResult.usage.cacheWrite += usage.cacheWrite || 0;
							currentResult.usage.cost += usage.cost?.total || 0;
							currentResult.usage.contextTokens = usage.totalTokens || 0;
						}
						if (!currentResult.model && msg.model) currentResult.model = msg.model;
						if (msg.stopReason) currentResult.stopReason = msg.stopReason;
						if (msg.errorMessage) currentResult.errorMessage = msg.errorMessage;
					}
					emitUpdate();
				}

				// Tool results need no extra handling beyond the generic push
				// of message_end events: the former "tool_result_end" branch
				// was dead code (no such pi event) and has been removed.
			};

			proc.stdout.on("data", (data) => {
				buffer += data.toString();
				const lines = buffer.split("\n");
				buffer = lines.pop() || "";
				for (const line of lines) processLine(line);
			});

			proc.stderr.on("data", (data) => {
				currentResult.stderr += data.toString();
			});

			proc.on("close", (code) => {
				if (buffer.trim()) processLine(buffer);
				finishEntry();
				resolve(code ?? 0);
			});

			proc.on("error", () => {
				finishEntry();
				resolve(1);
			});

			if (signal) {
				const killProc = () => {
					wasAborted = true;
					killWithEscalation(proc);
				};
				if (signal.aborted) killProc();
				else signal.addEventListener("abort", killProc, { once: true });
			}
		});

		currentResult.exitCode = exitCode;
		if (wasAborted) throw new Error("Subagent was aborted");
		return currentResult;
	} finally {
		// Safety net: if we exit without a close event (spawn threw), drop the
		// entry rather than leak it. The close path has already finished it.
		if (entry && !entry.completedAt) {
			runningSubagents.delete(entry.id);
			entry = null;
		}
		if (tmpPromptPath)
			try {
				fs.unlinkSync(tmpPromptPath);
			} catch {
				/* ignore */
			}
		if (tmpPromptDir)
			try {
				fs.rmdirSync(tmpPromptDir);
			} catch {
				/* ignore */
			}
	}
}

const TaskItem = Type.Object({
	agent: Type.String({ description: "Name of the agent to invoke" }),
	task: Type.String({ description: "Task to delegate to the agent" }),
	cwd: Type.Optional(Type.String({ description: "Working directory for the agent process" })),
});

const ChainItem = Type.Object({
	agent: Type.String({ description: "Name of the agent to invoke" }),
	task: Type.String({ description: "Task with optional {previous} placeholder for prior output" }),
	cwd: Type.Optional(Type.String({ description: "Working directory for the agent process" })),
});

const AgentScopeSchema = StringEnum(["user", "project", "both"] as const, {
	description: 'Which agent directories to use. Default: "user". Use "both" to include project-local agents.',
	default: "user",
});

const SubagentParams = Type.Object({
	agent: Type.Optional(Type.String({ description: "Name of the agent to invoke (for single mode)" })),
	task: Type.Optional(Type.String({ description: "Task to delegate (for single mode)" })),
	tasks: Type.Optional(Type.Array(TaskItem, { description: "Array of {agent, task} for parallel execution" })),
	chain: Type.Optional(Type.Array(ChainItem, { description: "Array of {agent, task} for sequential execution" })),
	agentScope: Type.Optional(AgentScopeSchema),
	confirmProjectAgents: Type.Optional(
		Type.Boolean({ description: "Prompt before running project-local agents. Default: true.", default: true }),
	),
	cwd: Type.Optional(Type.String({ description: "Working directory for the agent process (single mode)" })),
});

export default function (pi: ExtensionAPI) {
	// A steering message (typed Enter) while the parent is only waiting on
	// subagent workers must not sit queued until a long worker finishes:
	// interrupt the workers (they are resumable; their partial results stay
	// in the tool result) and re-inject the text as steering, so it is
	// delivered before the parent's very next LLM call — a non-queued
	// message even though the session shows "Working". Alt+Enter
	// (streamingBehavior "followUp") keeps its explicit queue-until-done
	// meaning, and extension-injected messages pass through untouched.
	pi.on("input", async (event) => {
		if (event.source === "extension") return { action: "continue" };
		if (event.streamingBehavior !== "steer") return { action: "continue" };
		if (registry.activeWithProc().length === 0) return { action: "continue" };
		registry.interruptActive();
		// sendUserMessage is typed void but the runtime returns a promise;
		// Promise.resolve absorbs either so a rejection cannot surface.
		void Promise.resolve(pi.sendUserMessage(event.text, { deliverAs: "steer" })).catch(() => {});
		return { action: "handled" };
	});

	pi.registerTool({
		name: "subagent",
		label: "Subagent",
		description: [
			"Delegate tasks to specialized subagents with isolated context.",
			"Modes: single (agent + task), parallel (tasks array), chain (sequential with {previous} placeholder).",
			`Default agent scope is "user" (from ${path.join(getAgentDir(), "agents")}).`,
			`To enable project-local agents in ${CONFIG_DIR_NAME}/agents, set agentScope: "both" (or "project").`,
		].join(" "),
		parameters: SubagentParams,

		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			const agentScope: AgentScope = params.agentScope ?? "user";
			const dispatchDefaults: DispatchDefaults = {
				model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
				thinkingLevel: ctx.thinkingLevel,
			};
			const discovery = discoverAgents(ctx.cwd, agentScope);
			const agents = discovery.agents;
			const confirmProjectAgents = params.confirmProjectAgents ?? true;
			// Headless-safe: ui.notify is a no-op without a UI; guarded anyway.
			const spawnNotify = (message: string) => {
				try {
					ctx.ui.notify(message, "info");
				} catch {
					/* ignore */
				}
			};

			const hasChain = (params.chain?.length ?? 0) > 0;
			const hasTasks = (params.tasks?.length ?? 0) > 0;
			const hasSingle = Boolean(params.agent && params.task);
			const modeCount = Number(hasChain) + Number(hasTasks) + Number(hasSingle);

			const makeDetails =
				(mode: "single" | "parallel" | "chain") =>
				(results: SingleResult[]): SubagentDetails => ({
					mode,
					agentScope,
					projectAgentsDir: discovery.projectAgentsDir,
					results,
				});

			if (modeCount !== 1) {
				const available = agents.map((a) => `${a.name} (${a.source})`).join(", ") || "none";
				return {
					content: [
						{
							type: "text",
							text: `Invalid parameters. Provide exactly one mode.\nAvailable agents: ${available}`,
						},
					],
					details: makeDetails("single")([]),
				};
			}

			if (
				(agentScope === "project" || agentScope === "both") &&
				confirmProjectAgents &&
				ctx.hasUI &&
				!ctx.isProjectTrusted()
			) {
				const requestedAgentNames = new Set<string>();
				if (params.chain) for (const step of params.chain) requestedAgentNames.add(step.agent);
				if (params.tasks) for (const t of params.tasks) requestedAgentNames.add(t.agent);
				if (params.agent) requestedAgentNames.add(params.agent);

				const projectAgentsRequested = Array.from(requestedAgentNames)
					.map((name) => agents.find((a) => a.name === name))
					.filter((a): a is AgentConfig => a?.source === "project");

				if (projectAgentsRequested.length > 0) {
					const names = projectAgentsRequested.map((a) => a.name).join(", ");
					const dir = discovery.projectAgentsDir ?? "(unknown)";
					const ok = await ctx.ui.confirm(
						"Run project-local agents?",
						`Agents: ${names}\nSource: ${dir}\n\nProject agents are repo-controlled. Only continue for trusted repositories.`,
					);
					if (!ok)
						return {
							content: [{ type: "text", text: "Canceled: project-local agents not approved." }],
							details: makeDetails(hasChain ? "chain" : hasTasks ? "parallel" : "single")([]),
						};
				}
			}

			if (params.chain && params.chain.length > 0) {
				const results: SingleResult[] = [];
				let previousOutput = "";

				for (let i = 0; i < params.chain.length; i++) {
					const step = params.chain[i];
					const taskWithContext = step.task.replace(/\{previous\}/g, previousOutput);

					// Create update callback that includes all previous results
					const chainUpdate: OnUpdateCallback | undefined = onUpdate
						? (partial) => {
								// Combine completed results with current streaming result
								const currentResult = partial.details?.results[0];
								if (currentResult) {
									const allResults = [...results, currentResult];
									onUpdate({
										content: partial.content,
										details: makeDetails("chain")(allResults),
									});
								}
							}
						: undefined;

					const result = await runSingleAgent(
						ctx.cwd,
						dispatchDefaults,
						agents,
						step.agent,
						taskWithContext,
						step.cwd,
						i + 1,
						signal,
						chainUpdate,
						makeDetails("chain"),
						"chain-step",
						spawnNotify,
					);
					results.push(result);

					const isError = isFailedResult(result);
					if (isError) {
						const errorMsg = getResultOutput(result);
						return {
							content: [{ type: "text", text: `Chain stopped at step ${i + 1} (${step.agent}): ${errorMsg}` }],
							details: makeDetails("chain")(results),
							isError: true,
						};
					}
					previousOutput = getFinalOutput(result.messages);
				}
				const last = results[results.length - 1];
				let text = getFinalOutput(last.messages) || "(no output)";
				if (last.turnBudgetExhausted) {
					text += `\n\n— turn budget exhausted (${last.usage.turns}/${last.turnLimit} turns)`;
				}
				return {
					content: [{ type: "text", text }],
					details: makeDetails("chain")(results),
				};
			}

			if (params.tasks && params.tasks.length > 0) {
				if (params.tasks.length > MAX_PARALLEL_TASKS)
					return {
						content: [
							{
								type: "text",
								text: `Too many parallel tasks (${params.tasks.length}). Max is ${MAX_PARALLEL_TASKS}.`,
							},
						],
						details: makeDetails("parallel")([]),
					};

				// Track all results for streaming updates
				const allResults: SingleResult[] = new Array(params.tasks.length);

				// Initialize placeholder results
				for (let i = 0; i < params.tasks.length; i++) {
					allResults[i] = {
						agent: params.tasks[i].agent,
						agentSource: "unknown",
						task: params.tasks[i].task,
						exitCode: -1, // -1 = still running
						messages: [],
						stderr: "",
						usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
					};
				}

				const emitParallelUpdate = () => {
					if (onUpdate) {
						const running = allResults.filter((r) => r.exitCode === -1).length;
						const done = allResults.filter((r) => r.exitCode !== -1).length;
						onUpdate({
							content: [
								{ type: "text", text: `Parallel: ${done}/${allResults.length} done, ${running} running...` },
							],
							details: makeDetails("parallel")([...allResults]),
						});
					}
				};

				const results = await mapWithConcurrencyLimit(params.tasks, MAX_CONCURRENCY, async (t, index) => {
					const result = await runSingleAgent(
						ctx.cwd,
						dispatchDefaults,
						agents,
						t.agent,
						t.task,
						t.cwd,
						undefined,
						signal,
						// Per-task update callback
						(partial) => {
							if (partial.details?.results[0]) {
								allResults[index] = partial.details.results[0];
								emitParallelUpdate();
							}
						},
						makeDetails("parallel"),
						"parallel-task",
						spawnNotify,
					);
					allResults[index] = result;
					emitParallelUpdate();
					return result;
				});

				const successCount = results.filter((r) => !isFailedResult(r)).length;
				const summaries = results.map((r) => {
					const output = truncateParallelOutput(getResultOutput(r));
					let status: string;
					if (isFailedResult(r)) {
						status = `failed${r.stopReason && r.stopReason !== "end" ? ` (${r.stopReason})` : ""}`;
					} else if (r.turnBudgetExhausted) {
						status = `completed — turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`;
					} else {
						status = "completed";
					}
					return `### [${r.agent}] ${status}\n\n${output}`;
				});
				return {
					content: [
						{
							type: "text",
							text: `Parallel: ${successCount}/${results.length} succeeded\n\n${summaries.join("\n\n---\n\n")}`,
						},
					],
					details: makeDetails("parallel")(results),
				};
			}

			if (params.agent && params.task) {
				const result = await runSingleAgent(
					ctx.cwd,
					dispatchDefaults,
					agents,
					params.agent,
					params.task,
					params.cwd,
					undefined,
					signal,
					onUpdate,
					makeDetails("single"),
					"single",
					spawnNotify,
				);
				const isError = isFailedResult(result);
				if (isError) {
					const errorMsg = getResultOutput(result);
					return {
						content: [{ type: "text", text: `Agent ${result.stopReason || "failed"}: ${errorMsg}` }],
						details: makeDetails("single")([result]),
						isError: true,
					};
				}
				let text = getFinalOutput(result.messages) || "(no output)";
				if (result.turnBudgetExhausted) {
					text += `\n\n— turn budget exhausted (${result.usage.turns}/${result.turnLimit} turns)`;
				}
				return {
					content: [{ type: "text", text }],
					details: makeDetails("single")([result]),
				};
			}

			const available = agents.map((a) => `${a.name} (${a.source})`).join(", ") || "none";
			return {
				content: [{ type: "text", text: `Invalid parameters. Available agents: ${available}` }],
				details: makeDetails("single")([]),
			};
		},

		renderCall(args, theme, _context) {
			const scope = args.agentScope as AgentScope | undefined;
			const scopeSuffix = scope && scope !== "user" ? theme.fg("muted", ` [${scope}]`) : "";
			if (args.chain && args.chain.length > 0) {
				return new Text(
					theme.fg("toolTitle", theme.bold("subagent ")) +
						theme.fg("accent", `chain (${args.chain.length} steps)`) +
						scopeSuffix,
					0,
					0,
				);
			}
			if (args.tasks && args.tasks.length > 0) {
				return new Text(
					theme.fg("toolTitle", theme.bold("subagent ")) +
						theme.fg("accent", `parallel (${args.tasks.length} tasks)`) +
						scopeSuffix,
					0,
					0,
				);
			}
			const agentName = args.agent || "...";
			const preview = args.task ? (args.task.length > 60 ? `${args.task.slice(0, 60)}...` : args.task) : "...";
			return new Text(
				theme.fg("toolTitle", theme.bold("subagent ")) +
					theme.fg("accent", agentName) +
					scopeSuffix +
					theme.fg("dim", ` — ${preview}`),
				0,
				0,
			);
		},

		renderResult(result, { expanded }, theme, _context) {
			const details = result.details as SubagentDetails | undefined;
			// Compact one-liner helper for the live views kept from the local
			// rewrite; finished results use the restored upstream views below.
			const firstLine = (text: string): string => {
				const line = text.split("\n").find((l) => l.trim()) || "";
				return line.length > 80 ? `${line.slice(0, 80)}...` : line;
			};

			if (!details || details.results.length === 0) {
				const text = result.content[0];
				return new Text(text?.type === "text" ? firstLine(text.text) : "(no output)", 0, 0);
			}

			const mdTheme = getMarkdownTheme();

			const renderDisplayItems = (items: DisplayItem[], limit?: number) => {
				const toShow = limit ? items.slice(-limit) : items;
				const skipped = limit && items.length > limit ? items.length - limit : 0;
				let text = "";
				if (skipped > 0) text += theme.fg("muted", `... ${skipped} earlier items\n`);
				for (const item of toShow) {
					if (item.type === "text") {
						const preview = expanded ? item.text : item.text.split("\n").slice(0, 3).join("\n");
						text += `${theme.fg("toolOutput", preview)}\n`;
					} else if (item.type === "toolCall") {
						text += `${theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme))}\n`;
					}
					// toolResult items are only rendered in the /subagents detail
					// transcript; the finished-result views stay as upstream.
				}
				return text.trimEnd();
			};

			const budgetMarker = (r: SingleResult): string =>
				r.turnBudgetExhausted
					? theme.fg("warning", `⚠ turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`)
					: "";

			if (details.mode === "single" && details.results.length === 1) {
				const r = details.results[0];

				// Running: keep the compact live one-liner from the local rewrite.
				if (r.exitCode === -1) {
					const tierSuffix = r.tier ? theme.fg("muted", ` (${r.tier})`) : "";
					const turns = r.usage.turns;
					const plural = turns === 1 ? "" : "s";
					return new Text(
						theme.fg("toolTitle", theme.bold("subagent ")) +
							theme.fg("accent", r.agent) +
							tierSuffix +
							theme.fg("muted", ` running (${turns} turn${plural})`),
						0,
						0,
					);
				}

				const isError = isFailedResult(r);
				const icon = isError ? theme.fg("error", "✗") : theme.fg("success", "✓");
				const sourceSuffix = theme.fg("muted", ` (${r.agentSource}${r.tier ? `, ${r.tier}` : ""})`);
				const displayItems = getDisplayItems(r.messages);
				const finalOutput = getFinalOutput(r.messages);

				if (expanded) {
					const container = new Container();
					let header = `${icon} ${theme.fg("toolTitle", theme.bold(r.agent))}${sourceSuffix}`;
					if (isError && r.stopReason) header += ` ${theme.fg("error", `[${r.stopReason}]`)}`;
					container.addChild(new Text(header, 0, 0));
					if (isError && r.errorMessage)
						container.addChild(new Text(theme.fg("error", `Error: ${r.errorMessage}`), 0, 0));
					container.addChild(new Spacer(1));
					container.addChild(new Text(theme.fg("muted", "─── Task ───"), 0, 0));
					container.addChild(new Text(theme.fg("dim", r.task), 0, 0));
					container.addChild(new Spacer(1));
					container.addChild(new Text(theme.fg("muted", "─── Output ───"), 0, 0));
					if (displayItems.length === 0 && !finalOutput) {
						container.addChild(new Text(theme.fg("muted", "(no output)"), 0, 0));
					} else {
						for (const item of displayItems) {
							if (item.type === "toolCall")
								container.addChild(
									new Text(
										theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme)),
										0,
										0,
									),
								);
						}
						if (finalOutput) {
							container.addChild(new Spacer(1));
							container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
						}
					}
					if (r.turnBudgetExhausted) {
						container.addChild(new Spacer(1));
						container.addChild(new Text(budgetMarker(r), 0, 0));
					}
					const usageStr = formatUsageStats(r.usage, r.model, r.turnLimit);
					if (usageStr) {
						container.addChild(new Spacer(1));
						container.addChild(new Text(theme.fg("dim", usageStr), 0, 0));
					}
					return container;
				}

				let text = `${icon} ${theme.fg("toolTitle", theme.bold(r.agent))}${sourceSuffix}`;
				if (isError && r.stopReason) text += ` ${theme.fg("error", `[${r.stopReason}]`)}`;
				if (isError && r.errorMessage) text += `\n${theme.fg("error", `Error: ${r.errorMessage}`)}`;
				else if (displayItems.length === 0) text += `\n${theme.fg("muted", "(no output)")}`;
				else {
					text += `\n${renderDisplayItems(displayItems, COLLAPSED_ITEM_COUNT)}`;
					if (displayItems.length > COLLAPSED_ITEM_COUNT) text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
				}
				if (r.turnBudgetExhausted) text += `\n${budgetMarker(r)}`;
				const usageStr = formatUsageStats(r.usage, r.model, r.turnLimit);
				if (usageStr) text += `\n${theme.fg("dim", usageStr)}`;
				return new Text(text, 0, 0);
			}

			// Running multi-step states keep the compact live one-liner from the
			// local rewrite; finished states use the restored upstream views.
			const isRunning = details.results.some((r) => r.exitCode === -1);
			if (isRunning) {
				const total = details.results.length;
				const done = details.results.filter((r) => r.exitCode !== -1).length;
				const failed = details.results.filter((r) => r.exitCode !== -1 && isFailedResult(r));
				let text =
					theme.fg("toolTitle", theme.bold("subagent ")) +
					theme.fg(
						"accent",
						`${details.mode} (${done}/${total} ${details.mode === "chain" ? "steps" : "done"})`,
					);
				if (failed.length > 0) {
					const r = failed[0];
					const reason = firstLine(r.errorMessage || r.stderr || "(no output)");
					text += theme.fg("error", ` — ${r.agent} failed: ${reason}`);
				}
				return new Text(text, 0, 0);
			}

			const aggregateUsage = (results: SingleResult[]) => {
				const total = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, turns: 0 };
				for (const r of results) {
					total.input += r.usage.input;
					total.output += r.usage.output;
					total.cacheRead += r.usage.cacheRead;
					total.cacheWrite += r.usage.cacheWrite;
					total.cost += r.usage.cost;
					total.turns += r.usage.turns;
				}
				return total;
			};

			if (details.mode === "chain") {
				const successCount = details.results.filter((r) => !isFailedResult(r)).length;
				const icon = successCount === details.results.length ? theme.fg("success", "✓") : theme.fg("error", "✗");

				if (expanded) {
					const container = new Container();
					container.addChild(
						new Text(
							icon +
								" " +
								theme.fg("toolTitle", theme.bold("chain ")) +
								theme.fg("accent", `${successCount}/${details.results.length} steps`),
							0,
							0,
						),
					);

					for (const r of details.results) {
						const rIcon = isFailedResult(r) ? theme.fg("error", "✗") : theme.fg("success", "✓");
						const displayItems = getDisplayItems(r.messages);
						const finalOutput = getFinalOutput(r.messages);

						container.addChild(new Spacer(1));
						container.addChild(
							new Text(
								`${theme.fg("muted", `─── Step ${r.step}: `) + theme.fg("accent", r.agent)} ${rIcon}`,
								0,
								0,
							),
						);
						container.addChild(new Text(theme.fg("muted", "Task: ") + theme.fg("dim", r.task), 0, 0));

						// Show tool calls
						for (const item of displayItems) {
							if (item.type === "toolCall") {
								container.addChild(
									new Text(
										theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme)),
										0,
										0,
									),
								);
							}
						}

						// Show final output as markdown
						if (finalOutput) {
							container.addChild(new Spacer(1));
							container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
						}

						if (r.turnBudgetExhausted) container.addChild(new Text(budgetMarker(r), 0, 0));

						const stepUsage = formatUsageStats(r.usage, r.model, r.turnLimit);
						if (stepUsage) container.addChild(new Text(theme.fg("dim", stepUsage), 0, 0));
					}

					const usageStr = formatUsageStats(aggregateUsage(details.results));
					if (usageStr) {
						container.addChild(new Spacer(1));
						container.addChild(new Text(theme.fg("dim", `Total: ${usageStr}`), 0, 0));
					}
					return container;
				}

				// Collapsed view
				let text =
					icon +
					" " +
					theme.fg("toolTitle", theme.bold("chain ")) +
					theme.fg("accent", `${successCount}/${details.results.length} steps`);
				for (const r of details.results) {
					const rIcon = isFailedResult(r) ? theme.fg("error", "✗") : theme.fg("success", "✓");
					const displayItems = getDisplayItems(r.messages);
					text += `\n\n${theme.fg("muted", `─── Step ${r.step}: `)}${theme.fg("accent", r.agent)} ${rIcon}`;
					if (r.turnBudgetExhausted) text += ` ${budgetMarker(r)}`;
					if (displayItems.length === 0) text += `\n${theme.fg("muted", "(no output)")}`;
					else text += `\n${renderDisplayItems(displayItems, 5)}`;
				}
				const usageStr = formatUsageStats(aggregateUsage(details.results));
				if (usageStr) text += `\n\n${theme.fg("dim", `Total: ${usageStr}`)}`;
				text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
				return new Text(text, 0, 0);
			}

			if (details.mode === "parallel") {
				const successCount = details.results.filter((r) => !isFailedResult(r)).length;
				const failCount = details.results.filter((r) => isFailedResult(r)).length;
				const icon = failCount > 0 ? theme.fg("warning", "◐") : theme.fg("success", "✓");
				const status = `${successCount}/${details.results.length} tasks`;

				if (expanded) {
					const container = new Container();
					container.addChild(
						new Text(
							`${icon} ${theme.fg("toolTitle", theme.bold("parallel "))}${theme.fg("accent", status)}`,
							0,
							0,
						),
					);

					for (const r of details.results) {
						const rIcon = isFailedResult(r) ? theme.fg("error", "✗") : theme.fg("success", "✓");
						const displayItems = getDisplayItems(r.messages);
						const finalOutput = getFinalOutput(r.messages);

						container.addChild(new Spacer(1));
						container.addChild(
							new Text(`${theme.fg("muted", "─── ") + theme.fg("accent", r.agent)} ${rIcon}`, 0, 0),
						);
						container.addChild(new Text(theme.fg("muted", "Task: ") + theme.fg("dim", r.task), 0, 0));

						// Show tool calls
						for (const item of displayItems) {
							if (item.type === "toolCall") {
								container.addChild(
									new Text(
										theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme)),
										0,
										0,
									),
								);
							}
						}

						// Show final output as markdown
						if (finalOutput) {
							container.addChild(new Spacer(1));
							container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
						}

						if (r.turnBudgetExhausted) container.addChild(new Text(budgetMarker(r), 0, 0));

						const taskUsage = formatUsageStats(r.usage, r.model, r.turnLimit);
						if (taskUsage) container.addChild(new Text(theme.fg("dim", taskUsage), 0, 0));
					}

					const usageStr = formatUsageStats(aggregateUsage(details.results));
					if (usageStr) {
						container.addChild(new Spacer(1));
						container.addChild(new Text(theme.fg("dim", `Total: ${usageStr}`), 0, 0));
					}
					return container;
				}

				// Collapsed view
				let text = `${icon} ${theme.fg("toolTitle", theme.bold("parallel "))}${theme.fg("accent", status)}`;
				for (const r of details.results) {
					const rIcon = isFailedResult(r) ? theme.fg("error", "✗") : theme.fg("success", "✓");
					const displayItems = getDisplayItems(r.messages);
					text += `\n\n${theme.fg("muted", "─── ")}${theme.fg("accent", r.agent)} ${rIcon}`;
					if (r.turnBudgetExhausted) text += ` ${budgetMarker(r)}`;
					if (displayItems.length === 0) text += `\n${theme.fg("muted", "(no output)")}`;
					else text += `\n${renderDisplayItems(displayItems, 5)}`;
				}
				const usageStr = formatUsageStats(aggregateUsage(details.results));
				if (usageStr) text += `\n\n${theme.fg("dim", `Total: ${usageStr}`)}`;
				text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
				return new Text(text, 0, 0);
			}

			const text = result.content[0];
			return new Text(text?.type === "text" ? firstLine(text.text) : "(no output)", 0, 0);
		},
	});

	pi.registerCommand("subagents", {
		description: "List subagents spawned this session; open one to watch its activity",
		handler: async (_args, cmdCtx) => {
			if (cmdCtx.mode !== "tui") return;
			const ui = cmdCtx.ui;

			const formatElapsed = (ms: number): string => {
				const seconds = Math.floor(ms / 1000);
				if (seconds < 60) return `${seconds}s`;
				const minutes = Math.floor(seconds / 60);
				if (minutes < 60) return `${minutes}m${seconds % 60}s`;
				return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
			};

			const statusOf = (entry: RunningSubagent): string => {
				const turns = entry.result?.usage?.turns ?? 0;
				if (!entry.completedAt) return `running (${turns} turns)`;
				if (isFailedResult(entry.result)) return "failed";
				if (entry.result.turnBudgetExhausted)
					return `exhausted (${turns}/${entry.turnLimit ?? entry.result.turnLimit ?? "?"})`;
				const cost = entry.result.usage?.cost ? `, $${entry.result.usage.cost.toFixed(4)}` : "";
				return `finished (${turns} turns${cost})`;
			};

			const elapsedOf = (entry: RunningSubagent): string =>
				formatElapsed((entry.completedAt?.getTime() ?? Date.now()) - entry.startedAt);

			// Every exit path unsubscribes exactly once: Escape unsubscribes
			// inline, and the .finally below covers disposal without Escape
			// (the overlay rejection lands in the outer catch).
			const openDetail = (entry: RunningSubagent): Promise<null> => {
				let unsubscribe: (() => void) | undefined;
				return ui.custom<null>(
					(tui, theme, _kb, done) => {
						const mdTheme = getMarkdownTheme();

						// Rebuild from the live SingleResult on every render so the
						// view tracks the running child. No truncation here: task,
						// status, and JSON args reach the renderer fully, which
						// word-wraps Text/Markdown at the real width.
						const buildContent = () => {
							const r = entry.result;
							const container = new Container();
							const statusColor = !entry.completedAt ? "warning" : isFailedResult(r) ? "error" : "success";
							container.addChild(
								new Text(
									theme.fg("toolTitle", theme.bold(`#${entry.id} ${entry.agent} (${entry.tier ?? "?"})`)) +
										theme.fg("muted", ` — ${entry.mode}`) +
										theme.fg(statusColor, ` ${statusOf(entry)}`) +
										theme.fg("muted", ` · ${elapsedOf(entry)}`),
									0,
									0,
								),
							);
							const turns = r.usage.turns;
							if (entry.turnLimit ?? r.turnLimit) {
								const turnInfo = `${turns}/${entry.turnLimit ?? r.turnLimit} turns`;
								container.addChild(
									new Text(theme.fg("muted", turnInfo + (r.model ? ` · ${r.model}` : "")), 0, 0),
								);
							}
							container.addChild(new Spacer(1));
							container.addChild(new Text(theme.fg("muted", "─── Task ───"), 0, 0));
							container.addChild(new Text(theme.fg("dim", entry.task), 0, 0));
							container.addChild(new Spacer(1));
							container.addChild(new Text(theme.fg("muted", "─── Activity ───"), 0, 0));
							const items = getDisplayItems(r.messages);
							if (items.length === 0) {
								container.addChild(
									new Text(
										theme.fg("muted", entry.completedAt ? "(no activity)" : "(waiting for first turn)"),
										0,
										0,
									),
								);
							} else {
								for (const item of items) {
									if (item.type === "toolCall") {
										container.addChild(
											new Text(
												theme.fg("muted", "→ ") +
													formatToolCall(item.name, item.args, theme.fg.bind(theme), true),
												0,
												0,
											),
										);
									} else if (item.type === "toolResult") {
										container.addChild(
											new Text(
												theme.fg(item.isError ? "error" : "toolOutput", indentPreview(item.text)),
												0,
												0,
											),
										);
									} else {
										container.addChild(new Text(theme.fg("toolOutput", item.text), 0, 0));
									}
								}
							}
							const finalOutput = getFinalOutput(r.messages);
							if (finalOutput) {
								container.addChild(new Spacer(1));
								container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
							}
							if (entry.partialText) {
								container.addChild(new Spacer(1));
								container.addChild(new Text(theme.fg("dim", entry.partialText), 0, 0));
							}
							if (r.turnBudgetExhausted) {
								container.addChild(new Spacer(1));
								container.addChild(
									new Text(
										theme.fg("warning", `⚠ turn budget exhausted (${turns}/${r.turnLimit} turns)`),
										0,
										0,
									),
								);
							}
							if (isFailedResult(r) && r.errorMessage) {
								container.addChild(new Text(theme.fg("error", `Error: ${r.errorMessage}`), 0, 0));
							}
							const usageStr = formatUsageStats(r.usage);
							if (usageStr) {
								container.addChild(new Text(theme.fg("dim", usageStr), 0, 0));
							}
							return container;
						};

						// Scroll state: overlays do not self-clamp, so the view
						// slices a window of the fully rendered lines itself.
						// Sticky followTail keeps live rebuilds pinned to the
						// bottom until the user scrolls up.
						let content = buildContent();
						let scrollOffset = 0;
						let followTail = true;
						let renderedLines = 0;

						const viewport = () => Math.max(4, tui.terminal.rows - 2);

						const listener = () => {
							content = buildContent();
							tui.requestRender();
						};
						entry.listeners.add(listener);
						unsubscribe = () => entry.listeners.delete(listener);

						return {
							render: (width: number) => {
								const lines = content.render(width);
								const vp = viewport();
								const maxOffset = Math.max(0, lines.length - vp);
								if (followTail) scrollOffset = maxOffset;
								scrollOffset = Math.min(Math.max(0, scrollOffset), maxOffset);
								renderedLines = lines.length;
								const window = lines.slice(scrollOffset, scrollOffset + vp);
								const scrolling = maxOffset > 0;
								const footer =
									"↑↓/PgUp/PgDn scroll · wheel scroll · esc back" +
									(scrolling
										? ` · lines ${scrollOffset + 1}–${Math.min(scrollOffset + vp, lines.length)}/${lines.length}`
										: "");
								// Settings hint style: dim, two-space indent, no box.
								return [
									...window,
									...new Text(theme.fg("dim", `  ${footer}`), 0, 0).render(width),
								];
							},
							invalidate: () => content.invalidate(),
							handleInput: (data: string) => {
								if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
									unsubscribe?.();
									unsubscribe = undefined;
									done(null);
									return;
								}
								const vp = viewport();
								if (matchesKey(data, "up")) {
									followTail = false;
									scrollOffset = Math.max(0, scrollOffset - 1);
								} else if (matchesKey(data, "down")) {
									followTail = true;
									scrollOffset += 1;
								} else if (matchesKey(data, "pageup")) {
									followTail = false;
									scrollOffset = Math.max(0, scrollOffset - (vp - 1));
								} else if (matchesKey(data, "pagedown")) {
									followTail = true;
									scrollOffset += vp - 1;
								} else if (matchesKey(data, "home")) {
									followTail = false;
									scrollOffset = 0;
								} else if (matchesKey(data, "end")) {
									followTail = true;
									scrollOffset = Number.MAX_SAFE_INTEGER;
								} else {
									return;
								}
								tui.requestRender();
							},
							handleMouse: (event: TuiMouseEvent) => {
								if (event.type !== "wheel") return;
								// pi-tui emits a negative wheelDelta on wheel-up
								// ("Negative values scroll up"), so adding it moves
								// the window toward earlier lines; wheel-up unpins
								// the tail, wheel-down re-pins at the bottom.
								scrollOffset += event.wheelDelta ?? 0;
								const vp = viewport();
								const maxOffset = Math.max(0, renderedLines - vp);
								scrollOffset = Math.min(Math.max(0, scrollOffset), maxOffset);
								followTail = scrollOffset >= maxOffset;
								tui.requestRender();
								return { handled: true };
							},
						};
					},
					{
						overlay: true,
						overlayOptions: { anchor: "center", width: "100%", maxHeight: "100%", margin: 0 },
					},
				).finally(() => {
					unsubscribe?.();
					unsubscribe = undefined;
				});
			};

			// Settings-styled list: border / body / border with the settings row
			// layout (→ cursor, aligned label column, muted value column, dim
			// description of the selected row, dim hint footer). Entries group
			// under Active/Inactive headers, active first; windowing over the
			// flat entry list mirrors SettingsList.getVisibleRange.
			const openList = (): Promise<RunningSubagent | null> =>
				ui.custom<RunningSubagent | null>(
					(tui, theme, _kb, done) => {
						const st = getSettingsListTheme();
						const border = new DynamicBorder((s: string) => theme.fg("border", s));
						const groups = [
							{ title: "Active", entries: [...getRunningSubagents()] },
							{ title: "Inactive", entries: [...getRecentSubagents()] },
						].filter((g) => g.entries.length > 0);
						const rows = groups.flatMap((g) =>
							g.entries.map((entry) => ({
								entry,
								groupTitle: g.title,
								groupCount: g.entries.length,
							})),
						);

						const empty = new Container();
						empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
						empty.addChild(new Text(st.hint("  No subagents spawned this session."), 0, 0));
						empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
						if (rows.length === 0) {
							return {
								render: (width: number) => empty.render(width),
								invalidate: () => empty.invalidate(),
								handleInput: (data: string) => {
									if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) done(null);
								},
							};
						}

						const labelOf = (entry: RunningSubagent): string =>
							`#${entry.id} ${entry.agent}${entry.tier ? ` (${entry.tier})` : ""}`;
						const maxLabelWidth = Math.min(
							36,
							Math.max(...rows.map((r) => visibleWidth(labelOf(r.entry)))),
						);

						const maxVisible = Math.min(rows.length, 10);
						let selected = 0;
						// y offsets of the entry rows from the last render, for mouse;
						// +1 because the component's top border shifts event.y down.
						let rowMap: { y: number; index: number }[] = [];

						const renderBody = (width: number): string[] => {
							const startIndex = Math.max(
								0,
								Math.min(selected - Math.floor(maxVisible / 2), rows.length - maxVisible),
							);
							const endIndex = Math.min(startIndex + maxVisible, rows.length);
							const lines: string[] = [];
							rowMap = [];
							let prevGroup = "";
							for (let i = startIndex; i < endIndex; i++) {
								const row = rows[i];
								if (row.groupTitle !== prevGroup) {
									if (prevGroup !== "") lines.push("");
									lines.push(
										truncateToWidth(
											theme.fg("accent", theme.bold(`${row.groupTitle} (${row.groupCount})`)),
											width,
										),
									);
									prevGroup = row.groupTitle;
								}
								const isSelected = i === selected;
								const prefix = isSelected ? st.cursor : "  ";
								const label = labelOf(row.entry);
								const labelPadded = label + " ".repeat(Math.max(0, maxLabelWidth - visibleWidth(label)));
								const separator = "  ";
								const usedWidth = visibleWidth(prefix) + maxLabelWidth + visibleWidth(separator);
								const valueMaxWidth = Math.max(0, width - usedWidth - 2);
								const valueText = st.value(truncateToWidth(statusOf(row.entry), valueMaxWidth, ""), isSelected);
								lines.push(
									truncateToWidth(prefix + st.label(labelPadded, isSelected) + separator + valueText, width),
								);
								rowMap.push({ y: lines.length, index: i });
							}
							if (startIndex > 0 || endIndex < rows.length) {
								lines.push(st.hint(truncateToWidth(`  (${selected + 1}/${rows.length})`, width - 2, "")));
							}
							const taskFlat = rows[selected].entry.task.replace(/\s+/g, " ").trim();
							if (taskFlat) {
								lines.push("");
								for (const line of wrapTextWithAnsi(taskFlat, Math.max(8, width - 4))) {
									lines.push(st.description(`  ${line}`));
								}
							}
							lines.push("");
							lines.push(
								st.hint(truncateToWidth("  ↑↓ navigate · Enter/Space to open · Esc to cancel", width, "")),
							);
							return lines;
						};

						return {
							render: (width: number) => [
								...border.render(width),
								...renderBody(width),
								...border.render(width),
							],
							invalidate: () => {},
							handleInput: (data: string) => {
								if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
									done(null);
									return;
								}
								if (matchesKey(data, "up")) selected = (selected - 1 + rows.length) % rows.length;
								else if (matchesKey(data, "down")) selected = (selected + 1) % rows.length;
								else if (matchesKey(data, "enter") || data === " ") {
									done(rows[selected].entry);
									return;
								} else return;
								tui.requestRender();
							},
							handleMouse: (event: TuiMouseEvent) => {
								if (event.type === "wheel") {
									// pi-tui emits a negative wheelDelta on wheel-up.
									selected =
										(selected + (event.wheelDelta && event.wheelDelta < 0 ? -1 : 1) + rows.length) % rows.length;
								} else if (event.type === "press" || event.type === "click") {
									const row = rowMap.find((r) => r.y === event.y);
									if (!row) return { handled: false };
									selected = row.index;
									if (event.type === "click") {
										done(rows[selected].entry);
										return { handled: true };
									}
								} else return { handled: false };
								tui.requestRender();
								return { handled: true };
							},
						};
					},
					{ overlay: true },
				);

			try {
				while (true) {
					const selected = await openList();
					if (!selected) return;
					await openDetail(selected);
				}
			} catch {
				/* overlay unavailable or canceled mid-loop */
			}
		},
	});
}
