"""Socle des tests de livraison Wallia (AUCUN Docker réel, aucun réseau, aucun root).

Les scripts shell sont exécutés dans un BAC À SABLE : copie de `scripts/` et du
`docker-compose.yml` dans un répertoire temporaire, `PATH` préfixé par des
doubles `docker` et `git` pilotés par un état JSON, `runtime/` propre au bac à
sable (données, sauvegardes, état de déploiement). Aucun conteneur n'est lancé,
aucun secret réel n'est utilisé ; la CI quality n'exécute pas ces tests (ils
sont lancés explicitement par le principal/Astra : `python -m pytest tests/delivery`).

Le double `docker` journalise TOUS ses appels (ordre inclus) dans
`state["calls"]` : les assertions portent sur ce journal et sur l'état final.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COPIED_SCRIPTS = (
    "lib.sh",
    "build.sh",
    "deploy.sh",
    "rollback.sh",
    "backup.sh",
    "restore.sh",
    "restore_lib.py",
    "fetch_models.py",
    "delivery_state.py",
    "delivery_env_check.py",
)

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""Double de `docker` pour les tests (état JSON, aucun démon Docker)."""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["WALLIA_FAKE_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))


def save():
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


args = sys.argv[1:]
state.setdefault("calls", []).append(" ".join(args))
save()

if not args:
    die("fake docker: sans argument")


def compose():
    rest = args[1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        if rest[i] in ("-p", "-f", "--env-file", "--project-directory"):
            i += 2
        else:
            i += 1
    sub = rest[i:]
    if not sub:
        die("fake docker compose: sous-commande manquante")
    command, tail = sub[0], sub[1:]
    if command == "config":
        # Le rendu reflète l'image EFFECTIVEMENT demandée via WALLIA_IMAGE (le
        # vrai compose rend ${WALLIA_IMAGE:-...}) : jamais un ID factice.
        data = json.loads(json.dumps(state.get("config_json") or {}))
        override = os.environ.get("WALLIA_IMAGE", "")
        if override and isinstance(data.get("services"), dict):
            for svc in ("api", "worker"):
                entry = data["services"].get(svc)
                if isinstance(entry, dict):
                    entry["image"] = override
        if "--format" in tail and "json" in tail:
            print(json.dumps(data, ensure_ascii=False))
        else:
            print(state.get("config_yaml") or "name: wallia\n")
        sys.exit(0)
    if command == "ps":
        if "--status" in tail:
            mode = state.get("fail_ps_running")
            after_stop = any(" stop " in f" {call} " for call in state.get("calls", [])[:-1])
            if mode is True or (mode == "after_stop" and after_stop):
                die("fake docker: compose ps en échec (injection)", 1)
            for svc in state.get("running", []):
                print(svc)
            sys.exit(0)
        if "-q" in tail:
            svc = tail[tail.index("-q") + 1]
            if svc in state.get("running", []):
                print("cid-" + svc)
            sys.exit(0)
        if "--format" in tail and "Names" in tail[tail.index("--format") + 1]:
            for name in state.get("names", []):
                print(name)
            sys.exit(0)
        sys.exit(0)
    if command == "stop":
        targets = [a for a in tail if not a.startswith("-")]
        if any(t in state.get("fail_stop", []) for t in targets):
            die("fake docker: stop refusé (injection)", 1)
        state["running"] = [s for s in state.get("running", []) if s not in targets]
        save()
        sys.exit(0)
    if command == "start":
        targets = [a for a in tail if not a.startswith("-")]
        if any(t in state.get("fail_start", []) for t in targets):
            die("fake docker: start refusé (injection)", 1)
        running = state.setdefault("running", [])
        for svc in targets:
            if svc not in running:
                running.append(svc)
        save()
        sys.exit(0)
    if command == "up":
        targets = [a for a in tail if a in ("db", "api", "worker")]
        if "--wait" in tail and state.get("fail_up"):
            save()
            die("fake docker: up --wait en échec (injection)", 1)
        running = state.setdefault("running", [])
        for svc in targets:
            if svc not in running:
                running.append(svc)
        save()
        sys.exit(0)
    if command == "run":
        if state.get("fail_run"):
            die("fake docker: run en échec (injection)", 1)
        sys.exit(0)
    if command == "exec":
        joined = " ".join(tail)
        if "pg_dump" in joined:
            print("-- fake wallia dump --")
            sys.exit(0)
        if "stored_relpath" in joined:
            print(state.get("refs_sql") or "")
            sys.exit(0)
        if "count(*)" in joined:
            print(state.get("counts_sql") or "")
            sys.exit(0)
        sys.exit(0)
    save()
    die("fake docker compose: sous-commande non simulée: " + command)


if args[0] == "compose":
    compose()

if args[0] == "inspect":
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    ref = args[-1]
    containers = state.get("containers", {})
    if "{{.Id}}" in fmt:
        print(state.get("image_ids", {}).get(ref, ""))
        sys.exit(0)
    if ".State.Health" in fmt:
        svc = ref[4:] if ref.startswith("cid-") else ref
        print(state.get("health", {}).get(svc, "healthy"))
        sys.exit(0)
    if ".Config.Env" in fmt:
        print(json.dumps(containers.get(ref, {}).get("env", []), ensure_ascii=False))
        sys.exit(0)
    if ".Config.Image" in fmt:
        print(containers.get(ref, {}).get("image", ""))
        sys.exit(0)
    if "{{.Image}}" in fmt:
        print(containers.get(ref, {}).get("image_id", ""))
        sys.exit(0)
    if "Labels" in fmt or "revision" in fmt:
        print(state.get("labels", {}).get(ref, ""))
        sys.exit(0)
    print("")
    sys.exit(0)

if args[:2] == ["image", "inspect"]:
    ref = args[-1]
    known = set(state.get("images", [])) | set(state.get("image_ids", {}))
    sys.exit(0 if ref in known else 1)

if args[0] == "ps":
    if "--filter" in args and "label=com.docker.compose.project=wallia" in " ".join(args):
        for name in state.get("project_containers", []):
            print(name)
        sys.exit(0)
    if "--format" in args and "Names" in args[args.index("--format") + 1]:
        for name in state.get("all_containers", []):
            print(name)
        sys.exit(0)
    sys.exit(0)

if args[0] == "run":
    sys.exit(0)

die("fake docker: commande non simulée: " + args[0])
'''

FAKE_GIT = r'''#!/usr/bin/env python3
"""Double de `git` pour les tests (état JSON, aucun dépôt réel)."""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["WALLIA_FAKE_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
state.setdefault("calls", []).append("git " + " ".join(sys.argv[1:]))
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

args = sys.argv[1:]
if args[:1] == ["-C"]:
    args = args[2:]
if not args:
    sys.exit(0)
command, rest = args[0], args[1:]
head = state.get("head_sha", "0" * 40)
if command == "rev-parse":
    if "--abbrev-ref" in rest:
        print(state.get("branch", "main"))
    elif "--verify" in rest:
        print(head)
    elif any(a.startswith("--short") for a in rest):
        print(head[:12])
    else:
        print(head)
    sys.exit(0)
if command == "ls-remote":
    print(head + "\trefs/heads/" + state.get("branch", "main"))
    sys.exit(0)
if command == "diff":
    sys.exit(1 if state.get("dirty") else 0)
if command == "ls-files":
    sys.exit(0)
sys.exit(0)
'''

DEFAULT_STATE = {
    "head_sha": "0" * 40,
    "branch": "main",
    "dirty": False,
    "running": ["api", "worker"],
    "health": {"db": "healthy", "api": "healthy", "worker": "healthy"},
    "images": [],
    "image_ids": {},
    "labels": {},
    "containers": {},
    "names": ["wallia-db-1", "wallia-api-1", "wallia-worker-1"],
    "all_containers": ["wallia-db-1", "wallia-api-1", "wallia-worker-1"],
    "project_containers": [],
    "config_json": None,
    "config_yaml": "name: wallia\nservices: {}\n",
    "counts_sql": "users=1\nconversations=0\nmessages=0\nattachments=1\ndocuments=1\nchunks=0\ningestion_jobs=0\nschema_migrations=2",
    "refs_sql": "attachments=conv1/note.txt\ndocuments=doc1.pdf",
    "calls": [],
}


class DeliverySandbox:
    """Bac à sable jetable : scripts copiés, doubles docker/git, état JSON."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.state_path = root / "fake-state.json"

    # -- chemins -----------------------------------------------------------
    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    def script(self, name: str) -> Path:
        return self.root / "scripts" / name

    # -- état du double docker --------------------------------------------
    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def update_state(self, **updates) -> None:
        state = self.state()
        state.update(updates)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def calls(self) -> list[str]:
        return self.state().get("calls", [])

    # -- environnement / exécution ----------------------------------------
    def env(self, overrides: dict | None = None) -> dict:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["WALLIA_FAKE_STATE"] = str(self.state_path)
        tmp = self.root / "tmp"
        tmp.mkdir(exist_ok=True)
        env["TMPDIR"] = str(tmp)
        for key in ("WALLIA_IMAGE", "WALLIA_COMPOSE_PROJECT", "WALLIA_HEALTH_TIMEOUT", "WALLIA_HEALTH_POLL"):
            env.pop(key, None)
        env["WALLIA_HEALTH_TIMEOUT"] = "30"
        env["WALLIA_HEALTH_POLL"] = "1"
        env.update(overrides or {})
        return env

    def run(self, script: str, *args: str, overrides: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.script(script)), *args],
            cwd=str(self.root),
            env=self.env(overrides),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def run_python(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args],
            cwd=str(self.root),
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=120,
        )

    # -- entrées de test ---------------------------------------------------
    def write_env_file(self, name: str = "app.production.env", **values: str) -> Path:
        content = {
            "WALLIA_ENV": "production",
            "WALLIA_COOKIE_SECURE": "1",
            "WALLIA_ALLOWED_ORIGINS": "https://wallia.valdev.me",
            "WALLIA_PROVIDER_ENDPOINT": "https://api.deepseek.com/v1",
            "WALLIA_PROVIDER_ALLOWED_DOMAINS": "api.deepseek.com",
            "WALLIA_PROVIDER_MODEL": "deepseek-flash",
        }
        content.update(values)
        path = self.runtime / "secrets" / name
        path.write_text("".join(f"{key}={value}\n" for key, value in content.items()), encoding="utf-8")
        path.chmod(0o600)
        return path

    def strict_config(self) -> dict:
        """Sortie `docker compose config --format json` STRICTE pour le HEAD courant."""
        image = f"wallia:{self.state()['head_sha']}"
        env = {
            "WALLIA_ENV": "production",
            "WALLIA_COOKIE_SECURE": "1",
            "WALLIA_ALLOWED_ORIGINS": "https://wallia.valdev.me",
            "WALLIA_PROVIDER_ENDPOINT": "https://api.deepseek.com/v1",
            "WALLIA_PROVIDER_ALLOWED_DOMAINS": "api.deepseek.com",
        }
        return {
            "name": "wallia",
            "services": {
                "db": {"image": "pgvector/pgvector:pg17"},
                "api": {"image": image, "environment": dict(env)},
                "worker": {"image": image, "environment": dict(env)},
                # Le faux fournisseur de test du dépôt vit sous profil : il ne
                # peut jamais démarrer via `up db api worker`.
                "fake-upstream": {"image": image, "profiles": ["testtools"]},
            },
        }

    def prepare_image(self, state: dict) -> None:
        """Rend l'image `wallia:<head>` inspectable (labels/ID cohérents)."""
        image = f"wallia:{state['head_sha']}"
        state.setdefault("images", []).append(image)
        state.setdefault("labels", {})[image] = state["head_sha"]
        state.setdefault("image_ids", {})[image] = "sha256:" + "a" * 64

    def deployed_containers(self, state: dict, image_id: str) -> None:
        """Conteneurs api/worker simulés portant RÉELLEMENT `image_id`."""
        containers = state.setdefault("containers", {})
        for service in ("api", "worker"):
            containers[f"cid-{service}"] = {"image_id": image_id}

    def write_current_state(self, image: str = "wallia:" + "0" * 40, image_id: str = "sha256:" + "a" * 64) -> Path:
        """État courant VALIDE : snapshot Compose rendu (JSON) + env, comme deploy.sh."""
        state_dir = self.runtime / "deploy-state"
        snapshot = state_dir / "compose-courant.json"
        snapshot.write_text(
            json.dumps(
                {
                    "name": "wallia",
                    "services": {"api": {"image": image_id}, "worker": {"image": image_id}},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        env = state_dir / "env-courant.env"
        env.write_text("WALLIA_ENV=production\n", encoding="utf-8")
        proc = self.run_python(
            "scripts/delivery_state.py", "write", str(state_dir / "current.json"),
            "--stamp", "x", "--image", image, "--image-id", image_id,
            "--compose", str(snapshot), "--env", str(env),
        )
        assert proc.returncode == 0, proc.stderr
        return state_dir / "current.json"

    def newest_bundle(self) -> Path:
        bundles = sorted((self.runtime / "backups").glob("wallia-bundle-*"))
        assert bundles, "aucun bundle créé"
        return bundles[-1]


def build_sandbox(root: Path, state: dict | None = None) -> DeliverySandbox:
    root.mkdir(parents=True, exist_ok=True)
    sandbox = DeliverySandbox(root)
    sandbox.bin.mkdir()
    docker_stub = sandbox.bin / "docker"
    docker_stub.write_text(FAKE_DOCKER, encoding="utf-8")
    docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    git_stub = sandbox.bin / "git"
    git_stub.write_text(FAKE_GIT, encoding="utf-8")
    git_stub.chmod(git_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    for name in COPIED_SCRIPTS:
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts_dir / name)
    shutil.copy2(REPO_ROOT / "docker-compose.yml", root / "docker-compose.yml")

    (root / "runtime" / "secrets").mkdir(parents=True)
    (root / "runtime" / "data" / "documents").mkdir(parents=True)
    (root / "runtime" / "data" / "uploads" / "conv1").mkdir(parents=True)
    (root / "runtime" / "backups").mkdir(parents=True)
    (root / "runtime" / "deploy-state").mkdir(parents=True)
    (root / "runtime" / "data" / "documents" / "doc1.pdf").write_bytes(b"%PDF-1.4 fake wallia\n")
    (root / "runtime" / "data" / "uploads" / "conv1" / "note.txt").write_bytes(b"note de test\n")

    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged.update(state or {})
    sandbox.state_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return sandbox


@pytest.fixture
def delivery(tmp_path: Path) -> DeliverySandbox:
    """Bac à sable fonctionnel (état par défaut : api+worker actifs, tout sain)."""
    return build_sandbox(tmp_path / "sandbox")
