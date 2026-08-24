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
import { readFileSync } from "node:fs";
import path from "node:path";

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

function lastJsonLine(text) {
  const lines = text.split("\n").filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].trim().startsWith("{")) return lines[i];
  }
  return null;
}

// ---------------------------------------------------------------------------
// In-browser SSE capture
//
// The frontend consumes task artifact streams through an XHR-based SSE client
// (modules/chat/utils/sse.ts), so native EventSource CDP events never fire.
// We patch XMLHttpRequest before the app loads and record every incremental
// responseText chunk for the task / workflow event channels.  Frames are then
// re-parsed after the run and normalized to the same stream records that the
// Python side already consumes (start / delta / end / aborted).
// ---------------------------------------------------------------------------

function parseSseFrames(text) {
  const frames = [];
  for (const raw of String(text || "").split(/\r?\n\r?\n/)) {
    if (!raw.trim()) continue;
    const frame = { event: "message", data: [] };
    for (const line of raw.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;
      const idx = line.indexOf(":");
      if (idx < 0) continue;
      const field = line.slice(0, idx).trim();
      const value = line.slice(idx + 1).trimStart();
      if (field === "event") frame.event = value;
      else if (field === "data") frame.data.push(value);
      else if (field === "id") frame.id = value;
    }
    if (frame.data.length) {
      frame.payload = frame.data.join("\n");
      frames.push(frame);
    }
  }
  return frames;
}

function buildSseCapture(capture) {
  const byUrl = new Map();
  for (const entry of capture || []) {
    const list = byUrl.get(entry.url) || [];
    list.push(entry);
    byUrl.set(entry.url, list);
  }
  const streams = new Map();
  let totalArtifactEvents = 0;
  let taskId = "";
  for (const [url, entries] of byUrl) {
    entries.sort((a, b) => a.at - b.at);
    const text = entries.map((e) => e.chunk).join("");
    const taskMatch = url.match(/\/api\/core\/tasks\/([^/?]+):stream/);
    if (taskMatch) taskId = taskMatch[1];
    for (const frame of parseSseFrames(text)) {
      if (!frame.event.startsWith("artifact_stream")) continue;
      totalArtifactEvents += 1;
      let payload = {};
      try {
        payload = JSON.parse(frame.payload);
      } catch (_) {
        continue;
      }
      const sid = String(payload.stream_id || "");
      if (!sid) continue;
      let rec = streams.get(sid);
      if (!rec) {
        rec = {
          stream_id: sid,
          events: [],
          text: "",
          slots: [],
          content_types: [],
          aborted: false,
        };
        streams.set(sid, rec);
      }
      if (frame.event === "artifact_stream_start") {
        rec.events.push("artifact_stream_start");
        if (Array.isArray(payload.slots)) {
          rec.slots = payload.slots.map(String);
        }
        if (Array.isArray(payload.content_types)) {
          rec.content_types = payload.content_types.map(String);
        }
        if (payload.aborted) rec.aborted = true;
      } else if (frame.event === "artifact_stream_end") {
        rec.events.push("artifact_stream_end");
        if (payload.aborted) rec.aborted = true;
      } else if (frame.event === "artifact_stream_abort") {
        // 终止帧等价于 end + abort，统一为 Python 侧的事件形态。
        rec.events.push("artifact_stream_end");
        rec.aborted = true;
      } else {
        // artifact_stream（delta 帧）
        rec.events.push("artifact_stream_delta");
        if (payload.chunk !== undefined && payload.chunk !== null) {
          rec.text += String(payload.chunk);
        }
      }
    }
  }
  return {
    task_id: taskId,
    total_artifact_events: totalArtifactEvents,
    streams: [...streams.values()],
  };
}

