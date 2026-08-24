import process from "node:process";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWriterConversation, selectTextInFirstBlock } from "./writer_ui_helpers";

async function openFinalDraft(page: Page, conversationId: string): Promise<Locator> {
  const panel = await openWriterConversation(page, conversationId);
  const tabs = panel.locator('[role="tab"]');
  if (await tabs.count()) await tabs.last().click();
  return panel;
}

async function applySelectionRewrite(
  page: Page,
  panel: Locator,
  blockSelector: string,
  actionSelector: string,
): Promise<void> {
  await selectTextInFirstBlock(page, blockSelector);
  const action = page.locator(actionSelector).first();
  await expect(action).toBeEnabled();
  await action.click();

  const dialog = page.locator('.artifact-rewrite-form');
  await expect(dialog).toBeVisible();
  await dialog.locator('.artifact-rewrite-form__input').fill('保持原意，把选中内容改得更简洁自然');
  const previewResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' && response.url().includes(':action-preview')
  ), { timeout: 180_000 });
  await dialog.locator('.artifact-rewrite-form__submit').click();
  expect((await previewResponse).ok()).toBeTruthy();

  const apply = page.locator('.artifact-rewrite-inline-diff__apply');
  await expect(apply).toBeVisible({ timeout: 180_000 });
  const checkpointResponse = page.waitForResponse((response) => (
    response.request().method() === 'PATCH'
    && response.url().includes('/slots/draft_document/items/idx/')
  ), { timeout: 180_000 });
  await apply.click();
  expect((await checkpointResponse).ok()).toBeTruthy();
  await expect(apply).toBeHidden({ timeout: 30_000 });
  await expect(panel).toHaveClass(/workflow-panel--completed/);
}

test.describe.configure({ mode: 'serial' });

test('legacy Markdown selection preview and checkpoint through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  await applySelectionRewrite(
    page,
    panel,
    '.writer-markdown-editor__surface .mdxeditor-root-contenteditable [contenteditable="true"] p',
    '.artifact-rewrite-selection-action',
  );
});

test('legacy IR selection preview and checkpoint through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_IR_EFFECT_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_IR_EFFECT_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  await applySelectionRewrite(
    page,
    panel,
    '.writer-ir__document--editable [data-writer-block][data-node-id][data-node-type="paragraph"]',
    '.writer-ir__format-button--rewrite',
  );
});

test('legacy manual IR edit is saved before write-back through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_WRITE_BACK_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_WRITE_BACK_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  const editor = panel.locator('.writer-ir__document--editable');
  await expect(editor).toBeVisible();
  const block = editor.locator('[data-writer-block][data-node-id][data-node-type="paragraph"]')
    .filter({ hasText: /\S/ })
    .first();
  await block.click();
  await page.keyboard.press('End');
  await page.keyboard.type('（UI手动编辑验证）');

  const saveResponse = page.waitForResponse((response) => (
    response.request().method() === 'PATCH'
    && response.url().includes('/slots/draft_document/items/idx/')
  ), { timeout: 180_000 });
  await page.keyboard.press('ControlOrMeta+S');
  expect((await saveResponse).ok()).toBeTruthy();

  const writeBack = panel.getByRole('button', { name: /写回飞书|Write back/i }).first();
  await expect(writeBack).toBeEnabled();
  const writeBackResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' && response.url().includes('/writer-document:write-back')
  ), { timeout: 180_000 });
  await writeBack.click();
  const response = await writeBackResponse;
  expect(response.ok()).toBeTruthy();
  const envelope = await response.json();
  expect(envelope?.data?.status).toBe('synced');
  await expect(panel.locator('.workflow-slot__write-back-status')).toBeVisible();
});

test('legacy Markdown draft can create or update a Feishu document through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  const writeBack = panel.getByRole('button', { name: /写回飞书|Write back/i }).first();
  await expect(writeBack).toBeEnabled();
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST' && response.url().includes('/writer-document:write-back')
  ), { timeout: 180_000 });
  await writeBack.click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  const envelope = await response.json();
  expect(envelope?.data?.status).toBe('synced');
  expect(envelope?.data?.feishu_synced).toBe(true);
  expect(envelope?.data?.artifact_saved).toBe(true);
});
