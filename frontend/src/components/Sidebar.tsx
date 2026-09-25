import { useState } from "react";
import type { Conversation, User } from "../types";
import { Badge, formatDate } from "../ui";
import { Book, Gear, Logout, Menu, Pencil, Plus, Trash } from "../icons";

export function Sidebar({
  user,
  conversations,
  currentId,
  view,
  open,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  onView,
  onLogout,
  onClose,
}: {
  user: User;
  conversations: Conversation[];
  currentId: string | null;
  view: "chat" | "library" | "settings";
  open: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onView: (view: "chat" | "library" | "settings") => void;
  onLogout: () => void;
  onClose: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  return (
    <>
      {open ? <div className="sidebar-backdrop" onClick={onClose} /> : null}
      <aside className={`sidebar ${open ? "sidebar-open" : ""}`} aria-label="Navigation">
        <div className="sidebar-head">
          <div className="brand">
            <span className="logo-mark" aria-hidden="true" />
            <span className="brand-name">Wallia</span>
          </div>
          <button type="button" className="btn btn-ghost btn-icon hide-desktop" onClick={onClose} aria-label="Fermer le menu">
            ✕
          </button>
        </div>

        <button type="button" className="btn btn-primary btn-block" onClick={onCreate}>
          <Plus size={15} /> Nouvelle conversation
        </button>

        <nav className="sidebar-views" aria-label="Espaces">
          <button
            type="button"
            className={`nav-item ${view === "chat" ? "nav-active" : ""}`}
            onClick={() => {
              onView("chat");
              onClose();
            }}
          >
            <Menu size={15} /> Conversations
          </button>
          {user.is_admin ? (
            <button
              type="button"
              className={`nav-item ${view === "library" ? "nav-active" : ""}`}
              onClick={() => {
                onView("library");
                onClose();
              }}
            >
              <Book size={15} /> Bibliothèque & jobs
            </button>
          ) : null}
          <button
            type="button"
            className={`nav-item ${view === "settings" ? "nav-active" : ""}`}
            onClick={() => {
              onView("settings");
              onClose();
            }}
          >
            <Gear size={15} /> Administration
          </button>
        </nav>

        <div className="sidebar-list" role="list">
          {conversations.length === 0 ? <p className="muted small pad">Aucune conversation.</p> : null}
          {conversations.map((conv) => (
            <div
              key={conv.id}
              role="listitem"
              className={`conv-item ${conv.id === currentId && view === "chat" ? "conv-active" : ""}`}
              onClick={() => {
                onSelect(conv.id);
                onClose();
              }}
            >
              {editingId === conv.id ? (
                <form
                  className="conv-rename"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const title = draft.trim();
                    if (title) onRename(conv.id, title);
                    setEditingId(null);
                  }}
                >
                  <input
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    maxLength={200}
                    autoFocus
                    onKeyDown={(event) => {
                      if (event.key === "Escape") setEditingId(null);
                    }}
                  />
                </form>
              ) : (
                <>
                  <div className="conv-main">
                    <span className="conv-title">{conv.title}</span>
                    <span className="conv-meta">
                      {conv.message_count ?? 0} msg · {formatDate(conv.updated_at)}
                    </span>
                    {conv.last_message?.status === "error" ? (
                      <Badge tone="error">erreur</Badge>
                    ) : conv.last_message?.status === "streaming" ? (
                      <Badge tone="accent">en cours</Badge>
                    ) : null}
                  </div>
                  <div className="conv-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-icon"
                      aria-label={`Renommer ${conv.title}`}
                      title="Renommer"
                      onClick={(event) => {
                        event.stopPropagation();
                        setDraft(conv.title);
                        setEditingId(conv.id);
                      }}
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-icon danger"
                      aria-label={`Supprimer ${conv.title}`}
                      title="Supprimer"
                      onClick={(event) => {
                        event.stopPropagation();
                        onDelete(conv.id);
                      }}
                    >
                      <Trash size={13} />
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>

        <div className="sidebar-foot">
          <div className="user-box">
            <span className="user-email" title={user.email}>
              {user.email}
            </span>
            <Badge tone="accent">admin</Badge>
          </div>
          <button type="button" className="btn btn-ghost btn-block" onClick={onLogout}>
            <Logout size={14} /> Se déconnecter
          </button>
        </div>
      </aside>
    </>
  );
}