async function waitForAttachmentUploaded(page, fileName) {
  // The chat composer disables the send button while files are uploading
  // (isUploading) and removes the file chip when an upload fails. Wait for the
  // chip to mount, then observe the disabled -> enabled transition so the
  // message is never sent with a still-pending/empty attachment record.
  const chip = page.locator(".chat-files-item", { hasText: fileName }).first();
  await chip.waitFor({ state: "visible", timeout: 15_000 });
  const send = page.locator(".send-button:visible").first();
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
  // a data-session-id that was not present before the send. Prefer that panel
  // over the first visible one to avoid associating with a stale panel from a
  // previous conversation (observed on the first C06 attempt).
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const panels = page.locator(".workflow-panel[data-session-id]:visible");
    const count = await panels.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      const sid = await panels.nth(i).getAttribute("data-session-id").catch(() => "");
      if (sid && !existingSessionIds.includes(sid)) {
        return panels.nth(i);
      }
    }
    await page.waitForTimeout(500);
  }
  return page.locator(".workflow-panel[data-session-id]:visible").first();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.scenario || !args.case || !args.prompt_file) {
    console.error("usage: writer_bridge.mjs --scenario X --case N --prompt-file path "
                  + "[--base-url URL] [--token TOK] [--attachment PATH] [--output-dir DIR]");
    process.exit(2);
  }
  const baseUrl = args.base_url || process.env.LAZYMIND_BASE_URL || "http://127.0.0.1:8090";
  const promptText = expandEnv(readFileSync(args.prompt_file, "utf8").trim());

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: args.output_dir || ".", size: { width: 1920, height: 1080 } },
  });
  const page = await context.newPage();

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
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__lazymindCapUrl = String(url);
      return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      const xhr = this;
      const url = xhr.__lazymindCapUrl || "";
      if (!/tasks\/[^/?]+:stream|conversations:chat|workflow-sessions\/[^/?]+\/events/.test(url)) {
        return originalSend.apply(xhr, arguments);
      }
      let lastIndex = 0;
      const record = () => {
        try {
          const text = xhr.responseText || "";
          if (text.length > lastIndex) {
            window.__lazymindSse.push({
              url,
              at: Date.now(),
              chunk: text.slice(lastIndex),
            });
            lastIndex = text.length;
          }
        } catch (_) {}
      };
      xhr.addEventListener("progress", record);
      xhr.addEventListener("readystatechange", record);
      xhr.addEventListener("loadend", record);
      return originalSend.apply(xhr, arguments);
    };
  }, { token: args.token || "", userId: process.env.LAZYMIND_USER_ID || "0" });

  let conversationId = "";
  let sessionId = "";
  let taskId = "";
  let writerStatus = "unknown";
  let finalText = "";
  let renderedImages = 0;
  let failedImages = [];
  let placeholderImages = [];
  let retryEvents = [];
  let capturedSteps = new Set();

  try {
    await page.goto(`${baseUrl}/agent/chat/home`);
    const input = page.locator('.chat-mention-editor[contenteditable="true"]:visible').last();
    await input.waitFor({ timeout: 180_000 });
    await input.fill("@AI Writer");
    await page.getByRole("option").filter({ hasText: "AI Writer" }).first().click();

    if (args.attachment) {
      await page.locator('.chat-add-resource-hidden-upload input[type="file"]')
        .setInputFiles(args.attachment);
      await waitForAttachmentUploaded(page, path.basename(args.attachment));
    }
    const send = page.locator(".send-button:visible");
    const existingSessionIds = await page
      .locator(".workflow-panel[data-session-id]")
      .evaluateAll((els) => els
        .map((el) => el.getAttribute("data-session-id"))
        .filter((value) => Boolean(value)));
    await input.press("End");
    await input.pressSequentially(` ${promptText}`, { delay: 1 });
    await send.click();

    const panel = await waitForNewWorkflowPanel(page, existingSessionIds);
    await panel.waitFor({ timeout: 180_000 });
    sessionId = (await panel.getAttribute("data-session-id")) || "";

    const deadline = Date.now() + 15 * 60 * 1000;
    const tabs = panel.locator(".workflow-panel__tabs > .workflow-panel__tab");
    const tabCount = await tabs.count();
    const readBadges = async () => {
      const out = [];
      for (let i = 0; i < tabCount; i++) {
        const cls = await tabs.nth(i)
          .locator(".workflow-panel__step-status")
          .getAttribute("class").catch(() => "");
        const status = (cls || "").match(/workflow-panel__step-status--([a-z]+)/);
        out.push(status ? status[1] : "");
      }
      return out;
    };
    let prevBadges = await readBadges();
    capturedSteps = new Set();
    retryEvents = [];
    const captureStepPanel = async (idx) => {
      if (capturedSteps.has(idx) || idx < 0 || idx >= tabCount) return;
      try { await tabs.nth(idx).click(); } catch (_) {}
      await page.waitForTimeout(1200); // 让步骤面板渲染进录屏
      capturedSteps.add(idx);
    };
    let lastFailedAt = 0;
    const FAILED_STABLE_MS = 30_000; // 会话自行重试的观察窗口
    // 挂载时如果已有阶段正在执行（徽标已出现），立即切到对应面板。
    for (let i = 0; i < tabCount; i++) {
      if (prevBadges[i]) await captureStepPanel(i);
    }

    while (Date.now() < deadline) {
      const cls = (await panel.getAttribute("class")) || "";
      const m = cls.match(/workflow-panel--(active|waiting|completed|failed)/);
      writerStatus = m ? m[1] : writerStatus;
      if (writerStatus === "completed") break;

      // 阶段开始跟随：步骤徽标从空变为非空（running/interrupted/failed）即该
      // 阶段开始执行，切到对应步骤面板，让录屏跟随当前执行中的阶段。
      const badges = await readBadges();
      for (let i = 0; i < tabCount; i++) {
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
        const proceed = panel.locator(".workflow-panel__action-btn--primary");
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
      await page.waitForTimeout(1000);
    }
    // 运行完成（completed）后切到最后一个步骤面板，展示最终成果。
    if (writerStatus === "completed" && tabCount > 0) {
      await captureStepPanel(tabCount - 1);
    }

    // Count draft-document images that actually rendered (naturalWidth > 0).
    // Markdown preview hides failed images and the IR editor keeps broken
    // <img> nodes.
    if (writerStatus === "completed") {
      const tabs = panel.locator(".workflow-panel__tabs > .workflow-panel__tab");
      const tabCount = await tabs.count();
      const classifyImages = () => panel.locator(
        ".writer-artifact__markdown img, .mdxeditor img, img[data-writer-image='true']",
      ).evaluateAll((imgs) => {
        const rendered = [];
        const failed = [];
        const placeholders = [];
        for (const el of imgs) {
          if (!(el instanceof HTMLImageElement)) continue;
          const src = el.currentSrc || el.src || "";
          if (src.includes("media-placeholder://")) {
            placeholders.push(src.slice(0, 160));
            continue;
          }
          // 未加载完成、解码失败或 0 尺寸的图在前端都会显示为异常图。
          const broken = !el.complete || el.naturalWidth === 0 || el.naturalHeight === 0;
          if (broken) failed.push(src.slice(0, 160));
          else rendered.push(src.slice(0, 160));
        }
        return { rendered: rendered.length, failed, placeholders };
      });
      let imageState = { rendered: 0, failed: [], placeholders: [] };
      for (let i = 0; i < tabCount; i++) {
        const tab = tabs.nth(i);
        if ((await tab.getAttribute("aria-selected")) !== "true") {
          await tab.click();
          await page.waitForTimeout(300);
        }
        const imageDeadline = Date.now() + 20_000;
        while (Date.now() < imageDeadline) {
          imageState = await classifyImages();
          if (imageState.rendered > 0 || imageState.failed.length > 0) break;
          await page.waitForTimeout(500);
        }
        if (imageState.rendered > 0 || imageState.failed.length > 0) break;
      }
      renderedImages = imageState.rendered;
      failedImages = imageState.failed.slice(0, 20);
      placeholderImages = imageState.placeholders.slice(0, 20);
    }

    // Pull the in-browser SSE capture (task artifact streams + workflow events).
    let sseCapture = { task_id: "", total_artifact_events: 0, streams: [] };
    try {
      const rawCapture = await page.evaluate(() => window.__lazymindSse || []);
      sseCapture = buildSseCapture(rawCapture);
    } catch (err) {
      console.error(`sse capture failed: ${err && err.message ? err.message : err}`);
    }

    // Pull conversation_id from the app state after the run lands; the app
    // stores the resolved id in sessionStorage (the URL never carries it).
    const storageCid = await page.evaluate(() =>
      sessionStorage.getItem("chat_resume_conversation_id") || "",
    ).catch(() => "");
    const url = page.url();
    const m = url.match(/conversation[s]?[/=]([A-Za-z0-9_-]+)/);
    conversationId = storageCid || (m ? m[1] : "");

    // Pull session_id from the panel again in case it changed.
    sessionId = (await panel.getAttribute("data-session-id")) || sessionId;
  } catch (err) {
    console.error(`bridge failed: ${err && err.message ? err.message : err}`);
  } finally {
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
    retry_events: retryEvents,
    captured_steps: [...capturedSteps],
    sse_capture: sseCapture,
    base_url: baseUrl,
    scenario: args.scenario,
    case: args.case,
  }) + "\n");
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
