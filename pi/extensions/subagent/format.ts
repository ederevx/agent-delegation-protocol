/**
 * Subagent formatting and message-shape helpers
 *
 * Pure functions only: they own no state and mutate nothing, so they stay
 * module-level per the single-responsibility convention (a class would own
 * nothing). Consumed by the run lifecycle, the dispatch, and the /subagents
 * views.
 */

import * as os from "node:os";
import type { Message } from "@earendil-works/pi-ai";
import type { RunningSubagent } from "./registry.ts";
import { MAX_PARALLEL_OUTPUT_CAP } from "./constants.ts";
import type { DisplayItem, SingleResult } from "./types.ts";

const TOOL_RESULT_PREVIEW_LINES = 10;

function formatTokens(count: number): string {
	if (count < 1000) return count.toString();
	if (count < 10000) return `${(count / 1000).toFixed(1)}k`;
	if (count < 1000000) return `${Math.round(count / 1000)}k`;
	return `${(count / 1000000).toFixed(1)}M`;
}

/**
 * Usage formatter with an optional `turnLimit`: when a turn budget is
 * known, turns print as `used/limit` instead of a bare count.
 */
export function formatUsageStats(
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
	opts?: { omitTurns?: boolean },
): string {
	const parts: string[] = [];
	if (usage.turns && !opts?.omitTurns)
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

export function formatToolCall(
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

export function getFinalOutput(messages: Message[]): string {
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

// Indented tool-result preview for the /subagents detail transcript, capped
// at a fixed number of lines per result.
export function indentPreview(text: string): string {
	return text
		.split("\n")
		.slice(0, TOOL_RESULT_PREVIEW_LINES)
		.map((line) => `  ${line}`)
		.join("\n");
}

/** Assistant text, tool calls, and tool results, in stream order. */
export function getDisplayItems(messages: Message[]): DisplayItem[] {
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

export function isFailedResult(result: SingleResult): boolean {
	// A still-running child (exitCode -1) is neither failed nor finished.
	if (result.exitCode === -1) return false;
	// A budget-exhausted child was terminated by us on purpose, so its exit
	// code and stop reason look like an abort; that is not a task failure.
	if (result.turnBudgetExhausted) return false;
	return result.exitCode !== 0 || result.stopReason === "error" || result.stopReason === "aborted";
}

export function getResultOutput(result: SingleResult): string {
	if (isFailedResult(result)) {
		return result.errorMessage || result.stderr || getFinalOutput(result.messages) || "(no output)";
	}
	return getFinalOutput(result.messages) || "(no output)";
}

export function truncateParallelOutput(output: string): string {
	const byteLength = Buffer.byteLength(output, "utf8");
	if (byteLength <= MAX_PARALLEL_OUTPUT_CAP) return output;

	let truncated = output.slice(0, MAX_PARALLEL_OUTPUT_CAP);
	while (Buffer.byteLength(truncated, "utf8") > MAX_PARALLEL_OUTPUT_CAP) {
		truncated = truncated.slice(0, -1);
	}
	return `${truncated}\n\n[Output truncated: ${byteLength - Buffer.byteLength(truncated, "utf8")} bytes omitted. Full output preserved in tool details.]`;
}

export function firstLine(text: string): string {
	const line = text.split("\n").find((l) => l.trim()) || "";
	return line.length > 80 ? `${line.slice(0, 80)}...` : line;
}

function formatElapsed(ms: number): string {
	const seconds = Math.floor(ms / 1000);
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m${seconds % 60}s`;
	return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

/** One-line status for a registry entry, as shown by the /subagents views. */
export function statusOf(entry: RunningSubagent): string {
	const turns = entry.result?.usage?.turns ?? 0;
	if (!entry.completedAt) return `running (${turns} turns)`;
	if (isFailedResult(entry.result)) return "failed";
	if (entry.result.turnBudgetExhausted)
		return `exhausted (${turns}/${entry.turnLimit ?? entry.result.turnLimit ?? "?"})`;
	const cost = entry.result.usage?.cost ? `, $${entry.result.usage.cost.toFixed(4)}` : "";
	return `finished (${turns} turns${cost})`;
}

export function elapsedOf(entry: { completedAt?: Date; startedAt: number }): string {
	return formatElapsed((entry.completedAt?.getTime() ?? Date.now()) - entry.startedAt);
}
