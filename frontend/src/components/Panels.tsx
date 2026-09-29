import { useEffect, useRef, useState } from "react";
import type { CaseItem, CaseState, Conversation, Source } from "../types";
import { api, documentOriginalUrl, isAllowedWebUrl } from "../api";
import { Badge, EmptyState, toast } from "../ui";

const RETRIEVAL_LABELS: Record<string, string> = {
  no_relevant_source: "Aucun passage pertinent trouvé pour cette question.",
  empty_corpus: "Corpus documentaire vide : aucun document indexé.",
  embeddings_unavailable: "Service d'embeddings indisponible.",
  retrieval_unavailable:
    "Reclassement documentaire momentanément indisponible : aucune citation produite (ce n'est pas une absence de source).",
  sources_unavailable:
    "Citation indisponible : les sources de ce message ne sont plus accessibles (message ancien ou source supprimée).",
};

export function SourcesPanel({
  open,
  sources,
  status,
  highlighted,
  onClose,
}: {
  open: boolean;
  sources: Source[];
  status: string | null;
  highlighted: number | null;
  onClose: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || highlighted === null) return;
    const node = containerRef.current?.querySelector(`[data-source-index="${highlighted}"]`);
    node?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [highlighted, open]);

  if (!open) return null;
  const highlightedMissing =
    highlighted !== null && highlighted >= 1 && sources.length > 0 && highlighted > sources.length;
  const hasWeb = sources.some((source) => source.source_type === "web");
  const sourceKey = (source: Source, index: number) => source.chunk_id ?? source.url ?? `source-${index}`;
  return (
    <>
      <div className="panel-backdrop" onClick={onClose} />
      <aside className="panel panel-sources" aria-label="Panneau des sources">
        <div className="panel-head">
          <h3>Sources documentaires</h3>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Fermer le panneau">
            ✕
          </button>
        </div>
        <div className="panel-body" ref={containerRef}>
          {sources.length === 0 ? (
            <EmptyState
              title="Aucune citation"
              hint={status ? RETRIEVAL_LABELS[status] ?? "Aucune source pertinente pour cette réponse." : "Aucune source sélectionnée."}
            />
          ) : (
            <>
              {highlightedMissing ? (
                <p className="form-error small">
                  Citation [{highlighted}] indisponible dans ce message : aucune source correspondante.
                </p>
              ) : null}
              <p className="muted small">
                {hasWeb
                  ? "Sources du corpus indexé et de la recherche web publique du constructeur (version non vérifiée). Leur présence ne prouve pas qu'elles répondent à la question."
                  : "Ces passages proviennent du corpus indexé. Leur présence ne prouve pas qu'ils répondent à la question."}
              </p>
              {sources.map((source, index) => {
                const isWeb = source.source_type === "web";
                return (
                  <article
                    key={sourceKey(source, index)}
                    className={`source-card ${highlighted === index + 1 ? "source-highlight" : ""}`}
                    data-source-index={index + 1}
                  >
                    <div className="source-head">
                      <span className="source-index">[{index + 1}]</span>
                      <span className="source-title" title={source.title}>
                        {source.title}
                      </span>
                    </div>
                    {isWeb ? (
                      <>
                        <div className="source-badges">
                          <Badge tone="accent" title="Web constructeur public — version non vérifiée">
                            Web constructeur public
                          </Badge>
                          <Badge tone="warn">version non vérifiée</Badge>
                          {source.domain ? <Badge tone="neutral">{source.domain}</Badge> : null}
                        </div>
                        <p className="source-section muted small">
                          Résultat de recherche non qualifié : aucune procédure constructeur n'en est déduite.
                        </p>
                        <p className={`source-text ${expanded === sourceKey(source, index) ? "" : "source-text-clamped"}`}>
                          {source.text}
                        </p>
                        <div className="source-actions">
                          <button
                            type="button"
                            className="btn btn-ghost btn-small"
                            onClick={() =>
                              setExpanded(expanded === sourceKey(source, index) ? null : sourceKey(source, index))
                            }
                          >
                            {expanded === sourceKey(source, index) ? "Réduire" : "Texte complet"}
                          </button>
                          {source.url && isAllowedWebUrl(source.url) ? (
                            <a
                              className="btn btn-ghost btn-small"
                              href={source.url}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              Ouvrir la source ({source.domain ?? "wallix.com"})
                            </a>
                          ) : source.url ? (
                            // Mêmes gardes syntaxiques que le serveur : une URL
                            // non conforme (schéma, userinfo, port, domaine)
                            // n'est jamais rendue ouvrable.
                            <span className="muted small" data-testid="source-web-url-refusee">
                              URL publique non conforme : lien désactivé.
                            </span>
                          ) : null}
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="source-badges">
                          {source.demo ? <Badge tone="demo">démo</Badge> : <Badge tone="ok">officiel</Badge>}
                          {source.product ? <Badge tone="neutral">{source.product}</Badge> : null}
                          {source.versions.length ? <Badge tone="accent">v. {source.versions.join(", ")}</Badge> : null}
                          <Badge tone="neutral">
                            {source.page_start ? `p. ${source.page_start}${source.page_end && source.page_end !== source.page_start ? `–${source.page_end}` : ""}` : "page inconnue"}
                          </Badge>
                          <Badge tone="neutral">{source.kind === "table" ? "tableau" : "texte"}</Badge>
                          {source.score !== null ? (
                            <Badge
                              tone="neutral"
                              title="logit brut du reclassement (classement réel, jamais une probabilité) · sélection des candidats : cosinus / texte"
                            >
                              logit {source.score.toFixed(2)}
                              {source.score_vector !== null ? ` · cos ${source.score_vector.toFixed(3)}` : " · cos —"}
                              {source.score_text !== null ? ` · texte ${source.score_text.toFixed(2)}` : ""}
                            </Badge>
                          ) : null}
                        </div>
                        {source.section ? <p className="source-section muted small">Section : {source.section}</p> : null}
                        <p className={`source-text ${expanded === sourceKey(source, index) ? "" : "source-text-clamped"}`}>
                          {source.text}
                        </p>
                        <div className="source-actions">
                          <button
                            type="button"
                            className="btn btn-ghost btn-small"
                            onClick={() =>
                              setExpanded(expanded === sourceKey(source, index) ? null : sourceKey(source, index))
                            }
                          >
                            {expanded === sourceKey(source, index) ? "Réduire" : "Texte complet"}
                          </button>
                          {source.available === false ? (
                            // Document supprimé depuis : l'excerpt historique reste
                            // affiché, l'original est désactivé (jamais un lien mort).
                            <span className="muted small" data-testid="source-unavailable">
                              Document indisponible (supprimé du corpus)
                            </span>
                          ) : source.document_id ? (
                            <a
                              className="btn btn-ghost btn-small"
                              href={documentOriginalUrl(source.document_id, source.page_start)}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              Ouvrir l'original{source.page_start ? ` (p. ${source.page_start})` : ""}
                            </a>
                          ) : null}
                        </div>
                      </>
                    )}
                  </article>
                );
              })}
            </>
          )}
        </div>
      </aside>
    </>
  );
}

