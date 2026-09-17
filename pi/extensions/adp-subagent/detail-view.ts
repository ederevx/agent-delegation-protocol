/**
 * SubagentDetailView — /subagents activity viewer
 *
 * Opens as a non-overlay `ctx.ui.custom()` component (no window). In
 * fullscreen TUI mode the viewport TUI supports a swappable layout root, so
 * `open()` installs this view itself as that root — the viewer replaces the
 * entire TUI with its own full-screen transcript — and `close()` hands the
 * previous screen back. In regular (inline) mode it dock-integrates like
 * the selector. Opening clears the screen first (a forced full repaint), so
 * the frame never lands on a diff of the previous screen.
 *
 * Log formatting is identical to the main transcript: entries render through
 * the same component classes pi uses in the chat (AssistantMessageComponent,
 * ToolExecutionComponent fed by toolResult messages, Spacer rhythm) instead
 * of a simplified text summary.
 *
 * Rendering follows the session history viewer's architecture, adapted for
 * a live append-only log:
 *   - Items are built incrementally — only messages that arrived since the
 *     last build fold into new components (assistant text, tool executions).
 *   - Flattened lines are cached per item; a toolResult updating an already
 *     flattened tool component re-renders from that item onward (the
 *     common case is a near-tail item). The frame emits only the visible
 *     window slice, so per-frame and per-event costs stay bounded.
 *   - The log is bottom-anchored lazily: only ~two windowfuls render at
 *     open; scrolling toward the top renders older items chunk by chunk and
 *     shifts the scroll offset by the added lines, so the current position
 *     is tracked — never reset — as more of the log loads. Home renders the
 *     full log explicitly.
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
	AssistantMessageComponent,
	getMarkdownTheme,
	ToolExecutionComponent,
	type KeybindingsManager,
	type Theme,
} from "@earendil-works/pi-coding-agent";
import {
	isViewportTUI,
	matchesKey,
	Spacer,
	Text,
	type Component,
	type TUI,
	type TuiMouseEvent,
	type ViewportTUI,
} from "@earendil-works/pi-tui";
import type { RunningSubagent } from "./registry.ts";
import { formatUsageStats, isFailedResult, statusOf, elapsedOf } from "./format.ts";
import { ConservativeWidth } from "./conservative-width.ts";
import { ViewerChrome } from "./viewer-chrome.ts";

export class SubagentDetailView {
	private readonly entry: RunningSubagent;
	private readonly tui: TUI;
	private readonly theme: Theme;
	private readonly done: (result: null) => void;
	private readonly cwd: string;
	private readonly mdTheme = getMarkdownTheme();
	private readonly chrome: ViewerChrome;

	// Append-only item list (main-view components) + per-item rendered lines.
	// spanFrom is the first item flattened into spanLines (lazy top);
	// renderedTo is one past the last flattened item. Scrolling up grows the
	// span downward from spanFrom with the scroll offset compensated, so the
	// current position is preserved while older items load. toolResult
	// messages update existing ToolExecutionComponents in place, tracked via
	// dirtyItem (the earliest item whose rendered lines need a refresh).
	private readonly items: Component[] = [];
	private itemLines: (string[] | undefined)[] = [];
	private spanFrom = 0;
	private renderedTo = 0;
	private spanLines: string[] = [];
	private cachedWidth: number | null = null;
	private builtMessages = 0;
	private dirtyItem: number | null = null;
	private appendedBudget = false;
	private appendedError = false;
	private readonly pendingTools = new Map<string, ToolExecutionComponent>();

	private scrollOffset = 0;
	private followTail = true;
	private tookLayoutRoot = false;
	private savedUserBindings: Record<string, unknown> | null = null;
	private unsubscribe: (() => void) | undefined;
	// Present only when the TUI is the fullscreen viewport variant; then
	// this view can replace the whole screen via setLayoutRoot.
	private readonly viewportTui: ViewportTUI | undefined;
	private readonly widthSafe = new ConservativeWidth();

	// Live stream wake-up: the rebuild happens lazily in the next render()
	// (the volatile tail — streamed partial, budget, error — recomputes
	// there; new messages fold in too).
	private readonly onStream = () => {
		this.tui.requestRender();
	};

	constructor(
		entry: RunningSubagent,
		tui: TUI,
		theme: Theme,
		done: (result: null) => void,
		private readonly keybindings: KeybindingsManager,
		private readonly sessionStats: string | undefined,
		cwd: string,
	) {
		this.entry = entry;
		this.tui = tui;
		this.theme = theme;
		this.done = done;
		this.cwd = cwd;
		this.viewportTui = isViewportTUI(tui) ? tui : undefined;
		this.chrome = new ViewerChrome(theme);
	}

	// ------------------------------------------------------------------
	// Lifecycle
	// ------------------------------------------------------------------

	/**
	 * Subscribe to live stream updates; in fullscreen TUI mode, replace the
	 * entire TUI by installing this view as the layout root — after a forced
	 * full repaint so the viewer opens on a cleared screen — and hand the
	 * previous screen back on close.
	 */
	open(): void {
		this.entry.listeners.add(this.onStream);
		this.unsubscribe = () => this.entry.listeners.delete(this.onStream);
		if (this.viewportTui) {
			this.tookLayoutRoot = true;
			this.viewportTui.setLayoutRoot(this);
			this.tui.requestRender(true);
		}
		// While this view owns the screen, the alt-screen scroll bindings
		// (plain PgUp/PgDn/Home/End) would be consumed by TuiAltScreen before
		// the focused component; unbind them so the viewer gets them, and
		// restore the user's bindings on close.
		this.savedUserBindings = this.keybindings.getUserBindings();
		this.keybindings.setUserBindings({
			...this.savedUserBindings,
			"tui.altScreen.pageUp": [],
			"tui.altScreen.pageDown": [],
			"tui.altScreen.top": [],
			"tui.altScreen.bottom": [],
		} as Parameters<KeybindingsManager["setUserBindings"]>[0]);
	}

	/** Idempotent teardown: unsubscribe and hand the screen back. */
	close(): void {
		this.unsubscribe?.();
		this.unsubscribe = undefined;
		if (this.tookLayoutRoot) {
			this.tookLayoutRoot = false;
			this.viewportTui?.setLayoutRoot(undefined);
		}
		if (this.savedUserBindings) {
			this.keybindings.setUserBindings(this.savedUserBindings as Parameters<KeybindingsManager["setUserBindings"]>[0]);
			this.savedUserBindings = null;
		}
	}

	// ------------------------------------------------------------------
	// Component surface (also the fullscreen layout root)
	// ------------------------------------------------------------------

	render(width: number): string[] {
		const rows = this.tui.terminal.rows;
		const windowHeight = this.windowHeight();
		const widthChanged = this.cachedWidth !== width;
		this.ensureItems();
		if (widthChanged) this.resetLines(width, windowHeight);
		this.syncLines();
		this.growToWindow(windowHeight);
		this.clampScroll(windowHeight);

		const out: string[] = [];
		out.push(this.widthSafe.truncate(this.titleLine(width, windowHeight), width));
		this.chrome.appendTop(out, width, rows);
		this.appendContentWindow(out, windowHeight);
		if (this.tookLayoutRoot) {
			this.chrome.statsBorder(out, width, this.statsLine(width));
		} else {
			out.push(this.widthSafe.truncate(this.statsLine(width), width));
		}
		return this.chrome.clipFrame(out, rows);
	}

	invalidate(): void {
		this.cachedWidth = null;
		this.itemLines = [];
		this.spanLines = [];
		this.spanFrom = this.items.length;
		this.renderedTo = this.items.length;
		this.dirtyItem = null;
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
			this.scrollTo(this.scrollOffset - 1, windowHeight);
		} else if (matchesKey(data, "down")) {
			this.scrollTo(this.scrollOffset + 1, windowHeight);
		} else if (matchesKey(data, "pageUp")) {
			this.followTail = false;
			this.scrollTo(this.scrollOffset - (windowHeight - 1), windowHeight);
		} else if (matchesKey(data, "pageDown")) {
			this.scrollTo(this.scrollOffset + (windowHeight - 1), windowHeight);
		} else if (matchesKey(data, "home")) {
			this.followTail = false;
			this.scrollToHome();
		} else if (matchesKey(data, "end")) {
			this.followTail = true;
			this.scrollTo(this.maxOffset(windowHeight), windowHeight);
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
		const wasFollowing = this.followTail;
		if (delta < 0) this.followTail = false;
		this.scrollTo(this.scrollOffset + delta, windowHeight);
		if (this.scrollOffset >= this.maxOffset(windowHeight)) this.followTail = true;
		this.tui.requestRender();
		return wasFollowing || this.followTail || delta !== 0;
	}

	// ------------------------------------------------------------------
	// Scrolling (span growth keeps the current position; nothing resets)
	// ------------------------------------------------------------------

	/** Max scroll offset for a given window height. */
	private maxOffset(windowHeight: number): number {
		return Math.max(0, this.spanLines.length - windowHeight);
	}

	/** Sticky bottom until the user scrolls up; clamp inside [0, maxOffset]. */
	private clampScroll(windowHeight: number): void {
		const maxOffset = this.maxOffset(windowHeight);
		if (this.followTail) this.scrollOffset = maxOffset;
		this.scrollOffset = Math.min(Math.max(0, this.scrollOffset), maxOffset);
	}

	/** Scroll to a span-relative target. Scrolling above the span's first
	 * rendered item grows the span upward by a windowful and shifts the
	 * offset by the added lines — the current position is preserved while
	 * older items load. The span always extends to the newest item, so
	 * downward targets just clamp. */
	private scrollTo(target: number, windowHeight: number): void {
		if (target < 0 && this.spanFrom > 0) {
			const added = this.growUpLines(windowHeight);
			this.scrollOffset += added;
			target += added;
		}
		const maxOffset = this.maxOffset(windowHeight);
		const clamped = Math.min(Math.max(0, target), maxOffset);
		if (clamped !== this.scrollOffset) {
			this.scrollOffset = clamped;
			this.tui.requestRender();
		}
	}

	/** Explicit Home jump: the full log renders and the view lands at line 0. */
	private scrollToHome(): void {
		while (this.spanFrom > 0) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
		}
		this.rebuildSpanLines();
		this.followTail = false;
		this.scrollOffset = 0;
		this.tui.requestRender();
	}

	// ------------------------------------------------------------------
	// Frame assembly (title → window → border → instructions)
	// ------------------------------------------------------------------

	/** Content-window height; one shared source of truth for render(),
	 * handleInput() page math, and wheel clamping. One row below the
	 * chrome's budget is reserved for the stats line at the bottom. */
	private windowHeight(): number {
		return Math.max(1, this.chrome.contentWindowHeight(this.tui.terminal.rows) - 1);
	}

	/** Accent title: `#<id> <agent> (<tier>) — <mode>`, dim status/elapsed,
	 * and the dim scroll position (same window arithmetic as render()). A
	 * trailing `+` marks a partial span (older items not yet rendered). */
	private titleLine(width: number, windowHeight: number): string {
		const entry = this.entry;
		const parts = [
			this.theme.fg("accent", `#${entry.id} ${entry.agent} (${entry.tier ?? "?"}) — ${entry.mode}`),
			this.theme.fg("dim", ` ${statusOf(entry)} · ${elapsedOf(entry)}`),
		];
		if (this.spanLines.length > 0) {
			const position =
				`lines ${this.scrollOffset + 1}–${Math.min(this.spanLines.length, this.scrollOffset + windowHeight)} of ${this.spanLines.length}${this.spanFrom > 0 ? "+" : ""}`;
			parts.push(this.theme.fg("dim", ` · ${position}`));
		}
		return this.widthSafe.truncate(parts.join(""), width);
	}

	/** Push the visible span slice, padded to exactly windowHeight rows,
	 * width-truncating each line (no emitted line may exceed the render
	 * width — a wider line would soft-wrap and shift the frame). */
	private appendContentWindow(out: string[], windowHeight: number): void {
		const slice = this.spanLines.slice(this.scrollOffset, this.scrollOffset + windowHeight);
		for (let i = 0; i < windowHeight; i++) {
			out.push(this.widthSafe.truncate(slice[i] ?? "", this.tui.terminal.columns));
		}
	}

	/** Dim model-usage line pinned at the frame's bottom edge, below the
	 * content window: turns, tokens, cache, cost, and model. */
	private statsLine(width: number): string {
		const r = this.entry.result;
		const usageStr = formatUsageStats(r.usage, r.model, r.turnLimit);
		const stats = this.theme.fg("muted", usageStr || "no usage yet") +
			(this.sessionStats ? this.theme.fg("dim", ` · ${this.sessionStats}`) : "");
		return stats;
	}

	// ------------------------------------------------------------------
	// Item building (main-view components; append-only, incremental)
	// ------------------------------------------------------------------

	/** Fold every message that arrived since the last build into items, and
	 * append the tail decorations (budget warning, error) when they first
	 * apply. */
	private ensureItems(): void {
		const msgs = this.entry.result.messages;
		for (; this.builtMessages < msgs.length; this.builtMessages++) {
			const msg = msgs[this.builtMessages];
			if (msg.role === "assistant") {
				if (this.items.length > 0) this.items.push(new Spacer(1));
				this.items.push(new AssistantMessageComponent(msg, false, this.mdTheme));
				for (const block of msg.content ?? []) {
					if (block.type !== "toolCall") continue;
					const component = new ToolExecutionComponent(
						block.name,
						block.id,
						block.arguments,
						undefined,
						undefined,
						this.tui,
						this.cwd,
					);
					this.items.push(component);
					this.pendingTools.set(block.id, component);
				}
			} else if (msg.role === "toolResult") {
				const pending = this.pendingTools.get(msg.toolCallId);
				if (!pending) continue;
				pending.updateResult(msg);
				this.pendingTools.delete(msg.toolCallId);
				const itemIndex = this.items.indexOf(pending);
				if (itemIndex >= 0) {
					this.dirtyItem = this.dirtyItem === null
						? itemIndex
						: Math.min(this.dirtyItem, itemIndex);
				}
			}
		}
		if (this.entry.result.turnBudgetExhausted && !this.appendedBudget) {
			this.appendedBudget = true;
			const r = this.entry.result;
			this.items.push(new Spacer(1));
			this.items.push(
				new Text(
					this.theme.fg("warning", `⚠ turn budget exhausted (${r.usage.turns}/${r.turnLimit} turns)`),
					0,
					0,
				),
			);
		}
		if (isFailedResult(this.entry.result) && this.entry.result.errorMessage && !this.appendedError) {
			this.appendedError = true;
			this.items.push(new Text(this.theme.fg("error", `Error: ${this.entry.result.errorMessage}`), 0, 0));
		}
	}

	// ------------------------------------------------------------------
	// Lazy line cache (bottom-anchored; grows upward in chunks)
	// ------------------------------------------------------------------

	/** Width change or first build: drop rendered lines, anchor at the
	 * bottom, and fill ~two windowfuls of items backwards. The view re-opens
	 * bottom-anchored on a width change (everything rewraps). */
	private resetLines(width: number, windowHeight: number): void {
		this.cachedWidth = width;
		this.itemLines = new Array(this.items.length);
		this.spanFrom = this.items.length;
		this.spanLines = [];
		this.followTail = true;
		this.scrollOffset = Number.MAX_SAFE_INTEGER;
		this.fillBottom(windowHeight * 2);
		this.clampScroll(windowHeight);
	}

	/** Render earlier items until ~targetLines more exist; returns the
	 * number of lines prepended. */
	private growUpLines(targetLines: number): number {
		let added = 0;
		while (this.spanFrom > 0 && added < targetLines) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
			added += (this.itemLines[this.spanFrom] as string[]).length;
		}
		this.rebuildSpanLines();
		return added;
	}

	/** Anchor at the bottom: render backwards until ~fillTarget lines exist. */
	private fillBottom(fillTarget: number): void {
		let have = this.spanLines.length;
		while (this.spanFrom > 0 && have < fillTarget) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
			have += (this.itemLines[this.spanFrom] as string[]).length;
		}
		this.rebuildSpanLines();
	}

	/** Render item i at the synced width, width-truncated. */
	private renderItem(i: number): string[] {
		const raw = (this.items[i] as Component).render(this.cachedWidth as number);
		return raw.map((line) => this.widthSafe.truncate(line, this.cachedWidth as number));
	}

	/** Flatten items [spanFrom, items.length) into spanLines (shared string
	 * references; O(span) pointer copies per sync). */
	private rebuildSpanLines(): void {
		const lines: string[] = [];
		for (let i = this.spanFrom; i < this.items.length; i++) {
			lines.push(...(this.itemLines[i] ?? []));
		}
		this.spanLines = lines;
	}

	/** Keep the per-item line cache current: re-render from the earliest
	 * in-place update (dirtyItem), flatten newly appended items, and leave
	 * items above the span unrendered (lazy top). */
	private syncLines(): void {
		const start = this.dirtyItem ?? this.renderedTo;
		for (let i = Math.min(start, this.spanFrom); i < this.items.length; i++) {
			if (this.itemLines[i] === undefined || i >= start) {
				this.itemLines[i] = this.renderItem(i);
			}
		}
		this.renderedTo = this.items.length;
		this.dirtyItem = null;
		this.rebuildSpanLines();
	}

	/** Make sure the span covers the whole visible window: grow upward when
	 * the view reaches above the span start (offset compensated — position
	 * is preserved while older items render). */
	private growToWindow(windowHeight: number): void {
		if (this.spanFrom > 0 && this.scrollOffset < 1) {
			const added = this.growUpLines(windowHeight);
			this.scrollOffset += added;
		}
	}
}
