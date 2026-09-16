/**
 * SubagentDispatch — one tool call's orchestration
 *
 * Validates the parameter modes, confirms project-local agents, and runs
 * the three dispatch shapes: chain (sequential with {previous}), parallel
 * (bounded fan-out), and single. Each runner owns its local result
 * collection for the duration of the call; children stream through
 * `SubagentRun` (run.ts).
 */

import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { MAX_CONCURRENCY, MAX_PARALLEL_TASKS } from "./constants.ts";
import { getFinalOutput, getResultOutput, isFailedResult, truncateParallelOutput } from "./format.ts";
import { SubagentRun } from "./run.ts";
import type { SubagentRegistry } from "./registry.ts";
import type { AgentConfig, AgentScope } from "./agents.ts";
import type {
	DispatchDefaults,
	OnUpdateCallback,
	SingleResult,
	SubagentDetails,
	SubagentMode,
	SubagentToolParams,
	SubagentToolResult,
	TaskSpec,
} from "./types.ts";

/**
 * Bound a fan-out by concurrency: at most `concurrency` tasks in flight,
 * results written back positionally.
 */
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

interface DispatchResources {
	agentScope: AgentScope;
	registry: SubagentRegistry;
	agents: AgentConfig[];
	projectAgentsDir: string | null;
	dispatchDefaults: DispatchDefaults;
	notifySpawn: (message: string) => void;
}

export class SubagentDispatch {
	private readonly ctx: ExtensionContext;
	private readonly agentScope: AgentScope;
	private readonly registry: SubagentRegistry;
	private readonly agents: AgentConfig[];
	private readonly projectAgentsDir: string | null;
	private readonly dispatchDefaults: DispatchDefaults;
	private readonly notifySpawn: (message: string) => void;

	constructor(ctx: ExtensionContext, resources: DispatchResources) {
		this.ctx = ctx;
		this.agentScope = resources.agentScope;
		this.registry = resources.registry;
		this.agents = resources.agents;
		this.projectAgentsDir = resources.projectAgentsDir;
		this.dispatchDefaults = resources.dispatchDefaults;
		this.notifySpawn = resources.notifySpawn;
	}

	async execute(
		params: SubagentToolParams,
		signal: AbortSignal | undefined,
		onUpdate: OnUpdateCallback | undefined,
	): Promise<SubagentToolResult> {
		const hasChain = (params.chain?.length ?? 0) > 0;
		const hasTasks = (params.tasks?.length ?? 0) > 0;
		const hasSingle = Boolean(params.agent && params.task);
		const modeCount = Number(hasChain) + Number(hasTasks) + Number(hasSingle);

		if (modeCount !== 1) return this.invalidParams("Provide exactly one mode.");

		if (
			(this.agentScope === "project" || this.agentScope === "both") &&
			(params.confirmProjectAgents ?? true) &&
			this.ctx.hasUI &&
			!this.ctx.isProjectTrusted() &&
			!(await this.confirmProjectAgents(params))
		) {
			return {
				content: [{ type: "text", text: "Canceled: project-local agents not approved." }],
				details: this.makeDetails(hasChain ? "chain" : hasTasks ? "parallel" : "single", []),
			};
		}

		if (hasChain) return this.runChain(params.chain as TaskSpec[], signal, onUpdate);
		if (hasTasks) return this.runParallel(params.tasks as TaskSpec[], signal, onUpdate);
		return this.runSingle(params.agent as string, params.task as string, params.cwd, signal, onUpdate);
	}

	// ------------------------------------------------------------------
	// Shared helpers
	// ------------------------------------------------------------------

	private makeDetails(mode: "single" | "parallel" | "chain", results: SingleResult[]): SubagentDetails {
		return {
			mode,
			agentScope: this.agentScope,
			projectAgentsDir: this.projectAgentsDir,
			results,
		};
	}

	private invalidParams(reason: string): SubagentToolResult {
		const available = this.agents.map((a) => `${a.name} (${a.source})`).join(", ") || "none";
		return {
			content: [{ type: "text", text: `Invalid parameters. ${reason}\nAvailable agents: ${available}` }],
			details: this.makeDetails("single", []),
		};
	}

	/** Ask before running repo-controlled project-local agents. */
	private async confirmProjectAgents(params: SubagentToolParams): Promise<boolean> {
		const requestedAgentNames = new Set<string>();
		if (params.chain) for (const step of params.chain) requestedAgentNames.add(step.agent);
		if (params.tasks) for (const t of params.tasks) requestedAgentNames.add(t.agent);
		if (params.agent) requestedAgentNames.add(params.agent);

		const projectAgentsRequested = Array.from(requestedAgentNames)
			.map((name) => this.agents.find((a) => a.name === name))
			.filter((a): a is AgentConfig => a?.source === "project");

		if (projectAgentsRequested.length === 0) return true;

		const names = projectAgentsRequested.map((a) => a.name).join(", ");
		const dir = this.projectAgentsDir ?? "(unknown)";
		return await this.ctx.ui.confirm(
			"Run project-local agents?",
			`Agents: ${names}\nSource: ${dir}\n\nProject agents are repo-controlled. Only continue for trusted repositories.`,
		);
	}

