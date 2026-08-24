import process from "node:process";
import { expect, type Page } from "@playwright/test";

type AuthSession = {
  token: string;
  refreshToken?: string;
  username: string;
  userId: string;
  role?: string;
  email?: string;
  displayName?: string;
  tenantId?: string;
  timestamp: number;
};

function isLoopbackBaseUrl(baseUrl: string): boolean {
  const hostname = new URL(baseUrl).hostname;
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

async function requestPasswordSession(page: Page, baseUrl: string): Promise<Partial<AuthSession> | undefined> {
  const useLocalDefaults = isLoopbackBaseUrl(baseUrl);
  const username = process.env.LAZYMIND_E2E_USERNAME?.trim() || (useLocalDefaults ? "admin" : "");
  const password = process.env.LAZYMIND_E2E_PASSWORD?.trim() || (useLocalDefaults ? "admin" : "");
  if (!username || !password) return undefined;
  const response = await page.request.post(new URL("/api/authservice/auth/login", baseUrl).toString(), {
    data: { username, password },
  });
  if (!response.ok()) return undefined;
  const payload = await response.json() as Record<string, unknown> & { data?: Record<string, unknown> };
  const login = payload.data || payload;
  const token = typeof login.access_token === "string" ? login.access_token : "";
  if (!token) return undefined;
  let userId = "";
  let displayName = "";
  try {
    const me = await page.request.get(new URL("/api/authservice/auth/me", baseUrl).toString(), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (me.ok()) {
      const mePayload = await me.json() as Record<string, unknown> & { data?: Record<string, unknown> };
      const meData = mePayload.data || mePayload;
      userId = typeof meData.user_id === "string" ? meData.user_id : "";
      displayName = typeof meData.display_name === "string" ? meData.display_name : "";
    }
  } catch {
    // Keep the configured LAZYMIND_USER_ID fallback when /me is unavailable.
  }
  return {
    token,
    refreshToken: typeof login.refresh_token === "string" ? login.refresh_token : undefined,
    username,
    role: typeof login.role === "string" ? login.role : undefined,
    tenantId: typeof login.tenant_id === "string" ? login.tenant_id : undefined,
    userId,
    displayName: displayName || undefined,
    timestamp: Date.now(),
  };
}

async function resolveAuthSession(page: Page): Promise<AuthSession> {
  const configuredUserId = process.env.LAZYMIND_USER_ID?.trim() || "";
  const baseUrl = process.env.LAZYMIND_BASE_URL?.trim() || "http://127.0.0.1:8090";
  let localSession: Partial<AuthSession> | undefined;
  let localSessionError = "";
  try {
    const response = await page.request.post(new URL("/_local/admin-session", baseUrl).toString());
    if (response.ok()) {
      const payload = await response.json() as { data?: Partial<AuthSession> } & Partial<AuthSession>;
      localSession = payload.data || payload;
    } else {
      localSessionError = `HTTP ${response.status()}`;
    }
  } catch (error) {
    localSessionError = error instanceof Error ? error.message : String(error);
  }

  if (!localSession?.token) {
    try {
      localSession = await requestPasswordSession(page, baseUrl);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      localSessionError = `${localSessionError || "local session unavailable"}; password login: ${detail}`;
    }
  }

  const token = localSession?.token?.trim() || process.env.LAZYMIND_ACCESS_TOKEN?.trim() || "";
  // In local mode the desktop proxy returns the current bootstrap-admin
  // identity; prefer it so tests follow a switched login without touching
  // env.local.sh. The configured LAZYMIND_USER_ID remains the fallback for
  // non-local deployments where the local session endpoint is unavailable.
  const userId = localSession?.userId?.trim() || configuredUserId || "";
  if (!token || !userId) {
    throw new Error(
      `Unable to obtain E2E auth automatically (${localSessionError || "empty local session"}); `
      + "configure LAZYMIND_USER_ID and, for non-local deployments, E2E credentials or LAZYMIND_ACCESS_TOKEN",
    );
  }

  // Python report/reset subprocesses use the same fresh credentials.
  process.env.LAZYMIND_ACCESS_TOKEN = token;
  process.env.LAZYMIND_USER_ID = userId;
  return {
    ...localSession,
    token,
    userId,
    username: localSession?.username || userId,
    timestamp: localSession?.timestamp || Date.now(),
  };
}

export async function installAuth(page: Page, conversationId = ""): Promise<void> {
  const authSession = await resolveAuthSession(page);
  await page.addInitScript(({ session, resumeConversationId }) => {
    localStorage.setItem("lazymind:user", JSON.stringify(session));
    sessionStorage.clear();
    if (resumeConversationId) {
      sessionStorage.setItem("chat_resume_conversation_id", resumeConversationId);
    }
  }, { session: authSession, resumeConversationId: conversationId });
}

export async function openWriterConversation(page: Page, conversationId: string) {
  await installAuth(page, conversationId);
  await page.goto("/agent/chat/home");
  const panel = page.locator(".workflow-panel[data-session-id]").first();
  await expect(panel).toBeVisible({ timeout: 180_000 });
  return panel;
}

export async function selectTextInFirstBlock(page: Page, selector: string): Promise<void> {
  const block = page.locator(selector).filter({ hasText: /\S/ }).first();
  await expect(block).toBeVisible();
  await block.evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const textNode = walker.nextNode();
    if (!textNode?.textContent?.trim()) throw new Error("selected block has no text node");
    const range = document.createRange();
    range.setStart(textNode, 0);
    range.setEnd(textNode, Math.min(textNode.textContent.length, 24));
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    document.dispatchEvent(new Event("selectionchange", { bubbles: true }));
  });
}
