#!/usr/bin/env node
// Writer UI bridge: invoked by writer-test/runners/func_run.py to drive the
// real chat UI through Playwright. The Python side spawns this as a
// subprocess, parses the final JSON line from stdout, and continues with
// trace-based analysis.
//
// Behavior:
//   1. install auth (Bearer token from --token)
//   2. open /agent/chat/home
//   3. choose the @AI Writer mention
//   4. upload --attachment if provided
//   5. send --prompt-file content
//   6. wait for .workflow-panel--completed on the panel
//   7. emit a single JSON line with conversation_id / session_id /
//      task_id / writer_status / final_text to stdout
//
// This is intentionally minimal: it drives the chat UI through Playwright for
// the writer-test functional stack.

import { chromium } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

// Keep implementation-detail selectors at one boundary. The bridge exposes
// semantic evidence to Python; no runner/assertion should know these classes.
const UI = Object.freeze({
  attachmentChip: ".chat-files-item",
  sendButton: ".send-button:visible",
  workflowPanel: ".workflow-panel[data-session-id]",
  visibleWorkflowPanel: ".workflow-panel[data-session-id]:visible",
  mentionEditor: '.chat-mention-editor[contenteditable="true"]:visible',
  mentionOption: ".chat-mention-group:not(.is-conversation) .chat-mention-option",
  mentionOptionName: ".chat-mention-option-name",
  mentionChip: ".chat-mention-chip",
  hiddenUpload: '.chat-add-resource-hidden-upload input[type="file"]',
  tabs: ".workflow-panel__tabs > .workflow-panel__tab",
  tabContent: ".workflow-panel__tab-content:visible",
  stepStatus: ".workflow-panel__step-status",
  proceed: ".workflow-panel__action-btn--primary",
  finalDocuments: [
    ".writer-ir__document:visible",
    ".mdxeditor-root-contenteditable:visible",
    ".writer-artifact__markdown:visible",
  ],
  renderedImages:
    ".writer-artifact__markdown img:visible, .mdxeditor img:visible, "
    + "img[data-writer-image='true']:visible",
});

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      out[a.slice(2).replace(/-/g, "_")] = argv[i + 1];
      i++;
    }
  }
  return out;
}

function expandEnv(name) {
  // minimal ${VAR} expansion so the Python side can pass through cases
  // whose prompt embeds env-supplied values like ${LAZYMIND_USER_ID}.
  return name.replace(/\$\{([A-Z_][A-Z0-9_]*)\}/g, (_, k) => process.env[k] ?? "");
}

// ---------------------------------------------------------------------------
// In-browser SSE capture
//
// The frontend consumes task artifact streams through an XHR-based SSE client
// (modules/chat/utils/sse.ts), so native EventSource CDP events never fire.
// We patch XMLHttpRequest before the app loads and record every incremental
// responseText chunk for the task / workflow event channels. After the run,
// current-protocol task events are returned to Python's canonical normalizer.
// ---------------------------------------------------------------------------

function parseSseFrames(text) {
  const frames = [];
  for (const raw of String(text || "").split(/\r?\n\r?\n/)) {
    if (!raw.trim()) continue;
    const frame = { data: [] };
    for (const line of raw.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      const idx = line.indexOf(":");
      if (idx < 0) continue;
      const field = line.slice(0, idx).trim();
      const value = line.slice(idx + 1).trimStart();
      if (field === "data") frame.data.push(value);
    }
    if (frame.data.length) {
      frame.payload = frame.data.join("\n");
      frames.push(frame);
    }
  }
  return frames;
}

