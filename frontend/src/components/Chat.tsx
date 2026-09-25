import { useEffect, useRef, useState } from "react";
import type { Attachment, Conversation, Message, Source } from "../types";
import { attachmentUrl } from "../api";
import { Markdown } from "../markdown";
import { Badge, CopyButton, EmptyState, Spinner, formatBytes, formatDate } from "../ui";
import { Alert, Paperclip, Refresh, Send, Stop } from "../icons";

export interface StreamingState {
  messageId: string | null;
  content: string;
  sources: Source[];
  sourcesStatus: string | null;
  statusLabel: string | null;
  demo: boolean;
  error: string | null;
}

export function ChatView({
  conversation,
  messages,
  attachments,
  streaming,
  providerConfigured,
  onSend,
  onStop,
  onRetry,
  onOpenSources,
  onOpenCase,
  onUpload,
  onDeleteAttachment,
  onCreateConversation,
  onOpenSidebar,
}: {
  conversation: Conversation | null;
  messages: Message[];
  attachments: Attachment[];
  streaming: StreamingState | null;
  providerConfigured: boolean;
  onSend: (text: string, attachmentIds: string[]) => void;
  onStop: () => void;
  onRetry: (messageId: string) => void;
  onOpenSources: (sources: Source[], messageId: string, status: string | null, highlight?: number) => void;
  onOpenCase: () => void;
  onUpload: (file: File) => Promise<Attachment | null>;
  onDeleteAttachment: (id: string) => void;
  onCreateConversation: () => void;
  onOpenSidebar: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<string[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const conversationAttachments = attachments;

  useEffect(() => {
    const node = listRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, streaming?.content]);

  useEffect(() => {
    const area = textareaRef.current;
    if (!area) return;
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight, 220)}px`;
  }, [draft]);

  const attach = async (files: FileList | File[]) => {
    setUploading(true);
    try {
      for (const file of Array.from(files).slice(0, 5)) {
        const created = await onUpload(file);
        if (created) setPending((prev) => [...prev, created.id]);
      }
    } finally {
      setUploading(false);
    }
  };

  const send = () => {
    const text = draft.trim();
    if (!text || streaming) return;
    onSend(text, pending);
    setDraft("");
    setPending([]);
  };

  const isStreaming = Boolean(streaming);
  const hasConversation = Boolean(conversation);
  const currentCase = conversation?.case_state;

  return (
    <section className="chat">
      <header className="chat-head">
        <button type="button" className="btn btn-ghost btn-icon hide-desktop" onClick={onOpenSidebar} aria-label="Ouvrir le menu">
          ☰
        </button>
        <div className="chat-title">
          <h2>{conversation?.title || "Conversation"}</h2>
          {currentCase?.product || currentCase?.version ? (
            <div className="chat-case-summary">
              {currentCase?.product ? <Badge tone="neutral">produit : {currentCase.product}</Badge> : null}
              {currentCase?.version ? <Badge tone="accent">version : {currentCase.version}</Badge> : null}
            </div>
          ) : null}
        </div>
        <div className="chat-head-actions">
          <button type="button" className="btn btn-ghost" onClick={onOpenCase} disabled={!hasConversation} title="État du cas">
            État du cas
          </button>
        </div>
      </header>

      {!providerConfigured ? (
        <div className="banner banner-demo" role="status">
          <Alert size={14} />
          <span>
            <strong>Mode démonstration — aucun modèle connecté.</strong> La recherche documentaire est réelle, les
            réponses rédigées nécessitent un fournisseur (Administration → Connexion modèle).
          </span>
        </div>
      ) : null}

      <div className="chat-body" ref={listRef}>
        {!hasConversation ? (
          <div className="chat-empty">
            <EmptyState
              title="Aucune conversation sélectionnée"
              hint="Créez une conversation pour déposer un cas, des pièces jointes et interroger le corpus documentaire."
            />
            <button type="button" className="btn btn-primary" onClick={onCreateConversation}>
              Créer une conversation
            </button>
          </div>
        ) : messages.length === 0 && !streaming ? (
          <div className="chat-empty">
            <EmptyState
              title="Nouveau cas"
              hint="Décrivez le symptôme, le produit et la version exacte. Les pièces jointes (PDF, logs, captures) restent isolées dans ce cas."
            />
          </div>
        ) : (
          <div className="messages">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                attachments={conversationAttachments}
                onRetry={onRetry}
                onOpenSources={onOpenSources}
              />
            ))}
            {streaming ? (
              <StreamingBubble
                state={streaming}
                onStop={onStop}
                onOpenSources={onOpenSources}
              />
            ) : null}
          </div>
        )}
      </div>

      <div
        className={`composer ${dragOver ? "composer-drag" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragOver(false);
          if (event.dataTransfer.files.length) void attach(event.dataTransfer.files);
        }}
      >
        {pending.length > 0 ? (
          <div className="pending-files">
            {pending.map((id) => {
              const attachment = conversationAttachments.find((a) => a.id === id);
              return (
                <span key={id} className="file-chip">
                  <Paperclip size={12} /> {attachment?.filename ?? "pièce jointe"}
                  {attachment?.kind === "image" ? <Badge tone="warn">vision inactive</Badge> : null}
                  <button
                    type="button"
                    className="btn btn-ghost btn-icon"
                    onClick={() => {
                      setPending((prev) => prev.filter((item) => item !== id));
                      onDeleteAttachment(id);
                    }}
                    aria-label="Retirer la pièce jointe"
                  >
                    ✕
                  </button>
                </span>
              );
            })}
          </div>
        ) : null}
        <div className="composer-row">
          <label className="btn btn-ghost btn-icon" title="Joindre un fichier (PDF, txt, log, image)">
            <Paperclip size={16} />
            <input
              type="file"
              hidden
              multiple
              accept=".pdf,.txt,.log,.png,.jpg,.jpeg,.webp"
              disabled={!hasConversation || uploading}
              onChange={(event) => {
                if (event.target.files?.length) void attach(event.target.files);
                event.target.value = "";
              }}
            />
          </label>
          <textarea
            ref={textareaRef}
            value={draft}
            placeholder={hasConversation ? "Décrivez le problème, collez un log ou une capture…" : "Créez d'abord une conversation"}
            disabled={!hasConversation}
            rows={1}
            maxLength={20000}
            onChange={(event) => setDraft(event.target.value)}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.files || []);
              if (files.length) {
                event.preventDefault();
                void attach(files);
              }
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
          />
          {isStreaming ? (
            <button type="button" className="btn btn-danger" onClick={onStop} title="Arrêter la génération">
              <Stop size={14} /> Arrêter
            </button>
          ) : (
            <button type="button" className="btn btn-primary" onClick={send} disabled={!draft.trim() || !hasConversation} title="Envoyer">
              <Send size={15} /> Envoyer
            </button>
          )}
        </div>
        <p className="composer-hint muted small">
          Entrée : envoyer · Maj+Entrée : nouvelle ligne · Dépôt ou collage de fichiers accepté (PDF ≤ 20 Mio, texte ≤ 1 Mio,
          image ≤ 10 Mio) · Analyse d'image inactive.
        </p>
      </div>
    </section>
  );
}

