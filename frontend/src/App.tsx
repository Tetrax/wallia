import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, setCsrfToken, setUnauthorizedHandler, streamChat, type ChatStream } from "./api";
import { ChatView, type StreamingState } from "./components/Chat";
import { LibraryView } from "./components/Library";
import { Login } from "./components/Login";
import { CasePanel, SourcesPanel } from "./components/Panels";
import { Sidebar } from "./components/Sidebar";
import { SettingsView } from "./components/Settings";
import type { Attachment, CaseState, Conversation, Message, Source, StatusPayload, User } from "./types";
import { Badge, Spinner, ToastHost, toast } from "./ui";

interface Streaming extends StreamingState {
  conversationId: string;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [streaming, setStreaming] = useState<Streaming | null>(null);
  const [sourcesPanel, setSourcesPanel] = useState<{ open: boolean; sources: Source[]; status: string | null; highlighted: number | null }>(
    { open: false, sources: [], status: null, highlighted: null },
  );
  const [caseOpen, setCaseOpen] = useState(false);
  const [view, setView] = useState<"chat" | "library" | "settings">("chat");
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const streamRef = useRef<ChatStream | null>(null);
  const currentIdRef = useRef<string | null>(null);

  currentIdRef.current = current?.id ?? null;

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
    } catch {
      /* statut non bloquant */
    }
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      const response = await api.listConversations();
      setConversations(response.conversations);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        toast("error", "Liste des conversations indisponible.");
      }
    }
  }, []);

  const openConversation = useCallback(async (id: string) => {
    try {
      const conversation = await api.getConversation(id);
      setCurrent(conversation);
    } catch (error) {
      toast("error", `Conversation illisible : ${(error as Error).message}`);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      setCurrent(null);
      setConversations([]);
    });
    api
      .me()
      .then((payload) => {
        setCsrfToken(payload.csrf_token);
        setUser(payload.user);
      })
      .catch(() => setUser(null))
      .finally(() => setBooting(false));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    if (!user) return;
    void refreshConversations();
    void refreshStatus();
    const timer = setInterval(() => void refreshStatus(), 60000);
    return () => clearInterval(timer);
  }, [user, refreshConversations, refreshStatus]);

  const finalizeStream = useCallback(async () => {
    setStreaming(null);
    streamRef.current = null;
    await refreshConversations();
    const id = currentIdRef.current;
    if (id) await openConversation(id);
    await refreshStatus();
  }, [openConversation, refreshConversations, refreshStatus]);

  const runStream = useCallback(
    (conversationId: string, text: string, attachmentIds: string[], retryMessageId?: string) => {
      setStreaming({
        conversationId,
        messageId: null,
        content: "",
        sources: [],
        sourcesStatus: null,
        statusLabel: "préparation",
        demo: status ? !status.provider.key_configured : true,
        error: null,
      });
      const stream = streamChat(
        conversationId,
        text,
        attachmentIds,
        {
          onMeta: (payload) =>
            setStreaming((previous) =>
              previous ? { ...previous, messageId: payload.message_id, demo: payload.demo } : previous,
            ),
          onStatus: (payload) =>
            setStreaming((previous) => (previous ? { ...previous, statusLabel: payload.label } : previous)),
          onSources: (payload) =>
            setStreaming((previous) =>
              previous ? { ...previous, sources: payload.sources, sourcesStatus: payload.status } : previous,
            ),
          onDelta: (delta) =>
            setStreaming((previous) => (previous ? { ...previous, content: previous.content + delta } : previous)),
          onError: (payload) =>
            setStreaming((previous) => (previous ? { ...previous, error: payload.message } : previous)),
        },
        retryMessageId,
      );
      streamRef.current = stream;
      stream.done
        .then(() => finalizeStream())
        .catch((error: unknown) => {
          if (error instanceof ApiError) toast("error", `Génération impossible : ${error.detail}`);
          else toast("error", "Génération interrompue (réseau).");
          void finalizeStream();
        });
    },
    [finalizeStream, status],
  );

  const handleSend = useCallback(
    (text: string, attachmentIds: string[]) => {
      if (!current || streaming) return;
      const optimistic: Message = {
        id: `local-${Date.now()}`,
        conversation_id: current.id,
        seq: 9999,
        role: "user",
        content: text,
        status: "complete",
        error: null,
        model: null,
        demo: false,
        sources: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      setCurrent({ ...current, messages: [...(current.messages ?? []), optimistic] });
      runStream(current.id, text, attachmentIds);
    },
    [current, runStream, streaming],
  );

  const handleRetry = useCallback(
    (messageId: string) => {
      if (!current || streaming) return;
      runStream(current.id, "", [], messageId);
    },
    [current, runStream, streaming],
  );

  const handleStop = useCallback(() => {
    const active = streaming;
    streamRef.current?.abort();
    if (active?.messageId) {
      void api.stopMessage(active.messageId).catch(() => undefined);
    }
    setStreaming((previous) => (previous ? { ...previous, statusLabel: "arrêt demandé…" } : previous));
  }, [streaming]);

  const handleCreateConversation = useCallback(async () => {
    try {
      const conversation = await api.createConversation();
      setConversations((previous) => [conversation, ...previous]);
      setCurrent({ ...conversation, messages: [], attachments: [] });
      setView("chat");
    } catch (error) {
      toast("error", `Création impossible : ${(error as Error).message}`);
    }
  }, []);

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      const target = conversations.find((conversation) => conversation.id === id);
      if (!window.confirm(`Supprimer la conversation « ${target?.title ?? id} » et ses pièces jointes ?`)) return;
      try {
        await api.deleteConversation(id);
        setConversations((previous) => previous.filter((conversation) => conversation.id !== id));
        if (current?.id === id) setCurrent(null);
        toast("success", "Conversation supprimée.");
      } catch (error) {
        toast("error", `Suppression impossible : ${(error as Error).message}`);
      }
    },
    [conversations, current],
  );

  const handleRename = useCallback(async (id: string, title: string) => {
    try {
      const updated = await api.renameConversation(id, title);
      setConversations((previous) => previous.map((conversation) => (conversation.id === id ? { ...conversation, ...updated } : conversation)));
      setCurrent((previous) => (previous && previous.id === id ? { ...previous, title: updated.title } : previous));
    } catch (error) {
      toast("error", `Renommage impossible : ${(error as Error).message}`);
    }
  }, []);

  const handleUpload = useCallback(
    async (file: File): Promise<Attachment | null> => {
      if (!current) return null;
      try {
        const attachment = await api.uploadAttachment(current.id, file);
        setCurrent((previous) =>
          previous ? { ...previous, attachments: [...(previous.attachments ?? []), attachment] } : previous,
        );
        return attachment;
      } catch (error) {
        toast("error", `Pièce jointe refusée : ${(error as Error).message}`);
        return null;
      }
    },
    [current],
  );

  const handleDeleteAttachment = useCallback(async (id: string) => {
    try {
      await api.deleteAttachment(id);
      setCurrent((previous) =>
        previous ? { ...previous, attachments: (previous.attachments ?? []).filter((item) => item.id !== id) } : previous,
      );
    } catch (error) {
      toast("error", `Suppression impossible : ${(error as Error).message}`);
    }
  }, []);

  const handleCaseSaved = useCallback((state: CaseState) => {
    setCurrent((previous) => (previous ? { ...previous, case_state: state } : previous));
    void refreshConversations();
  }, [refreshConversations]);

  const handleLogout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* la session est peut-être déjà expirée */
    }
    setUser(null);
    setCurrent(null);
    setConversations([]);
    setStatus(null);
  }, []);

  if (booting) {
    return (
      <div className="boot-screen">
        <Spinner label="Chargement de Wallia…" />
      </div>
    );
  }

  if (!user) {
    return (
      <>
        <Login
          onAuthenticated={(authenticatedUser) => {
            setUser(authenticatedUser);
          }}
        />
        <ToastHost />
      </>
    );
  }

  const streamingForCurrent = streaming && current && streaming.conversationId === current.id ? streaming : null;

  return (
    <div className="app-shell">
      <Sidebar
        user={user}
        conversations={conversations}
        currentId={current?.id ?? null}
        view={view}
        open={sidebarOpen}
        onSelect={(id) => void openConversation(id)}
        onCreate={() => void handleCreateConversation()}
        onRename={(id, title) => void handleRename(id, title)}
        onDelete={(id) => void handleDeleteConversation(id)}
        onView={setView}
        onLogout={() => void handleLogout()}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="main">
        <div className="global-strip">
          <Badge tone="demo" title="Le corpus documentaire est synthétique et ne constitue aucune documentation constructeur.">
            Corpus démo non officiel — ne pas utiliser comme procédure WALLIX
          </Badge>
          <span className="grow" />
          {status ? (
            <span className="muted small">
              {status.embedding.model.split("/").pop()} · {status.app.env}
              {status.jobs.worker.alive ? "" : " · worker inactif"}
            </span>
          ) : null}
        </div>

        {view === "chat" ? (
          <ChatView
            conversation={current}
            messages={current?.messages ?? []}
            attachments={current?.attachments ?? []}
            streaming={streamingForCurrent}
            providerConfigured={Boolean(status?.provider.key_configured)}
            onSend={handleSend}
            onStop={handleStop}
            onRetry={handleRetry}
            onOpenSources={(sources, _messageId, sourceStatus, highlight) =>
              setSourcesPanel({ open: true, sources, status: sourceStatus, highlighted: highlight ?? null })
            }
            onOpenCase={() => setCaseOpen(true)}
            onUpload={handleUpload}
            onDeleteAttachment={(id) => void handleDeleteAttachment(id)}
            onCreateConversation={() => void handleCreateConversation()}
            onOpenSidebar={() => setSidebarOpen(true)}
          />
        ) : view === "library" ? (
          <div className="view-scroll">
            <LibraryView />
          </div>
        ) : (
          <div className="view-scroll">
            <SettingsView status={status} onRefreshStatus={() => void refreshStatus()} onCsrfRotated={() => void api.me().then((payload) => setCsrfToken(payload.csrf_token))} />
          </div>
        )}
      </main>

      <SourcesPanel
        open={sourcesPanel.open}
        sources={sourcesPanel.sources}
        status={sourcesPanel.status}
        highlighted={sourcesPanel.highlighted}
        onClose={() => setSourcesPanel({ open: false, sources: [], status: null, highlighted: null })}
      />
      <CasePanel open={caseOpen} conversation={current} onClose={() => setCaseOpen(false)} onSaved={handleCaseSaved} />
      <ToastHost />
    </div>
  );
}
