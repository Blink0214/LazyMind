import test from 'node:test';
import assert from 'node:assert/strict';
import { runPublicationSequence, publicationOutcome, sanitizeEvidence, createPublicationDriver } from './writer_publication.mjs';

const providers = ['feishu', 'notion', 'github', 'wechat', 'obsidian'];
const initial = { artifact_id: 'source', revision: 1, draft_version: 1, markdown: '# Original' };
function driverFor(outcomes = {}) {
  let current = { ...initial };
  let active = 0, peak = 0;
  const calls = [];
  return {
    calls, peak: () => peak,
    ready: async () => true,
    snapshot: async () => ({ ...current }),
    async publish(provider) {
      const request = { artifact_id: current.artifact_id, body: { base_revision: current.revision, base_draft_version: current.draft_version } };
      calls.push(provider); active++; peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 2));
      active--;
      const override = outcomes[provider];
      if (override) return { ...override, request };
      current = { artifact_id: provider, revision: current.revision + 1, draft_version: 1,
        markdown: current.markdown + '\n' + provider };
      return { request, http_status: 200, response: { provider, artifact_id: provider, provider_synced: true, artifact_saved: true } };
    },
    settled: async () => true,
  };
}
const run = (driver, extra = {}) => runPublicationSequence(driver, { providers, sessionId: 'session', initial, ...extra });

test('serial publication consumes previous result and never resets original', async () => {
  const driver = driverFor();
  const result = await run(driver);
  assert.deepEqual(driver.calls, providers);
  assert.equal(driver.peak(), 1);
  assert.equal(result.publications.notion.request.artifact_id, 'feishu');
  assert.equal(result.publications.obsidian.request.artifact_id, 'wechat');
  assert.equal('source_snapshot' in result.publications.obsidian, false);
  assert.equal(result.initial_source.markdown, '# Original');
});
test('explicit pre-write failure continues on unchanged source', async () => {
  const result = await run(driverFor({ notion: { http_status: 403, error_code: 'PROVIDER_CREDENTIALS_UNAVAILABLE' } }));
  assert.equal(result.publications.notion.status, 'failed_before_write');
  assert.equal(result.publications.github.request.artifact_id, 'feishu');
  assert.equal(result.publications.github.status, 'succeeded');
});
test('unknown outcome stops all later clicks without automatic retry', async () => {
  const driver = driverFor({ notion: { http_status: 504 } });
  const result = await run(driver);
  assert.deepEqual(driver.calls, ['feishu', 'notion']);
  assert.equal(result.publications.notion.status, 'outcome_unknown');
  assert.equal(result.publications.obsidian.status, 'not_attempted');
});
test('unconfigured GitHub stays unattempted while later platforms continue', async () => {
  const driver = driverFor();
  const result = await run(driver, { skipProviders: ['github'] });
  assert.deepEqual(driver.calls, ['feishu', 'notion', 'wechat', 'obsidian']);
  assert.equal(result.publications.github.status, 'unavailable');
  assert.equal(result.publications.wechat.request.artifact_id, 'notion');
});
test('success response alone does not permit next click before UI settles', async () => {
  const driver = driverFor(); driver.settled = async () => false;
  const result = await run(driver);
  assert.deepEqual(driver.calls, ['feishu']);
  assert.equal(result.publications.notion.status, 'not_attempted');
});
test('checkpoints preserve progress for each provider including skipped ones', async () => {
  const sizes = [];
  await run(driverFor(), { checkpoint: evidence => sizes.push(Object.keys(evidence.publications).length) });
  assert.equal(sizes[0], 0);
  assert.deepEqual([...new Set(sizes)], [0, 1, 2, 3, 4, 5]);
});
test('unknown errors and partial local persistence cannot count as success', () => {
  assert.equal(publicationOutcome({ http_status: 200, response: { artifact_id: 'a', provider_synced: true, artifact_saved: false } }), 'outcome_unknown');
  assert.equal(publicationOutcome({ http_status: 200, response: { artifact_id: 'a', provider_synced: true, artifact_saved: true } }), 'succeeded');
});
test('evidence redacts credentials and signed URL queries', () => {
  const result = sanitizeEvidence({ authorization: 'secret', markdown: '![x](https://example.test/a.png?token=secret)', nested: { access_token: 'secret' } });
  assert.equal(JSON.stringify(result).includes('secret'), false);
  assert.equal(result.markdown, '![x](https://example.test/a.png)');
});


test('snapshot reads exact selected artifact and converts IR through preview only', async () => {
  const calls = [];
  const slot = { slot_id: 'draft_document', selected: true, artifact_id: 'a', document: { representation: 'ir' } };
  const page = { request: {
    async get(url) {
      calls.push(['GET', url]);
      const payload = url.endsWith('/slots') ? { data: { slots: [slot] } }
        : url.includes('/workflow-artifacts/') ? { result: { artifact_id: 'a', revision: 2,
            draft_version: 3, value: { data: { title: 'Title', blocks: [] } } } }
        : { data: { session: { session_id: 's' } } };
      return { ok: () => true, json: async () => payload };
    },
    async post(url, options) {
      calls.push(['POST', url]);
      assert.equal(options.data.action, 'convert_document');
      assert.equal(options.data.base_draft_version, 3);
      return { ok: () => true, json: async () => ({ data: { content: '# Title' } }) };
    },
  } };
  const driver = createPublicationDriver({ page, panel: {}, baseUrl: 'http://localhost:8090', token: 'test-only', sessionId: 's' });
  const result = await driver.snapshot(Date.now() + 1000);
  assert.equal(result.markdown, '# Title');
  assert.equal(result.artifact_id, 'a');
  assert.equal(result.revision, 2);
  assert.equal(result.draft_version, 3);
  assert.equal(calls.filter(([method]) => method === 'POST').length, 1);
  assert.equal(calls.at(-1)[1].endsWith('/document-actions:preview'), true);
});

test('wrong-provider success receipt stops the sequence', async () => {
  const driver = driverFor({ feishu: { http_status: 200, response: {
    provider: 'notion', artifact_id: 'wrong', provider_synced: true, artifact_saved: true } } });
  const result = await run(driver);
  assert.deepEqual(driver.calls, ['feishu']);
  assert.equal(result.publications.feishu.status, 'outcome_unknown');
});
