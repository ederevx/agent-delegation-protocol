/**
 * Subagent Tool — Delegate tasks to specialized agents
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
 *   raised to 10 to match the ADP active-worker cap; steering Enter while
 *   workers are active interrupts the workers and delivers the message
 *   non-queued before the next LLM call; throttled parent updates (≤4/s);
 *   /subagents UI: settings-integrated (non-overlay, editor-dock mount
 *   exactly like /settings) selector grouping entries under Active/Inactive
 *   headers (active first); the detail viewer replaces the entire TUI in
 *   fullscreen mode via the viewport TUI's setLayoutRoot (own full-screen
 *   root, previous root restored on close) and dock-integrates in regular
 *   mode; mouse+keyboard throughout
 *
 * This file is only the wiring: schema, tool, command, and steering. The
 * logic is split by responsibility — see types.ts, registry.ts, run.ts,
 * dispatch.ts, result-views.ts, selector-view.ts, detail-view.ts. The
 * wiring itself is decomposed into single-responsibility classes
 * (SteeringGate, SubagentToolHandler, SubagentsBrowser, DetailViewSession)
 * composed by SubagentExtension, so no callback body owns another's state.
 */

import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME, getAgentDir } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import type { AgentScope } from "./agents.ts";
import { discoverAgents } from "./agents.ts";
import { SubagentDispatch } from "./dispatch.ts";
import { SubagentDetailView } from "./detail-view.ts";
import { SubagentRegistry, type RunningSubagent } from "./registry.ts";
import { SubagentResultViews } from "./result-views.ts";
import { SubagentSelectorView } from "./selector-view.ts";
import type {
	SubagentDetails,
	SubagentToolParams,
	SubagentToolResult,
} from "./types.ts";

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

function buildToolDescription(): string {
	return [
		"Delegate tasks to specialized subagents with isolated context.",
		"Modes: single (agent + task), parallel (tasks array), chain (sequential with {previous} placeholder).",
		`Default agent scope is "user" (from ${getAgentDir()}/agents).`,
		`To enable project-local agents in ${CONFIG_DIR_NAME}/agents, set agentScope: "both" (or "project").`,
	].join(" ");
}

/** The steering interrupt: a typed Enter (steering) while workers are
 *  active must not queue behind a long worker. Alt+Enter
 *  (streamingBehavior "followUp") keeps its explicit queue-until-done
 *  meaning, and extension-injected messages pass through untouched. */
class SteeringGate {
	constructor(
		private readonly registry: SubagentRegistry,
		private readonly sendUserMessage: (text: string) => void,
	) {}

	handle(event: { source?: string; streamingBehavior?: string; text: string }) {
		if (event.source === "extension") return { action: "continue" as const };
		if (event.streamingBehavior !== "steer") return { action: "continue" as const };
		if (this.registry.activeWithProc().length === 0) {
			return { action: "continue" as const };
		}
		this.registry.interruptActive();
		// sendUserMessage is typed void but the runtime returns a promise;
		// Promise.resolve absorbs either so a rejection cannot surface.
		void Promise.resolve(this.sendUserMessage(event.text)).catch(() => {});
		return { action: "handled" as const };
	}
}

/** The subagent tool's execution: assembles per-call collaborators and
 *  delegates to the dispatch. Rendering is a one-line pass-through to
 *  the views. */
class SubagentToolHandler {
	constructor(
		private readonly registry: SubagentRegistry,
		private readonly views: SubagentResultViews,
	) {}

	async execute(
		_toolCallId: string,
		params: SubagentToolParams,
		signal: AbortSignal | undefined,
		onUpdate: ((partial: AgentToolResult<SubagentDetails>) => void) | undefined,
		ctx: any,
	): Promise<SubagentToolResult> {
		const discovery = discoverAgents(ctx.cwd, this.agentScope(params));
		const dispatch = new SubagentDispatch(ctx, {
			agentScope: this.agentScope(params),
			registry: this.registry,
			agents: discovery.agents,
			projectAgentsDir: discovery.projectAgentsDir,
			dispatchDefaults: this.buildDefaults(ctx),
			notifySpawn: (message: string) => this.notifySpawn(ctx, message),
		});
		return dispatch.execute(params, signal, onUpdate);
	}

	renderCall(args: Record<string, any>, theme: any) {
		return this.views.call(args, theme);
	}

	renderResult(
		result: AgentToolResult<SubagentDetails>,
		expanded: boolean,
		theme: any,
	) {
		return this.views.result(result, expanded, theme);
	}

	private agentScope(params: SubagentToolParams): AgentScope {
		return params.agentScope ?? "user";
	}

