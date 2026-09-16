/**
 * SubagentResultViews — finished and live tool-call/result rendering
 *
 * Stateless rendering for the registered `subagent` tool: one-line call
 * previews and the per-mode result views (single/chain/parallel, collapsed
 * and expanded), including the local additions over upstream — turn-budget
 * markers and the compact live one-liners while a child is still running.
 * All state comes in through method parameters.
 */

import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import {
	Container,
	Markdown,
	Spacer,
	Text,
	type Component,
} from "@earendil-works/pi-tui";
import { getMarkdownTheme, type Theme } from "@earendil-works/pi-coding-agent";
import { COLLAPSED_ITEM_COUNT } from "./constants.ts";
import type { AgentScope } from "./agents.ts";
import {
	firstLine,
	formatToolCall,
	formatUsageStats,
	getDisplayItems,
	getFinalOutput,
	isFailedResult,
} from "./format.ts";
import type { DisplayItem, SingleResult, SubagentDetails } from "./types.ts";

/**
 * Subset of the tool renderer's ToolRenderContext that the call renderer
 * needs: the tool row's phase flags. isPartial stays true from construction
 * through every partial update and only becomes false when the FINAL result
 * lands (tool-execution.ts defaults isPartial = true; updateResult() sets it
 * false); executionStarted flips on markExecutionStarted(). Pending is
 * executionStarted === false; running is executionStarted && isPartial.
 */
export interface ToolCallPhase {
	executionStarted: boolean;
	isPartial: boolean;
}

export class SubagentResultViews {
	/**
	 * Call-slot preview of the tool call arguments. Chain and parallel keep
	 * their summary lines (nothing else shows that overview). Single-agent
	 * calls render an EMPTY component in every phase: while the task runs
	 * the spawn notification already carries agent, tier, mode, turn budget,
	 * and the task preview — repeating them in the call slot duplicated the
	 * notification directly above it — and once the final result lands
	 * (isPartial false) the agent identity is in the result header anyway,
	 * so restoring the preview would reintroduce the duplication. The flags
	 * used to reason about the phases are ToolRenderContext.executionStarted
	 * and .isPartial (see ToolCallPhase).
	 */
	call(args: Record<string, any>, theme: Theme, context?: ToolCallPhase): Component {
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
		// Single-agent call slot: empty in EVERY phase. Pending/running
		// (executionStarted false, or isPartial true) would duplicate the
		// persistent spawn notification directly above it; finished (isPartial
		// false) would duplicate both that notification and the result header.
		return new Text("", 0, 0);
	}

	/** Render a (possibly still-running) tool result by mode. */
	result(
		result: AgentToolResult<SubagentDetails>,
		expanded: boolean,
		theme: Theme,
	): Component {
		const details = result.details as SubagentDetails | undefined;

		if (!details || details.results.length === 0) {
			const text = result.content[0];
			return new Text(text?.type === "text" ? firstLine(text.text) : "(no output)", 0, 0);
		}

		if (details.mode === "single" && details.results.length === 1) {
			return this.renderSingle(details.results[0], expanded, theme);
		}

		// Running multi-step states keep the compact live one-liner from the
		// local rewrite; finished states use the restored upstream views.
		if (details.results.some((r) => r.exitCode === -1)) {
			return this.renderRunningMulti(details, theme);
		}
		if (details.mode === "chain") return this.renderChain(details, expanded, theme);
		if (details.mode === "parallel") return this.renderParallel(details, expanded, theme);

		const text = result.content[0];
		return new Text(text?.type === "text" ? firstLine(text.text) : "(no output)", 0, 0);
	}

	// ------------------------------------------------------------------
	// Shared render helpers
	// ------------------------------------------------------------------

	private renderDisplayItems(
		items: DisplayItem[],
		limit: number | undefined,
		theme: Theme,
		expanded: boolean,
	): string {
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
	}

	private budgetMarker(r: SingleResult, theme: Theme): string {
		return r.turnBudgetExhausted
			? theme.fg("warning", `⚠ turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`)
			: "";
	}

	// ------------------------------------------------------------------
	// Single
	// ------------------------------------------------------------------

	private renderSingle(r: SingleResult, expanded: boolean, theme: Theme): Component {
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
		const mdTheme = getMarkdownTheme();

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
				container.addChild(new Text(this.budgetMarker(r, theme), 0, 0));
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
			text += `\n${this.renderDisplayItems(displayItems, COLLAPSED_ITEM_COUNT, theme, expanded)}`;
			if (displayItems.length > COLLAPSED_ITEM_COUNT) text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
		}
		if (r.turnBudgetExhausted) text += `\n${this.budgetMarker(r, theme)}`;
		const usageStr = formatUsageStats(r.usage, r.model, r.turnLimit);
		if (usageStr) text += `\n${theme.fg("dim", usageStr)}`;
		return new Text(text, 0, 0);
	}

	// ------------------------------------------------------------------
	// Multi-step
	// ------------------------------------------------------------------

	private renderRunningMulti(details: SubagentDetails, theme: Theme): Component {
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

	private renderChain(details: SubagentDetails, expanded: boolean, theme: Theme): Component {
		const successCount = details.results.filter((r) => !isFailedResult(r)).length;
		const icon = successCount === details.results.length ? theme.fg("success", "✓") : theme.fg("error", "✗");
		const mdTheme = getMarkdownTheme();

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

				if (finalOutput) {
					container.addChild(new Spacer(1));
					container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
				}

				if (r.turnBudgetExhausted) container.addChild(new Text(this.budgetMarker(r, theme), 0, 0));

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
			if (r.turnBudgetExhausted) text += ` ${this.budgetMarker(r, theme)}`;
			if (displayItems.length === 0) text += `\n${theme.fg("muted", "(no output)")}`;
			else text += `\n${this.renderDisplayItems(displayItems, 5, theme, expanded)}`;
		}
		const usageStr = formatUsageStats(aggregateUsage(details.results));
		if (usageStr) text += `\n\n${theme.fg("dim", `Total: ${usageStr}`)}`;
		text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
		return new Text(text, 0, 0);
	}

	private renderParallel(details: SubagentDetails, expanded: boolean, theme: Theme): Component {
		const successCount = details.results.filter((r) => !isFailedResult(r)).length;
		const failCount = details.results.filter((r) => isFailedResult(r)).length;
		const icon = failCount > 0 ? theme.fg("warning", "◐") : theme.fg("success", "✓");
		const status = `${successCount}/${details.results.length} tasks`;
		const mdTheme = getMarkdownTheme();

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

				if (finalOutput) {
					container.addChild(new Spacer(1));
					container.addChild(new Markdown(finalOutput.trim(), 0, 0, mdTheme));
				}

				if (r.turnBudgetExhausted) container.addChild(new Text(this.budgetMarker(r, theme), 0, 0));

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
			if (r.turnBudgetExhausted) text += ` ${this.budgetMarker(r, theme)}`;
			if (displayItems.length === 0) text += `\n${theme.fg("muted", "(no output)")}`;
			else text += `\n${this.renderDisplayItems(displayItems, 5, theme, expanded)}`;
		}
		const usageStr = formatUsageStats(aggregateUsage(details.results));
		if (usageStr) text += `\n\n${theme.fg("dim", `Total: ${usageStr}`)}`;
		text += `\n${theme.fg("muted", "(Ctrl+O to expand)")}`;
		return new Text(text, 0, 0);
	}
}

/** Sum of per-result usage across a multi-step dispatch. */
function aggregateUsage(results: SingleResult[]) {
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
}
