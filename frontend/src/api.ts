import type {
  Attachment,
  CaseState,
  ChunkItem,
  Conversation,
  DocumentItem,
  Job,
  Message,
  SettingsPayload,
  Source,
  StatusPayload,
  User,
} from "./types";

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (!headers.has("Content-Type") && options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    onUnauthorized?.();
    throw new ApiError(401, "authentification requise");
  }
  if (!response.ok) {
    let detail = `erreur HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === "string") detail = payload.detail;
      else if (Array.isArray(payload?.detail)) detail = payload.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* corps non JSON */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export interface ChatHandlers {
  onMeta?: (payload: { message_id: string; conversation_id: string; model: string | null; demo: boolean }) => void;
  onStatus?: (payload: { state: string; label: string }) => void;
  onSources?: (payload: { status: string; sources: Source[]; diagnostics: Record<string, unknown> }) => void;
  onDelta?: (text: string) => void;
  onDone?: (payload: { message_id: string; status: string; model: string | null; demo: boolean; sources_count?: number }) => void;
  onError?: (payload: { message: string; retryable: boolean; message_id: string }) => void;
}

async function parseSse(response: Response, handlers: ChatHandlers, controller: AbortController) {
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError(500, "flux illisible");
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      let payload: any;
      try {
        payload = JSON.parse(data);
      } catch {
        continue;
      }
      if (event === "meta") handlers.onMeta?.(payload);
      else if (event === "status") handlers.onStatus?.(payload);
      else if (event === "sources") handlers.onSources?.(payload);
      else if (event === "delta") handlers.onDelta?.(payload.text ?? "");
      else if (event === "done") handlers.onDone?.(payload);
      else if (event === "error") handlers.onError?.(payload);
    }
  }
  void controller;
}

export function attachmentUrl(id: string, inline = false): string {
  return `/api/attachments/${id}/content${inline ? "?inline=1" : ""}`;
}

export function documentOriginalUrl(id: string, page?: number | null): string {
  const base = `/api/documents/${id}/original`;
  return page && page > 0 ? `${base}#page=${page}` : base;
}

export const api = {
  async login(email: string, password: string): Promise<{ user: User; csrf_token: string }> {
    const payload = await request<{ user: User; csrf_token: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setCsrfToken(payload.csrf_token);
    return payload;
  },
  async logout(): Promise<void> {
    await request("/api/auth/logout", { method: "POST", body: "{}" });
    setCsrfToken(null);
  },
  me: () => request<{ user: User; csrf_token: string }>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean; csrf_token: string }>("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  listConversations: () => request<{ conversations: Conversation[] }>("/api/conversations"),
  createConversation: (title?: string) =>
    request<Conversation>("/api/conversations", { method: "POST", body: JSON.stringify({ title: title ?? null }) }),
  getConversation: (id: string) => request<Conversation>(`/api/conversations/${id}`),
  renameConversation: (id: string, title: string) =>
    request<Conversation>(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteConversation: (id: string) => request<{ ok: boolean }>(`/api/conversations/${id}`, { method: "DELETE" }),
  patchCaseState: (id: string, case_state: Partial<CaseState>) =>
    request<{ case_state: CaseState }>(`/api/conversations/${id}/case_state`, {
      method: "PATCH",
      body: JSON.stringify(case_state),
    }),
  listAttachments: (conversationId: string) =>
    request<{ attachments: Attachment[] }>(`/api/conversations/${conversationId}/attachments`),
  uploadAttachment: (conversationId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Attachment>(`/api/conversations/${conversationId}/attachments`, { method: "POST", body: form });
  },
  deleteAttachment: (id: string) => request<{ ok: boolean }>(`/api/attachments/${id}`, { method: "DELETE" }),
  stopMessage: (id: string) => request<{ ok: boolean }>(`/api/messages/${id}/stop`, { method: "POST", body: "{}" }),
  search: (query: string, product?: string | null, version?: string | null) =>
    request<{ status: string; sources: Source[]; diagnostics: Record<string, unknown> }>("/api/search", {
      method: "POST",
      body: JSON.stringify({ query, product: product || null, version: version || null, scope: "all" }),
    }),
  status: () => request<StatusPayload>("/api/status"),
  listDocuments: (params: Record<string, string> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request<{ documents: DocumentItem[] }>(`/api/documents${qs ? `?${qs}` : ""}`);
  },
  importDocument: (form: FormData) => request<DocumentItem>("/api/documents", { method: "POST", body: form }),
  patchDocument: (id: string, patch: Record<string, unknown>) =>
    request<DocumentItem>(`/api/documents/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  reindexDocument: (id: string) =>
    request<{ job: Job; document: DocumentItem }>(`/api/documents/${id}/reindex`, { method: "POST", body: "{}" }),
  deleteDocument: (id: string) => request<{ ok: boolean }>(`/api/documents/${id}`, { method: "DELETE" }),
  documentChunks: (id: string, generation?: number) =>
    request<{ chunks: ChunkItem[]; generation: number; current_generation: number }>(
      `/api/documents/${id}/chunks${generation ? `?generation=${generation}` : ""}`,
    ),
  listJobs: (status?: string) => request<{ jobs: Job[] }>(`/api/jobs${status ? `?status=${status}` : ""}`),
  retryJob: (id: string) => request<{ job: Job }>(`/api/jobs/${id}/retry`, { method: "POST", body: "{}" }),
  getSettings: () => request<SettingsPayload>("/api/settings"),
  putSettings: (patch: Record<string, unknown>) =>
    request<SettingsPayload>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
  testProvider: () =>
    request<{ ok: boolean; error: string | null; latency_ms: number | null; model: string }>("/api/settings/test", {
      method: "POST",
      body: "{}",
    }),
  webStatus: () => request<{ available: boolean; reason: string | null }>("/api/web/status"),
};

export interface ChatStream {
  abort: () => void;
  done: Promise<void>;
}

export function streamChat(
  conversationId: string,
  text: string,
  attachmentIds: string[],
  handlers: ChatHandlers,
  retryMessageId?: string,
): ChatStream {
  const controller = new AbortController();
  const headers = new Headers({ "Content-Type": "application/json" });
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const path = retryMessageId
    ? `/api/messages/${retryMessageId}/retry`
    : `/api/conversations/${conversationId}/chat`;
  const body = retryMessageId ? "{}" : JSON.stringify({ text, attachment_ids: attachmentIds });
  const done = (async () => {
    try {
      const response = await fetch(path, {
        method: "POST",
        headers,
        body,
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (response.status === 401) {
        onUnauthorized?.();
        throw new ApiError(401, "authentification requise");
      }
      if (!response.ok) {
        let detail = `erreur HTTP ${response.status}`;
        try {
          const payload = await response.json();
          if (typeof payload?.detail === "string") detail = payload.detail;
        } catch {
          /* ignore */
        }
        throw new ApiError(response.status, detail);
      }
      await parseSse(response, handlers, controller);
    } catch (error) {
      if ((error as Error).name === "AbortError") return;
      throw error;
    }
  })();
  return { abort: () => controller.abort(), done };
}

export function getMessage(id: string) {
  return request<Message>(`/api/conversations/${id}/messages`);
}