function MessageBubble({
  message,
  attachments,
  onRetry,
  onOpenSources,
}: {
  message: Message;
  attachments: Attachment[];
  onRetry: (id: string) => void;
  onOpenSources: (sources: Source[], messageId: string, status: string | null, highlight?: number) => void;
}) {
  const linked = attachments.filter((a) => a.message_id === message.id);
  const sources = message.sources ?? [];
  return (
    <article className={`message message-${message.role}`}>
      <div className="message-head">
        <span className="message-role">{message.role === "user" ? "Vous" : "Wallia"}</span>
        {message.demo ? <Badge tone="demo">démonstration</Badge> : null}
        {message.model ? <Badge tone="neutral">{message.model}</Badge> : null}
        <span className="message-date muted small">{formatDate(message.created_at)}</span>
        <CopyButton text={message.content} label="Copier" />
      </div>
      {message.role === "assistant" ? (
        <Markdown
          content={message.content}
          sourcesCount={sources.length}
          onCitation={(index) => onOpenSources(sources, message.id, null, index)}
        />
      ) : (
        <p className="user-text">{message.content}</p>
      )}
      {linked.length > 0 ? (
        <div className="message-files">
          {linked.map((file) => (
            <a key={file.id} className="file-chip" href={attachmentUrl(file.id, file.kind === "image")} target="_blank" rel="noopener noreferrer">
              <Paperclip size={12} /> {file.filename} <span className="muted small">{formatBytes(file.size_bytes)}</span>
            </a>
          ))}
        </div>
      ) : null}
      {sources.length > 0 ? (
        <div className="message-sources">
          <button type="button" className="btn btn-ghost btn-small" onClick={() => onOpenSources(sources, message.id, null)}>
            Sources ({sources.length})
          </button>
          {sources.slice(0, 4).map((source, index) => (
            <button
              key={source.chunk_id}
              type="button"
              className="source-chip"
              onClick={() => onOpenSources(sources, message.id, null)}
              title={source.title}
            >
              [{index + 1}] {source.title.slice(0, 40)}
              {source.page_start ? ` · p.${source.page_start}` : ""}
            </button>
          ))}
        </div>
      ) : null}
      {message.status === "cancelled" ? (
        <div className="message-state">
          <Badge tone="warn">génération arrêtée</Badge>
          <button type="button" className="btn btn-ghost btn-small" onClick={() => onRetry(message.id)}>
            <Refresh size={13} /> Relancer
          </button>
        </div>
      ) : null}
      {message.status === "interrupted" ? (
        <div className="message-state">
          <Badge tone="warn">interrompue (redémarrage)</Badge>
          <button type="button" className="btn btn-ghost btn-small" onClick={() => onRetry(message.id)}>
            <Refresh size={13} /> Relancer
          </button>
        </div>
      ) : null}
      {message.status === "error" ? (
        <div className="message-state message-error">
          <Badge tone="error">erreur</Badge>
          <span className="error-text">{message.error}</span>
          <button type="button" className="btn btn-ghost btn-small" onClick={() => onRetry(message.id)}>
            <Refresh size={13} /> Relancer
          </button>
        </div>
      ) : null}
    </article>
  );
}