	private buildDefaults(ctx: any) {
		return {
			model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
			thinkingLevel: ctx.thinkingLevel,
		};
	}

	private notifySpawn(ctx: any, message: string) {
		// Headless-safe: ui.notify is a no-op without a UI; guarded anyway.
		try {
			ctx.ui.notify(message, "info");
		} catch {
			/* ignore */
		}
	}
}

/** Owns one detail-view instance so the ui.custom callback's write and
 *  the disposal read touch a single owner instead of a closure variable. */
class DetailViewSession {
	private view: SubagentDetailView | undefined;

	mount(ui: any, entry: RunningSubagent): Promise<null> {
		return (ui.custom as (cb: (tui: any, theme: any, kb: any, done: any) => any) => Promise<null>)(
			(tui, theme, _kb, done) => {
				this.view = new SubagentDetailView(entry, tui, theme, done);
				this.view.open();
				return this.view;
			},
		).finally(() => this.view?.close());
	}
}

/** The /subagents navigation loop: selector → detail → back, until the
 *  user exits the selector. */
class SubagentsBrowser {
	constructor(private readonly registry: SubagentRegistry) {}

	async run(ui: any): Promise<void> {
		const sessions = new DetailViewSession();
		try {
			while (true) {
				const selected = await this.openList(ui);
				if (!selected) return;
				await sessions.mount(ui, selected);
			}
		} catch {
			/* selector or viewer unavailable/canceled mid-loop */
		}
	}

	private openList(ui: any): Promise<RunningSubagent | null> {
		// Selector: settings-integrated — a plain non-overlay custom
		// component mounts into the editor dock exactly like /settings.
		return ui.custom(
			(tui: any, theme: any, _kb: any, done: any) =>
				new SubagentSelectorView(this.registry, tui, theme, done),
		) as Promise<RunningSubagent | null>;
	}
}

/** Owns the long-lived collaborators and exposes the handlers the
 *  ExtensionAPI registration binds to. */
class SubagentExtension {
	private readonly steering: SteeringGate;
	private readonly tool: SubagentToolHandler;
	private readonly browser: SubagentsBrowser;

	constructor(
		private readonly registry: SubagentRegistry,
		private readonly views: SubagentResultViews,
		private readonly sendUserMessage: (text: string) => void,
	) {
		this.steering = new SteeringGate(registry, sendUserMessage);
		this.tool = new SubagentToolHandler(registry, views);
		this.browser = new SubagentsBrowser(registry);
	}

	handleInput(event: { source?: string; streamingBehavior?: string; text: string }) {
		return this.steering.handle(event);
	}

	executeTool(
		toolCallId: string,
		params: SubagentToolParams,
		signal: AbortSignal | undefined,
		onUpdate: ((partial: AgentToolResult<SubagentDetails>) => void) | undefined,
		ctx: any,
	): Promise<SubagentToolResult> {
		return this.tool.execute(toolCallId, params, signal, onUpdate, ctx);
	}

	renderCall(args: Record<string, any>, theme: any) {
		return this.tool.renderCall(args, theme);
	}

	renderResult(result: AgentToolResult<SubagentDetails>, expanded: boolean, theme: any) {
		return this.tool.renderResult(result, expanded, theme);
	}

	async runCommand(_args: string, cmdCtx: any): Promise<void> {
		if (cmdCtx.mode !== "tui") return;
		await this.browser.run(cmdCtx.ui);
	}
}

export default function (pi: ExtensionAPI) {
	const app = new SubagentExtension(
		new SubagentRegistry(),
		new SubagentResultViews(),
		(text: string) => {
			// sendUserMessage is typed void but the runtime returns a
			// promise; Promise.resolve absorbs either.
			void Promise.resolve(pi.sendUserMessage(text, { deliverAs: "steer" })).catch(() => {});
		},
	);

	pi.on("input", (event) => app.handleInput(event));

	pi.registerTool({
		name: "subagent",
		label: "Subagent",
		description: buildToolDescription(),
		parameters: SubagentParams,

		async execute(toolCallId, params, signal, onUpdate, ctx) {
			return app.executeTool(
				toolCallId,
				params as SubagentToolParams,
				signal,
				onUpdate,
				ctx,
			);
		},

		renderCall(args, theme, _context) {
			return app.renderCall(args, theme);
		},

		renderResult(result, { expanded }, theme, _context) {
			return app.renderResult(result as AgentToolResult<SubagentDetails>, expanded, theme);
		},
	});

	pi.registerCommand("subagents", {
		description: "List subagents spawned this session; open one to watch its activity",
		handler: async (_args, cmdCtx) => {
			await app.runCommand(_args, cmdCtx);
		},
	});
}
