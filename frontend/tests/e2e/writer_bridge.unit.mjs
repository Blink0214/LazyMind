import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { once } from "node:events";
import { chromium } from "@playwright/test";

import {
  buildSseCapture,
  createTaskStreamObserver,
  waitForCaptureSettlement,
  latestDraftStreamState,
  openFinalDocumentTab,
  readVisibleFinalDocument,
  resolveConversationId,
} from "./writer_bridge.mjs";

test("extracts the authoritative conversation id from the chat SSE", () => {
  const capture = buildSseCapture([{
    url: "/api/core/conversations:chat",
    at: 1,
    chunk: 'data: {"result":{"conversation_id":"conversation-new"}}\n\n',
  }]);
  assert.deepEqual(capture.conversation_ids, ["conversation-new"]);
  assert.deepEqual(
    resolveConversationId(capture.conversation_ids, "conversation-new", "conversation-new"),
    {
      conversationId: "conversation-new",
      sources: {
        chat_sse: "conversation-new",
        route: "conversation-new",
        session_storage: "conversation-new",
      },
    },
  );
  assert.throws(
    () => resolveConversationId(["conversation-new"], "conversation-other", ""),
    /conversation id mismatch/,
  );
  assert.throws(
    () => resolveConversationId([], "conversation-from-route", "conversation-from-storage"),
    /conversation id missing.*conversations:chat SSE/,
  );
});

test("collects current task SSE data events", () => {
  const url = "/api/core/tasks/task-current:stream";
  const capture = buildSseCapture([
    {
      url, at: 1,
      chunk: 'data: {"type":"artifact_stream_start","stream_id":"stream-1","slot":"draft_document","content_type":"text/markdown","chunk_index":1}\n\n',
    },
    {
      url, at: 2,
      chunk: 'data: {"type":"artifact_stream","stream_id":"stream-1","delta":"hello ","chunk_index":2}\n\n',
    },
    {
      url, at: 3,
      chunk: 'data: {"type":"artifact_stream","stream_id":"stream-1","delta":"world","chunk_index":3}\n\n',
    },
    {
      url, at: 4,
      chunk: 'data: {"type":"artifact_stream_end","stream_id":"stream-1","chunk_index":4}\n\n',
    },
  ]);
  assert.equal(capture.task_id, "task-current");
  assert.equal(capture.total_artifact_events, 4);
  assert.deepEqual(capture.events.map((event) => event.type), [
    "artifact_stream_start", "artifact_stream",
    "artifact_stream", "artifact_stream_end",
  ]);
  assert.deepEqual(latestDraftStreamState(capture), {
    started: true, settled: true, terminal: "end",
  });
});

test("reports a draft stream that has not received its terminal event", () => {
  const capture = buildSseCapture([{
    url: "/api/core/tasks/task-current:stream",
    at: 1,
    chunk: [
      'data: {"type":"artifact_stream_start","stream_id":"stream-1","slot":"draft_document","content_type":"text/markdown","chunk_index":1}',
      'data: {"type":"artifact_stream","stream_id":"stream-1","delta":"partial","chunk_index":2}',
      "",
    ].join("\n\n"),
  }]);
  assert.deepEqual(latestDraftStreamState(capture), {
    started: true, settled: false, terminal: "",
  });
});

test("reads only visible non-empty IR/Markdown document DOM", async (t) => {
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();

  await t.test("accepts visible non-empty IR and strips zero-width text", async () => {
    await page.setContent(`
      <section class="workflow-panel">
        <div class="workflow-panel__tab-content">
          <article class="writer-ir__document">\u200b最终成稿</article>
        </div>
      </section>
    `);
    const result = await readVisibleFinalDocument(
      page, page.locator(".workflow-panel"), 100,
    );
    assert.deepEqual(result, {
      visible: true,
      text: "最终成稿",
      selector: ".writer-ir__document:visible",
    });
  });

  await t.test("accepts visible non-empty editable Markdown", async () => {
    await page.setContent(`
      <section class="workflow-panel">
        <div class="workflow-panel__tab-content">
          <div class="mdxeditor-root-contenteditable">Markdown 成稿</div>
        </div>
      </section>
    `);
    const result = await readVisibleFinalDocument(
      page, page.locator(".workflow-panel"), 100,
    );
    assert.equal(result.visible, true);
    assert.equal(result.text, "Markdown 成稿");
    assert.equal(result.selector, ".mdxeditor-root-contenteditable:visible");
  });

  await t.test("rejects hidden IR and empty Markdown", async () => {
    await page.setContent(`
      <section class="workflow-panel">
        <div class="workflow-panel__tab-content">
          <article class="writer-ir__document" style="display:none">隐藏内容</article>
          <div class="mdxeditor-root-contenteditable"> \u200b </div>
        </div>
      </section>
    `);
    const result = await readVisibleFinalDocument(
      page, page.locator(".workflow-panel"), 20,
    );
    assert.deepEqual(result, { visible: false, text: "", selector: "" });
  });
});

