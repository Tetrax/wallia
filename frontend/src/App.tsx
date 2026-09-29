import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, setCsrfToken, setUnauthorizedHandler, streamChat, type ChatStream } from "./api";
import { ChatView, type StreamingState } from "./components/Chat";
import { LibraryView } from "./components/Library";
import { Login } from "./components/Login";
import { CasePanel, SourcesPanel } from "./components/Panels";
import { Sidebar } from "./components/Sidebar";
import { SettingsView } from "./components/Settings";
import type { Attachment, CaseState, Conversation, Message, Source, StatusPayload, User, WebFallbackMeta } from "./types";
import { Badge, Spinner, ToastHost, toast } from "./ui";

interface Streaming extends StreamingState {
  conversationId: string;
  /** Identité du flux : un finaliseur/callback d'un flux ANCIEN ne doit jamais
   *  toucher l'état d'un flux NOUVEAU (autre session ou autre flux). */
  streamId: number;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [streaming, setStreaming] = useState<Streaming | null>(null);
  // Statut web observé par message, pour la session en cours uniquement :
  // après un reload, seules les sources persistées (provenance web) subsistent
  // — aucun statut opérationnel historique n'est inventé.
  const [messageWeb, setMessageWeb] = useState<Record<string, WebFallbackMeta>>({});
  const [sourcesPanel, setSourcesPanel] = useState<{ open: boolean; sources: Source[]; status: string | null; highlighted: number | null }>(
    { open: false, sources: [], status: null, highlighted: null },
  );
  const [caseOpen, setCaseOpen] = useState(false);
  const [view, setView] = useState<"chat" | "library" | "settings">("chat");
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Flux actif : identité (seq) + handle d'abandon, remplacés ensemble.
  const streamRef = useRef<{ seq: number; stream: ChatStream } | null>(null);
  const streamSeqRef = useRef(0);
  const currentIdRef = useRef<string | null>(null);
  // Génération de session : toute réponse asynchrone arrivée après un logout
  // (ou une expiration) est ignorée, jamais réappliquée à l'écran suivant.
  const sessionRef = useRef(0);
  // Dernière ouverture demandée : une réponse tardive pour A ne peut pas
  // écraser l'affichage du cas B.
  const openRequestRef = useRef(0);
  // Statut web du flux actif : synchronisé DIRECTEMENT par runStream et ses
  // callbacks validés (jamais par un render — des callbacks regroupés dans un
  // même chunk réseau peuvent précéder tout render), puis consommé et effacé
  // par le finaliseur du BON flux uniquement.
  const streamingWebRef = useRef<{ streamId: number; messageId: string | null; web: WebFallbackMeta | null } | null>(null);

  currentIdRef.current = current?.id ?? null;

  const refreshStatus = useCallback(async () => {
    const generation = sessionRef.current;
    try {
      const payload = await api.status();
      if (generation !== sessionRef.current) return;
      setStatus(payload);
    } catch {
      /* statut non bloquant */
    }
  }, []);

