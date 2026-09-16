/**
 * SubagentDetailView — /subagents activity viewer
 *
 * Opens as a non-overlay `ctx.ui.custom()` component (no window). In
 * fullscreen TUI mode the viewport TUI supports a swappable layout root, so
 * `open()` installs this view itself as that root — the viewer replaces the
 * entire TUI with its own full-screen transcript — and `close()` hands the
 * previous screen back. In regular (inline) mode it dock-integrates like
 * the selector.
 *
 * The view owns its content cache and scroll window. It rebuilds from the
 * live SingleResult on every stream event so the view tracks the running
 * child; sticky followTail keeps rebuilds pinned to the bottom until the
 * user scrolls up. No truncation: task, status, and JSON args reach the
 * renderer fully, which word-wraps Text/Markdown at the real width.
 * Mouse+keyboard throughout.
 */

import {
	getMarkdownTheme,
	type Theme,
} from "@earendil-works/pi-coding-agent";
import {
	Container,
	isViewportTUI,
	Markdown,
	matchesKey,
	Spacer,
	Text,
	type TuiMouseEventResult,
	type TUI,
	type TuiMouseEvent,
	type ViewportTUI,
} from "@earendil-works/pi-tui";
import type { RunningSubagent } from "./registry.ts";
import {
	elapsedOf,
	formatToolCall,
	formatUsageStats,
	getDisplayItems,
	getFinalOutput,
	indentPreview,
	isFailedResult,
	statusOf,
} from "./format.ts";

export class SubagentDetailView {
	private readonly entry: RunningSubagent;
	private readonly tui: TUI;
	private readonly theme: Theme;
	private readonly done: (result: null) => void;
	private readonly mdTheme = getMarkdownTheme();

	private content: Container;
	private scrollOffset = 0;
	private followTail = true;
	private renderedLines = 0;
	private tookLayoutRoot = false;
	private unsubscribe: (() => void) | undefined;
	// Present only when the TUI is the fullscreen viewport variant; then
	// this view can replace the whole screen via setLayoutRoot.
	private readonly viewportTui: ViewportTUI | undefined;

	// Live stream wake-up: rebuild the content and request a render.
	private readonly onStream = () => {
		this.content = this.buildContent();
		this.tui.requestRender();
	};

	constructor(entry: RunningSubagent, tui: TUI, theme: Theme, done: (result: null) => void) {
		this.entry = entry;
		this.tui = tui;
		this.theme = theme;
		this.done = done;
		this.viewportTui = isViewportTUI(tui) ? tui : undefined;
		this.content = this.buildContent();
	}

	// ------------------------------------------------------------------
	// Lifecycle
	// ------------------------------------------------------------------

	/**
	 * Subscribe to live stream updates; in fullscreen TUI mode, replace the
	 * entire TUI by installing this view as the layout root (the previous
	 * root is restored on close).
	 */
	open(): void {
		this.entry.listeners.add(this.onStream);
		this.unsubscribe = () => this.entry.listeners.delete(this.onStream);
		if (this.viewportTui) {
			this.tookLayoutRoot = true;
			this.viewportTui.setLayoutRoot(this);
		}
	}

	/** Idempotent teardown: unsubscribe and hand the screen back. */
	close(): void {
		this.unsubscribe?.();
		this.unsubscribe = undefined;
		if (this.tookLayoutRoot) {
			this.tookLayoutRoot = false;
			this.viewportTui?.setLayoutRoot(undefined);
		}
	}

	// ------------------------------------------------------------------
	// Component surface (also the fullscreen layout root)
	// ------------------------------------------------------------------

	render(width: number): string[] {
		const lines = this.content.render(width);
		const vp = this.viewport();
		const maxOffset = Math.max(0, lines.length - vp);
		if (this.followTail) this.scrollOffset = maxOffset;
		this.scrollOffset = Math.min(Math.max(0, this.scrollOffset), maxOffset);
		this.renderedLines = lines.length;
		const window = lines.slice(this.scrollOffset, this.scrollOffset + vp);
		const scrolling = maxOffset > 0;
		const footer =
			"↑↓/PgUp/PgDn scroll · wheel scroll · esc back" +
			(scrolling
				? ` · lines ${this.scrollOffset + 1}–${Math.min(this.scrollOffset + vp, lines.length)}/${lines.length}`
				: "");
		// Settings hint style: dim, two-space indent, no box.
		return [...window, ...new Text(this.theme.fg("dim", `  ${footer}`), 0, 0).render(width)];
	}

