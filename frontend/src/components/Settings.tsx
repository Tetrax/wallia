import { useEffect, useState } from "react";
import { api } from "../api";
import type { SettingsPayload, StatusPayload } from "../types";
import { Badge, formatDate, toast } from "../ui";

export function SettingsView({
  status,
  onRefreshStatus,
  onCsrfRotated,
}: {
  status: StatusPayload | null;
  onRefreshStatus: () => void;
  onCsrfRotated: () => void;
}) {
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [timeoutS, setTimeoutS] = useState(240);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ ok: boolean; error: string | null; latency_ms: number | null; model: string } | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const load = async () => {
    try {
      const payload = await api.getSettings();
      setSettings(payload);
      setEndpoint(payload.provider.endpoint);
      setModel(payload.provider.model);
      setTimeoutS(payload.provider.timeout_s);
    } catch (error) {
      toast("error", `Réglages illisibles : ${(error as Error).message}`);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const saveProvider = async (clearKey = false) => {
    setBusy(clearKey ? "clear" : "save");
    try {
      const payload = await api.putSettings({
        provider_endpoint: endpoint,
        provider_model: model,
        provider_timeout_s: timeoutS,
        api_key: apiKey || null,
        clear_api_key: clearKey,
      });
      setSettings(payload);
      setApiKey("");
      toast("success", clearKey ? "Clé supprimée." : "Réglages enregistrés.");
      onRefreshStatus();
    } catch (error) {
      toast("error", `Enregistrement impossible : ${(error as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const testProvider = async () => {
    setBusy("test");
    setTestResult(null);
    try {
      const result = await api.testProvider();
      setTestResult(result);
      toast(result.ok ? "success" : "error", result.ok ? `Fournisseur joignable (${result.latency_ms} ms).` : `Test en échec : ${result.error}`);
      onRefreshStatus();
    } catch (error) {
      toast("error", `Test impossible : ${(error as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const changePassword = async (event: React.FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirmPassword) {
      toast("error", "Les deux mots de passe ne correspondent pas.");
      return;
    }
    setBusy("password");
    try {
      await api.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      onCsrfRotated();
      toast("success", "Mot de passe modifié. Les autres sessions ont été révoquées.");
    } catch (error) {
      toast("error", `Changement impossible : ${(error as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="view">
      <header className="view-head">
        <h2>Administration</h2>
        <div className="view-actions">
          <button type="button" className="btn btn-ghost" onClick={() => void load()}>
            Recharger
          </button>
        </div>
      </header>

      <div className="cards">
        <div className="card">
          <h4>Application</h4>
          <p>
            version <code>{status?.app.version ?? "—"}</code>
          </p>
          <p>
            environnement <Badge tone={status?.app.env === "production" ? "ok" : "warn"}>{status?.app.env ?? "—"}</Badge>
          </p>
          <p className="muted small">révision {status?.app.git_sha ?? "inconnue"}</p>
          {status?.app.demo_mode ? <Badge tone="demo">mode démonstration (aucun modèle connecté)</Badge> : <Badge tone="ok">modèle connecté</Badge>}
        </div>
        <div className="card">
          <h4>Embeddings (recherche)</h4>
          <p>
            <code>{status?.embedding.model ?? "—"}</code>
          </p>
          <p className="muted small">
            révision {status?.embedding.revision?.slice(0, 12) ?? "—"}… · dimension {status?.embedding.dim ?? "—"} ·{" "}
            {status?.embedding.backend ?? "—"} · CPU
          </p>
        </div>
        <div className="card">
          <h4>Fournisseur de chat</h4>
          <p>
            {settings?.provider.key_configured ? <Badge tone="ok">clé configurée</Badge> : <Badge tone="warn">clé absente</Badge>}{" "}
            <Badge tone="neutral">streaming</Badge>
          </p>
          <p className="muted small">
            {settings?.provider.endpoint ?? "—"} · {settings?.provider.model ?? "—"}
          </p>
          <p>
            {status?.vision.available ? (
              <Badge tone="ok">vision active</Badge>
            ) : (
              <Badge tone="warn" title={status?.vision.reason ?? "analyse d'image inactive"}>
                vision inactive
              </Badge>
            )}
          </p>
          {settings?.last_provider_test ? (
            <p className="muted small">
              dernier test : {(settings.last_provider_test as { ok?: boolean }).ok ? "succès" : "échec"} ·{" "}
              {formatDate((settings.last_provider_test as { at?: string }).at ?? null)}
            </p>
          ) : null}
        </div>
        <div className="card">
          <h4>Web & vision</h4>
          <p>
            web : <Badge tone={status?.web.available ? "ok" : "neutral"}>{status?.web.available ? "actif" : "inactif"}</Badge>
          </p>
          <p className="muted small">{status?.web.reason ?? "non configuré"}</p>
          <p>
            vision : <Badge tone={status?.vision.available ? "ok" : "neutral"}>{status?.vision.available ? "active" : "inactive"}</Badge>
          </p>
          <p className="muted small">{status?.vision.reason ?? "analyse d'image non activée"}</p>
        </div>
        <div className="card">
          <h4>Corpus & pipeline</h4>
          <p>
            {status?.corpus.documents_total ?? 0} document(s) · {status?.corpus.chunks_serving ?? 0} passage(s) servis
          </p>
          <p className="muted small">statuts : {JSON.stringify(status?.corpus.documents_by_status ?? {})}</p>
          <p className="muted small">worker : {status?.jobs.worker.alive ? "actif (battement récent)" : "injoignable"}</p>
          <p className="muted small">jobs : {JSON.stringify(status?.jobs.by_status ?? {})}</p>
        </div>
        <div className="card">
          <h4>Recherche hybride</h4>
          <p className="muted small">
            fusion RRF (vecteur 384 + plein texte), top-k {status?.retrieval.top_k ?? "—"}, puis reclassement par
            cross-encoder : <code>{status?.retrieval.reranker?.model ?? "—"}</code>
          </p>
          <p className="muted small">
            révision {status?.retrieval.reranker?.revision?.slice(0, 12) ?? "—"}… · état{" "}
            {status?.retrieval.reranker?.state ?? "inconnu"} · seuil de logit{" "}
            {status?.retrieval.reranker?.threshold ?? "—"} (gelé, non ajustable)
          </p>
          <p className="muted small">
            Le corpus de démonstration est fictif et non officiel : aucun résultat ne constitue une procédure constructeur.
          </p>
        </div>
      </div>

      <h3 className="section-title">Connexion modèle (OpenAI-compatible)</h3>
      <form
        className="form-grid card"
        onSubmit={(event) => {
          event.preventDefault();
          void saveProvider(false);
        }}
      >
        <label>
          Endpoint (HTTPS, domaine autorisé)
          <input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} maxLength={300} />
        </label>
        <label>
          Modèle
          <input value={model} onChange={(event) => setModel(event.target.value)} maxLength={100} />
        </label>
        <label>
          Délai total (secondes)
          <input
            type="number"
            min={5}
            max={600}
            value={timeoutS}
            onChange={(event) => setTimeoutS(Number(event.target.value))}
          />
        </label>
        <label>
          Clé API (write-only — laisser vide pour conserver)
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={settings?.provider.key_configured ? "•••••• (conservée)" : "clé absente"}
            autoComplete="off"
          />
        </label>
        <p className="muted small">
          Domaines autorisés : {settings?.provider.allowed_domains.join(", ") ?? "—"}. La clé n'est jamais renvoyée par
          l'API ni incluse dans le frontend.
        </p>
        <div className="modal-actions">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              // Suppression de clé : confirmation explicite, jamais silencieuse.
              if (window.confirm("Supprimer la clé API du fournisseur ? Les générations repasseront en mode démonstration.")) {
                void saveProvider(true);
              }
            }}
            disabled={busy !== null || !settings?.provider.key_configured}
          >
            Supprimer la clé
          </button>
          <button type="button" className="btn" onClick={() => void testProvider()} disabled={busy !== null}>
            {busy === "test" ? "Test…" : "Tester la connexion"}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy !== null}>
            {busy === "save" ? "Enregistrement…" : "Enregistrer"}
          </button>
        </div>
        {testResult ? (
          <p className={testResult.ok ? "muted small" : "form-error"}>
            {testResult.ok
              ? `Test réussi : modèle « ${testResult.model} », ${testResult.latency_ms} ms.`
              : `Test en échec : ${testResult.error}`}
          </p>
        ) : null}
      </form>

      <h3 className="section-title">Mot de passe</h3>
      <form className="form-grid card" onSubmit={changePassword}>
        <label>
          Mot de passe actuel
          <input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required />
        </label>
        <label>
          Nouveau mot de passe (≥ 12 caractères)
          <input type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} minLength={12} required />
        </label>
        <label>
          Confirmation
          <input type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} minLength={12} required />
        </label>
        <div className="modal-actions">
          <button type="submit" className="btn btn-primary" disabled={busy !== null}>
            {busy === "password" ? "Modification…" : "Changer le mot de passe"}
          </button>
        </div>
      </form>
    </section>
  );
}