export function buildSseCapture(capture) {
  const byUrl = new Map();
  for (const entry of capture || []) {
    // Reconnects must not splice a partial JSON frame into a new connection.
    const key = entry.request_id || entry.url;
    const list = byUrl.get(key) || [];
    list.push(entry);
    byUrl.set(key, list);
  }
  const taskEvents = [];
  let totalArtifactEvents = 0;
  let taskId = "";
  const conversationIds = new Set();
  let parseErrors = 0;
  let replayedEvents = 0;
  const seenEvents = new Map();
  for (const entries of byUrl.values()) {
    const url = entries[0].url;
    entries.sort((a, b) => a.at - b.at);
    const text = entries.map((e) => e.chunk).join("");
    const taskMatch = url.match(/\/api\/core\/tasks\/([^/?]+):stream/);
    if (taskMatch) taskId = taskMatch[1];
    for (const frame of parseSseFrames(text)) {
      let payload = {};
      try {
        payload = JSON.parse(frame.payload);
      } catch (_) {
        parseErrors += 1;
        continue;
      }
      if (url.includes("conversations:chat")) {
        const result = payload && typeof payload.result === "object"
          ? payload.result
          : payload;
        if (result && result.conversation_id) {
          conversationIds.add(String(result.conversation_id));
        }
      }
      const eventType = String(payload.type || "");
      if (!eventType.startsWith("artifact_stream")) continue;
      // Task reconnects may replay an identical indexed event. Keep conflicting
      // payloads visible, but do not count an identical replay as a second start.
      if (payload.stream_id && Number.isInteger(payload.chunk_index)) {
        const key = `${payload.stream_id}:${payload.chunk_index}`;
        const encoded = JSON.stringify(payload);
        if (seenEvents.get(key) === encoded) { replayedEvents += 1; continue; }
        seenEvents.set(key, encoded);
      }
      totalArtifactEvents += 1;
      taskEvents.push(payload);
    }
  }
  return {
    task_id: taskId,
    conversation_ids: [...conversationIds],
    total_artifact_events: totalArtifactEvents,
    events: taskEvents,
    diagnostics: { connections: byUrl.size, parse_errors: parseErrors,
      replayed_events: replayedEvents },
  };
}

export function latestDraftStreamState(capture) {
  const states = new Map();
  let latestStreamId = "";
  for (const event of capture?.events || []) {
    const type = String(event?.type || "");
    const streamId = String(event?.stream_id || "");
    if (!streamId) continue;
    if (type === "artifact_stream_start"
        && event.slot === "draft_document"
        && event.content_type === "text/markdown") {
      latestStreamId = streamId;
      states.set(streamId, { started: true, settled: false, terminal: "" });
      continue;
    }
    if (!states.has(streamId)) continue;
    if (type === "artifact_stream_end" || type === "artifact_stream_abort") {
      states.set(streamId, {
        started: true,
        settled: true,
        terminal: type === "artifact_stream_abort" ? "abort" : "end",
      });
    }
  }
  return states.get(latestStreamId)
    || { started: false, settled: false, terminal: "" };
}

export async function waitForCaptureSettlement(readCapture, timeoutMs = 30_000,
                                                quietMs = 500) {
  const deadline = Date.now() + timeoutMs;
  const startedAt = Date.now();
  let changedAt = startedAt;
  let previousSignature = "";
  while (true) {
    const capture = await readCapture();
    const signature = JSON.stringify([capture.total_artifact_events,
      capture.events?.at(-1)]);
    if (signature !== previousSignature) changedAt = Date.now();
    previousSignature = signature;
    const state = latestDraftStreamState(capture);
    const settled = state.settled && Date.now() - changedAt >= quietMs;
    if (settled || Date.now() >= deadline) {
      capture.diagnostics = { ...capture.diagnostics,
        drain_wait_ms: Date.now() - startedAt,
        drain_reason: settled ? "terminal_and_quiet" : "timeout",
        draft_terminal: state.terminal || null,
      };
      return capture;
    }
    await new Promise((resolve) => setTimeout(resolve,
      Math.min(100, Math.max(1, deadline - Date.now()))));
  }
}

