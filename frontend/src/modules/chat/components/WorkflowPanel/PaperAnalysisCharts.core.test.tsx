import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { axiosInstance } from '@/components/request';
import type { SlotRevision, WorkflowSession } from '@/modules/chat/store/workflowPanel';
import { PaperAnalysisCharts, paperChartDescriptions } from './PaperAnalysisCharts';
import { PaperAnalysisProgress } from './PaperAnalysisProgress';

vi.mock('@/components/auth', () => ({ AgentAppsAuth: { getAuthHeaders: () => ({}) } }));
vi.mock('@/components/request', () => ({ BASE_URL: '', axiosInstance: { get: vi.fn() } }));
vi.mock('@/modules/chat/utils/sse', () => ({ Method: { GET: 'GET' }, SSE: class { close() {} } }));
vi.mock('./SlotComponents', async () => ({ SlotDownloadContext: (await import('react')).createContext(true) }));
vi.mock('@/modules/knowledge/utils/imageUrl', () => ({
  resolveMarkdownImageUrlAsync: async (source: string) => source,
}));
afterEach(cleanup);

function revision(slot: string, artifact_value: unknown): SlotRevision {
  return { slot_id: slot, slot, step_id: 'data_analysis', revision: 1, selected: true,
    created_at: '2026-01-01T00:00:00Z', artifact_value };
}
const files = ['svg', 'png', 'pdf'].map(format => revision('analysis_chart', {
  filename: `分析图表-means.${format}`, path: `/static-files/means.${format}`,
}));
const metadata = [revision('data_analysis_pool', { data: {
  schema: 'paper-material-pool.v1', current: { charts: { id: 'charts', version: 1 } },
  materials: [{ id: 'charts', version: 1, kind: 'charts', content: [
    { id: 'means', title: 'Mean scores', caption: 'Synthetic group means.' },
  ] }],
} })];

it('groups exports into one figure with its external title, caption and download links', async () => {
  render(<PaperAnalysisCharts revisions={files} metadataSlots={metadata} />);
  const image = await screen.findByRole('img', { name: 'Mean scores' });
  expect(image).toHaveAttribute('src', '/static-files/means.png');
  expect(screen.getByRole('heading', { name: 'Mean scores' })).toBeInTheDocument();
  expect(screen.getByText('Synthetic group means.')).toBeInTheDocument();
  expect(document.querySelectorAll('figure')).toHaveLength(1);
  for (const format of ['SVG', 'PNG', 'PDF']) {
    expect(screen.getByRole('link', { name: `下载 Mean scores ${format}` }))
      .toHaveAttribute('href', `/static-files/means.${format.toLowerCase()}?download=1`);
  }
});

it('falls back to SVG and keeps downloads accessible when previews fail', async () => {
  render(<PaperAnalysisCharts revisions={files} metadataSlots={metadata} />);
  const image = await screen.findByRole('img', { name: 'Mean scores' });
  fireEvent.error(image);
  await waitFor(() => expect(image).toHaveAttribute('src', '/static-files/means.svg'));
  fireEvent.error(image);
  expect(await screen.findByText('图表预览暂时无法加载，仍可下载原文件。')).toBeInTheDocument();
  expect(screen.getAllByRole('link')).toHaveLength(3);
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  expect(await screen.findByRole('img', { name: 'Mean scores' }))
    .toHaveAttribute('src', '/static-files/means.png');
});


it.each(['paper-material-pool.v1', 'paper-material-pool.v2'])('reads figure metadata from %s', schema => {
  const value = { ...metadata[0].artifact_value.data, schema };
  expect(paperChartDescriptions({ data: value })).toEqual([
    { id: 'means', title: 'Mean scores', caption: 'Synthetic group means.' },
  ]);
  expect(paperChartDescriptions({ ...value, schema: 'unknown' })).toEqual([]);
});

it('shows backend token estimates for output and reasoning without changing their text', async () => {
  const time = '2026-01-01T00:00:00Z';
  const body = 'The observed mean is 2.', reasoning = 'Compare the conditions.';
  vi.mocked(axiosInstance.get).mockResolvedValueOnce({ data: {
    task_id: 'task', execution_id: 'execution', revision: 1, status: 'succeeded',
    timing: { started_at: time, finished_at: time, measured_at: time },
    process_steps: [{ step_id: 'f4-analysis', title: '生成分析', order: 0, status: 'succeeded',
      started_at: time, finished_at: time, elapsed_ms: 0,
      output: { text: body, estimated_tokens: 1234, format: 'text', mode: 'complete' },
      reasoning: { text: reasoning, estimated_tokens: 21, format: 'text', mode: 'complete' },
    }],
  } });
  const session: WorkflowSession = { session_id: 'session', conversation_id: 'conversation',
    workflow_id: 'paper-writing', workflow_mode: 'auto', status: 'completed', current_step_id: 'data_analysis',
    created_at: time, updated_at: time, steps: [{ id: 'step', session_id: 'session', step_id: 'data_analysis',
      task_id: 'task', attempt: 1, status: 'succeeded', created_at: time, updated_at: time }],
  };
  render(<PaperAnalysisProgress session={session} />);
  expect(await screen.findByText(`TEXT · 约 ${(1234).toLocaleString()} tokens`)).toBeInTheDocument();
  expect(screen.getByText('TEXT · 约 21 tokens')).toBeInTheDocument();
  expect(screen.queryByText(/字符/)).not.toBeInTheDocument();
  expect(screen.getByText(body)).toBeInTheDocument();
  expect(screen.getByText(reasoning)).toBeInTheDocument();
});