test("waits for the final result tab when workflow UI metadata mounts late", async (t) => {
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setContent('<section class="workflow-panel"><div class="workflow-panel__tabs"></div></section>');
  await page.evaluate(() => {
    window.setTimeout(() => {
      const panel = document.querySelector(".workflow-panel");
      const tabs = panel.querySelector(".workflow-panel__tabs");
      const button = document.createElement("button");
      button.className = "workflow-panel__tab";
      button.setAttribute("aria-controls", "workflow-tab-panel-result");
      button.setAttribute("aria-selected", "false");
      button.addEventListener("click", () => button.setAttribute("aria-selected", "true"));
      tabs.append(button);
      const content = document.createElement("div");
      content.className = "workflow-panel__tab-content";
      content.innerHTML = '<article class="writer-artifact__markdown">延迟挂载的最终成稿</article>';
      panel.append(content);
    }, 100);
  });

  const result = await openFinalDocumentTab(
    page, page.locator(".workflow-panel"), 2_000,
  );
  assert.equal(result.visible, true);
  assert.equal(result.text, "延迟挂载的最终成稿");
  assert.equal(result.diagnostics.tab_count, 1);
  assert.equal(result.diagnostics.selected_control, "workflow-tab-panel-result");
  assert.equal(result.diagnostics.document_selector, ".writer-artifact__markdown:visible");
});


test("SSE reconnects do not splice partial frames across connections", () => {
  const url = "/api/core/tasks/task:stream";
  const capture = buildSseCapture([
    { url, request_id: 1, at: 1, chunk: 'data: {"type":"artifact_stream",' },
    { url, request_id: 2, at: 2, chunk: 'data: {"type":"artifact_stream_end","stream_id":"s"}\n\n' },
  ]);
  assert.equal(capture.events.length, 1);
  assert.equal(capture.events[0].type, "artifact_stream_end");
  assert.equal(capture.diagnostics.parse_errors, 1);
});

test("drain waits for delayed tail/end and reports an absent end without fabricating it", async () => {
  const start = { type: "artifact_stream_start", stream_id: "s",
    slot: "draft_document", content_type: "text/markdown" };
  const events = [start, { type: "artifact_stream", stream_id: "s", delta: "head" }];
  const started = Date.now();
  const capture = await waitForCaptureSettlement(async () => {
    if (Date.now() - started > 120 && events.length === 2) events.push(
      { type: "artifact_stream", stream_id: "s", delta: "tail" },
      { type: "artifact_stream_end", stream_id: "s" });
    return { total_artifact_events: events.length, events: [...events] };
  }, 1500, 100);
  assert.equal(capture.events.length, 4);
  assert.equal(capture.diagnostics.drain_reason, "terminal_and_quiet");
  assert.ok(capture.diagnostics.drain_wait_ms >= 220);
  const missing = await waitForCaptureSettlement(async () => ({
    total_artifact_events: 1, events: [start],
  }), 120, 20);
  assert.equal(missing.diagnostics.drain_reason, "timeout");
  assert.equal(latestDraftStreamState(missing).settled, false);
});

test("independent observer retains delayed server tail after browser subscription aborts", async (t) => {
  const frame = (type, rest = {}) => `data: ${JSON.stringify({
    type, stream_id: "s", ...rest,
  })}\n\n`;
  const server = createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*" });
    res.write(frame("artifact_stream_start", { slot: "draft_document", content_type: "text/markdown" }));
    res.write(frame("artifact_stream", { delta: "head" }));
    const timer = setTimeout(() => {
      res.end(frame("artifact_stream", { delta: "tail" }) + frame("artifact_stream_end"));
    }, 250);
    res.on("close", () => clearTimeout(timer));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const observer = createTaskStreamObserver(base, "synthetic-test-token");
  t.after(() => observer.close());
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  page.on("request", (request) => observer.observe(request.url()));
  await page.evaluate(async (url) => new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", url);
    xhr.onprogress = () => { if (xhr.responseText.includes("head")) xhr.abort(); };
    xhr.onabort = () => resolve();
    xhr.onerror = () => resolve();
    xhr.send();
  }), `${base}/api/core/tasks/current:stream`);
  const capture = await waitForCaptureSettlement(async () => observer.snapshot(), 3000, 100);
  assert.equal(capture.events.filter((e) => e.type === "artifact_stream").map((e) => e.delta).join(""), "headtail");
  assert.equal(latestDraftStreamState(capture).terminal, "end");
  assert.equal(capture.transport.length, 1);
  assert.equal(capture.transport[0].status, "closed");
  assert.ok(!JSON.stringify(capture).includes("synthetic-test-token"));
});


test("identical indexed replay is not a second stream start", () => {
  const url = "/api/core/tasks/task:stream";
  const event = { type: "artifact_stream_start", stream_id: "s", chunk_index: 1,
    slot: "draft_document", content_type: "text/markdown" };
  const chunk = `data: ${JSON.stringify(event)}\n\n`;
  const capture = buildSseCapture([
    { url, request_id: 1, at: 1, chunk },
    { url, request_id: 2, at: 2, chunk },
  ]);
  assert.equal(capture.events.length, 1);
  assert.equal(capture.diagnostics.replayed_events, 1);
});
