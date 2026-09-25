import * as React from "react";
import { Check, Copy } from "./icons";

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "warn" | "error" | "accent" | "demo";
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner" role="status" aria-label={label || "chargement"}>
      <span className="spinner-dot" />
      {label ? <span className="spinner-label">{label}</span> : null}
    </span>
  );
}

export function CopyButton({ text, label = "Copier" }: { text: string; label?: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-small"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* presse-papiers indisponible */
        }
      }}
      title={label}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />} {copied ? "Copié" : label}
    </button>
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-label={title}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button type="button" className="btn btn-ghost" onClick={onClose} aria-label="Fermer">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

type ToastKind = "info" | "error" | "success";
interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

let toastItems: ToastItem[] = [];
let toastListeners: Array<(items: ToastItem[]) => void> = [];
let toastSeq = 1;

export function toast(kind: ToastKind, text: string) {
  const item: ToastItem = { id: toastSeq++, kind, text };
  toastItems = [...toastItems, item];
  toastListeners.forEach((listener) => listener(toastItems));
  setTimeout(() => {
    toastItems = toastItems.filter((t) => t.id !== item.id);
    toastListeners.forEach((listener) => listener(toastItems));
  }, 5200);
}

export function ToastHost() {
  const [items, setItems] = React.useState<ToastItem[]>(toastItems);
  React.useEffect(() => {
    const listener = (next: ToastItem[]) => setItems([...next]);
    toastListeners.push(listener);
    return () => {
      toastListeners = toastListeners.filter((l) => l !== listener);
    };
  }, []);
  return (
    <div className="toast-host" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={`toast toast-${item.kind}`}>
          {item.text}
        </div>
      ))}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {hint ? <p className="empty-hint">{hint}</p> : null}
    </div>
  );
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} Kio`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} Mio`;
}
