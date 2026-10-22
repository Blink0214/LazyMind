import { describe, expect, it } from 'vitest';
import type { WorkflowSession } from '@/modules/chat/store/workflowPanel';
import type { SubAgentTask, TaskArtifactStream } from '@/modules/chat/store/taskCenter';
import { findWriterArtifactStream } from './writerArtifactStream';

function stream(slot: string, streamId: string): TaskArtifactStream {
  return {
    task_id: `${streamId}-task`,
    slot,
    content_type: 'text/markdown',
    stream_id: streamId,
    chunk_index: 2,
    content: 'preview',
    state: 'streaming',
  };
}

function task(taskId: string, status: SubAgentTask['status'], streams: TaskArtifactStream[]): SubAgentTask {
  return {
    task_id: taskId,
    title: '',
    agent_type: 'workflow_step',
    mode: 'auto',
    status,
    progress_pct: 50,
    sources: [],
    artifacts: [],
    artifact_streams: streams,
    execution_log: [],
  };
}

describe('findWriterArtifactStream', () => {
  const stepCases: Array<[string, string]> = [
    ['outline', 'outline_document'],
    ['write_flat_document', 'flat_draft_document'],
    ['write_document', 'draft_document'],
    ['write_document', 'write_document_document'],
    ['revise_document', 'revise_document_document'],
    ['experiment', 'experiment_document'],
    ['experiment', 'experiment_code'],
    ['experiment', 'experiment_results'],
  ];

  it.each(stepCases)('matches the %s step stream to %s', (stepId: string, slotId: string) => {
    const expected = stream(slotId, `${stepId}-stream`);
    const session = {
      steps: [{ step_id: stepId, task_id: `${stepId}-task` }],
    } as WorkflowSession;

    expect(findWriterArtifactStream(
      session,
      stepId,
      slotId,
      [task(`${stepId}-task`, 'running', [expected])],
    )).toBe(expected);
  });

  it('uses only an unambiguous live fallback for the requested slot', () => {
    const expected = stream('draft_document', 'draft-stream');
    const session = { steps: [] } as unknown as WorkflowSession;

    expect(findWriterArtifactStream(
      session,
      'write_document',
      'draft_document',
      [task('draft-task', 'running', [expected])],
    )).toBe(expected);

    expect(findWriterArtifactStream(
      session,
      'write_document',
      'draft_document',
      [
        task('draft-task-1', 'running', [expected]),
        task('draft-task-2', 'pending', [stream('draft_document', 'other-stream')]),
      ],
    )).toBeUndefined();
  });

  it('does not attach Writer previews to unrelated slots', () => {
    expect(findWriterArtifactStream(
      { steps: [] } as unknown as WorkflowSession,
      'prepare',
      'source_document',
      [task('prepare-task', 'running', [stream('source_document', 'source-stream')])],
    )).toBeUndefined();
  });

  it.each(['write_document_document', 'revise_document_document'])(
    'withdraws rejected paper previews for %s, including the live fallback',
    (slotId) => {
      const rejected = { ...stream(slotId, 'rejected'), state: 'aborted' as const };
      const tasks = [task('paper-task', 'running', [rejected])];
      expect(findWriterArtifactStream(
        { steps: [{ step_id: 'paper', task_id: 'paper-task' }] } as WorkflowSession,
        'paper', slotId, tasks,
      )).toBeUndefined();
      expect(findWriterArtifactStream(
        { steps: [] } as unknown as WorkflowSession, 'paper', slotId, tasks,
      )).toBeUndefined();
    },
  );
});
