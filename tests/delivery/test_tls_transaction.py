"""Tests de la TRANSACTION TLS de l'activateur — doubles NON PRIVILÉGIÉS.

Ce qui est SIMULÉ (explicitement) :
- les racines système (`/etc/nginx`, `/var/lib/wallia`, `/var/www`,
  `/etc/letsencrypt`, `/etc/wallia/tls-templates`) sont RÉÉCRITES vers un bac à
  sable `tmp_path` dans une COPIE du script ; le script livré conserve ses
  racines fixes (aucun override par variable d'environnement) ;
- `id` est un double qui répond 0 pour `-u` : aucun privilège réel n'est requis ;
- `install` est un double qui retire `-o root -g root` (le propriétaire ne peut
  pas être changé sans privilège) avant de déléguer au vrai `install` ;
- `nginx`, `systemctl` et `curl` sont des doubles pilotés par un état de test ;
- les certificats utilisés sont des paires auto-signées ÉPHÉMÈRES.

Rien n'est jamais écrit dans `/etc`, `/var/lib` ou `/var/www` réels ; aucun
Nginx réel n'est invoqué ; aucun certificat réel n'est touché.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVATE_SOURCE = REPO_ROOT / "deployment" / "wallia-tls-activate.sh"
DOMAIN = "wallia.valdev.me"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl requis")

STUB_ID = """#!/usr/bin/env bash
if [[ "${1:-}" == "-u" ]]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
"""

STUB_INSTALL = """#!/usr/bin/env bash
# Double non privilégié : retire -o root / -g root puis délègue au vrai install.
args=(); skip=0
for arg in "$@"; do
  if [[ "$skip" == "1" ]]; then skip=0; continue; fi
  case "$arg" in
    -o|-g) skip=1 ;;
    *) args+=("$arg") ;;
  esac
done
printf 'install %s\\n' "${args[*]}" >> "${TLS_STUB_LOG:?}"
fail_dest="$(cat "${TLS_STUB_STATE:?}/install_fail_dest" 2>/dev/null || true)"
if [[ -n "$fail_dest" ]]; then
  dest="${args[${#args[@]}-1]:-}"
  if [[ "$dest" == "$fail_dest" ]]; then
    echo "install refuse (fail_dest simule): $dest" >> "${TLS_STUB_LOG:?}"
    exit 1
  fi
fi
exec /usr/bin/install "${args[@]}"
"""

STUB_NGINX = """#!/usr/bin/env bash
printf 'nginx %s\\n' "$*" >> "${TLS_STUB_LOG:?}"
mode="$(cat "${TLS_STUB_STATE:?}/nginx" 2>/dev/null || echo ok)"
if [[ "$mode" == "fail" ]]; then
  echo "nginx: configuration file test failed (double)" >&2
  exit 1
fi
exit 0
"""

STUB_SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "${TLS_STUB_LOG:?}"
mode="$(cat "${TLS_STUB_STATE:?}/systemctl" 2>/dev/null || echo ok)"
if [[ "$mode" == "fail" ]]; then
  echo "systemctl: reload failed (double)" >&2
  exit 1
fi
exit 0
"""

STUB_CURL = """#!/usr/bin/env bash
# Double du probe ACME : sert le contenu du webroot du bac à sable, avec une
# éventualité de N premiers appels en échec (retries bornés).
printf 'curl %s\\n' "$*" >> "${TLS_STUB_LOG:?}"
count_file="${TLS_STUB_STATE:?}/curl_calls"
count="$(cat "$count_file" 2>/dev/null || echo 0)"
count=$((count + 1))
printf '%s\\n' "$count" > "$count_file"
fail_first="$(cat "${TLS_STUB_STATE:?}/curl_fail_first" 2>/dev/null || echo 0)"
if (( count <= fail_first )); then exit 22; fi
webroot="$(cat "${TLS_STUB_STATE:?}/webroot" 2>/dev/null || echo)"
if [[ -n "$webroot" && -d "$webroot/.well-known/acme-challenge" ]]; then
  cat "$webroot"/.well-known/acme-challenge/wallia-probe-* 2>/dev/null && exit 0
fi
exit 22
"""


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _generate_pair(cert: Path, key: Path) -> None:
    proc = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "2", "-subj", f"/CN={DOMAIN}",
            "-addext", f"subjectAltName=DNS:{DOMAIN}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


