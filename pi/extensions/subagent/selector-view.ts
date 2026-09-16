/**
 * SubagentSelectorView — /subagents entry list
 *
 * Settings-styled list mounted exactly like /settings: a non-overlay
 * `ctx.ui.custom()` component, so it integrates with the TUI (editor dock)
 * instead of opening a floating window. Border / body / border with the
 * settings row layout (→ cursor, aligned label column, muted value column,
 * dim description of the selected row, dim hint footer). Entries group
 * under Active/Inactive headers, active first; windowing over the flat
 * entry list mirrors SettingsList.getVisibleRange. Mouse+keyboard.
 *
 * The view owns its selection and row-map state only; entries come from the
 * registry snapshot taken at construction (matching the old behavior) and
 * every close path hands control back via `done`.
 */

import {
	DynamicBorder,
	getSettingsListTheme,
	type Theme,
} from "@earendil-works/pi-coding-agent";
import {
	Container,
	matchesKey,
	Text,
	truncateToWidth,
	type TuiMouseEventResult,
	visibleWidth,
	wrapTextWithAnsi,
	type Component,
	type TUI,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import type { SubagentRegistry, RunningSubagent } from "./registry.ts";
import { statusOf } from "./format.ts";

interface SelectorRow {
	entry: RunningSubagent;
	groupTitle: string;
	groupCount: number;
}

export class SubagentSelectorView {
	private readonly tui: TUI;
	private readonly theme: Theme;
	private readonly done: (entry: RunningSubagent | null) => void;
	private readonly st = getSettingsListTheme();
	private readonly rows: SelectorRow[];
	private readonly maxLabelWidth: number;
	private readonly maxVisible: number;
	private readonly border: DynamicBorder;
	private readonly empty: Container;
	private selected = 0;
	// y offsets of the entry rows from the last render, for mouse;
	// +1 because the component's top border shifts event.y down.
	private rowMap: { y: number; index: number }[] = [];

	constructor(registry: SubagentRegistry, tui: TUI, theme: Theme, done: (entry: RunningSubagent | null) => void) {
		this.tui = tui;
		this.theme = theme;
		this.done = done;
		const groups = [
			{ title: "Active", entries: registry.runningEntries() },
			{ title: "Inactive", entries: [...registry.recentEntries()] },
		].filter((g) => g.entries.length > 0);
		this.rows = groups.flatMap((g) =>
			g.entries.map((entry) => ({
				entry,
				groupTitle: g.title,
				groupCount: g.entries.length,
			})),
		);
		this.maxLabelWidth =
			this.rows.length > 0
				? Math.min(36, Math.max(...this.rows.map((r) => visibleWidth(this.labelOf(r.entry)))))
				: 0;
		this.maxVisible = Math.min(this.rows.length, 10);
		this.border = new DynamicBorder((s: string) => theme.fg("border", s));
		this.empty = new Container();
		this.empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
		this.empty.addChild(new Text(this.st.hint("  No subagents spawned this session."), 0, 0));
		this.empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
	}

	// ------------------------------------------------------------------
	// Component surface (ctx.ui.custom non-overlay: editor-dock mount)
	// ------------------------------------------------------------------

	render(width: number): string[] {
		if (this.rows.length === 0) return this.empty.render(width);
		return [...this.border.render(width), ...this.renderBody(width), ...this.border.render(width)];
	}

	invalidate(): void {}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.done(null);
			return;
		}
		if (this.rows.length === 0) return;
		if (matchesKey(data, "up")) this.selected = (this.selected - 1 + this.rows.length) % this.rows.length;
		else if (matchesKey(data, "down")) this.selected = (this.selected + 1) % this.rows.length;
		else if (matchesKey(data, "enter") || data === " ") {
			this.done(this.rows[this.selected].entry);
			return;
		} else return;
		this.tui.requestRender();
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult {
		if (this.rows.length === 0) return { handled: false };
		if (event.type === "wheel") {
			// pi-tui emits a negative wheelDelta on wheel-up.
			this.selected =
				(this.selected + (event.wheelDelta && event.wheelDelta < 0 ? -1 : 1) + this.rows.length) %
				this.rows.length;
		} else if (event.type === "press" || event.type === "click") {
			const row = this.rowMap.find((r) => r.y === event.y);
			if (!row) return { handled: false };
			this.selected = row.index;
			if (event.type === "click") {
				this.done(this.rows[this.selected].entry);
				return { handled: true };
			}
		} else return { handled: false };
		this.tui.requestRender();
		return { handled: true };
	}

	// ------------------------------------------------------------------
	// Rendering
	// ------------------------------------------------------------------

	private labelOf(entry: RunningSubagent): string {
		return `#${entry.id} ${entry.agent}${entry.tier ? ` (${entry.tier})` : ""}`;
	}

	private renderBody(width: number): string[] {
		const startIndex = Math.max(
			0,
			Math.min(this.selected - Math.floor(this.maxVisible / 2), this.rows.length - this.maxVisible),
		);
		const endIndex = Math.min(startIndex + this.maxVisible, this.rows.length);
		const lines: string[] = [];
		this.rowMap = [];
		let prevGroup = "";
		for (let i = startIndex; i < endIndex; i++) {
			const row = this.rows[i];
			if (row.groupTitle !== prevGroup) {
				if (prevGroup !== "") lines.push("");
				lines.push(
					truncateToWidth(
						this.theme.fg("accent", this.theme.bold(`${row.groupTitle} (${row.groupCount})`)),
						width,
					),
				);
				prevGroup = row.groupTitle;
			}
			const isSelected = i === this.selected;
			const prefix = isSelected ? this.st.cursor : "  ";
			const label = this.labelOf(row.entry);
			const labelPadded = label + " ".repeat(Math.max(0, this.maxLabelWidth - visibleWidth(label)));
			const separator = "  ";
			const usedWidth = visibleWidth(prefix) + this.maxLabelWidth + visibleWidth(separator);
			const valueMaxWidth = Math.max(0, width - usedWidth - 2);
			const valueText = this.st.value(
				truncateToWidth(statusOf(row.entry), valueMaxWidth, ""),
				isSelected,
			);
			lines.push(
				truncateToWidth(prefix + this.st.label(labelPadded, isSelected) + separator + valueText, width),
			);
			this.rowMap.push({ y: lines.length, index: i });
		}
		if (startIndex > 0 || endIndex < this.rows.length) {
			lines.push(this.st.hint(truncateToWidth(`  (${this.selected + 1}/${this.rows.length})`, width - 2, "")));
		}
		const taskFlat = this.rows[this.selected].entry.task.replace(/\s+/g, " ").trim();
		if (taskFlat) {
			lines.push("");
			for (const line of wrapTextWithAnsi(taskFlat, Math.max(8, width - 4))) {
				lines.push(this.st.description(`  ${line}`));
			}
		}
		lines.push("");
		lines.push(
			this.st.hint(truncateToWidth("  ↑↓ navigate · Enter/Space to open · Esc to cancel", width, "")),
		);
		return lines;
	}
}
