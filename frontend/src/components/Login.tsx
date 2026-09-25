import { useState } from "react";
import { api, ApiError } from "../api";
import type { User } from "../types";

export function Login({ onAuthenticated }: { onAuthenticated: (user: User, csrf: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const payload = await api.login(email.trim(), password);
      onAuthenticated(payload.user, payload.csrf_token);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setError(err.status === 403 ? "Origine non autorisée." : "Identifiants invalides.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Trop de tentatives. Patientez avant de réessayer.");
      } else {
        setError("Connexion impossible. Vérifiez le service puis réessayez.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <span className="logo-mark" aria-hidden="true" />
          <div>
            <h1>Wallia</h1>
            <p className="muted">Assistant de support technique — accès privé</p>
          </div>
        </div>
        <label>
          Adresse e-mail
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            autoFocus
          />
        </label>
        <label>
          Mot de passe
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Connexion…" : "Se connecter"}
        </button>
        <p className="muted small">
          Prototype privé, indépendant et non officiel WALLIX. Les identifiants initiaux se trouvent dans
          runtime/initial-access.txt (côté serveur).
        </p>
      </form>
    </div>
  );
}
