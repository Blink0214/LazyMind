import process from "node:process";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWriterConversation, selectTextInFirstBlock } from "./writer_ui_helpers";

const WRITER_UI = {
  rewriteForm: ".artifact-rewrite-form",
  rewriteInput: ".artifact-rewrite-form__input",
  rewriteSubmit: ".artifact-rewrite-form__submit",
  rewriteDiff: ".artifact-rewrite-inline-diff__overlay",
  rewriteRemoved: ".artifact-rewrite-inline-diff__removed",
  rewriteAdded: ".artifact-rewrite-inline-diff__added",
  rewriteApply: ".artifact-rewrite-inline-diff__apply",
  markdownParagraph: '.writer-markdown-editor__surface .mdxeditor-root-contenteditable [contenteditable="true"] p',
  markdownRewrite: ".artifact-rewrite-selection-action",
  irEditor: ".writer-ir__document--editable",
  irParagraph: '.writer-ir__document--editable [data-writer-block][data-node-id][data-node-type="paragraph"]',
  irRewrite: ".writer-ir__format-button--rewrite",
  writeBackStatus: ".workflow-slot__write-back-status",
} as const;

const WRITER_API = {
  actionPreview: ":action-preview",
  draftCheckpoint: "/slots/draft_document/items/idx/",
  writeBack: "/writer-document:write-back",
} as const;

async function openFinalDraft(page: Page, conversationId: string): Promise<Locator> {
  const panel = await openWriterConversation(page, conversationId);
  const tabs = panel.locator('[role="tab"]');
  if (await tabs.count()) await tabs.last().click();
  return panel;
}

async function applySelectionRewrite(
  page: Page,
  panel: Locator,
  conversationId: string,
  blockSelector: string,
  actionSelector: string,
): Promise<void> {
  const normalize = (value: string) => value.replace(/\s+/g, ' ').trim();
  const selection = await selectTextInFirstBlock(panel, blockSelector);
  expect(normalize(selection.selectedText)).not.toBe('');
  expect(normalize(selection.blockText)).toContain(normalize(selection.selectedText));
  const action = page.locator(actionSelector).first();
  await expect(action).toBeEnabled();
  await action.click();

  const dialog = page.locator(WRITER_UI.rewriteForm);
  await expect(dialog).toBeVisible();
  await dialog.locator(WRITER_UI.rewriteInput).fill('保持原意，把选中内容改得更简洁自然');
  const previewResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().includes(WRITER_API.actionPreview)
  ), { timeout: 180_000 });
  await dialog.locator(WRITER_UI.rewriteSubmit).click();
  expect((await previewResponse).ok()).toBeTruthy();

  const diff = page.locator(WRITER_UI.rewriteDiff).first();
  await expect(diff).toBeVisible({ timeout: 180_000 });
  const removedParts = (await diff.locator(
    WRITER_UI.rewriteRemoved,
  ).allTextContents()).map(normalize).filter(Boolean);
  const addedParts = (await diff.locator(
    WRITER_UI.rewriteAdded,
  ).allTextContents()).map(normalize).filter(Boolean);
  expect(removedParts.length).toBeGreaterThan(0);
  expect(addedParts.length).toBeGreaterThan(0);
  expect(addedParts.join(' ')).not.toBe(removedParts.join(' '));
  const meaningfulAddedParts = addedParts.filter((part) => part.length >= 2);
  expect(meaningfulAddedParts.length).toBeGreaterThan(0);

  const apply = diff.locator(WRITER_UI.rewriteApply);
  await expect(apply).toBeVisible({ timeout: 180_000 });
  const checkpointResponse = page.waitForResponse((response) => (
    response.request().method() === 'PATCH'
    && response.url().includes(WRITER_API.draftCheckpoint)
  ), { timeout: 180_000 });
  await apply.click();
  expect((await checkpointResponse).ok()).toBeTruthy();
  await expect(apply).toBeHidden({ timeout: 30_000 });
  await expect(panel).toHaveClass(/workflow-panel--completed/);

  const appliedBlock = panel.locator(blockSelector).filter({ hasText: /\S/ }).first();
  await expect.poll(async () => normalize(await appliedBlock.innerText())).not.toBe(
    normalize(selection.blockText),
  );
  const appliedText = normalize(await appliedBlock.innerText());
  for (const added of meaningfulAddedParts) {
    expect(appliedText).toContain(added);
  }

  const reopenedPanel = await openFinalDraft(page, conversationId);
  const persistedBlock = reopenedPanel.locator(blockSelector).filter({ hasText: /\S/ }).first();
  await expect(persistedBlock).toBeVisible();
  await expect.poll(async () => normalize(await persistedBlock.innerText())).toBe(appliedText);
}

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  const configuredConversationIds = [
    process.env.AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID,
    process.env.AI_WRITER_IR_EFFECT_CONVERSATION_ID,
    process.env.AI_WRITER_WRITE_BACK_CONVERSATION_ID,
    process.env.AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID,
  ].filter((value) => value?.trim());
  if (configuredConversationIds.length === 0) {
    throw new Error(
      'No Writer effect conversation IDs are configured; refusing to report a zero-execution success',
    );
  }
});

