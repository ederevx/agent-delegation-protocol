/**
 * Subagent registry
 *
 * Owns subagent membership for the session: which children are running,
 * the bounded recent ring for finished ones, and id allocation. It also
 * owns the teardown handles: `finish` strips the child-process handle from
 * a retiring entry so finished entries never pin a proc, and
 * `interruptActive` SIGTERMs every active child (steering interrupt),
 * escalating to SIGKILL after five seconds.
 *
 * The streaming lifecycle of each child belongs to `SubagentRun` (run.ts),
 * which creates its entry, registers it here, and hands it back via
 * `finish` on close. Listeners on an entry are the /subagents live views;
 * the run notifies them, the registry only stores membership.
 */

import type { ChildProcess } from "node:child_process";
import type { SingleResult } from "./types.ts";

export interface RunningSubagent {
	id: number;
	agent: string;
	tier?: string;
	task: string;
	mode: "single" | "parallel-task" | "chain-step";
	// Accumulated assistant text_delta output for the in-flight turn; cleared
	// when the matching assistant message_end arrives. Written by the owning
	// SubagentRun, read by the live views.
	partialText: string;
	// Epoch milliseconds.
	startedAt: number;
	completedAt?: Date;
	proc?: ChildProcess;
	// The live SingleResult; mutated in place as stream events arrive.
	result: SingleResult;
	turnLimit?: number;
	listeners: Set<() => void>;
}

export const MAX_RECENT_SUBAGENTS = 10;

/**
 * SIGTERM the child, escalating to SIGKILL after five seconds if it is still
 * running. `subprocess.killed` is true the moment kill() is called, so the
 * escalation checks the exit code and signal instead.
 */
export function killWithEscalation(proc: ChildProcess): void {
	proc.kill("SIGTERM");
	const timer = setTimeout(() => {
		if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
	}, 5000);
	timer.unref();
}

export class SubagentRegistry {
	private readonly running = new Map<number, RunningSubagent>();
	private readonly recent: RunningSubagent[] = [];
	private nextId = 1;

	allocateId(): number {
		return this.nextId++;
	}

	register(entry: RunningSubagent): void {
		this.running.set(entry.id, entry);
	}

	/**
	 * Close path: retire the entry from `running` into the recent ring (last
	 * MAX_RECENT_SUBAGENTS kept) so a killed or errored worker never leaks a
	 * running slot.
	 */
	finish(entry: RunningSubagent): void {
		this.running.delete(entry.id);
		entry.completedAt = new Date();
		entry.proc = undefined;
		this.recent.push(entry);
		if (this.recent.length > MAX_RECENT_SUBAGENTS)
			this.recent.splice(0, this.recent.length - MAX_RECENT_SUBAGENTS);
	}

	/** Safety net for exits without a close event (spawn threw). */
	drop(entry: RunningSubagent): void {
		this.running.delete(entry.id);
	}

	/** Running entries that still have a live child-process handle. */
	activeWithProc(): RunningSubagent[] {
		return [...this.running.values()].filter((entry) => entry.proc);
	}

	runningEntries(): RunningSubagent[] {
		return [...this.running.values()];
	}

	recentEntries(): readonly RunningSubagent[] {
		return this.recent;
	}

	/** Steering interrupt: SIGTERM every active child (escalating later). */
	interruptActive(): void {
		for (const entry of this.activeWithProc()) {
			if (entry.proc) killWithEscalation(entry.proc);
		}
	}
}
