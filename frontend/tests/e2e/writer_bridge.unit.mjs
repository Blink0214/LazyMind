import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSseCapture,
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

test("reconstructs current task SSE data events", () => {
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
  assert.deepEqual(capture.events.map(event => event.type), [
    "artifact_stream_start", "artifact_stream", "artifact_stream", "artifact_stream_end",
  ]);
  assert.equal(capture.events[0].slot, "draft_document");
  assert.equal(capture.events[0].content_type, "text/markdown");
  assert.equal(capture.events.map(event => event.delta || "").join(""), "hello world");
  assert.deepEqual(capture.events.map(event => event.chunk_index), [1, 2, 3, 4]);
});

test("keeps compatibility with named artifact stream events", () => {
  const capture = buildSseCapture([{
    url: "/api/core/tasks/task-legacy:stream",
    at: 1,
    chunk: 'event: artifact_stream\ndata: {"stream_id":"stream-legacy","chunk":"legacy"}\n\n',
  }]);
  assert.equal(capture.total_artifact_events, 1);
  assert.equal(capture.events[0].chunk, "legacy");
});

test("reads only visible non-empty IR/Markdown document DOM", async (t) => {
  const { chromium } = await import("@playwright/test");
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
