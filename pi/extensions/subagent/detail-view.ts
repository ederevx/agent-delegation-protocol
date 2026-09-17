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
 * Rendering follows the session history viewer's architecture (the pattern
 * proven against lots-of-logs breakdowns): each display item is rendered
 * ONCE per width into a cached flat line array (invalidated on width change
 * or listener events, rebuilt lazily on the next render), and every frame
 * emits ONLY a window slice — the viewport height minus the chrome — so the
 * per-frame cost stays bounded no matter how many log lines a worker
 * produced. Listener events just mark the cache dirty; they never render.
 *
 * Frame layout (shared chrome in viewer-chrome.ts): accent title line with
 * dim status/elapsed and the scroll position, then the log window, a dim
 * border rule, and the word-wrapped instruction lines LAST, pinned to the
 * bottom — the window absorbs all slack and the frame emits EXACTLY
 * tui.terminal.rows lines. Sticky followTail keeps the window pinned to the
 * bottom until the user scrolls up; wheel scrolling keeps the pi-tui
 * semantics (wheel-up unpins the tail, wheel-down re-pins at the bottom).
 * Mouse+keyboard throughout.
 */

import {
	getMarkdownTheme,
	type Theme,
} from "@earendil-works/pi-coding-agent";
import {
	isViewportTUI,
	Markdown,
	matchesKey,
	Spacer,
	Text,
	truncateToWidth,
	type Component,
	type TUI,
	type TuiMouseEvent,
	type ViewportTUI,
} from "@earendil-works/pi-tui";
import type { RunningSubagent } from "./registry.ts";
import {
	formatToolCall,
	formatUsageStats,
	getDisplayItems,
	getFinalOutput,
	indentPreview,
	isFailedResult,
	statusOf,
	elapsedOf,
} from "./format.ts";
import { ViewerChrome } from "./viewer-chrome.ts";

/** Dim viewer instructions, word-wrapped under the border rule. */
const INSTRUCTION_TEXT =
	"↑↓/PgUp/PgDn/Home/End scroll · wheel scroll · esc back";

export class SubagentDetailView {
	private readonly entry: RunningSubagent;
	private readonly tui: TUI;
	private readonly theme: Theme;
	private readonly done: (result: null) => void;
	private readonly mdTheme = getMarkdownTheme();
	private readonly chrome: ViewerChrome;

	// Cached flat line array: built once per width, rebuilt lazily after a
	// listener event or width change; render() only slices it.
	private cachedWidth: number | null = null;
	private cachedLines: string[] = [];
	private dirty = true;
	private scrollOffset = 0;
	private followTail = true;
	private tookLayoutRoot = false;
	private unsubscribe: (() => void) | undefined;
	// Present only when the TUI is the fullscreen viewport variant; then
	// this view can replace the whole screen via setLayoutRoot.
	private readonly viewportTui: ViewportTUI | undefined;

	// Live stream wake-up: mark the cache dirty and request a render; the
	// rebuild happens lazily in the next render() and stays per-change, not
	// per-frame.
	private readonly onStream = () => {
		this.dirty = true;
		this.tui.requestRender();
	};

	constructor(entry: RunningSubagent, tui: TUI, theme: Theme, done: (result: null) => void) {
		this.entry = entry;
		this.tui = tui;
		this.theme = theme;
		this.done = done;
		this.viewportTui = isViewportTUI(tui) ? tui : undefined;
		this.chrome = new ViewerChrome(theme, INSTRUCTION_TEXT);
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
		if (this.cachedWidth !== width || this.dirty) this.rebuild(width);
		const rows = this.tui.terminal.rows;
		const instructions = this.chrome.keptInstructions(rows);
		const windowHeight = this.chrome.contentWindowHeight(rows, instructions.length);
		this.clampScroll(windowHeight);

		const out: string[] = [];
		out.push(truncateToWidth(this.titleLine(width, windowHeight), width));
		this.chrome.appendTop(out, width, rows);
		this.appendContentWindow(out, windowHeight);
		out.push(truncateToWidth(this.statsLine(width), width));
		return this.chrome.clipFrame(out, rows);
	}

