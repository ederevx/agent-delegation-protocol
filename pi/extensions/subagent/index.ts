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
 * Local modifications: maxTurns turn budgets; child registry + /subagents command;
 *   spawn notifications; tool_result_end dead-branch removal; expanded renderer
 *   restored with turn-budget markers
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
	withFileMutationQueue,
} from "@earendil-works/pi-coding-agent";
import {
	Container,
	Markdown,
	matchesKey,
	type SelectItem,
	SelectList,
	Spacer,
	Text,
} from "@earendil-works/pi-tui";
import { Type } from "typebox";
import { type AgentConfig, type AgentScope, discoverAgents } from "./agents.ts";

const MAX_PARALLEL_TASKS = 8;
const MAX_CONCURRENCY = 4;
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
): string {
	const shortenPath = (p: string) => {
		const home = os.homedir();
		return p.startsWith(home) ? `~${p.slice(home.length)}` : p;
	};

	switch (toolName) {
		case "bash": {
			const command = (args.command as string) || "...";
			const preview = command.length > 60 ? `${command.slice(0, 60)}...` : command;
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
			const preview = argsStr.length > 50 ? `${argsStr.slice(0, 50)}...` : argsStr;
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
	id: string;
	agent: string;
	tier?: string;
	task: string;
	mode: "single" | "parallel-task" | "chain-step";
	startedAt: Date;
	completedAt?: Date;
	proc?: ChildProcess;
	// The live SingleResult; mutated in place as stream events arrive.
	result: SingleResult;
	turnLimit?: number;
	listeners: Set<() => void>;
}

// Registry backing /subagents. Entries live from spawn until the child
// closes; on close an entry moves to `finished` (last 20 kept, each with
// completedAt and its final result), so a killed or errored worker never
// leaks a running slot.
const runningSubagents = new Map<string, RunningSubagent>();
const finishedSubagents: RunningSubagent[] = [];
const MAX_FINISHED_SUBAGENTS = 20;
let nextSubagentId = 1;

export const subagentRegistry = {
	running: runningSubagents,
	finished: finishedSubagents,
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

type DisplayItem = { type: "text"; text: string } | { type: "toolCall"; name: string; args: Record<string, any> };

// Assistant text and tool calls, in stream order; backs the /subagents
// activity view.
function getDisplayItems(messages: Message[]): DisplayItem[] {
	const items: DisplayItem[] = [];
	for (const msg of messages) {
		if (msg.role === "assistant") {
			for (const part of msg.content) {
				if (part.type === "text") items.push({ type: "text", text: part.text });
				else if (part.type === "toolCall")
					items.push({ type: "toolCall", name: part.name, args: part.arguments as Record<string, any> });
			}
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
	// Close/error path: move the entry out of `running` into the finished
	// list, stamp completion time, and wake any listeners watching the live
	// result.
	const finishEntry = () => {
		if (!entry) return;
		runningSubagents.delete(entry.id);
		entry.completedAt = new Date();
		finishedSubagents.push(entry);
		if (finishedSubagents.length > MAX_FINISHED_SUBAGENTS)
			finishedSubagents.splice(0, finishedSubagents.length - MAX_FINISHED_SUBAGENTS);
		for (const listener of entry.listeners) {
			try {
				listener();
			} catch {
				/* listener errors must not break the close path */
			}
		}
		entry = null;
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
		if (onUpdate) {
			onUpdate({
				content: [{ type: "text", text: getFinalOutput(currentResult.messages) || "(running...)" }],
				details: makeDetails([currentResult]),
			});
		}
		notifyListeners();
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
				id: `${agentName}#${nextSubagentId++}`,
				agent: agentName,
				tier,
				task,
				mode,
				startedAt: new Date(),
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

			notifySpawn?.(
				`Subagent spawned: ${agent.name}${tier ? ` (${tier})` : ""} — ${
					task.length > 60 ? `${task.slice(0, 60)}...` : task
				}`,
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

				if (event.type === "message_end" && event.message) {
					const msg = event.message as Message;
					// Both assistant turns and tool results (message.role ===
					// "toolResult") arrive as message_end events; the push below
					// collects both. Only the assistant branch increments turns.
					currentResult.messages.push(msg);

					if (msg.role === "assistant") {
						currentResult.usage.turns++;
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
					} else {
						text += `${theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme))}\n`;
					}
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
			if (!cmdCtx?.hasUI) return;
			const ui = cmdCtx.ui;

			const formatElapsed = (ms: number): string => {
				const seconds = Math.floor(ms / 1000);
				if (seconds < 60) return `${seconds}s`;
				const minutes = Math.floor(seconds / 60);
				if (minutes < 60) return `${minutes}m${seconds % 60}s`;
				return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
			};

			const statusOf = (entry: RunningSubagent): string => {
				if (!entry.completedAt) return "running";
				if (entry.result.turnBudgetExhausted) return "exhausted";
				return `exit ${entry.result.exitCode}`;
			};

			const elapsedOf = (entry: RunningSubagent): string =>
				formatElapsed((entry.completedAt ?? new Date()).getTime() - entry.startedAt.getTime());

			// Every exit path unsubscribes exactly once: Escape unsubscribes
			// inline, and the .finally below covers disposal without Escape
			// (the overlay rejection lands in the outer catch).
			const openDetail = (entry: RunningSubagent): Promise<null> => {
				let unsubscribe: (() => void) | undefined;
				return ui.custom<null>(
					(tui, theme, _kb, done) => {
						const mdTheme = getMarkdownTheme();

						// Rebuild from the live SingleResult on every render so the
						// view tracks the running child.
						const buildContainer = () => {
							const r = entry.result;
							const container = new Container();
							const statusColor = !entry.completedAt ? "warning" : isFailedResult(r) ? "error" : "success";
							container.addChild(
								new Text(
									theme.fg("toolTitle", theme.bold(`${entry.agent} (${entry.tier ?? "?"})`)) +
										theme.fg(statusColor, ` ${statusOf(entry)}`) +
										theme.fg("muted", ` · ${entry.mode} · ${elapsedOf(entry)}`),
									0,
									0,
								),
							);
							const turns = r.usage.turns;
							const turnInfo = r.turnLimit ? `${turns}/${r.turnLimit} turns` : `${turns} turns`;
							container.addChild(
								new Text(theme.fg("muted", turnInfo + (r.model ? ` · ${r.model}` : "")), 0, 0),
							);
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
												theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, theme.fg.bind(theme)),
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
							container.addChild(new Text(theme.fg("dim", "Esc: back to list"), 0, 0));
							return container;
						};

						let view = buildContainer();
						const listener = () => {
							view = buildContainer();
							tui.requestRender();
						};
						entry.listeners.add(listener);
						unsubscribe = () => entry.listeners.delete(listener);

						return {
							render: (width: number) => view.render(width),
							invalidate: () => view.invalidate(),
							handleInput: (data: string) => {
								if (matchesKey(data, "escape")) {
									unsubscribe?.();
									unsubscribe = undefined;
									done(null);
								}
							},
						};
					},
					{ overlay: true },
				).finally(() => {
					unsubscribe?.();
					unsubscribe = undefined;
				});
			};

			const openList = (): Promise<RunningSubagent | null> =>
				ui.custom<RunningSubagent | null>(
					(tui, theme, _kb, done) => {
						const entries: RunningSubagent[] = [
							...subagentRegistry.running.values(),
							...subagentRegistry.finished,
						];
						if (entries.length === 0) {
							const empty = new Text(
								theme.fg("muted", "No subagents spawned this session.\n\nEsc: close"),
								1,
								1,
							);
							return {
								render: (width: number) => empty.render(width),
								invalidate: () => empty.invalidate(),
								handleInput: (data: string) => {
									if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) done(null);
								},
							};
						}

						const items: SelectItem[] = entries.map((entry) => ({
							value: entry.id,
							label: `${entry.agent} (${entry.tier ?? "?"}) ${statusOf(entry)} ${elapsedOf(entry)}`,
							description: entry.task.length > 80 ? `${entry.task.slice(0, 80)}...` : entry.task,
						}));
						const container = new Container();
						container.addChild(new DynamicBorder((s: string) => theme.fg("accent", s)));
						container.addChild(new Text(theme.fg("accent", theme.bold("Subagents")), 0, 0));
						const selectList = new SelectList(items, Math.min(items.length, 10), {
							selectedPrefix: (t) => theme.fg("accent", t),
							selectedText: (t) => theme.fg("accent", t),
							description: (t) => theme.fg("muted", t),
							scrollInfo: (t) => theme.fg("dim", t),
							noMatch: (t) => theme.fg("warning", t),
						});
						selectList.onSelect = (item) => {
							const entry = entries.find((e) => e.id === item.value);
							if (entry) done(entry);
						};
						selectList.onCancel = () => done(null);
						container.addChild(selectList);
						container.addChild(new Text(theme.fg("dim", "↑↓ navigate · enter watch · esc close"), 1, 0));
						container.addChild(new DynamicBorder((s: string) => theme.fg("accent", s)));

						return {
							render: (width: number) => container.render(width),
							invalidate: () => container.invalidate(),
							handleInput: (data: string) => {
								selectList.handleInput(data);
								tui.requestRender();
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
