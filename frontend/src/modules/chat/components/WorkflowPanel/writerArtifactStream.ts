import type { WorkflowSession } from '@/modules/chat/store/workflowPanel';
import type { SubAgentTask, TaskArtifactStream } from '@/modules/chat/store/taskCenter';

const PAPER_STREAM_SLOT_IDS = new Set(['write_outline_document', 'write_document_document', 'revise_document_document']);

const WRITER_STREAM_SLOT_IDS = new Set([
  'outline_document',
  'flat_draft_document',
  'draft_document',
  ...PAPER_STREAM_SLOT_IDS,
]);

export function findWriterArtifactStream(
  session: WorkflowSession,
  stepId: string | undefined,
  slotId: string,
  tasks: SubAgentTask[],
): TaskArtifactStream | undefined {
  if (!stepId) return undefined;

  const findStream = (task: SubAgentTask | undefined) => task?.artifact_streams
    ?.slice()
    .reverse()
    .find((candidate) => candidate.slot === slotId && candidate.content_type === 'text/markdown');

  const steps = (session.steps ?? [])
    .filter((step) => step.step_id === stepId)
    .slice()
    .reverse();
  for (const step of steps) {
    const stream = findStream(tasks.find((task) => task.task_id === step.task_id));
    if (stream) {
      // A rejected paper preview must not replace a previously published manuscript.
      if (PAPER_STREAM_SLOT_IDS.has(slotId) && stream.state === 'aborted') return undefined;
      return stream;
    }
  }

  // The session projection can briefly lag task_created. Only use the fallback
  // when one live stream for this exact slot is unambiguous.
  // Other plugins use exact task ownership; never borrow an unrelated task's preview.
  if (!WRITER_STREAM_SLOT_IDS.has(slotId)) return undefined;
  const liveStreams = tasks
    .filter((task) => task.status === 'pending' || task.status === 'running')
    .map(findStream)
    .filter((stream): stream is TaskArtifactStream => Boolean(
      stream && !(PAPER_STREAM_SLOT_IDS.has(slotId) && stream.state === 'aborted'),
    ));
  return liveStreams.length === 1 ? liveStreams[0] : undefined;
}
