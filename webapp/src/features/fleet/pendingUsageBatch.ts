import type { PendingUsageAssignmentGroup, PendingUsageMapping } from '@/api/types';

/**
 * The server accepts at most 10,000 event ids in one batch request, counted
 * across every provider in the batch. A larger selection is split into
 * requests of this size, so a backlog of any size can be drained in one
 * action instead of failing the whole request.
 */
export const MAX_BATCH_EVENT_IDS = 10_000;

/**
 * Split a selection into the requests the server will accept.
 *
 * The server counts the event ids across the whole batch, so the split is by
 * total ids rather than per assignment. Each chunk is filled to the limit, so
 * an assignment can continue into the next chunk; an assignment without event
 * ids carries nothing to assign and is left out.
 */
export function chunkBatchAssignments(
  assignments: PendingUsageAssignmentGroup[],
  maxEventIds: number = MAX_BATCH_EVENT_IDS,
): PendingUsageAssignmentGroup[][] {
  if (maxEventIds < 1) throw new Error('A batch chunk must allow at least one event');
  const chunks: PendingUsageAssignmentGroup[][] = [];
  let current: PendingUsageAssignmentGroup[] = [];
  let currentCount = 0;
  for (const assignment of assignments) {
    let offset = 0;
    while (offset < assignment.event_ids.length) {
      const room = maxEventIds - currentCount;
      if (room <= 0) {
        chunks.push(current);
        current = [];
        currentCount = 0;
        continue;
      }
      const take = assignment.event_ids.slice(offset, offset + room);
      offset += take.length;
      current.push({ ...assignment, event_ids: take });
      currentCount += take.length;
    }
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}

export interface BatchAssignmentOutcome {
  /** The events the server confirmed. */
  assigned: number;
  /** The event ids the server confirmed. */
  assignedEventIds: number[];
  /** The event ids that are still unassigned. */
  unassignedEventIds: number[];
  /** The provider/host mappings the server wrote, across every chunk. */
  mappings: PendingUsageMapping[];
  /** How many requests the selection needed. */
  chunks: number;
  /** The chunk that failed, with the error it failed with. */
  error?: unknown;
}

/**
 * Assign a selection in as many requests as it needs.
 *
 * The chunks go one after another: the server deletes the pending rows it
 * assigned, so two requests in flight at once could see each other's rows
 * disappear. The first failure stops the run and leaves the remaining
 * events unassigned — the caller keeps them selected for a retry.
 */
export async function assignBatchInChunks(
  assignments: PendingUsageAssignmentGroup[],
  assignChunk: (chunk: PendingUsageAssignmentGroup[]) => Promise<{ assigned: number; mappings: PendingUsageMapping[] }>,
  maxEventIds: number = MAX_BATCH_EVENT_IDS,
): Promise<BatchAssignmentOutcome> {
  const chunks = chunkBatchAssignments(assignments, maxEventIds);
  const assignedEventIds: number[] = [];
  const mappings: PendingUsageMapping[] = [];
  let assigned = 0;
  for (const chunk of chunks) {
    try {
      const result = await assignChunk(chunk);
      assigned += result.assigned;
      mappings.push(...result.mappings);
      for (const group of chunk) assignedEventIds.push(...group.event_ids);
    } catch (error) {
      const confirmed = new Set(assignedEventIds);
      return {
        assigned,
        assignedEventIds,
        unassignedEventIds: assignments
          .flatMap((item) => item.event_ids)
          .filter((id) => !confirmed.has(id)),
        mappings,
        chunks: chunks.length,
        error,
      };
    }
  }
  return {
    assigned,
    assignedEventIds,
    unassignedEventIds: [],
    mappings,
    chunks: chunks.length,
  };
}