function StreamingBubble({
  state,
  onStop,
  onOpenSources,
}: {
  state: StreamingState;
  onStop: () => void;
  onOpenSources: (sources: Source[], messageId: string, status: string | null, highlight?: number) => void;
}) {
  return (
    <article className="message message-assistant message-streaming">
      <div className="message-head">
        <span className="message-role">Wallia</span>
        {state.demo ? <Badge tone="demo">démonstration</Badge> : null}
        <span className="streaming-status">
          <Spinner label={state.statusLabel || "génération"} />
        </span>
      </div>
      {state.content ? (
        <Markdown
          content={state.content}
          sourcesCount={state.sources.length}
          onCitation={(index) => onOpenSources(state.sources, state.messageId ?? "", state.sourcesStatus, index)}
        />
      ) : (
        <p className="muted">…</p>
      )}
      {state.sources.length > 0 ? (
        <div className="message-sources">
          <button
            type="button"
            className="btn btn-ghost btn-small"
            onClick={() => onOpenSources(state.sources, state.messageId ?? "", state.sourcesStatus)}
          >
            Sources ({state.sources.length})
          </button>
        </div>
      ) : null}
      {state.error ? (
        <div className="message-state message-error">
          <Badge tone="error">erreur</Badge>
          <span className="error-text">{state.error}</span>
        </div>
      ) : null}
      <div className="message-state">
        <button type="button" className="btn btn-ghost btn-small" onClick={onStop}>
          <Stop size={13} /> Arrêter
        </button>
      </div>
    </article>
  );
}
