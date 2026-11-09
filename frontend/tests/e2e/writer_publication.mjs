// Sequential UI publication. Reads/preview conversions use APIs; writes only use clicks.
export function sanitizeEvidence(value) {
  if (Array.isArray(value)) return value.map(sanitizeEvidence);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !['authorization', 'access_token', 'secret'].includes(key.toLowerCase()))
    .map(([key, item]) => [key, sanitizeEvidence(item)]));
  if (typeof value !== 'string') return value;
  return value.replace(/https?:\/\/[^\s<>"\)]+/g, raw => {
    try { const url = new URL(raw);
      if (url.username || /signature=|token=|x-amz-|credential=|sig=/i.test(url.search)) {
        url.username = ''; url.password = ''; url.search = ''; return url.toString();
      }
    } catch { /* Leave non-URL document text intact. */ }
    return raw;
  });
}

export const BEFORE_WRITE = new Set(['DOCUMENT_ACTION_INVALID', 'REVISION_CONFLICT',
  'DRAFT_VERSION_CONFLICT', 'ARTIFACT_IN_USE', 'DOCUMENT_ACTION_UNSUPPORTED',
  'DOCUMENT_PROVIDERS_UNAVAILABLE', 'PROVIDER_CREDENTIALS_UNAVAILABLE',
  'DOCUMENT_CONVERSION_FAILED', 'PUBLICATION_RECOVERY_CLOSED']);

export function publicationOutcome(capture, provider) {
  const result = capture.response;
  if (capture.http_status >= 200 && capture.http_status < 300
      && result?.provider_synced === true && result?.artifact_saved === true && result?.artifact_id
      && (!provider || result.provider === provider)) return 'succeeded';
  if (BEFORE_WRITE.has(capture.error_code)) return 'failed_before_write';
  return 'outcome_unknown';
}

export async function runPublicationSequence(driver, { providers, sessionId, initial, timeoutMs = 180000, skipProviders = [], checkpoint = () => {} }) {
  const evidence = { mode: 'sequential', initial_source: initial, publications: {} };
  let blocked = '';
  checkpoint(evidence);
  for (const [index, provider] of providers.entries()) {
    const entry = { session_id: sessionId, sequence_index: index };
    evidence.publications[provider] = entry;
    if (skipProviders.includes(provider)) { entry.status = 'unavailable'; entry.reason = 'Publication target not configured'; checkpoint(evidence); continue; }
    if (blocked) { entry.status = 'not_attempted'; entry.reason = blocked; checkpoint(evidence); continue; }
    const deadline = Date.now() + timeoutMs;
    try {
      if (!await driver.ready(deadline)) throw new Error('publication_ui_blocked');
      const capture = await driver.publish(provider, deadline, captured => {
        Object.assign(entry, captured); checkpoint(evidence);
      });
      Object.assign(entry, capture);
      if (capture.status === 'unavailable') { checkpoint(evidence); continue; }
      entry.status = publicationOutcome(capture, provider);
      // Never recover by reissuing a write, switching pages or resetting the draft.
      if (entry.status === 'outcome_unknown') blocked = `Previous publication outcome unknown: ${provider}`;
      else if (!await driver.settled(entry, deadline)) blocked = `Publication UI did not settle: ${provider}`;
    } catch {
      entry.status = entry.request ? 'outcome_unknown' : 'blocked';
      entry.reason = 'UI publication or source capture did not complete within its budget';
      blocked = `Unresolved publication state: ${provider}`;
    }
    checkpoint(evidence);
  }
  return evidence;
}

function unwrap(value) {
  for (let i = 0; i < 5 && value && typeof value === 'object'; i++) {
    if ('data' in value) value = value.data;
    else if ('text' in value) value = value.text;
    else break;
  }
  return value;
}

