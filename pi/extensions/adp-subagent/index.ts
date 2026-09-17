/**
 * adp-subagent — the ADP-owned subagent extension.
 *
 * Delegate tasks to specialized agents in an isolated context.
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
 * ADP coupling: this extension is the ADP-owned delegation vehicle and
 * hosts the delegation enforcer (enforcer.ts, registered below). Behavior
 * changes here must be mirrored in ~/.pi/agent/rules/delegation-protocol.md
 * — an extension update without the matching ADP update is incomplete.
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
 *   workers are active is left to pi's own steer delivery (the message
 *   queues behind the pending subagent tool result; workers are never
 *   killed by a typed message - only the tool-level abort signal, an
 *   explicit Esc, kills them); throttled parent updates (≤4/s);
 *   /subagents UI: settings-integrated (non-overlay, editor-dock mount
 *   exactly like /settings) selector grouping entries under Active/Inactive
 *   headers (active first); the detail viewer replaces the entire TUI in
 *   fullscreen mode via the viewport TUI's setLayoutRoot (own full-screen
 *   root, previous root restored on close) and dock-integrates in regular
 *   mode; mouse+keyboard throughout; the detail viewer renders history-viewer
 *   style: each item flattened once per width into a cached line array
 *   (listener events just mark it dirty; frames emit only a bounded window
 *   slice) under a title line carrying the scroll position, with a dim
 *   border rule and word-wrapped instructions pinned to the bottom of an
 *   exactly-rows-line frame (shared chrome in viewer-chrome.ts); the
 *   selector drops the selected-entry task preview (it garbled with long
 *   logs); the spawn notification carries the full informative set (agent,
 *   tier, mode, turn budget, task preview) and the single-agent call slot
 *   renders empty so it no longer duplicates the notification
 *
 * This file is only the wiring: schema, tool, command, and the enforcer
 * registration. The logic is split by responsibility — see types.ts,
 * registry.ts, run.ts, dispatch.ts, result-views.ts, selector-view.ts,
 * detail-view.ts, and enforcer.ts.
 * wiring itself is decomposed into single-responsibility classes
 * (SubagentToolHandler, SubagentsBrowser, DetailViewSession)
 * composed by SubagentExtension, so no callback body owns another's state.
 */

import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import type { ExtensionAPI, KeybindingsManager } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME, getAgentDir } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import type { AgentScope } from "./agents.ts";
import { discoverAgents } from "./agents.ts";
import { SubagentDispatch } from "./dispatch.ts";
import { registerDelegationEnforcer } from "./enforcer.ts";
import { SubagentDetailView } from "./detail-view.ts";
import { DetailViewWheelBridge } from "./wheel-input.ts";
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

	renderCall(args: Record<string, any>, theme: any, context: any) {
		return this.views.call(args, theme, context);
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

	mount(ui: any, entry: RunningSubagent, cwd: string, sessionStats: string | undefined): Promise<null> {
		// Raw wheel support in regular mode: without terminal mouse tracking
		// the wheel scrolls the terminal's own scrollback, dragging the pinned
		// instruction block away with the content. attach() is a no-op in
		// fullscreen (handleMouse stays the path); detach runs exactly once.
		const wheel = new DetailViewWheelBridge(ui);
		return (ui.custom as (
			cb: (tui: any, theme: any, kb: KeybindingsManager, done: (result: null) => void) => any,
		) => Promise<null>)(
			(tui, theme, kb, done) => {
				// Order matters: the view's constructor is (entry, tui, theme,
				// done, keybindings, sessionStats) — done comes BEFORE the
				// keybindings manager here because ui.custom's factory passes
				// the keybindings manager as its third argument.
				this.view = new SubagentDetailView(entry, tui, theme, done, kb, sessionStats, cwd);
				this.view.open();
				wheel.attach(tui, this.view);
				return this.view;
			},
		).finally(() => {
			wheel.detach();
			this.view?.close();
		});
	}
}

/** The /subagents navigation loop: selector → detail → back, until the
 *  user exits the selector. */
class SubagentsBrowser {
	constructor(private readonly registry: SubagentRegistry) {}

	async run(ui: any, cwd: string, sessionStats: string | undefined): Promise<void> {
		const sessions = new DetailViewSession();
		try {
			while (true) {
				const selected = await this.openList(ui);
				if (!selected) return;
				await sessions.mount(ui, selected, cwd, sessionStats);
			}
		} catch {
			/* selector or viewer unavailable/canceled mid-loop */
		}
	}

	private openList(ui: any): Promise<RunningSubagent | null> {
		// Selector: dock-integrates like /settings (non-overlay custom
		// component); only the detail viewer takes over the full screen in
		// fullscreen mode.
		return ui.custom(
			(tui: any, theme: any, _kb: any, done: any) =>
				new SubagentSelectorView(this.registry, tui, theme, done),
		) as Promise<RunningSubagent | null>;
	}
}

/** Owns the long-lived collaborators and exposes the handlers the
 *  ExtensionAPI registration binds to. */
class SubagentExtension {
	private readonly tool: SubagentToolHandler;
	private readonly browser: SubagentsBrowser;

	constructor(
		private readonly registry: SubagentRegistry,
		private readonly views: SubagentResultViews,
	) {
		this.tool = new SubagentToolHandler(registry, views);
		this.browser = new SubagentsBrowser(registry);
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

	renderCall(args: Record<string, any>, theme: any, context: any) {
		return this.tool.renderCall(args, theme, context);
	}

	renderResult(result: AgentToolResult<SubagentDetails>, expanded: boolean, theme: any) {
		return this.tool.renderResult(result, expanded, theme);
	}

	async runCommand(_args: string, cmdCtx: any): Promise<void> {
		if (cmdCtx.mode !== "tui") return;
		// Computed once before the browse loop: the model/context strip the
		// detail viewer appends to its usage line (session-level stats).
		const usage = cmdCtx.getContextUsage();
		const sessionStats = [
			cmdCtx.model ? `${cmdCtx.model.provider}/${cmdCtx.model.id}` : undefined,
			usage?.percent != null ? `ctx ${Math.round(usage.percent)}%` : undefined,
		].filter(Boolean).join(" · ") || undefined;
		await this.browser.run(cmdCtx.ui, cmdCtx.cwd, sessionStats);
	}
}

export default function (pi: ExtensionAPI) {
	// The delegation enforcer registers first: its tool_call gate must see
	// every call, and its before_agent_start routing policy lands before
	// this extension's own handlers.
	registerDelegationEnforcer(pi);

	const app = new SubagentExtension(
		new SubagentRegistry(),
		new SubagentResultViews(),
	);

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

		renderCall(args, theme, context) {
			return app.renderCall(args, theme, context);
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