// Subscribe when the UI opens a task stream, not after workflow completion.
// This independent read-only observer survives an early frontend XHR abort.
// Its evidence must remain separate: never repair the browser capture with it.
export function createTaskStreamObserver(baseUrl, token) {
  const connections = new Map();
  const records = [];
  const observe = (requestUrl) => {
    const url = new URL(requestUrl, baseUrl);
    if (url.origin !== new URL(baseUrl).origin
        || !/^\/api\/core\/tasks\/[^/]+:stream$/.test(url.pathname)) return;
    const target = url.origin + url.pathname; // Never persist query credentials.
    if (connections.has(target)) return;
    const controller = new AbortController();
    const entry = { controller, status: "connecting", started_at: Date.now() };
    connections.set(target, entry);
    entry.done = (async () => {
      try {
        const response = await fetch(target, {
          headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
          signal: controller.signal,
          redirect: "error",
        });
        entry.http_status = response.status;
        if (!response.ok || !response.body) { entry.status = "http_error"; return; }
        entry.status = "streaming";
        const decoder = new TextDecoder();
        const reader = response.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          const chunk = done ? decoder.decode() : decoder.decode(value, { stream: true });
          if (chunk) records.push({ url: target, request_id: target,
            at: Date.now(), chunk });
          if (done) break;
        }
        entry.status = "closed";
      } catch (error) {
        entry.status = controller.signal.aborted ? "observer_stopped" : "network_error";
        entry.error_type = error?.name || "Error";
      } finally { entry.closed_at = Date.now(); }
    })();
  };
  return {
    observe,
    snapshot: () => ({ ...buildSseCapture(records),
      transport: [...connections].map(([url, entry]) => ({
        path: new URL(url).pathname, status: entry.status,
        started_at: entry.started_at, closed_at: entry.closed_at,
        http_status: entry.http_status, error_type: entry.error_type,
      })),
    }),
    close: async () => {
      for (const entry of connections.values()) entry.controller.abort();
      await Promise.all([...connections.values()].map((entry) => entry.done));
    },
  };
}

async function waitForAttachmentUploaded(page, fileName) {
  // The chat composer disables the send button while files are uploading
  // (isUploading) and removes the file chip when an upload fails. Wait for the
  // chip to mount, then observe the disabled -> enabled transition so the
  // message is never sent with a still-pending/empty attachment record.
  const chip = page.locator(UI.attachmentChip, { hasText: fileName }).first();
  await chip.waitFor({ state: "visible", timeout: 15_000 });
  const send = page.locator(UI.sendButton).first();
  await send.waitFor({ state: "visible", timeout: 15_000 });
  const deadline = Date.now() + 120_000;
  const graceStartedAt = Date.now();
  let sawDisabled = false;
  while (Date.now() < deadline) {
    const cls = (await send.getAttribute("class").catch(() => "")) || "";
    const disabled = /(^|\s)disabled(\s|$)/.test(cls);
    if (disabled) {
      sawDisabled = true;
    } else if (sawDisabled) {
      break; // disabled -> enabled: upload finished.
    } else if (Date.now() - graceStartedAt > 5_000) {
      break; // Never observed the disabled state; treat the upload as complete.
    }
    await page.waitForTimeout(250);
  }
  if (sawDisabled && Date.now() >= deadline) {
    throw new Error(`Timed out waiting for attachment upload to finish: ${fileName}`);
  }
  // A failed upload removes the chip; a vanished chip means the file never
  // attached, so fail loudly instead of sending an empty attachment record.
  await chip.waitFor({ state: "visible", timeout: 5_000 });
}

async function waitForNewWorkflowPanel(page, existingSessionIds) {
  // A fresh send creates a new workflow session, so the panel for THIS run has
  // a data-session-id that was not present before the send. Require that panel
  // so a stale panel from a previous conversation can never be associated with
  // this run.
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const panels = page.locator(UI.visibleWorkflowPanel);
    const count = await panels.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      const sid = await panels.nth(i).getAttribute("data-session-id").catch(() => "");
      if (sid && !existingSessionIds.includes(sid)) {
        return panels.nth(i);
      }
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    "timed out waiting for a new workflow panel with a previously unseen session id",
  );
}

