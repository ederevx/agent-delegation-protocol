/**
 * ConservativeWidth — ANSI-aware line truncation that accounts for
 * Windows Terminal's Extended_Pictographic width model.
 *
 * Windows Terminal renders bare Extended_Pictographic characters (⚠, ↔,
 * ✓, ✗, etc.) two cells wide, while pi's visibleWidth counts them as one.
 * Lines that pass pi's truncateToWidth can therefore physically soft-wrap
 * in WT, shifting the cursor and breaking the frame renderer's relative
 * positioning. This class measures those characters as two cells and
 * truncates to the conservative width.
 */

import { stripTerminalSequences, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

const EXT_PICTOGRAPHIC = /\p{Extended_Pictographic}/u;

export class ConservativeWidth {
	private readonly segmenter = new Intl.Segmenter();

	/** Terminal-column width under the conservative Windows model. */
	measure(line: string): number {
		const stripped = stripTerminalSequences(line);
		let w = 0;
		for (const { segment } of this.segmenter.segment(stripped)) {
			const model = visibleWidth(segment);
			w += model === 1 && EXT_PICTOGRAPHIC.test(segment) ? 2 : model;
		}
		return w;
	}

	/** ANSI-aware truncation that also fits the conservative model: pi-tui's
	 * truncateToWidth first (correct ellipsis handling), then trim by the
	 * measured overflow, bounded rounds since each pass drops ≥1 visible
	 * column. Optional `ellipsis` passes through to truncateToWidth (empty
	 * string disables the ellipsis, matching the settings-row callers). */
	truncate(line: string, width: number, ellipsis?: string): string {
		let out = truncateToWidth(line, width, ellipsis);
		let over = this.measure(out) - width;
		for (let round = 0; over > 0 && round < 4; round++) {
			out = truncateToWidth(out, width - over, ellipsis, false);
			over = this.measure(out) - width;
		}
		return out;
	}
}
