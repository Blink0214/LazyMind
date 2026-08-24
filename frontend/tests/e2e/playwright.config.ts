import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const now = new Date().toISOString();
const generatedRunId = [
  now.slice(0, 19).replace(/[-:]/g, "").replace("T", "-"),
  now.slice(20, 23),
].join("-");
const runId = process.env.AI_WRITER_E2E_RUN_ID || generatedRunId;
if (!/^[A-Za-z0-9_-]+$/.test(runId)) {
  throw new Error(`Invalid AI_WRITER_E2E_RUN_ID: ${runId}`);
}
process.env.AI_WRITER_E2E_RUN_ID = runId;
// 统一使用 writer-test 的 reports 目录约定（不再写仓库外 test-artifacts）。
const artifactRoot = path.resolve(
  repoRoot,
  "tests/e2e/writer-test/reports/ui",
  runId,
);

export default defineConfig({
  testDir: ".",
  testMatch: /writer_effect_ui\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  preserveOutput: "always",
  timeout: 40 * 60 * 1000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { outputFolder: path.join(artifactRoot, "playwright-report"), open: "never" }]],
  outputDir: path.join(artifactRoot, "playwright-results"),
  use: {
    baseURL: process.env.LAZYMIND_BASE_URL || "http://127.0.0.1:8090",
    headless: process.env.PLAYWRIGHT_HEADED !== "1",
    viewport: { width: 1920, height: 1080 },
    trace: "retain-on-failure",
    screenshot: "on",
    video: { mode: "on", size: { width: 1920, height: 1080 } },
  },
});