function conversationIdFromUrl(url) {
  const current = String(url || "").match(
    /\/agent\/chat\/home\/([^/?#]+)\/?(?:[?#].*)?$/,
  );
  const encoded = current?.[1] || "";
  if (!encoded) return "";
  try {
    return decodeURIComponent(encoded);
  } catch (_) {
    return encoded;
  }
}

export function resolveConversationId(chatConversationIds, routeCid, storageCid) {
  const chatIds = [...new Set((chatConversationIds || []).filter(Boolean).map(String))];
  if (chatIds.length === 0) {
    throw new Error(
      "conversation id missing from this run's conversations:chat SSE response",
    );
  }
  if (chatIds.length > 1) {
    throw new Error(
      `multiple conversation ids observed in this chat request: ${JSON.stringify(chatIds)}`,
    );
  }
  const chatCid = chatIds[0];
  const observedIds = [chatCid, routeCid, storageCid].filter(Boolean);
  if (new Set(observedIds).size > 1) {
    throw new Error(
      "conversation id mismatch between chat SSE, route and session storage: "
      + JSON.stringify({ chat_sse: chatCid, route: routeCid, session_storage: storageCid }),
    );
  }
  return {
    conversationId: chatCid,
    sources: {
      chat_sse: chatCid,
      route: routeCid || "",
      session_storage: storageCid || "",
    },
  };
}

export async function readVisibleFinalDocument(page, panel, timeoutMs = 20_000) {
  // Do not use the tab container's text: it also contains controls, status
  // labels and slot chrome, which can make an empty artifact look non-empty.
  // Prefer the concrete document roots used by IR, editable Markdown and the
  // read-only Markdown fallback, in that order.
  const content = panel.locator(UI.tabContent).last();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await content.count().catch(() => 0)) {
      for (const selector of UI.finalDocuments) {
        const document = content.locator(selector).first();
        if (!(await document.isVisible().catch(() => false))) continue;
        const text = ((await document.innerText().catch(() => "")) || "")
          .replace(/[\u200B-\u200D\uFEFF]/g, "")
          .trim();
        if (text) return { visible: true, text, selector };
      }
    }
    await page.waitForTimeout(250);
  }
  return { visible: false, text: "", selector: "" };
}