  const refreshConversations = useCallback(async () => {
    const generation = sessionRef.current;
    try {
      const response = await api.listConversations();
      if (generation !== sessionRef.current) return;
      setConversations(response.conversations);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        toast("error", "Liste des conversations indisponible.");
      }
    }
  }, []);

  const openConversation = useCallback(async (id: string) => {
    const generation = sessionRef.current;
    const request = ++openRequestRef.current;
    if (id !== currentIdRef.current) {
      // Changement de cas : les panneaux du cas précédent sont vidés
      // IMMÉDIATEMENT (jamais des sources ou un état de cas de A sur B).
      setSourcesPanel({ open: false, sources: [], status: null, highlighted: null });
      setCaseOpen(false);
    }
    try {
      const conversation = await api.getConversation(id);
      if (generation !== sessionRef.current || request !== openRequestRef.current) return;
      setCurrent(conversation);
    } catch (error) {
      if (generation === sessionRef.current && request === openRequestRef.current) {
        toast("error", `Conversation illisible : ${(error as Error).message}`);
      }
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      // Expiration de session : tout ce qui appartient à la session précédente
      // est vidé (flux en cours compris), jamais réutilisé par la suivante.
      sessionRef.current += 1;
      openRequestRef.current += 1;
      streamSeqRef.current += 1; // invalide tout callback/finaliseur de l'ancien flux
      streamRef.current?.stream.abort();
      streamRef.current = null;
      streamingWebRef.current = null;
      setStreaming(null);
      setMessageWeb({});
      setSourcesPanel({ open: false, sources: [], status: null, highlighted: null });
      setCaseOpen(false);
      setSidebarOpen(false);
      setView("chat");
      setUser(null);
      setCurrent(null);
      setConversations([]);
      setStatus(null);
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

  const finalizeStream = useCallback(
    async (streamId: number, conversationId?: string) => {
      // Un finaliseur ANCIEN ne coupe jamais un flux NOUVEAU ni ne reprend la
      // main sur l'écran : il ne s'applique que s'il correspond encore au flux
      // actif, et jamais au-delà de sa génération de session.
      const generation = sessionRef.current;
      if (streamRef.current?.seq !== streamId) return;
      streamRef.current = null;
      // Le statut web observé pendant CE flux est conservé pour SON message,
      // en état local de session : la fin du flux ne le perd pas (après un
      // reload, seule la provenance persistée subsiste).
      const observed = streamingWebRef.current;
      if (observed && observed.streamId === streamId) {
        // Le bon flux a fini : sa ref est consommée puis effacée — jamais
        // celle d'un flux plus récent.
        streamingWebRef.current = null;
        const { messageId, web: observedWeb } = observed;
        if (messageId && observedWeb) {
          setMessageWeb((previous) => ({ ...previous, [messageId]: observedWeb }));
        }
      }
      setStreaming((previous) => (previous && previous.streamId === streamId ? null : previous));
      await refreshConversations();
      if (generation !== sessionRef.current) return;
      // La fin du flux ne concerne QUE la conversation streamée : un autre cas
      // affiché n'est jamais écrasé, et l'historique partiel du cas streamé est
      // rechargé depuis le serveur (jamais perdu à l'écran).
      const id = conversationId ?? currentIdRef.current;
      if (id && currentIdRef.current === id) await openConversation(id);
      await refreshStatus();
    },
    [openConversation, refreshConversations, refreshStatus],
  );

  const runStream = useCallback(
    (conversationId: string, text: string, attachmentIds: string[], retryMessageId?: string) => {
      const generation = sessionRef.current;
      const seq = ++streamSeqRef.current;
      // Statut web du flux : synchronisé DIRECTEMENT (ici puis dans les
      // callbacks validés), jamais dépendant d'un render — des callbacks
      // regroupés dans un même chunk réseau peuvent précéder tout render.
      streamingWebRef.current = { streamId: seq, messageId: null, web: null };
      // Les événements d'un flux révolu (nouvelle session, nouveau flux) sont
      // ignorés : jamais appliqués au flux ou à l'écran suivants.
      const isCurrent = () => streamRef.current?.seq === seq && sessionRef.current === generation;
      setStreaming((previous) => {
        if (streamRef.current && !isCurrent()) return previous;
        return {
          conversationId,
          streamId: seq,
          messageId: null,
          content: "",
          sources: [],
          sourcesStatus: null,
          web: null,
          statusLabel: "préparation",
          demo: status ? !status.provider.key_configured : true,
          error: null,
        };
      });
      const stream = streamChat(
        conversationId,
        text,
        attachmentIds,
        {
          onMeta: (payload) => {
            if (!isCurrent()) return;
            const observed = streamingWebRef.current;
            if (observed && observed.streamId === seq) observed.messageId = payload.message_id;
            setStreaming((previous) =>
              previous && previous.streamId === seq
                ? { ...previous, messageId: payload.message_id, demo: payload.demo }
                : previous,
            );
          },
          onStatus: (payload) => {
            if (!isCurrent()) return;
            setStreaming((previous) =>
              previous && previous.streamId === seq ? { ...previous, statusLabel: payload.label } : previous,
            );
          },
          onSources: (payload) => {
            if (!isCurrent()) return;
            const observed = streamingWebRef.current;
            if (observed && observed.streamId === seq) {
              // Même règle que l'état : la valeur observée gagne, une frame
              // ultérieure sans champ `web` ne l'efface pas.
              observed.web = payload.web ?? observed.web;
            }
            setStreaming((previous) =>
              previous && previous.streamId === seq
                ? {
                    ...previous,
                    sources: payload.sources,
                    sourcesStatus: payload.status,
                    // Le statut web est conservé : la valeur observée gagne,
                    // une frame ultérieure sans champ `web` ne l'efface pas.
                    web: payload.web ?? previous.web,
                  }
                : previous,
            );
          },
          onDelta: (delta) => {
            if (!isCurrent()) return;
            setStreaming((previous) =>
              previous && previous.streamId === seq ? { ...previous, content: previous.content + delta } : previous,
            );
          },
          onError: (payload) => {
            if (!isCurrent()) return;
            setStreaming((previous) =>
              previous && previous.streamId === seq ? { ...previous, error: payload.message } : previous,
            );
          },
        },
        retryMessageId,
      );
      streamRef.current = { seq, stream };
      stream.done
        .then(() => finalizeStream(seq, conversationId))
        .catch((error: unknown) => {
          if (isCurrent()) {
            if (error instanceof ApiError) toast("error", `Génération impossible : ${error.detail}`);
            else toast("error", "Génération interrompue (réseau).");
          }
          void finalizeStream(seq, conversationId);
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
    const activeStream = streamRef.current;
    // On n'arrête QUE le flux actif observé, jamais un flux plus récent.
    if (activeStream && active && activeStream.seq === active.streamId) {
      activeStream.stream.abort();
    }
    if (active?.messageId) {
      void api.stopMessage(active.messageId).catch(() => undefined);
    }
    setStreaming((previous) =>
      previous && previous.streamId === active?.streamId ? { ...previous, statusLabel: "arrêt demandé…" } : previous,
    );
  }, [streaming]);

  const handleCreateConversation = useCallback(async () => {
    const generation = sessionRef.current;
    // Une création de cas invalide les ouvertures en vol et vide les panneaux :
    // le nouveau cas vide ne réutilise ni sources ni état de cas affichés.
    const openSeq = ++openRequestRef.current;
    setSourcesPanel({ open: false, sources: [], status: null, highlighted: null });
    setCaseOpen(false);
    try {
      const conversation = await api.createConversation();
      if (generation !== sessionRef.current) return;
      setConversations((previous) => [conversation, ...previous]);
      // Réponse tardive : si l'utilisateur a ouvert un autre cas PENDANT la
      // création, cette ouverture plus récente n'est jamais écrasée. Le
      // nouveau cas reste dans la liste, simplement non affiché.
      if (openRequestRef.current !== openSeq) return;
      setCurrent({ ...conversation, messages: [], attachments: [] });
      setView("chat");
    } catch (error) {
      if (generation !== sessionRef.current) return;
      toast("error", `Création impossible : ${(error as Error).message}`);
    }
  }, []);

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      const target = conversations.find((conversation) => conversation.id === id);
      if (!window.confirm(`Supprimer la conversation « ${target?.title ?? id} » et ses pièces jointes ?`)) return;
      try {
        await api.deleteConversation(id);
        // Le cas supprimé n'a plus de flux ni de panneaux : rien ne doit rester
        // accroché (pending fantôme), et le flux éventuel est réellement arrêté.
        if (streaming?.conversationId === id) {
          streamRef.current?.stream.abort();
          streamRef.current = null;
          if (streamingWebRef.current?.streamId === streaming?.streamId) streamingWebRef.current = null;
          setStreaming(null);
        }
        if (sourcesPanel.open && current?.id === id) {
          setSourcesPanel({ open: false, sources: [], status: null, highlighted: null });
        }
        setConversations((previous) => previous.filter((conversation) => conversation.id !== id));
        if (current?.id === id) setCurrent(null);
        toast("success", "Conversation supprimée.");
      } catch (error) {
        toast("error", `Suppression impossible : ${(error as Error).message}`);
      }
    },
    [conversations, current, sourcesPanel.open, streaming],
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
      const targetId = current.id;
      const generation = sessionRef.current;
      try {
        const attachment = await api.uploadAttachment(targetId, file);
        if (generation !== sessionRef.current) return null;
        setCurrent((previous) =>
          // La pièce jointe appartient au cas ciblé au moment de l'envoi :
          // une réponse tardive ne doit pas se rattacher au cas actuellement ouvert.
          previous && previous.id === targetId
            ? { ...previous, attachments: [...(previous.attachments ?? []), attachment] }
            : previous,
        );
        return attachment;
      } catch (error) {
        if (generation !== sessionRef.current) return null;
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

  const handleCaseSaved = useCallback(
    (conversationId: string, state: CaseState) => {
      // Une réponse de sauvegarde pour A n'est appliquée que si A est toujours
      // le cas affiché (jamais appliquée à l'écran de B).
      setCurrent((previous) => (previous && previous.id === conversationId ? { ...previous, case_state: state } : previous));
      void refreshConversations();
    },
    [refreshConversations],
  );

  const handleLogout = useCallback(async () => {
    sessionRef.current += 1;
    openRequestRef.current += 1;
    streamSeqRef.current += 1; // tout callback/finaliseur en vol est invalidé
    try {
      await api.logout();
    } catch {
      /* la session est peut-être déjà expirée */
    }
    streamRef.current?.stream.abort();
    streamRef.current = null;
    streamingWebRef.current = null;
    setStreaming(null);
    setMessageWeb({});
    setSourcesPanel({ open: false, sources: [], status: null, highlighted: null });
    setCaseOpen(false);
    setSidebarOpen(false);
    setView("chat");
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
          {view !== "chat" ? (
            <button
              type="button"
              className="btn btn-ghost btn-icon hide-desktop"
              onClick={() => setSidebarOpen(true)}
              aria-label="Ouvrir le menu"
            >
              ☰
            </button>
          ) : null}
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
            messageWeb={messageWeb}
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