test('Markdown selection preview and checkpoint through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_MARKDOWN_EFFECT_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  await applySelectionRewrite(
    page,
    panel,
    conversationId!,
    WRITER_UI.markdownParagraph,
    WRITER_UI.markdownRewrite,
  );
});

test('IR selection preview and checkpoint through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_IR_EFFECT_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_IR_EFFECT_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  await applySelectionRewrite(
    page,
    panel,
    conversationId!,
    WRITER_UI.irParagraph,
    WRITER_UI.irRewrite,
  );
});

test('manual IR edit is saved before write-back through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_WRITE_BACK_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_WRITE_BACK_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  const editor = panel.locator(WRITER_UI.irEditor);
  await expect(editor).toBeVisible();
  const block = editor.locator('[data-writer-block][data-node-id][data-node-type="paragraph"]')
    .filter({ hasText: /\S/ })
    .first();
  await block.click();
  await page.keyboard.press('End');
  await page.keyboard.type('（UI手动编辑验证）');

  const saveResponse = page.waitForResponse((response) => (
    response.request().method() === 'PATCH'
    && response.url().includes(WRITER_API.draftCheckpoint)
  ), { timeout: 180_000 });
  await page.keyboard.press('ControlOrMeta+S');
  expect((await saveResponse).ok()).toBeTruthy();

  const writeBack = panel.getByRole('button', { name: /写回飞书|Write back/i }).first();
  await expect(writeBack).toBeEnabled();
  const writeBackResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().includes(WRITER_API.writeBack)
  ), { timeout: 180_000 });
  await writeBack.click();
  const response = await writeBackResponse;
  expect(response.ok()).toBeTruthy();
  const envelope = await response.json();
  expect(envelope?.data?.status).toBe('synced');
  await expect(panel.locator(WRITER_UI.writeBackStatus)).toBeVisible();
});

test('Markdown draft can create or update a Feishu document through UI', async ({ page }) => {
  const conversationId = process.env.AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID?.trim();
  test.skip(!conversationId, 'AI_WRITER_MARKDOWN_WRITE_BACK_CONVERSATION_ID is not configured');
  const panel = await openFinalDraft(page, conversationId!);
  const writeBack = panel.getByRole('button', { name: /写回飞书|Write back/i }).first();
  await expect(writeBack).toBeEnabled();
  const responsePromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().includes(WRITER_API.writeBack)
  ), { timeout: 180_000 });
  await writeBack.click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  const envelope = await response.json();
  expect(envelope?.data?.status).toBe('synced');
  expect(envelope?.data?.feishu_synced).toBe(true);
  expect(envelope?.data?.artifact_saved).toBe(true);
});