export function createPublicationDriver({ page, panel, baseUrl, token, sessionId }) {
  const base = `${baseUrl}/api/core`;
  const headers = { Authorization: `Bearer ${token}`, 'Workflow-Contract-Version': 'workflow.v1' };
  const budget = deadline => Math.max(1, Math.min(30000, deadline - Date.now()));
  const menu = () => panel.getByRole('button', { name: /^(选择发布平台|Choose publishing platform)$/ });
  const get = async (path, deadline) => {
    const response = await page.request.get(`${base}${path}`, { headers, timeout: budget(deadline) });
    if (!response.ok()) throw new Error('snapshot_read_failed');
    return response.json();
  };
  const session = async deadline => {
    const payload = await get(`/workflow-sessions/${encodeURIComponent(sessionId)}`, deadline);
    const state = payload?.data?.session;
    if (!state || state.session_id !== sessionId) throw new Error('session_mismatch');
    const slots = await get(`/workflow-sessions/${encodeURIComponent(sessionId)}/slots`, deadline);
    return { ...state, slots: slots?.data?.slots || [] };
  };
  const snapshot = async deadline => {
    const state = await session(deadline);
    const selected = state.slots.filter(slot => slot.slot_id === 'draft_document' && slot.selected && slot.artifact_id);
    if (selected.length !== 1) throw new Error('selected_draft_ambiguous');
    const slot = selected[0];
    const payload = await get(`/workflow-artifacts/${encodeURIComponent(slot.artifact_id)}`, deadline);
    const artifact = payload.result;
    if (!artifact || artifact.artifact_id !== slot.artifact_id) throw new Error('artifact_mismatch');
    let value = unwrap(artifact.value);
    if (value && typeof value === 'object' && (value.url || value.path)) {
      let path = value.url || value.path;
      if (path.startsWith('/static-files/')) path = '/api/core' + path;
      const url = new URL(path, baseUrl);
      if (url.origin !== new URL(baseUrl).origin) throw new Error('external_artifact_carrier');
      const response = await page.request.get(url.toString(), { headers, timeout: budget(deadline) });
      if (!response.ok()) throw new Error('artifact_carrier_failed');
      const text = await response.text();
      value = slot.document?.representation === 'ir' ? unwrap(JSON.parse(text)) : text;
    }
    let markdown = value;
    if (typeof markdown !== 'string') {
      const response = await page.request.post(`${base}/workflow-artifacts/${encodeURIComponent(slot.artifact_id)}/document-actions:preview`, {
        headers, timeout: budget(deadline), data: { action: 'convert_document',
          base_revision: artifact.revision, base_draft_version: artifact.draft_version,
          input: { output_format: 'markdown', document: value } },
      });
      if (!response.ok()) throw new Error('snapshot_conversion_failed');
      markdown = (await response.json())?.data?.content;
    }
    if (typeof markdown !== 'string' || !markdown.trim()) throw new Error('empty_source');
    return sanitizeEvidence({ artifact_id: artifact.artifact_id, revision: artifact.revision,
      draft_version: artifact.draft_version, markdown, session: state });
  };
  const ready = async deadline => {
    while (Date.now() < deadline) {
      if (await menu().isVisible().catch(() => false) && await menu().isEnabled()) return true;
      await page.waitForTimeout(250);
    }
    return false;
  };
  return {
    snapshot, ready,
    async settled(entry, deadline) {
      while (Date.now() < deadline) {
        if (await menu().isEnabled().catch(() => false)) {
          if (entry.status !== 'succeeded') return true;
          const state = await session(deadline);
          if (state.slots.some(slot => slot.selected && slot.slot_id === 'draft_document'
              && slot.artifact_id === entry.response.artifact_id
              && slot.revision === entry.response.revision)) return true;
        }
        await page.waitForTimeout(250);
      }
      return false;
    },
    async publish(provider, deadline, onCapture = () => {}) {
      const labels = { feishu: /^(飞书|Feishu|Lark)(\s|$)/i, notion: /^Notion(\s|$)/i,
        github: /^GitHub(\s|$)/i, wechat: /^(微信公众号草稿箱|微信公众号|微信|WeChat)(\s|$)/i, obsidian: /^Obsidian(\s|$)/i };
      await menu().click({ timeout: budget(deadline) });
      const option = page.getByRole('menuitem').filter({ hasText: labels[provider] });
      if (await option.count() !== 1) { await page.keyboard.press('Escape'); return { status: 'unavailable', reason: 'Provider menu item unavailable' }; }
      const label = await option.innerText();
      if (/授权|Authorize|检查|Checking|失败|failed|未启用/i.test(label)) {
        await page.keyboard.press('Escape');
        return { status: 'unavailable', reason: 'Provider connection not ready' };
      }
      // Capture the actual source artifact/version from the UI request before waiting for its response.
      let capturedRequest;
      let capturedResponse;
      const onRequest = request => {
        if (!/\/workflow-artifacts\/[^/]+\/document-actions:execute$/.test(new URL(request.url()).pathname)) return;
        let body; try { body = request.postDataJSON(); } catch { return; }
        if (body?.action !== 'publish_document' || body?.input?.provider !== provider) return;
        capturedRequest = request;
        onCapture(sanitizeEvidence({ request: {
          artifact_id: decodeURIComponent(new URL(request.url()).pathname.split('/').at(-2)), body,
        }, status: 'write_pending' }));
      };
      const onResponse = response => { if (response.request() === capturedRequest) capturedResponse = response; };
      page.on('request', onRequest); page.on('response', onResponse);
      try {
        await option.click({ timeout: budget(deadline) });
        while (!capturedResponse && Date.now() < deadline) {
          await page.waitForTimeout(100);
        }
        const request = capturedRequest ? { artifact_id: decodeURIComponent(new URL(capturedRequest.url()).pathname.split('/').at(-2)),
          body: capturedRequest.postDataJSON() } : undefined;
        if (!capturedResponse) return { request, status: 'outcome_unknown', error_code: 'UI_RESPONSE_TIMEOUT' };
        const envelope = await capturedResponse.json();
        return sanitizeEvidence({ request, response: envelope.data, http_status: capturedResponse.status(),
          error_code: envelope?.data?.code || envelope?.error?.code || '', captured_at: new Date().toISOString() });
      } finally { page.off('request', onRequest); page.off('response', onResponse); }
    },
  };
}