export async function openFinalDocumentTab(page, panel, timeoutMs = 20_000) {
  // Workflow UI metadata is loaded independently from the session panel.  A
  // panel can therefore mount before its tabs exist; never snapshot tab count
  // and assume the last tab at that instant is the final document.
  const deadline = Date.now() + timeoutMs;
  const tabs = panel.locator(UI.tabs);
  let diagnostics = {
    tab_count: 0,
    tab_controls: [],
    selected_control: "",
    document_selector: "",
    click_error: "",
  };
  while (Date.now() < deadline) {
    const tabCount = await tabs.count().catch(() => 0);
    const controls = [];
    for (let i = 0; i < tabCount; i++) {
      controls.push((await tabs.nth(i).getAttribute("aria-controls").catch(() => "")) || "");
    }
    diagnostics = { ...diagnostics, tab_count: tabCount, tab_controls: controls };

    const finalIndex = controls.findIndex((value) => value === "workflow-tab-panel-result");
    if (finalIndex >= 0) {
      const finalTab = tabs.nth(finalIndex);
      try {
        await finalTab.click();
      } catch (err) {
        diagnostics.click_error = String(err?.message || err || "").slice(0, 200);
      }
      const selected = (await finalTab.getAttribute("aria-selected").catch(() => "")) === "true";
      diagnostics.selected_control = selected ? controls[finalIndex] : "";
      if (selected) {
        const remaining = Math.max(1, deadline - Date.now());
        const document = await readVisibleFinalDocument(page, panel, remaining);
        diagnostics.document_selector = document.selector;
        return { ...document, diagnostics };
      }
    }
    await page.waitForTimeout(Math.min(250, Math.max(1, deadline - Date.now())));
  }
  return { visible: false, text: "", selector: "", diagnostics };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.scenario || !args.case || !args.prompt_file) {
    console.error("usage: writer_bridge.mjs --scenario X --case N --prompt-file path "
                  + "--token TOK --user-id ID "
                  + "[--base-url URL] [--attachment PATH] [--output-dir DIR]");
    process.exit(2);
  }
  if (!args.token || !args.user_id) {
    console.error("writer_bridge requires a token and matching user-id");
    process.exit(2);
  }
  const baseUrl = args.base_url || process.env.LAZYMIND_BASE_URL || "http://127.0.0.1:8090";
  const promptText = expandEnv(readFileSync(args.prompt_file, "utf8").trim());
  const timeoutMs = Math.max(1_000, Number(args.timeout_ms || 595_000));
  const bridgeDeadline = Date.now() + timeoutMs;
  const statusPollMs = Math.max(1_000, Number(args.status_poll_ms || 10_000));

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: args.output_dir || ".", size: { width: 1920, height: 1080 } },
  });
  const page = await context.newPage();
  const taskObserver = createTaskStreamObserver(baseUrl, args.token);
  page.on("request", (request) => taskObserver.observe(request.url()));

  // install auth via localStorage mirror of the e2e helper pattern, and patch
  // XMLHttpRequest so we can capture task SSE chunks while the app runs.
  await page.addInitScript(({ token, userId }) => {
    localStorage.setItem("lazymind:user", JSON.stringify({
      token,
      refreshToken: "",
      username: userId,
      userId,
      role: "admin",
      timestamp: Date.now(),
    }));
    sessionStorage.clear();
    window.__lazymindSse = [];
    window.__lazymindSseRequests = [];
    let requestSequence = 0;
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__lazymindCapUrl = String(url);
      return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      const xhr = this;
      const rawUrl = new URL(xhr.__lazymindCapUrl || "", window.location.href);
      const url = rawUrl.origin + rawUrl.pathname;
      if (!/tasks\/[^/?]+:stream|conversations:chat|workflow-sessions\/[^/?]+\/events/.test(url)) {
        return originalSend.apply(xhr, arguments);
      }
      let lastIndex = 0;
      const requestId = ++requestSequence;
      const requestState = { request_id: requestId, url, started_at: Date.now(),
        last_chunk_at: null, closed_at: null, terminal: null };
      window.__lazymindSseRequests.push(requestState);
      const record = () => {
        try {
          const text = xhr.responseText || "";
          if (text.length > lastIndex) {
            window.__lazymindSse.push({
              url,
              request_id: requestId,
              at: Date.now(),
              chunk: text.slice(lastIndex),
            });
            lastIndex = text.length;
            requestState.last_chunk_at = Date.now();
          }
        } catch (_) {}
      };
      xhr.addEventListener("progress", record);
      xhr.addEventListener("readystatechange", record);
      xhr.addEventListener("loadend", record);
      for (const type of ["abort", "error", "load", "timeout"]) {
        xhr.addEventListener(type, () => {
          record();
          requestState.terminal = type;
          requestState.closed_at = Date.now();
        });
      }
      return originalSend.apply(xhr, arguments);
    };
  }, { token: args.token, userId: args.user_id });

  let conversationId = "";
  let sessionId = "";
  let taskId = "";
  let writerStatus = "unknown";
  let finalText = "";
  let renderedImages = 0;
  let failedImages = [];
  let placeholderImages = [];
  let finalPanelVisible = false;
  let finalUiDiagnostics = {};
  let retryEvents = [];
  let capturedSteps = new Set();
  let sseCapture = {
    task_id: "", conversation_ids: [], total_artifact_events: 0, streams: [],
  };
  let observerCapture = {};
  let conversationIdSources = {};

  try {
    await page.goto(`${baseUrl}/agent/chat/home`);
    const initialStorageConversationId = await page.evaluate(() =>
      sessionStorage.getItem("chat_resume_conversation_id") || "",
    );
    const initialConversationId = conversationIdFromUrl(page.url())
      || initialStorageConversationId;
    if (initialConversationId) {
      throw new Error(
        `expected a fresh chat composer, found conversation ${initialConversationId}`,
      );
    }
    const input = page.locator(UI.mentionEditor).last();
    await input.waitFor({ timeout: 180_000 });
    // Restrict autocomplete to workflows. A plain "@AI Writer" query also
    // returns conversations whose titles start with "AI Writer" and can insert
    // an old conversation link instead of the Writer workflow mention.
    await input.fill("@workflow:AI Writer");
    const workflowOption = page
      .locator(UI.mentionOption)
      .filter({
        has: page.locator(UI.mentionOptionName, { hasText: /^AI Writer$/ }),
      })
      .first();
    await workflowOption.waitFor({ state: "visible", timeout: 180_000 });
    await workflowOption.click();

    // Never send unless the editor contains exactly the intended workflow
    // mention. This keeps a stale conversation search result from contaminating
    // the functional-test prompt.
    const mentionState = await input.locator(UI.mentionChip).evaluateAll((chips) =>
      chips.map((chip) => ({
        type: chip.getAttribute("data-mention-type") || "",
        resourceId: chip.getAttribute("data-resource-id") || "",
      })),
    );
    if (mentionState.length !== 1
        || mentionState[0].type !== "workflow"
        || mentionState[0].resourceId !== "builtin:writer-workflow") {
      throw new Error(`invalid Writer mention: ${JSON.stringify(mentionState)}`);
    }

    if (args.attachment) {
      await page.locator(UI.hiddenUpload)
        .setInputFiles(args.attachment);
      await waitForAttachmentUploaded(page, path.basename(args.attachment));
    }
    const send = page.locator(UI.sendButton);
    const existingSessionIds = await page
      .locator(UI.workflowPanel)
      .evaluateAll((els) => els
        .map((el) => el.getAttribute("data-session-id"))
        .filter((value) => Boolean(value)));
    await input.press("End");
    await input.pressSequentially(` ${promptText}`, { delay: 1 });
    await send.click();

    const panel = await waitForNewWorkflowPanel(page, existingSessionIds);
    sessionId = (await panel.getAttribute("data-session-id")) || "";
    if (!sessionId) {
      throw new Error("new workflow panel lost its session id before capture");
    }

    // Leave time to collect late events before Python's subprocess deadline.
    const deadline = bridgeDeadline - 35_000;
    const tabs = panel.locator(UI.tabs);
    const readBadges = async () => {
      const out = [];
      const tabCount = await tabs.count().catch(() => 0);
      for (let i = 0; i < tabCount; i++) {
        const cls = await tabs.nth(i)
          .locator(UI.stepStatus)
          .getAttribute("class").catch(() => "");
        const status = (cls || "").match(/workflow-panel__step-status--([a-z]+)/);
        out.push(status ? status[1] : "");
      }
      return out;
    };
    let prevBadges = await readBadges();
    capturedSteps = new Set();
    retryEvents = [];
    const captureStepPanel = async (idx, force = false) => {
      const tabCount = await tabs.count().catch(() => 0);
      if ((!force && capturedSteps.has(idx)) || idx < 0 || idx >= tabCount) return;
      try { await tabs.nth(idx).click(); } catch (_) {}
      await page.waitForTimeout(1200); // 让步骤面板渲染进录屏
      capturedSteps.add(idx);
    };
    let lastFailedAt = 0;
    const FAILED_STABLE_MS = 30_000; // 会话自行重试的观察窗口
    // 挂载时如果已有阶段正在执行（徽标已出现），立即切到对应面板。
    for (let i = 0; i < prevBadges.length; i++) {
      if (prevBadges[i]) await captureStepPanel(i);
    }

    while (Date.now() < deadline) {
      let sessionStatus = "";
      try {
        const response = await page.request.get(
          `${baseUrl}/api/core/workflow-sessions/${encodeURIComponent(sessionId)}`,
          { headers: { Authorization: `Bearer ${args.token}` } },
        );
        if (response.ok()) {
          const envelope = await response.json();
          const session = envelope?.data?.session || {};
          sessionStatus = typeof session.status === "string" ? session.status : "";
        }
      } catch (_) {}
      // Durable session state is the sole completion authority.
      writerStatus = sessionStatus || writerStatus;
      if (writerStatus === "completed") break;

      // 阶段开始跟随：步骤徽标从空变为非空（running/interrupted/failed）即该
      // 阶段开始执行，切到对应步骤面板，让录屏跟随当前执行中的阶段。
      const badges = await readBadges();
      for (let i = 0; i < badges.length; i++) {
        if (!prevBadges[i] && badges[i]) {
          await captureStepPanel(i);
        }
        if (prevBadges[i] === "failed" && badges[i] === "running") {
          // 同一阶段失败后由会话自行重试（failed -> running）。
          retryEvents.push({ type: "step_retried", tab: i, at: new Date().toISOString() });
        }
      }
      prevBadges = badges;

      if (writerStatus === "waiting") {
        const proceed = panel.locator(UI.proceed);
        if (await proceed.isEnabled().catch(() => false)) {
          await proceed.click();
          await page.waitForTimeout(500);
        }
      }
      if (writerStatus === "failed") {
        // 会话可能自行重试（failed -> active/waiting 恢复）；只有稳定失败才终止。
        if (!lastFailedAt) {
          lastFailedAt = Date.now();
          retryEvents.push({ type: "panel_failed", at: new Date().toISOString() });
        }
        if (Date.now() - lastFailedAt >= FAILED_STABLE_MS) break;
      } else {
        if (lastFailedAt) {
          retryEvents.push({ type: "panel_recovered", at: new Date().toISOString() });
          lastFailedAt = 0;
        }
      }
      await page.waitForTimeout(Math.min(statusPollMs, Math.max(250, deadline - Date.now())));
    }
    // UI metadata may finish loading after the session panel.  Wait for the
    // semantically stable result tab instead of using an early tab-count
    // snapshot or assuming the current last index is final.
    if (writerStatus === "completed") {
      const finalDocument = await openFinalDocumentTab(page, panel,
        Math.max(1, Math.min(20_000, bridgeDeadline - Date.now() - 30_000)));
      finalText = finalDocument.text;
      finalUiDiagnostics = finalDocument.diagnostics;
      finalPanelVisible = finalDocument.visible && Boolean(finalText);
    }

    // Count draft-document images that actually rendered (naturalWidth > 0).
    // Markdown preview hides failed images and the IR editor keeps broken
    // <img> nodes.
    if (writerStatus === "completed") {
      const classifyImages = () => panel.locator(UI.renderedImages).evaluateAll((imgs) => {
        const rendered = [];
        const failed = [];
        const placeholders = [];
        let pending = 0;
        for (let index = 0; index < imgs.length; index++) {
          const el = imgs[index];
          if (!(el instanceof HTMLImageElement)) continue;
          const src = el.currentSrc || el.src || "";
          if (src.includes("media-placeholder://")) {
            placeholders.push(`image#${index + 1}`);
            continue;
          }
          if (!el.complete) {
            pending += 1;
          } else if (el.naturalWidth === 0 || el.naturalHeight === 0) {
            // Never persist signed media URLs in reports/evidence.
            failed.push(`image#${index + 1}: decode/load failed`);
          } else {
            rendered.push(`image#${index + 1}`);
          }
        }
        return { rendered: rendered.length, failed, placeholders, pending, total: imgs.length };
      });
      let imageState = { rendered: 0, failed: [], placeholders: [], pending: 0, total: 0 };
      const imageDeadline = Math.min(Date.now() + 20_000, bridgeDeadline - 30_000);
      while (Date.now() < imageDeadline) {
        imageState = await classifyImages();
        if (imageState.total > 0 && imageState.pending === 0) break;
        await page.waitForTimeout(500);
      }
      renderedImages = imageState.rendered;
      failedImages = imageState.failed.slice(0, 20);
      placeholderImages = imageState.placeholders.slice(0, 20);
    }

    // Pull the in-browser SSE capture after a bounded drain. Durable
    // workflow completion can become visible just before the final XHR progress
    // callback delivers artifact_stream_end; waiting avoids truncating evidence
    // while still failing closed when the producer never terminates.
    try {
      const readBrowserCapture = async () => {
        // Flush responseText through the registered callbacks before snapshotting
        // is handled by progress/readystatechange/loadend; retain close timing.
        const state = await page.evaluate(() => ({
          chunks: window.__lazymindSse || [],
          requests: window.__lazymindSseRequests || [],
        }));
        return { ...buildSseCapture(state.chunks), transport: state.requests };
      };
      [sseCapture, observerCapture] = writerStatus === "completed"
        ? await Promise.all([
          waitForCaptureSettlement(readBrowserCapture,
            Math.max(0, Math.min(30_000, bridgeDeadline - Date.now()))),
          waitForCaptureSettlement(async () => taskObserver.snapshot(),
            Math.max(0, Math.min(30_000, bridgeDeadline - Date.now()))),
        ])
        : [await readBrowserCapture(), taskObserver.snapshot()];
      if (args.output_dir) {
        mkdirSync(args.output_dir, { recursive: true });
        writeFileSync(path.join(args.output_dir, "sse_capture.json"), JSON.stringify({
          browser: sseCapture, observer: observerCapture,
        }, null, 2));
      }
      taskId = sseCapture.task_id || taskId;
    } catch (err) {
      console.error(`sse capture failed: ${err && err.message ? err.message : err}`);
    }

    // The chat SSE identifies the conversation created by this send. Route and
    // sessionStorage are corroborating evidence only; neither may substitute
    // for a missing SSE conversation id.
    const storageCid = await page.evaluate(() =>
      sessionStorage.getItem("chat_resume_conversation_id") || "",
    ).catch(() => "");
    const url = page.url();
    const routeCid = conversationIdFromUrl(url);
    const resolvedConversation = resolveConversationId(
      sseCapture.conversation_ids || [], routeCid, storageCid,
    );
    conversationIdSources = resolvedConversation.sources;
    conversationId = resolvedConversation.conversationId;
  } catch (err) {
    console.error(`bridge failed: ${err && err.message ? err.message : err}`);
  } finally {
    await taskObserver.close();
    try { await context.close(); } catch (_) {}
    try { await browser.close(); } catch (_) {}
  }

  process.stdout.write(JSON.stringify({
    conversation_id: conversationId,
    session_id: sessionId,
    task_id: taskId,
    writer_status: writerStatus,
    final_text: finalText,
    rendered_images: renderedImages,
    failed_images: failedImages,
    failed_images_count: failedImages.length,
    placeholder_images: placeholderImages,
    final_panel_visible: finalPanelVisible,
    final_ui_diagnostics: finalUiDiagnostics,
    retry_events: retryEvents,
    conversation_id_sources: conversationIdSources,
    captured_steps: [...capturedSteps],
    sse_capture: sseCapture,
    sse_observer_capture: observerCapture,
    base_url: baseUrl,
    scenario: args.scenario,
    case: args.case,
  }) + "\n");
}

const invokedDirectly = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (invokedDirectly) {
  main().catch((err) => {
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  });
}
