/**
 * ViewerChrome — full-window viewer chrome shared within the /subagents
 * extension.
 *
 * Owns the frame elements the detail viewer adopted from the session
 * history viewer's layout contract: the word-wrapped instruction lines
 * pinned to the TOP of the frame (below the title), the dim border rule
 * below them, and the content-window height arithmetic shared by render,
 * wheel, and page math. Frame contract: title (1 line) + kept instruction
 * lines + border (1 line) + content window (absorbs all slack) emit
 * EXACTLY `rows` lines for every rows ≥ 3; tiny terminals shrink the
 * window to its floor of 1 first, then drop instruction lines from the
 * TAIL (the head is kept, so the first instruction lines stay visible).
 */

import { truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import type { Theme } from "@earendil-works/pi-coding-agent";

export class ViewerChrome {
	private wrappedInstructions: string[] = [];
	private cachedWidth: number | null = null;

	constructor(
		private readonly theme: Theme,
		// Already-styled instruction text: colors are applied once here in
		// the constructor's caller; wrapTextWithAnsi preserves the spans.
		private readonly instructionText: string,
	) {}

	/** Re-wrap the instruction text at `width`; a no-op while unchanged.
	 * Callers run this before any height/kept-line query at that width. */
	layout(width: number): void {
		if (this.cachedWidth === width) return;
		this.wrappedInstructions = wrapTextWithAnsi(
			this.theme.fg("muted", this.instructionText), Math.max(8, width));
		this.cachedWidth = width;
	}

	/** Instruction lines kept at `rows`. They pin to the frame's top:
	 * the budget is rows minus the title, the window's floor of 1, and the
	 * border (rows − 3), so for every rows ≥ 3 the frame is EXACTLY rows
	 * lines with no tail slack. Tiny-terminal priority: the window shrinks
	 * to its floor of 1 first, then instruction lines drop from the TAIL
	 * (the head is kept, so the first instruction lines stay visible);
	 * clipFrame is a rows<3-only final safety. */
	keptInstructions(rows: number): string[] {
		const all = this.wrappedInstructions;
		const budget = Math.max(0, rows - 3);
		if (budget === 0) return [];
		return all.length <= budget ? all : all.slice(0, budget);
	}

	/** Height of the scrollable content window: every row except the title,
	 * the border rule, and the kept instruction lines (floored at 1). Single
	 * source of truth shared by render(), handleInput() page math, and
	 * handleMouse() clamping; derives from the cached wrapped instructions
	 * (1 line assumed while cold), which render() may pass precomputed. */
	contentWindowHeight(rows: number, instructionCount?: number): number {
		const instructions = instructionCount ??
			(this.cachedWidth === null ? 1 : this.keptInstructions(rows).length);
		return Math.max(1, rows - 2 - instructions);
	}

	/** Append the pinned top block after the title: exactly the kept
	 * instruction lines, then the dim border rule (consistent with the
	 * height math above, so the assembled frame totals `rows` lines before
	 * clipFrame). */
	appendTop(out: string[], width: number, rows: number): void {
		for (const line of this.keptInstructions(rows)) {
			out.push(truncateToWidth(line, width));
		}
		out.push(truncateToWidth(this.theme.fg("border", "─".repeat(Math.max(1, width))), width));
	}

	/** rows<3-only final safety: for rows ≥ 3 the layout above yields
	 * EXACTLY `rows` lines, so nothing is ever padded or popped after the
	 * instructions; below that, clip from the tail (instructions first). */
	clipFrame(out: string[], rows: number): string[] {
		while (out.length > rows) out.pop();
		return out;
	}

	/** Drop the cached wrap so the next layout() re-wraps. */
	invalidate(): void {
		this.cachedWidth = null;
		this.wrappedInstructions = [];
	}
}
