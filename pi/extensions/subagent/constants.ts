/**
 * Tunables shared across the subagent extension files.
 *
 * Kept together so the dispatch caps and the parallel output cap stay
 * visible in one place; view-local tunables (e.g. collapsed preview count)
 * live next to their only consumer.
 */

export const MAX_PARALLEL_TASKS = 10;
export const MAX_CONCURRENCY = 10;
export const MAX_PARALLEL_OUTPUT_CAP = 50 * 1024;
export const COLLAPSED_ITEM_COUNT = 10;