	invalidate(): void {
		this.content.invalidate();
	}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.done(null);
			return;
		}
		const vp = this.viewport();
		if (matchesKey(data, "up")) {
			this.followTail = false;
			this.scrollOffset = Math.max(0, this.scrollOffset - 1);
		} else if (matchesKey(data, "down")) {
			this.followTail = true;
			this.scrollOffset += 1;
		} else if (matchesKey(data, "pageUp")) {
			this.followTail = false;
			this.scrollOffset = Math.max(0, this.scrollOffset - (vp - 1));
		} else if (matchesKey(data, "pageDown")) {
			this.followTail = true;
			this.scrollOffset += vp - 1;
		} else if (matchesKey(data, "home")) {
			this.followTail = false;
			this.scrollOffset = 0;
		} else if (matchesKey(data, "end")) {
			this.followTail = true;
			this.scrollOffset = Number.MAX_SAFE_INTEGER;
		} else {
			return;
		}
		this.tui.requestRender();
	}

	handleMouse(event: TuiMouseEvent) {
		if (event.type !== "wheel") return undefined;
		// pi-tui emits a negative wheelDelta on wheel-up ("Negative values
		// scroll up"), so adding it moves the window toward earlier lines;
		// wheel-up unpins the tail, wheel-down re-pins at the bottom.
		this.scrollOffset += event.wheelDelta ?? 0;
		const vp = this.viewport();
		const maxOffset = Math.max(0, this.renderedLines - vp);
		this.scrollOffset = Math.min(Math.max(0, this.scrollOffset), maxOffset);
		this.followTail = this.scrollOffset >= maxOffset;
		this.tui.requestRender();
		return { handled: true };
	}

	// ------------------------------------------------------------------
	// Content building (one section per method)
	// ------------------------------------------------------------------

	private viewport(): number {
		return Math.max(4, this.tui.terminal.rows - 2);
	}

	private buildContent(): Container {
		const container = new Container();
		this.addHeader(container);
		this.addTurnMeta(container);
		container.addChild(new Spacer(1));
		this.addTask(container);
		container.addChild(new Spacer(1));
		this.addActivity(container);
		this.addFinalOutput(container);
		this.addStreamedPartial(container);
		this.addBudgetWarning(container);
		this.addError(container);
		this.addUsage(container);
		return container;
	}

	private addHeader(container: Container): void {
		const entry = this.entry;
		const statusColor = !entry.completedAt ? "warning" : isFailedResult(entry.result) ? "error" : "success";
		container.addChild(
			new Text(
				this.theme.fg("toolTitle", this.theme.bold(`#${entry.id} ${entry.agent} (${entry.tier ?? "?"})`)) +
					this.theme.fg("muted", ` — ${entry.mode}`) +
					this.theme.fg(statusColor, ` ${statusOf(entry)}`) +
					this.theme.fg("muted", ` · ${elapsedOf(entry)}`),
				0,
				0,
			),
		);
	}

	private addTurnMeta(container: Container): void {
		const entry = this.entry;
		const turns = entry.result.usage.turns;
		const turnLimit = entry.turnLimit ?? entry.result.turnLimit;
		if (!turnLimit) return;
		const turnInfo = `${turns}/${turnLimit} turns`;
		container.addChild(
			new Text(this.theme.fg("muted", turnInfo + (entry.result.model ? ` · ${entry.result.model}` : "")), 0, 0),
		);
	}

	private addTask(container: Container): void {
		container.addChild(new Text(this.theme.fg("muted", "─── Task ───"), 0, 0));
		container.addChild(new Text(this.theme.fg("dim", this.entry.task), 0, 0));
	}

	private addActivity(container: Container): void {
		const r = this.entry.result;
		container.addChild(new Text(this.theme.fg("muted", "─── Activity ───"), 0, 0));
		const items = getDisplayItems(r.messages);
		if (items.length === 0) {
			container.addChild(
				new Text(
					this.theme.fg("muted", this.entry.completedAt ? "(no activity)" : "(waiting for first turn)"),
					0,
					0,
				),
			);
			return;
		}
		for (const item of items) {
			if (item.type === "toolCall") {
				container.addChild(
					new Text(
						this.theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, this.theme.fg.bind(this.theme), true),
						0,
						0,
					),
				);
			} else if (item.type === "toolResult") {
				container.addChild(
					new Text(
						this.theme.fg(item.isError ? "error" : "toolOutput", indentPreview(item.text)),
						0,
						0,
					),
				);
			} else {
				container.addChild(new Text(this.theme.fg("toolOutput", item.text), 0, 0));
			}
		}
	}

	private addFinalOutput(container: Container): void {
		const finalOutput = getFinalOutput(this.entry.result.messages);
		if (!finalOutput) return;
		container.addChild(new Spacer(1));
		container.addChild(new Markdown(finalOutput.trim(), 0, 0, this.mdTheme));
	}

	private addStreamedPartial(container: Container): void {
		if (!this.entry.partialText) return;
		container.addChild(new Spacer(1));
		container.addChild(new Text(this.theme.fg("dim", this.entry.partialText), 0, 0));
	}

	private addBudgetWarning(container: Container): void {
		const r = this.entry.result;
		if (!r.turnBudgetExhausted) return;
		container.addChild(new Spacer(1));
		container.addChild(
			new Text(
				this.theme.fg("warning", `⚠ turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`),
				0,
				0,
			),
		);
	}

	private addError(container: Container): void {
		const r = this.entry.result;
		if (!isFailedResult(r) || !r.errorMessage) return;
		container.addChild(new Text(this.theme.fg("error", `Error: ${r.errorMessage}`), 0, 0));
	}

	private addUsage(container: Container): void {
		const usageStr = formatUsageStats(this.entry.result.usage);
		if (!usageStr) return;
		container.addChild(new Text(this.theme.fg("dim", usageStr), 0, 0));
	}
}