	/** Spawn one child with this dispatch's resources. */
	private spawnRun(options: {
		agentName: string;
		task: string;
		cwd?: string;
		step?: number;
		signal?: AbortSignal;
		onUpdate?: OnUpdateCallback;
		mode: SubagentMode;
		makeDetails: (results: SingleResult[]) => SubagentDetails;
	}): SubagentRun {
		return new SubagentRun({
			registry: this.registry,
			agents: this.agents,
			defaultCwd: this.ctx.cwd,
			dispatchDefaults: this.dispatchDefaults,
			agentName: options.agentName,
			task: options.task,
			cwd: options.cwd,
			step: options.step,
			signal: options.signal,
			onUpdate: options.onUpdate,
			makeDetails: options.makeDetails,
			mode: options.mode,
			notifySpawn: this.notifySpawn,
		});
	}

	// ------------------------------------------------------------------
	// Chain
	// ------------------------------------------------------------------

	private async runChain(
		chain: TaskSpec[],
		signal: AbortSignal | undefined,
		onUpdate: OnUpdateCallback | undefined,
	): Promise<SubagentToolResult> {
		const results: SingleResult[] = [];
		let previousOutput = "";

		for (let i = 0; i < chain.length; i++) {
			const step = chain[i];
			const taskWithContext = step.task.replace(/\{previous\}/g, previousOutput);

			// Combine completed results with the current streaming result.
			const chainUpdate: OnUpdateCallback | undefined = onUpdate
				? (partial) => {
						const currentResult = partial.details?.results[0];
						if (currentResult) {
							onUpdate({
								content: partial.content,
								details: this.makeDetails("chain", [...results, currentResult]),
							});
						}
					}
				: undefined;

			const result = await this.spawnRun({
				agentName: step.agent,
				task: taskWithContext,
				cwd: step.cwd,
				step: i + 1,
				signal,
				onUpdate: chainUpdate,
				mode: "chain-step",
				makeDetails: (rs) => this.makeDetails("chain", rs),
			}).run();
			results.push(result);

			if (isFailedResult(result)) {
				return {
					content: [
						{
							type: "text",
							text: `Chain stopped at step ${i + 1} (${step.agent}): ${getResultOutput(result)}`,
						},
					],
					details: this.makeDetails("chain", results),
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
			details: this.makeDetails("chain", results),
		};
	}

	// ------------------------------------------------------------------
	// Parallel
	// ------------------------------------------------------------------

	private async runParallel(
		tasks: TaskSpec[],
		signal: AbortSignal | undefined,
		onUpdate: OnUpdateCallback | undefined,
	): Promise<SubagentToolResult> {
		if (tasks.length > MAX_PARALLEL_TASKS) {
			return {
				content: [
					{
						type: "text",
						text: `Too many parallel tasks (${tasks.length}). Max is ${MAX_PARALLEL_TASKS}.`,
					},
				],
				details: this.makeDetails("parallel", []),
			};
		}

		// Track all results for streaming updates; placeholders carry
		// exitCode -1 (still running) until each child closes.
		const allResults: SingleResult[] = tasks.map((t) => ({
			agent: t.agent,
			agentSource: "unknown",
			task: t.task,
			exitCode: -1,
			messages: [],
			stderr: "",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 },
		}));

		const emitParallelUpdate = () => {
			if (!onUpdate) return;
			const running = allResults.filter((r) => r.exitCode === -1).length;
			const done = allResults.length - running;
			onUpdate({
				content: [{ type: "text", text: `Parallel: ${done}/${allResults.length} done, ${running} running...` }],
				details: this.makeDetails("parallel", [...allResults]),
			});
		};

		const results = await mapWithConcurrencyLimit(tasks, MAX_CONCURRENCY, async (t, index) => {
			const result = await this.spawnRun({
				agentName: t.agent,
				task: t.task,
				cwd: t.cwd,
				signal,
				// Per-task update callback
				onUpdate: (partial) => {
					if (partial.details?.results[0]) {
						allResults[index] = partial.details.results[0];
						emitParallelUpdate();
					}
				},
				mode: "parallel-task",
				makeDetails: (rs) => this.makeDetails("parallel", rs),
			}).run();
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
			details: this.makeDetails("parallel", results),
		};
	}

	// ------------------------------------------------------------------
	// Single
	// ------------------------------------------------------------------

	private async runSingle(
		agentName: string,
		task: string,
		cwd: string | undefined,
		signal: AbortSignal | undefined,
		onUpdate: OnUpdateCallback | undefined,
	): Promise<SubagentToolResult> {
		const result = await this.spawnRun({
			agentName,
			task,
			cwd,
			signal,
			onUpdate,
			mode: "single",
			makeDetails: (rs) => this.makeDetails("single", rs),
		}).run();

		if (isFailedResult(result)) {
			return {
				content: [{ type: "text", text: `Agent ${result.stopReason || "failed"}: ${getResultOutput(result)}` }],
				details: this.makeDetails("single", [result]),
				isError: true,
			};
		}
		let text = getFinalOutput(result.messages) || "(no output)";
		if (result.turnBudgetExhausted) {
			text += `\n\n— turn budget exhausted (${result.usage.turns}/${result.turnLimit} turns)`;
		}
		return {
			content: [{ type: "text", text }],
			details: this.makeDetails("single", [result]),
		};
	}
}