	invalidate(): void {
		this.cachedWidth = null;
		this.cachedLines = [];
		this.dirty = true;
		this.chrome.invalidate();
	}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.done(null);
			return;
		}
		const windowHeight = this.windowHeight();
		if (matchesKey(data, "up")) {
			this.followTail = false;
			this.scrollOffset = Math.max(0, this.scrollOffset - 1);
		} else if (matchesKey(data, "down")) {
			this.followTail = true;
			this.scrollOffset += 1;
		} else if (matchesKey(data, "pageUp")) {
			this.followTail = false;
			this.scrollOffset = Math.max(0, this.scrollOffset - (windowHeight - 1));
		} else if (matchesKey(data, "pageDown")) {
			this.followTail = true;
			this.scrollOffset += windowHeight - 1;
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
		if (this.cachedWidth === null) return undefined;
		return { handled: this.applyWheelDelta(event.wheelDelta ?? 0) };
	}

	/** Raw wheel input (regular TUI mode): same semantics as handleMouse —
	 * pi-tui emits a negative delta on wheel-up, so adding it moves the
	 * window toward earlier lines; wheel-up unpins the tail, wheel-down
	 * re-pins at the bottom. No-op before the first render built the cache. */
	applyWheelDelta(delta: number): boolean {
		if (this.cachedWidth === null) return false;
		const windowHeight = this.windowHeight();
		const maxOffset = Math.max(0, this.cachedLines.length - windowHeight);
		this.scrollOffset = Math.min(
			Math.max(0, this.scrollOffset + delta),
			maxOffset,
		);
		const changed = this.followTail || this.scrollOffset !== maxOffset || delta !== 0;
		this.followTail = this.scrollOffset >= maxOffset;
		this.tui.requestRender();
		return changed;
	}

	// ------------------------------------------------------------------
	// Frame assembly (title → window → border → instructions)
	// ------------------------------------------------------------------

	/** Content-window height; one shared source of truth for render(),
	 * handleInput() page math, and handleMouse() clamping. One row below
	 * the chrome's budget is reserved for the stats line at the bottom. */
	private windowHeight(): number {
		return Math.max(1, this.chrome.contentWindowHeight(this.tui.terminal.rows) - 1);
	}

	/** Sticky bottom until the user scrolls up; clamp inside [0, maxOffset]. */
	private clampScroll(windowHeight: number): void {
		const maxOffset = Math.max(0, this.cachedLines.length - windowHeight);
		if (this.followTail) this.scrollOffset = maxOffset;
		this.scrollOffset = Math.min(Math.max(0, this.scrollOffset), maxOffset);
	}

	/** Accent title: `#<id> <agent> (<tier>) — <mode>`, dim status/elapsed,
	 * and the dim scroll position (same window arithmetic as render()). */
	private titleLine(width: number, windowHeight: number): string {
		const entry = this.entry;
		const parts = [
			this.theme.fg("accent", `#${entry.id} ${entry.agent} (${entry.tier ?? "?"}) — ${entry.mode}`),
			this.theme.fg("dim", ` ${statusOf(entry)} · ${elapsedOf(entry)}`),
		];
		if (this.cachedLines.length > 0) {
			const position =
				`lines ${this.scrollOffset + 1}–${Math.min(this.cachedLines.length, this.scrollOffset + windowHeight)} of ${this.cachedLines.length}`;
			parts.push(this.theme.fg("dim", ` · ${position}`));
		}
		return truncateToWidth(parts.join(""), width);
	}

	/** Push the visible slice, padded to exactly windowHeight rows. */
	private appendContentWindow(out: string[], windowHeight: number): void {
		const slice = this.cachedLines.slice(this.scrollOffset, this.scrollOffset + windowHeight);
		for (let i = 0; i < windowHeight; i++) out.push(slice[i] ?? "");
	}

	/** Dim model-usage line pinned at the frame's bottom edge, below the
	 * content window: turns, tokens, cache, cost, and model. */
	private statsLine(width: number): string {
		const r = this.entry.result;
		const usageStr = formatUsageStats(r.usage, r.model, r.turnLimit);
		return this.theme.fg("dim", usageStr || "no usage yet");
	}

	// ------------------------------------------------------------------
	// Content building (one section per method; flattened once per width)
	// ------------------------------------------------------------------

	/** Rebuild the flat line cache at `width`: render each display item
	 * once, then flatten. Runs only after a width change or a listener
	 * event (dirty flag) — never twice for the same frame. */
	private rebuild(width: number): void {
		this.chrome.layout(width);
		const lines: string[] = [];
		for (const item of this.buildItems()) {
			for (const line of item.render(width)) {
				// ANSI-aware truncation: a line wider than the terminal would
				// soft-wrap, shifting every row below down and pushing the
				// border + instruction block off-screen. No frame line may
				// exceed the render width.
				lines.push(truncateToWidth(line, width));
			}
		}
		this.cachedLines = lines;
		this.cachedWidth = width;
		this.dirty = false;
	}

	/** Fresh renderable components for the current entry state (the title
	 * chrome lives in titleLine(); everything below it lands in the log
	 * window). */
	private buildItems(): Component[] {
		const items: Component[] = [];
		this.addTurnMeta(items);
		this.addTask(items);
		this.addActivity(items);
		this.addFinalOutput(items);
		this.addStreamedPartial(items);
		this.addBudgetWarning(items);
		this.addError(items);
		return items;
	}

	private addTurnMeta(items: Component[]): void {
		const entry = this.entry;
		const turns = entry.result.usage.turns;
		const turnLimit = entry.turnLimit ?? entry.result.turnLimit;
		if (!turnLimit) return;
		const turnInfo = `${turns}/${turnLimit} turns`;
		items.push(
			new Text(this.theme.fg("muted", turnInfo + (entry.result.model ? ` · ${entry.result.model}` : "")), 0, 0),
		);
	}

	private addTask(items: Component[]): void {
		items.push(new Text(this.theme.fg("muted", "─── Task ───"), 0, 0));
		items.push(new Text(this.theme.fg("dim", this.entry.task), 0, 0));
	}

	private addActivity(items: Component[]): void {
		const r = this.entry.result;
		items.push(new Text(this.theme.fg("muted", "─── Activity ───"), 0, 0));
		const displayItems = getDisplayItems(r.messages);
		if (displayItems.length === 0) {
			items.push(
				new Text(
					this.theme.fg("muted", this.entry.completedAt ? "(no activity)" : "(waiting for first turn)"),
					0,
					0,
				),
			);
			return;
		}
		for (const item of displayItems) {
			if (item.type === "toolCall") {
				items.push(
					new Text(
						this.theme.fg("muted", "→ ") + formatToolCall(item.name, item.args, this.theme.fg.bind(this.theme), true),
						0,
						0,
					),
				);
			} else if (item.type === "toolResult") {
				items.push(
					new Text(
						this.theme.fg(item.isError ? "error" : "toolOutput", indentPreview(item.text)),
						0,
						0,
					),
				);
			} else {
				items.push(new Text(this.theme.fg("toolOutput", item.text), 0, 0));
			}
		}
	}

	private addFinalOutput(items: Component[]): void {
		const finalOutput = getFinalOutput(this.entry.result.messages);
		if (!finalOutput) return;
		items.push(new Spacer(1));
		items.push(new Markdown(finalOutput.trim(), 0, 0, this.mdTheme));
	}

	private addStreamedPartial(items: Component[]): void {
		if (!this.entry.partialText) return;
		items.push(new Spacer(1));
		items.push(new Text(this.theme.fg("dim", this.entry.partialText), 0, 0));
	}

	private addBudgetWarning(items: Component[]): void {
		const r = this.entry.result;
		if (!r.turnBudgetExhausted) return;
		items.push(new Spacer(1));
		items.push(
			new Text(
				this.theme.fg("warning", `⚠ turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`),
				0,
				0,
			),
		);
	}

	private addError(items: Component[]): void {
		const r = this.entry.result;
		if (!isFailedResult(r) || !r.errorMessage) return;
		items.push(new Text(this.theme.fg("error", `Error: ${r.errorMessage}`), 0, 0));
	}

	private addUsage(items: Component[]): void {
		const usageStr = formatUsageStats(this.entry.result.usage);
		if (!usageStr) return;
		items.push(new Text(this.theme.fg("dim", usageStr), 0, 0));
	}
}
