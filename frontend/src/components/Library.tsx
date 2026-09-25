import { useCallback, useEffect, useMemo, useState } from "react";
import type { ChunkItem, DocumentItem, Job } from "../types";
import { api, documentOriginalUrl } from "../api";
import { Badge, EmptyState, Modal, Spinner, formatBytes, formatDate, toast } from "../ui";
import { Refresh } from "../icons";

const STATUS_TONES: Record<string, "ok" | "warn" | "error" | "accent" | "neutral"> = {
  ready: "ok",
  queued: "warn",
  processing: "accent",
  failed: "error",
  deleting: "error",
  succeeded: "ok",
  running: "accent",
  cancelled: "neutral",
};

function JobBadge({ job }: { job?: Job | null }) {
  if (!job) return null;
  const progress = job.progress || {};
  const stage = typeof progress.stage === "string" ? progress.stage : null;
  const done = typeof progress.done === "number" ? progress.done : null;
  const total = typeof progress.chunks === "number" ? progress.chunks : null;
  return (
    <Badge tone={STATUS_TONES[job.status] ?? "neutral"} title={job.error ?? undefined}>
      job {job.status}
      {stage ? ` · ${stage}` : ""}
      {done !== null && total !== null ? ` ${done}/${total}` : ""}
      {job.attempts > 1 ? ` · essai ${job.attempts}` : ""}
    </Badge>
  );
}

