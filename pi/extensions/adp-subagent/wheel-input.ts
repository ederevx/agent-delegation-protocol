/**
 * Raw mouse-wheel support for the detail view in regular TUI mode.
 *
 * Regular mode never enables terminal mouse tracking (pi-tui writes
 * ?1000h/?1006h only in fullscreen), so the view's handleMouse can never
 * fire there and the wheel scrolls the terminal's own scrollback instead —
 * dragging the pinned instruction block away with the content. This
 * bridge enables the minimal tracking set while the detail view is open,
 * decodes wheel sequences from raw stdin, and forwards deltas to the
 * view; on close it disables tracking and unsubscribes exactly once.
 * Mirrors the proven bridge in the pi-history extension.
 */

import type { TUI } from "@earendil-works/pi-tui";

/** SGR (ESC[<b;x;y M|m) and 6-byte X10 (ESC [ M + 3 raw bytes) mouse
 * sequences as [complete-at-start, partial-prefix-at-start] regex pairs. */
const SGR_MOUSE = [/^\x1b\[<(\d+);(\d+);(\d+)[Mm]/, /^\x1b\[<[\d;]*[Mm]?/] as const;
const X10_MOUSE = [/^\x1b\[M[\s\S]{3}/, /^\x1b\[M[\s\S]{0,2}/] as const;

/** A partial-sequence buffer longer than this is malformed, not a sequence. */
const WHEEL_BUFFER_LIMIT = 32;

/** Minimum mouse-tracking set pi-tui fullscreen enables (tui-alt-screen.js)
 * and the matching disable, for the regular-mode raw wheel path. */
const MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h";
const MOUSE_DISABLE = "\x1b[?1006l\x1b[?1000l";

/** The slice of the view the bridge drives (no import cycle with the
 * detail view; it supplies applyWheelDelta). */
interface WheelTarget {
	applyWheelDelta(delta: number): boolean;
}

/** Parses terminal mouse-wheel input; state is only a partial-sequence
 * buffer. Mirrors pi-tui's parseWheelEvent (tui-alt-screen.js): SGR wheel
 * buttons 64 (up) / 65 (down), the X10 fallback, direction bit 0=up / 1=down.
 * Complete non-wheel mouse sequences are consumed silently (delta 0) so they
 * never reach the editor as garbage keys; unrecognized input passes through. */
export class WheelInputDecoder {
	private buffer = "";

	/** Decode every complete mouse sequence at the chunk front (fast
	 * scrolling coalesces several into one read), accumulating wheel deltas;
	 * a partial sequence at the end waits for the next chunk; leftover bytes
	 * never reach the editor as garbage keys once a sequence was consumed. */
	feed(data: string): { delta: number; consume: boolean } {
		this.buffer += data;
		if (this.buffer.length > WHEEL_BUFFER_LIMIT) return this.settle(0, false);
		let delta = 0;
		let consumedAny = false;
		for (;;) {
			const sgr = SGR_MOUSE[0].exec(this.buffer);
			if (sgr) {
				delta += this.wheelDelta(Number.parseInt(sgr[1], 10));
				consumedAny = true;
				this.buffer = this.buffer.slice(sgr[0].length);
				continue;
			}
			const x10 = X10_MOUSE[0].exec(this.buffer);
			if (x10) {
				delta += this.wheelDelta(this.buffer.charCodeAt(3) - 32);
				consumedAny = true;
				this.buffer = this.buffer.slice(x10[0].length);
				continue;
			}
			break;
		}
		// A partial prefix waits for the rest; consume the chunk when it already
		// carried complete sequences so nothing trailing leaks as keystrokes.
		const partial = SGR_MOUSE[1].test(this.buffer) || X10_MOUSE[1].test(this.buffer);
		if (partial && this.buffer.length > 0) return { delta, consume: consumedAny };
		return this.settle(delta, consumedAny);
	}

	/** pi-tui convention: bit 6 wheel button, bits 0/1 direction (0=up→−1). */
	private wheelDelta(button: number): number {
		if ((button & 64) === 0) return 0;
		return (button & 3) === 0 ? -1 : (button & 3) === 1 ? 1 : 0;
	}

	/** Clear the buffer and report a decision. */
	private settle(delta: number, consume: boolean): { delta: number; consume: boolean } {
		this.buffer = "";
		return { delta, consume };
	}
}

/** Minimal ui surface the bridge needs (avoids depending on the full
 * ExtensionUIContext type in tests). */
interface TerminalUi {
	onTerminalInput(handler: (data: string) => { consume?: boolean } | undefined): () => void;
}

/** Raw-input wheel path for regular (non-fullscreen) TUI mode. attach() is a
 * no-op in fullscreen (handleMouse stays the path); detach() runs exactly
 * once, whether the view closes via Escape, replacement, or an error. */
export class DetailViewWheelBridge {
	private readonly decoder = new WheelInputDecoder();
	private tui: TUI | null = null;
	private unsubscribe: (() => void) | null = null;
	private detached = false;

	constructor(private readonly ui: TerminalUi) {}

	/** Enable tracking and register the raw listener; no-op in fullscreen. */
	attach(tui: TUI, target: WheelTarget): void {
		if (tui.mode !== "regular") return;
		this.tui = tui;
		tui.terminal.write(MOUSE_ENABLE);
		this.unsubscribe = this.ui.onTerminalInput((data) => {
			// Consume only mouse sequences so nothing leaks to the editor.
			const result = this.decoder.feed(data);
			if (result.consume && result.delta !== 0) target.applyWheelDelta(result.delta);
			return result.consume ? { consume: true } : undefined;
		});
	}

	/** Idempotent: disable and unregister exactly once. */
	detach(): void {
		if (this.detached) return;
		this.detached = true;
		this.unsubscribe?.();
		if (this.tui) this.tui.terminal.write(MOUSE_DISABLE);
	}
}
