/**
 * Shared subagent types
 *
 * Pure type declarations for the vendored subagent extension, split by
 * responsibility. No runtime behavior lives here.
 *
 * `SingleResult` is the live child-state object: a `SubagentRun` (run.ts)
 * is its single writer while the child streams, and the /subagents views
 * read it in place.
 */

import type { AgentToolResult, ThinkingLevel } from "@earendil-works/pi-agent-core";
import type { Message } from "@earendil-works/pi-ai";
import type { AgentScope } from "./agents.ts";

export interface UsageStats {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	cost: number;
	contextTokens: number;
	turns: number;
}

export interface SingleResult {
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

/** One spawned child's dispatch role within its parent tool call. */
export type SubagentMode = "single" | "parallel-task" | "chain-step";

export interface SubagentDetails {
	mode: "single" | "parallel" | "chain";
	agentScope: AgentScope;
	projectAgentsDir: string | null;
	results: SingleResult[];
}

export interface DispatchDefaults {
	model?: string;
	thinkingLevel?: ThinkingLevel;
}

export type OnUpdateCallback = (partial: AgentToolResult<SubagentDetails>) => void;

/** One task unit inside a parallel fan-out or a chain step. */
export interface TaskSpec {
	agent: string;
	task: string;
	cwd?: string;
}

/** Structural shape of the registered `subagent` tool parameters. */
export interface SubagentToolParams {
	agent?: string;
	task?: string;
	tasks?: TaskSpec[];
	chain?: TaskSpec[];
	agentScope?: AgentScope;
	confirmProjectAgents?: boolean;
	cwd?: string;
}

/**
 * Tool result with the error hint pi surfaces for failed dispatches.
 * `AgentToolResult` itself has no `isError`, but the runtime accepts and
 * renders the extra flag, so the dispatch carries it.
 */
export interface SubagentToolResult extends AgentToolResult<SubagentDetails> {
	isError?: boolean;
}

/** Assistant text, tool calls, and tool results, in stream order. */
export type DisplayItem =
	| { type: "text"; text: string }
	| { type: "toolCall"; name: string; args: Record<string, any> }
	| { type: "toolResult"; toolName: string; text: string; isError: boolean };