export function LibraryView() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("");
  const [docStatus, setDocStatus] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [chunkDoc, setChunkDoc] = useState<DocumentItem | null>(null);
  const [chunks, setChunks] = useState<ChunkItem[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (scope) params.scope = scope;
      if (docStatus) params.status = docStatus;
      if (query.trim()) params.q = query.trim();
      const [docsResponse, jobsResponse] = await Promise.all([api.listDocuments(params), api.listJobs()]);
      setDocuments(docsResponse.documents);
      setJobs(jobsResponse.jobs);
    } catch (error) {
      toast("error", `Chargement impossible : ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [scope, docStatus, query]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const hasActiveJobs = useMemo(
    () => jobs.some((job) => job.status === "queued" || job.status === "running"),
    [jobs],
  );

  useEffect(() => {
    if (!hasActiveJobs) return;
    const timer = setInterval(() => void reload(), 4000);
    return () => clearInterval(timer);
  }, [hasActiveJobs, reload]);

  const openChunks = async (document: DocumentItem) => {
    setChunkDoc(document);
    setChunksLoading(true);
    try {
      const response = await api.documentChunks(document.id);
      setChunks(response.chunks);
    } catch (error) {
      toast("error", `Passages illisibles : ${(error as Error).message}`);
    } finally {
      setChunksLoading(false);
    }
  };

  const reindex = async (document: DocumentItem) => {
    try {
      await api.reindexDocument(document.id);
      toast("info", `Réindexation lancée pour « ${document.title} ».`);
      void reload();
    } catch (error) {
      toast("error", `Réindexation impossible : ${(error as Error).message}`);
    }
  };

  const remove = async (document: DocumentItem) => {
    if (!window.confirm(`Supprimer définitivement « ${document.title} » et ses passages ?`)) return;
    try {
      const response = await api.deleteDocument(document.id);
      toast("success", `Document supprimé (${response && "chunks_removed" in response ? (response as { chunks_removed: number }).chunks_removed : 0} passages retirés).`);
      void reload();
    } catch (error) {
      toast("error", `Suppression impossible : ${(error as Error).message}`);
    }
  };

  const retryJob = async (job: Job) => {
    try {
      await api.retryJob(job.id);
      toast("info", "Job relancé.");
      void reload();
    } catch (error) {
      toast("error", `Relance impossible : ${(error as Error).message}`);
    }
  };

  return (
    <section className="view">
      <header className="view-head">
        <h2>Bibliothèque documentaire</h2>
        <div className="view-actions">
          <button type="button" className="btn btn-ghost" onClick={() => void reload()}>
            <Refresh size={14} /> Actualiser
          </button>
          <button type="button" className="btn btn-primary" onClick={() => setImportOpen(true)}>
            Importer un PDF
          </button>
        </div>
      </header>

      <div className="filters">
        <input placeholder="Rechercher un titre…" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select value={scope} onChange={(event) => setScope(event.target.value)}>
          <option value="">Tous périmètres</option>
          <option value="demo">Démonstration</option>
          <option value="official">Officiel</option>
        </select>
        <select value={docStatus} onChange={(event) => setDocStatus(event.target.value)}>
          <option value="">Tous statuts</option>
          <option value="ready">Prêt</option>
          <option value="queued">En attente</option>
          <option value="processing">En cours</option>
          <option value="failed">Échec</option>
        </select>
      </div>

      {loading && documents.length === 0 ? (
        <Spinner label="Chargement…" />
      ) : documents.length === 0 ? (
        <EmptyState
          title="Aucun document"
          hint="Importez un PDF pour l'indexer réellement (Docling + embeddings pgvector). Les documents de démonstration sont fictifs."
        />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Titre</th>
                <th>Produit / versions</th>
                <th>Statut</th>
                <th>Pages</th>
                <th>Passages</th>
                <th>Importé</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => (
                <tr key={document.id}>
                  <td>
                    <div className="doc-title">{document.title}</div>
                    <div className="muted small">
                      {document.original_filename} · {formatBytes(document.size_bytes)} · {document.language.toUpperCase()}
                    </div>
                    {document.demo ? <Badge tone="demo">démo non officiel</Badge> : <Badge tone="ok">officiel</Badge>}
                    {document.error ? <p className="error-text small">{document.error}</p> : null}
                  </td>
                  <td>
                    <div>{document.product ?? "—"}</div>
                    <div className="muted small">{document.versions.length ? document.versions.join(", ") : "versions inconnues"}</div>
                  </td>
                  <td>
                    <Badge tone={STATUS_TONES[document.status] ?? "neutral"}>{document.status}</Badge>
                    <div>
                      <JobBadge job={document.last_job} />
                    </div>
                  </td>
                  <td>{document.page_count ?? "—"}</td>
                  <td title="Passages de la génération servie en recherche">{document.chunks_current ?? 0}</td>
                  <td className="muted small">{formatDate(document.created_at)}</td>
                  <td>
                    <div className="row-actions">
                      <a className="btn btn-ghost btn-small" href={documentOriginalUrl(document.id)} target="_blank" rel="noopener noreferrer">
                        Original
                      </a>
                      <button type="button" className="btn btn-ghost btn-small" onClick={() => void openChunks(document)}>
                        Passages
                      </button>
                      <button type="button" className="btn btn-ghost btn-small" onClick={() => void reindex(document)} disabled={document.status === "deleting"}>
                        Réindexer
                      </button>
                      <button type="button" className="btn btn-ghost btn-small danger" onClick={() => void remove(document)} disabled={document.status === "deleting"}>
                        Supprimer
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="section-title">Jobs d'ingestion</h3>
      {jobs.length === 0 ? (
        <p className="muted">Aucun job.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Document</th>
                <th>Type</th>
                <th>Statut</th>
                <th>Progression</th>
                <th>Erreur</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.slice(0, 30).map((job) => (
                <tr key={job.id}>
                  <td>{job.document_title ?? job.document_id ?? "—"}</td>
                  <td>{job.kind}</td>
                  <td>
                    <Badge tone={STATUS_TONES[job.status] ?? "neutral"}>{job.status}</Badge>
                  </td>
                  <td className="muted small">
                    {job.progress && Object.keys(job.progress).length
                      ? JSON.stringify(job.progress)
                      : "—"}
                    {` · essais ${job.attempts}/${job.max_attempts}`}
                  </td>
                  <td className="error-text small">{job.error ?? "—"}</td>
                  <td>
                    <div className="row-actions">
                      {job.status === "failed" ? (
                        <button type="button" className="btn btn-ghost btn-small" onClick={() => void retryJob(job)} disabled={job.manual_retries >= 3}>
                          Relancer
                        </button>
                      ) : null}
                      {job.document_id ? (
                        <a className="btn btn-ghost btn-small" href={`/api/documents/${job.document_id}/jobs`} target="_blank" rel="noopener noreferrer">
                          Détail JSON
                        </a>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {importOpen ? <ImportModal onClose={() => setImportOpen(false)} onDone={() => void reload()} /> : null}

      {chunkDoc ? (
        <Modal title={`Passages — ${chunkDoc.title}`} onClose={() => setChunkDoc(null)} wide>
          {chunksLoading ? (
            <Spinner label="Chargement des passages…" />
          ) : chunks.length === 0 ? (
            <EmptyState title="Aucun passage" hint="Le document n'a pas encore été indexé avec succès." />
          ) : (
            <div className="chunk-list">
              {chunks.slice(0, 200).map((chunk) => (
                <article key={chunk.id} className="chunk">
                  <div className="muted small">
                    #{chunk.seq} · {chunk.kind} ·{" "}
                    {chunk.page_start ? `p. ${chunk.page_start}${chunk.page_end && chunk.page_end !== chunk.page_start ? `–${chunk.page_end}` : ""}` : "page inconnue"}{" "}
                    · {chunk.token_count ?? "?"} tokens{chunk.section ? ` · ${chunk.section}` : ""}
                  </div>
                  <p>{chunk.text}</p>
                </article>
              ))}
            </div>
          )}
        </Modal>
      ) : null}
    </section>
  );
}

function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [origin, setOrigin] = useState("demo");
  const [product, setProduct] = useState("Aster");
  const [versions, setVersions] = useState("");
  const [language, setLanguage] = useState("fr");
  const [documentDate, setDocumentDate] = useState("");
  const [demo, setDemo] = useState(true);
  const [scope, setScope] = useState("demo");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) {
      setError("Sélectionnez un PDF.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("title", title || file.name);
      form.append("origin", origin);
      form.append("product", product);
      form.append("versions", JSON.stringify(versions.split(",").map((v) => v.trim()).filter(Boolean)));
      form.append("language", language);
      form.append("document_date", documentDate);
      form.append("demo", demo ? "1" : "0");
      form.append("scope", scope);
      await api.importDocument(form);
      toast("success", "Document importé — ingestion en file d'attente.");
      onDone();
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Importer un document (PDF)" onClose={onClose}>
      <form className="form-grid" onSubmit={submit}>
        <label>
          Fichier PDF
          <input
            type="file"
            accept="application/pdf"
            onChange={(event) => {
              const selected = event.target.files?.[0] ?? null;
              setFile(selected);
              if (selected && !title) setTitle(selected.name.replace(/\.pdf$/i, ""));
            }}
            required
          />
        </label>
        <label>
          Titre
          <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} required />
        </label>
        <label>
          Origine
          <input value={origin} onChange={(event) => setOrigin(event.target.value)} maxLength={200} />
        </label>
        <label>
          Produit
          <input value={product} onChange={(event) => setProduct(event.target.value)} maxLength={200} />
        </label>
        <label>
          Versions (séparées par des virgules)
          <input value={versions} onChange={(event) => setVersions(event.target.value)} placeholder="10.9, 10.9.1" />
        </label>
        <label>
          Langue
          <select value={language} onChange={(event) => setLanguage(event.target.value)}>
            <option value="fr">Français</option>
            <option value="en">Anglais</option>
            <option value="other">Autre</option>
          </select>
        </label>
        <label>
          Date du document (optionnel)
          <input type="date" value={documentDate} onChange={(event) => setDocumentDate(event.target.value)} />
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={demo} onChange={(event) => setDemo(event.target.checked)} />
          Document de démonstration (non officiel)
        </label>
        <label>
          Périmètre
          <select value={scope} onChange={(event) => setScope(event.target.value)}>
            <option value="demo">Démonstration</option>
            <option value="official">Officiel</option>
          </select>
        </label>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Annuler
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Import…" : "Importer et indexer"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