class TlsSandbox:
    """Bac à sable non privilégié : script copié aux racines réécrites + doubles."""

    TEMPLATES = {
        "nginx-wallia-http-initial.conf": "# HTTP-INITIAL\nserver { listen 80; }\n",
        "nginx-wallia-le-bootstrap.conf": "# LE-BOOTSTRAP\n",
        "nginx-wallia-final.conf": "# FINAL\n",
    }

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.state = root / "stub-state"
        self.log = root / "stub.log"
        self.etc = root / "etc"
        self.var = root / "var"
        for directory in (
            self.bin,
            self.state,
            self.etc / "nginx" / "sites-available",
            self.etc / "nginx" / "sites-enabled",
            self.etc / "wallia" / "tls-templates",
            self.etc / "letsencrypt" / "live" / DOMAIN,
            self.var / "lib" / "wallia" / "certificates",
            self.var / "lib" / "wallia" / "vhost-backups",
            self.var / "www" / "wallia-acme" / ".well-known" / "acme-challenge",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for name, content in self.TEMPLATES.items():
            (self.etc / "wallia" / "tls-templates" / name).write_text(content, encoding="utf-8")
        # Le webroot est dans le bac à sable : le double curl le connaît.
        (self.state / "webroot").write_text(str(self.var / "www" / "wallia-acme"), encoding="utf-8")

        _executable(self.bin / "id", STUB_ID)
        _executable(self.bin / "install", STUB_INSTALL)
        _executable(self.bin / "nginx", STUB_NGINX)
        _executable(self.bin / "systemctl", STUB_SYSTEMCTL)
        _executable(self.bin / "curl", STUB_CURL)

        self.script = self._rewrite_script()

    @property
    def vhost_file(self) -> Path:
        return self.etc / "nginx" / "sites-available" / DOMAIN

    @property
    def enabled(self) -> Path:
        return self.etc / "nginx" / "sites-enabled" / DOMAIN

    @property
    def backup_dir(self) -> Path:
        return self.var / "lib" / "wallia" / "vhost-backups"

    def _rewrite_script(self) -> Path:
        base = str(self.root)
        text = ACTIVATE_SOURCE.read_text(encoding="utf-8")
        replacements = (
            ('LE_DIR="/etc/letsencrypt/live/$DOMAIN"', f'LE_DIR="{base}/etc/letsencrypt/live/$DOMAIN"'),
            ('MANAGED_ROOT="/var/lib/wallia/certificates"', f'MANAGED_ROOT="{base}/var/lib/wallia/certificates"'),
            ('VHOST_DIR="/etc/nginx/sites-available"', f'VHOST_DIR="{base}/etc/nginx/sites-available"'),
            ('VHOST_ENABLED_DIR="/etc/nginx/sites-enabled"', f'VHOST_ENABLED_DIR="{base}/etc/nginx/sites-enabled"'),
            ('VHOST_BACKUP_DIR="/var/lib/wallia/vhost-backups"', f'VHOST_BACKUP_DIR="{base}/var/lib/wallia/vhost-backups"'),
            ('ACME_WEBROOT="/var/www/wallia-acme"', f'ACME_WEBROOT="{base}/var/www/wallia-acme"'),
            ('TEMPLATES_DIR="/etc/wallia/tls-templates"', f'TEMPLATES_DIR="{base}/etc/wallia/tls-templates"'),
        )
        for needle, replacement in replacements:
            # Garde-fou de harnais : toute dérive des constantes fait échouer le
            # test AVANT d'exécuter une racine réelle.
            assert needle in text, f"constante attendue absente du script: {needle}"
            assert replacement not in text
            text = text.replace(needle, replacement)
        target = self.root / "wallia-tls-activate.sh"
        target.write_text(text, encoding="utf-8")
        return target

    def set_state(self, name: str, value: str) -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def run(self, *args: str, overrides: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["TLS_STUB_STATE"] = str(self.state)
        env["TLS_STUB_LOG"] = str(self.log)
        env["WALLIA_TLS_SNI_RETRIES"] = "2"
        env["WALLIA_TLS_SNI_DELAY"] = "0"
        env["WALLIA_TLS_HTTP_PROBE_RETRIES"] = "2"
        env["WALLIA_TLS_HTTP_PROBE_DELAY"] = "0"
        env["WALLIA_TLS_OPENSSL_TIMEOUT"] = "5"
        env.update(overrides or {})
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture
def tls(tmp_path: Path) -> TlsSandbox:
    return TlsSandbox(tmp_path / "tls-sandbox")


def test_install_http_nominal_installe_et_servi(tls: TlsSandbox) -> None:
    proc = tls.run("--install-http")
    assert proc.returncode == 0, proc.stderr
    assert tls.vhost_file.read_text(encoding="utf-8") == TlsSandbox.TEMPLATES["nginx-wallia-http-initial.conf"]
    assert tls.enabled.is_symlink()
    assert os.readlink(tls.enabled) == str(tls.vhost_file)
    assert any(line.startswith("nginx -t") for line in tls.log_lines())
    assert any(line.startswith("systemctl reload") for line in tls.log_lines())
    assert "ROLLBACK" not in proc.stderr
    # Le fichier de probe est retiré après vérification.
    assert not list((tls.var / "www" / "wallia-acme" / ".well-known" / "acme-challenge").glob("wallia-probe-*"))


def test_nginx_echec_rollback_etat_absent(tls: TlsSandbox) -> None:
    tls.set_state("nginx", "fail")
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert "nginx -t/reload en échec" in proc.stderr
    # UN SEUL rollback (jamais de double restauration).
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    # État précédent ABSENT : le rollback ne laisse rien derrière lui.
    assert not tls.vhost_file.exists()
    assert not tls.enabled.exists() and not tls.enabled.is_symlink()


def test_nginx_echec_rollback_restaure_le_lien_relatif_exact(tls: TlsSandbox) -> None:
    tls.vhost_file.write_text("OLD-VHOST\n", encoding="utf-8")
    tls.vhost_file.chmod(0o640)
    os.symlink("../sites-available/" + DOMAIN, tls.enabled)  # cible RELATIVE d'origine
    tls.set_state("nginx", "fail")
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    # Fichier vhost restauré avec son CONTENU et son MODE exacts.
    assert tls.vhost_file.read_text(encoding="utf-8") == "OLD-VHOST\n"
    assert stat.S_IMODE(tls.vhost_file.stat().st_mode) == 0o640
    # Lien sites-enabled restauré avec sa cible TEXTUELLE relative exacte.
    assert tls.enabled.is_symlink()
    assert os.readlink(tls.enabled) == "../sites-available/" + DOMAIN


def test_nginx_echec_rollback_restaure_le_fichier_enabled_exact(tls: TlsSandbox) -> None:
    tls.vhost_file.write_text("OLD-VHOST\n", encoding="utf-8")
    tls.vhost_file.chmod(0o600)
    tls.enabled.write_text("ENABLED-OLD\n", encoding="utf-8")  # FICHIER, pas un lien
    tls.enabled.chmod(0o640)
    tls.set_state("nginx", "fail")
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    assert not tls.enabled.is_symlink()
    assert tls.enabled.read_text(encoding="utf-8") == "ENABLED-OLD\n"
    assert stat.S_IMODE(tls.enabled.stat().st_mode) == 0o640
    assert tls.vhost_file.read_text(encoding="utf-8") == "OLD-VHOST\n"
    assert stat.S_IMODE(tls.vhost_file.stat().st_mode) == 0o600
    # Deux sauvegardes DISTINCTES, préfixées par leur rôle : la capture du
    # fichier enabled ne peut plus écraser celle du vhost (même basename,
    # même seconde, même processus).
    backups = sorted(p.name for p in tls.backup_dir.iterdir())
    assert len(backups) == 2, backups
    assert len(set(backups)) == 2
    assert any("VHOST_STATE" in name for name in backups), backups
    assert any("ENABLED_STATE" in name for name in backups), backups


def test_rollback_partiel_restauration_en_echec_pas_de_succes_global(tls: TlsSandbox) -> None:
    """Une restauration en échec ⇒ AUCUNE phrase de succès global."""
    tls.vhost_file.write_text("OLD-VHOST\n", encoding="utf-8")
    tls.vhost_file.chmod(0o600)
    tls.enabled.write_text("ENABLED-OLD\n", encoding="utf-8")
    tls.enabled.chmod(0o640)
    # Double non privilégié : la restauration du fichier enabled échoue
    # (aucun privilège réel, aucun Nginx réel).
    tls.set_state("install_fail_dest", str(tls.enabled))
    tls.set_state("nginx", "fail")
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    # Le vhost a bien été restauré (contenu/mode exacts)…
    assert tls.vhost_file.read_text(encoding="utf-8") == "OLD-VHOST\n"
    assert stat.S_IMODE(tls.vhost_file.stat().st_mode) == 0o600
    # …mais une restauration a échoué : pas de succès global, intervention
    # signalée, aucun rechargement nginx sur un état mixte.
    assert "rollback appliqué" not in proc.stderr
    assert "INTERVENTION MANUELLE requise" in proc.stderr
    assert not any(line.startswith("systemctl reload") for line in tls.log_lines())
    # enabled n'a PAS été restauré : il porte encore le lien installé par la phase.
    assert tls.enabled.is_symlink()


def test_install_en_echec_rollback_exact(tls: TlsSandbox) -> None:
    tls.vhost_file.write_text("OLD-VHOST\n", encoding="utf-8")
    tls.vhost_file.chmod(0o644)
    os.symlink(str(tls.vhost_file), tls.enabled)
    # La source (modèle root-owned) devient illisible : install échoue APRÈS la
    # capture → rollback exact attendu, y compris hors nginx/probe.
    template = tls.etc / "wallia" / "tls-templates" / "nginx-wallia-http-initial.conf"
    template.chmod(0o000)
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    assert tls.vhost_file.read_text(encoding="utf-8") == "OLD-VHOST\n"
    assert stat.S_IMODE(tls.vhost_file.stat().st_mode) == 0o644
    assert tls.enabled.is_symlink()
    assert os.readlink(tls.enabled) == str(tls.vhost_file)


def test_probe_acme_echec_rollback(tls: TlsSandbox) -> None:
    tls.set_state("curl_fail_first", "99")  # tous les appels échouent
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert "probe ACME local en échec" in proc.stderr
    assert proc.stderr.count("ROLLBACK de la transaction TLS") == 1
    assert not tls.vhost_file.exists()
    assert not tls.enabled.exists()
    # Retries BORNÉS : deux tentatives, pas une seule.
    assert len([line for line in tls.log_lines() if line.startswith("curl ")]) == 2


def test_probe_acme_retry_puis_succes(tls: TlsSandbox) -> None:
    tls.set_state("curl_fail_first", "1")  # premier appel en échec, puis OK
    proc = tls.run("--install-http")
    assert proc.returncode == 0, proc.stderr
    assert tls.vhost_file.read_text(encoding="utf-8") == TlsSandbox.TEMPLATES["nginx-wallia-http-initial.conf"]
    assert sum(1 for line in tls.log_lines() if line.startswith("curl ")) == 2


def test_template_manquant_refus_avant_mutation(tls: TlsSandbox) -> None:
    (tls.etc / "wallia" / "tls-templates" / "nginx-wallia-http-initial.conf").unlink()
    proc = tls.run("--install-http")
    assert proc.returncode != 0
    assert "modèle root-owned introuvable" in proc.stderr
    assert not tls.vhost_file.exists()
    assert not tls.enabled.exists()
    # Aucun nginx appelé : refus AVANT toute mutation/rechargement.
    assert not any(line.startswith("nginx ") for line in tls.log_lines())