const SECTIONS: Array<{ key: keyof CaseState; label: string; hint?: string }> = [
  { key: "facts", label: "Faits établis" },
  { key: "hypotheses", label: "Hypothèses" },
  { key: "proposed_checks", label: "Contrôles proposés", hint: "Un contrôle proposé n'est pas un contrôle réalisé." },
  { key: "performed_checks", label: "Contrôles réellement effectués" },
  { key: "results", label: "Résultats observés" },
  { key: "missing_info", label: "Informations manquantes" },
];

const STATUS_LABELS: Record<CaseItem["status"], string> = {
  proposed: "proposé",
  confirmed: "confirmé",
  refuted: "réfuté",
  missing: "inconnu",
};

const ORIGIN_LABELS: Record<CaseItem["origin"], string> = {
  user_message: "message utilisateur",
  user_explicit: "saisie explicite",
  assistant: "assistant",
  unknown: "provenance inconnue",
};

function emptyItem(): CaseItem {
  return {
    id: crypto.randomUUID(),
    text: "",
    status: "proposed",
    origin: "user_explicit",
    message_id: null,
    created_at: new Date().toISOString(),
  };
}

export function CasePanel({
  open,
  conversation,
  onClose,
  onSaved,
}: {
  open: boolean;
  conversation: Conversation | null;
  onClose: () => void;
  onSaved: (conversationId: string, state: CaseState) => void;
}) {
  const [state, setState] = useState<CaseState | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open && conversation) {
      setState(JSON.parse(JSON.stringify(conversation.case_state)) as CaseState);
    }
  }, [open, conversation]);

  if (!open || !conversation || !state) return null;

  const updateItems = (key: keyof CaseState, items: CaseItem[]) => {
    setState({ ...state, [key]: items } as CaseState);
  };

  const save = async () => {
    setSaving(true);
    try {
      // Contrat API : les items transmis ne contiennent que les champs
      // acceptés (id/text/status/origin/message_id) — `created_at` est
      // horodaté par le serveur, jamais envoyé par l'UI.
      const cleanItems = (items: CaseItem[]) =>
        items
          .filter((item) => item.text.trim().length > 0)
          .map((item) => ({
            id: item.id,
            text: item.text,
            status: item.status,
            origin: item.origin,
            message_id: item.message_id,
          }));
      const payload = {
        product: state.product || null,
        version: state.version || null,
        symptom: state.symptom || null,
        facts: cleanItems(state.facts),
        hypotheses: cleanItems(state.hypotheses),
        proposed_checks: cleanItems(state.proposed_checks),
        performed_checks: cleanItems(state.performed_checks),
        results: cleanItems(state.results),
        missing_info: cleanItems(state.missing_info),
      };
      const response = await api.patchCaseState(conversation.id, payload as Partial<CaseState>);
      // L'identifiant du cas est transmis : une réponse pour A ne peut jamais
      // être appliquée à l'écran du cas B.
      onSaved(conversation.id, response.case_state);
      toast("success", "État du cas enregistré.");
      onClose();
    } catch (error) {
      toast("error", `Enregistrement impossible : ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="panel-backdrop" onClick={onClose} />
      <aside className="panel panel-case" aria-label="État du cas">
        <div className="panel-head">
          <h3>État du cas</h3>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Fermer le panneau">
            ✕
          </button>
        </div>
        <div className="panel-body">
          <p className="muted small">
            Une hypothèse n'est pas un fait ; une vérification proposée n'est pas une vérification réalisée.
          </p>
          <p className="muted small">
            Produit et version peuvent aussi être conservés automatiquement quand ils sont déclarés explicitement
            dans un message (« version 10.10 », « produit : Aster »). Tous les autres champs restent saisis
            manuellement ici : rien n'est déduit automatiquement de la conversation.
          </p>
          <div className="case-grid">
            <label>
              Produit
              <input value={state.product ?? ""} onChange={(event) => setState({ ...state, product: event.target.value || null })} />
            </label>
            <label>
              Version
              <input
                value={state.version ?? ""}
                onChange={(event) => setState({ ...state, version: event.target.value || null })}
                placeholder="ex. 10.10"
              />
            </label>
          </div>
          <label>
            Symptôme
            <textarea
              rows={2}
              value={state.symptom ?? ""}
              onChange={(event) => setState({ ...state, symptom: event.target.value || null })}
              placeholder="Description observable du problème"
            />
          </label>

          {SECTIONS.map((section) => {
            const items = (state[section.key] as CaseItem[]) ?? [];
            return (
              <div className="case-section" key={section.key}>
                <div className="case-section-head">
                  <h4>{section.label}</h4>
                  <button
                    type="button"
                    className="btn btn-ghost btn-small"
                    onClick={() => updateItems(section.key, [...items, emptyItem()])}
                  >
                    + Ajouter
                  </button>
                </div>
                {section.hint ? <p className="muted small">{section.hint}</p> : null}
                {items.length === 0 ? <p className="muted small">Aucun élément.</p> : null}
                {items.map((item, index) => (
                  <div className="case-item" key={item.id}>
                    <input
                      value={item.text}
                      placeholder="Décrire l'élément"
                      onChange={(event) => {
                        const next = [...items];
                        next[index] = { ...item, text: event.target.value };
                        updateItems(section.key, next);
                      }}
                    />
                    <select
                      value={item.status}
                      title="Statut"
                      onChange={(event) => {
                        const next = [...items];
                        next[index] = { ...item, status: event.target.value as CaseItem["status"] };
                        updateItems(section.key, next);
                      }}
                    >
                      {Object.entries(STATUS_LABELS).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                    <span className="case-origin muted small" title="Provenance">
                      {ORIGIN_LABELS[item.origin]}
                    </span>
                    <button
                      type="button"
                      className="btn btn-ghost btn-icon danger"
                      aria-label="Supprimer l'élément"
                      onClick={() => updateItems(section.key, items.filter((_, idx) => idx !== index))}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        <div className="panel-foot">
          <button type="button" className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Enregistrement…" : "Enregistrer l'état du cas"}
          </button>
        </div>
      </aside>
    </>
  );
}
