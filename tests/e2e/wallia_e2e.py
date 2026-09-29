#!/usr/bin/env python3
"""Recette E2E VPS canonique Wallia (lot4b) — runner Playwright **Python**.

Portage RÉEL des invariants des specs TypeScript historiques
(`tests/e2e/specs/*.spec.ts`, non exécutables ici : installation npm Playwright
refusée) vers un runner Python exécuté avec l'outillage préinstallé :

    PYTHONDONTWRITEBYTECODE=1 /home/hermes/fortiupgrade-convergence-venv/bin/python \
        tests/e2e/wallia_e2e.py --projects desktop,mobile

Cibles : Chromium **desktop 1440x900** et **mobile 390x844 tactile**
(pas Safari/WebKit). Runtime attendu : la pile de recette isolée
`wallia-e2e` (API 127.0.0.1:13746 + faux fournisseur local), jamais la pile
live. Compte : celui du fichier `runtime/e2e-isolated/credentials.json`
(synthétique, jamais journalisé ni recopié dans les preuves).

Sorties : JSON par projet + résumé sous `runtime/evidence/lot4-e2e-*.json`,
captures `runtime/evidence/lot4-*-<projet>.png`. Code de sortie non nul si au
moins un contrôle échoue, si une erreur console/HTTP inattendue est observée
ou si des captures manquent.

Doubles EXPLICITEMENT étiquetés (mécanique d'interface uniquement) : embeddings
`fixture`, reranker `fixture`, fournisseur `fake-upstream`. Aucune revendication
de chat natif, de RAG sémantique ni de capacité WALLIX.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("WALLIA_E2E_BASE_URL", "http://127.0.0.1:13746").rstrip("/")
CREDENTIALS_FILE = Path(
    os.environ.get("WALLIA_E2E_CREDENTIALS", str(ROOT / "runtime/e2e-isolated/credentials.json"))
)
EVIDENCE_DIR = Path(os.environ.get("WALLIA_EVIDENCE_DIR", str(ROOT / "runtime/evidence")))
FAKE_UPSTREAM_ENV = os.environ.get("WALLIA_E2E_FAKE_UPSTREAM", "").strip()

STREAM_TIMEOUT_MS = 45_000
UI_TIMEOUT_MS = 15_000
RUN_TAG = time.strftime("%H%M%S")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

Q_MAIN = "Sur l'Aster 10.10, que vérifier quand le voyant ambre clignote ?"
Q_ATT = "Analyse locale de la capture jointe (vision inactive)."
Q_STOP = "Peux-tu détailler la procédure complète, étape par étape ?"
Q_ERROR = "Test d'erreur contrôlée du fournisseur local."
Q_XSS = "Rappel du voyant ambre Aster 10.10 avec le formatage."
Q_B = "Message du cas B uniquement."
Q_E = "Message du cas E avant le finaliseur retardé."
Q_D2 = "Second flux pendant un finaliseur retardé."

# --- Classification EXPLICITE des erreurs HTTP/console attendues (par URL+statut) ---
EXPECTED_FAILURES = [
    {
        "path": "/api/auth/me",
        "status": 401,
        "min": 1,
        "max": 1,
        "reason": "bootstrap : session absente avant connexion (attendu)",
    },
    {
        "path": "/api/auth/login",
        "status": 401,
        "min": 1,
        "max": 1,
        "reason": "tentative volontairement invalide (contrôle d'erreur)",
    },
    {
        "path": "/api/documents",
        "status": 415,
        "min": 1,
        "max": 1,
        "reason": "import non-PDF volontairement refusé par le serveur",
    },
]


class CheckFailure(Exception):
    """Échec de contrôle : le rapport doit le dire, jamais l'arrondir."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_credentials() -> dict:
    payload = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    if not payload.get("email") or not payload.get("password"):
        raise RuntimeError(f"identifiants illisibles dans {CREDENTIALS_FILE}")
    return payload


def resolve_fake_upstream() -> str:
    if FAKE_UPSTREAM_ENV:
        return FAKE_UPSTREAM_ENV.rstrip("/")
    try:
        ip = subprocess.run(
            [
                "docker",
                "inspect",
                "wallia-e2e-fake-upstream-1",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "faux amont introuvable : définir WALLIA_E2E_FAKE_UPSTREAM (http://ip:8099)"
        ) from exc
    if not ip:
        raise RuntimeError("IP du conteneur faux amont vide")
    return f"http://{ip}:8099"


def minimal_pdf(text: str) -> bytes:
    """PDF minimal VALIDE (une page, texte ASCII) pour l'import réel."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


class ApiClient:
    """Client API indépendant du navigateur (vérités serveur + nettoyage)."""

    def __init__(self, base_url: str, credentials: dict) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.csrf and method not in ("GET", "HEAD", "OPTIONS"):
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
            return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def login(self) -> None:
        status, body, _ = self._request(
            "POST",
            "/api/auth/login",
            {"email": self.credentials["email"], "password": self.credentials["password"]},
        )
        if status != 200:
            raise CheckFailure(f"login API indépendant : HTTP {status}")
        self.csrf = json.loads(body)["csrf_token"]

    async def call(self, method: str, path: str, payload: dict | None = None):
        return await asyncio.to_thread(self._request, method, path, payload)

    async def json(self, method: str, path: str, payload: dict | None = None, relogin_on_401: bool = True):
        status, body, _ = await self.call(method, path, payload)
        if status == 401 and relogin_on_401:
            await asyncio.to_thread(self.login)
            status, body, _ = await self.call(method, path, payload)
        if status not in (200, 201, 202):
            raise CheckFailure(f"{method} {path} -> HTTP {status}")
        return json.loads(body)


class UpstreamCtl:
    """Contrôle du faux fournisseur local (modes SSE de recette)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as response:
            return json.loads(response.read())

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    async def set_mode(self, mode: str) -> None:
        await asyncio.to_thread(self._post, "/mode", {"mode": mode})

    async def state(self) -> dict:
        return await asyncio.to_thread(self._get, "/state")


# ---------------------------------------------------------------------------
# Recette
# ---------------------------------------------------------------------------

class Recette:
    def __init__(self, project: str, viewport: dict, *, is_mobile: bool, has_touch: bool) -> None:
        self.project = project
        self.viewport = viewport
        self.is_mobile = is_mobile
        self.has_touch = has_touch
        self.checks: list[dict] = []
        self.console_errors: list[dict] = []
        self.page_errors: list[str] = []
        self.http_failures: list[dict] = []
        self.screenshots: list[str] = []
        self.dialog_events: list[str] = []
        self.credentials = load_credentials()
        self.api = ApiClient(BASE_URL, self.credentials)
        self.upstream = UpstreamCtl(resolve_fake_upstream())
        self.conversations: dict[str, str] = {}  # label -> id (nettoyage)
        self.cleanup_ids: list[str] = []
        self.started_at = _now()
        self.finished_at: str | None = None
        self.notes: list[str] = []
        self._dialog_handler = None

    # -- journalisation ----------------------------------------------------
    async def phase(self, name: str, coro) -> bool:
        started = time.monotonic()
        try:
            await coro
            elapsed = int((time.monotonic() - started) * 1000)
            self.checks.append({"name": name, "status": "PASS", "elapsed_ms": elapsed})
            print(f"  [PASS] {name} ({elapsed} ms)", flush=True)
            return True
        except CheckFailure as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            self.checks.append({"name": name, "status": "FAIL", "elapsed_ms": elapsed, "detail": str(exc)})
            print(f"  [FAIL] {name} ({elapsed} ms): {exc}", flush=True)
            await self._failure_shot(name)
            return False
        except Exception as exc:  # noqa: BLE001 — jamais un crash silencieux
            elapsed = int((time.monotonic() - started) * 1000)
            self.checks.append(
                {
                    "name": name,
                    "status": "FAIL",
                    "elapsed_ms": elapsed,
                    "detail": f"exception {exc.__class__.__name__}: {exc}",
                }
            )
            print(f"  [FAIL] {name} ({elapsed} ms): {exc.__class__.__name__}: {exc}", flush=True)
            await self._failure_shot(name)
            return False

    async def _failure_shot(self, name: str) -> None:
        page = getattr(self, "_current_page", None)
        if page is None:
            return
        try:
            await self.shot(page, f"FAIL-{name}")
        except Exception:  # noqa: BLE001 — la capture d'échec ne masque jamais l'échec réel
            pass

    async def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise CheckFailure(message)

    # -- attentes ----------------------------------------------------------
    async def poll(self, fn, expected, timeout_ms: int, message: str, interval: float = 0.2):
        deadline = time.monotonic() + timeout_ms / 1000
        last = None
        while time.monotonic() < deadline:
            last = await fn()
            if last == expected:
                return last
            await asyncio.sleep(interval)
        raise CheckFailure(f"attente échouée : {message} (dernier={last!r}, attendu={expected!r})")

    async def poll_truthy(self, fn, timeout_ms: int, message: str, interval: float = 0.25):
        deadline = time.monotonic() + timeout_ms / 1000
        last = None
        while time.monotonic() < deadline:
            last = await fn()
            if last:
                return last
            await asyncio.sleep(interval)
        raise CheckFailure(f"attente échouée : {message} (dernier={last!r})")

    async def wait_visible(self, locator, message: str, timeout_ms: int = UI_TIMEOUT_MS) -> None:
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # Cause réelle conservée dans le diagnostic (classe d'exception),
            # jamais un secret ni le détail du sélecteur ambigu.
            raise CheckFailure(f"élément invisible : {message} ({exc.__class__.__name__})") from exc

    # -- captures / overflow ----------------------------------------------
    async def shot(self, page: Page, name: str) -> None:
        path = EVIDENCE_DIR / f"lot4-{name}-{self.project}.png"
        await page.screenshot(path=str(path), full_page=True)
        self.screenshots.append(str(path))

    async def wait_toast_gone(self, page: Page, text: str, timeout_ms: int = 9000) -> None:
        await self.poll(
            lambda: page.locator(".toast", has_text=text).count(), 0, timeout_ms, f"toast disparu : {text}"
        )

    async def assert_no_overflow(self, page: Page, context: str) -> None:
        values = await page.evaluate(
            "() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})"
        )
        await self.assert_true(
            values["scrollWidth"] <= values["innerWidth"] + 2,
            f"débordement horizontal ({values['scrollWidth']}px > {values['innerWidth']}px) — {context}",
        )

    # -- composants --------------------------------------------------------
    def chat_title(self, page: Page):
        return page.locator(".chat-title h2")

    def composer(self, page: Page):
        return page.locator(".composer textarea")

    def settled(self, page: Page):
        return page.locator(".message-assistant:not(.message-streaming)")

    def streaming(self, page: Page):
        return page.locator(".message-streaming")

    async def ensure_sidebar(self, page: Page) -> None:
        if self.is_mobile:
            if await page.locator(".sidebar.sidebar-open").count() == 0:
                await page.get_by_role("button", name="Ouvrir le menu").click()
                await self.wait_visible(page.locator(".sidebar.sidebar-open"), "drawer ouvert")
        else:
            await self.wait_visible(page.locator(".sidebar"), "sidebar desktop")

    async def close_sidebar_if_mobile(self, page: Page) -> None:
        if self.is_mobile and await page.locator(".sidebar.sidebar-open").count():
            await page.get_by_role("button", name="Fermer le menu").click()
            await self.poll(lambda: page.locator(".sidebar.sidebar-open").count(), 0, 5000, "drawer fermé")

    async def nav(self, page: Page, name: str) -> None:
        await self.ensure_sidebar(page)
        await page.get_by_role("button", name=name).click()
        await self.close_sidebar_if_mobile(page)

    async def sidebar_item(self, page: Page, title: str):
        await self.ensure_sidebar(page)
        return page.locator(".conv-item", has_text=title).first

    async def click_sidebar_item(self, page: Page, title: str) -> None:
        item = await self.sidebar_item(page, title)
        await item.click()

    async def rename_conversation(self, page: Page, old_title: str, new_title: str) -> None:
        item = await self.sidebar_item(page, old_title)
        await item.locator('button[title="Renommer"]').click()
        field = page.locator(".conv-rename input")
        await self.wait_visible(field, "champ de renommage")
        await field.fill(new_title)
        await field.press("Enter")
        await self.poll(
            lambda: page.locator(".conv-item", has_text=new_title).count(), 1, 8000, f"renommage {new_title}"
        )
        await self.close_sidebar_if_mobile(page)

    async def create_conversation(self, page: Page) -> dict:
        await self.ensure_sidebar(page)
        async with page.expect_response(
            lambda r: r.url == f"{BASE_URL}/api/conversations" and r.request.method == "POST"
        ) as waiter:
            await page.get_by_role("button", name="Nouvelle conversation", exact=True).click()
        response = await waiter.value
        await self.assert_true(response.status == 201, f"création HTTP {response.status}")
        conversation = await response.json()
        self.cleanup_ids.append(conversation["id"])
        await self.close_sidebar_if_mobile(page)
        return conversation

    async def open_case(self, page: Page, title: str, conversation_id: str | None = None) -> None:
        if conversation_id:
            await self.ensure_sidebar(page)
        await self.click_sidebar_item(page, title)
        await self.poll(lambda: self.chat_title(page).inner_text(), title, UI_TIMEOUT_MS, f"cas affiché {title}")
        await self.close_sidebar_if_mobile(page)

    async def _settled_and_idle(self, page: Page, before: int) -> bool:
        return (await self.settled(page).count()) == before + 1 and (await self.streaming(page).count()) == 0

    async def wait_settled_plus_one(self, page: Page, before: int, timeout_ms: int = STREAM_TIMEOUT_MS) -> None:
        await self.poll(
            lambda: self._settled_and_idle(page, before), True, timeout_ms, "réponse terminée (nouveau message)"
        )

    async def wait_streaming_visible(self, page: Page, timeout_ms: int = 15_000) -> None:
        await self.wait_visible(self.streaming(page), "bulle de génération", timeout_ms)

    # -- cycle de vie ------------------------------------------------------
    async def run(self, browser) -> dict:
        context = await browser.new_context(
            viewport=self.viewport,
            is_mobile=self.is_mobile,
            has_touch=self.has_touch,
            device_scale_factor=2 if self.is_mobile else 1,
        )
        page = await context.new_page()
        self._current_page = page
        page.set_default_timeout(UI_TIMEOUT_MS)

        def on_console(message) -> None:
            if message.type == "error":
                location = message.location or {}
                self.console_errors.append(
                    {"text": message.text, "url": location.get("url", ""), "line": location.get("lineNumber")}
                )

        async def on_dialog(dialog) -> None:
            self.dialog_events.append(f"{dialog.type}:{dialog.message[:120]}")
            await dialog.accept()

        page.on("console", on_console)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("dialog", on_dialog)
        page.on("response", self._on_response)

        print(f"[{self.project}] recette {self.viewport} démarrée", flush=True)
        try:
            await self.phase("01-bootstrap-401", self._p01_bootstrap(page))
            await self.phase("02-login-erreur-controlee", self._p02_login_guard(page))
            await self.phase("03-connexion-drawer-overflow", self._p03_login(page))
            await self.phase("04-cas-a-dialogue-sources", self._p04_case_a(page))
            await self.phase("05-lien-original-authentifie", self._p05_original_link(page, context, browser))
            await self.phase("06-etat-du-cas-sauvegarde", self._p06_case_state(page))
            await self.phase("07-pieces-jointes-image", self._p07_attachments(page, context))
            await self.phase("08-stop-retry", self._p08_stop_retry(page))
            await self.phase("09-erreur-fournisseur", self._p09_error(page))
            await self.phase("10-xss-markdown-citations", self._p10_xss(page))
            await self.phase("11-cas-b-message", self._p11_case_b(page))
            await self.phase("12-race-get-a-n-ecrase-pas-b", self._p12_race_get(page))
            await self.phase("13-separation-deux-cas", self._p13_separation(page))
            await self.phase("14-race-createur", self._p14_race_create(page))
            await self.phase("15-logout-invalide-upload", self._p15_race_logout_upload(page))
            await self.phase("16-logout-invalide-stream", self._p16_race_logout_stream(page))
            await self.phase("17-logout-invalide-create-refresh", self._p17_race_logout_create(page))
            await self.phase("18-finaliseur-retarde", self._p18_finalizer(page))
            await self.phase("18b-suppression-de-cas-ui", self._p18b_delete_case(page))
            await self.phase("19-bibliotheque-jobs-import", self._p19_library(page))
            await self.phase("20-administration-secret", self._p20_admin(page))
            await self.phase("21-mot-de-passe-reccette", self._p21_password(page))
            await self.phase("22-deconnexion-finale", self._p22_logout(page))
            await self.phase("23-nettoyage", self._p23_cleanup())
        finally:
            self.finished_at = _now()
            await context.close()

        self.classify_failures()
        return self.report()

    # -- classification console/HTTP --------------------------------------
    def _on_response(self, response) -> None:
        if response.status >= 400:
            self.http_failures.append(
                {"url": response.url, "status": response.status, "method": response.request.method}
            )

    def classify_failures(self) -> None:
        self.http_classified = []
        self.http_unexpected = []
        for failure in self.http_failures:
            matched = self._match_expected(failure["url"], failure["status"])
            entry = {**failure, "expected": bool(matched), "reason": matched["reason"] if matched else None}
            (self.http_classified if matched else self.http_unexpected).append(entry)

        self.console_classified = []
        self.console_unexpected = []
        for message in self.console_errors:
            matched = None
            for expected in EXPECTED_FAILURES:
                if expected["path"] in (message.get("url") or "") and str(expected["status"]) in message["text"]:
                    matched = expected
                    break
            entry = {**message, "expected": bool(matched), "reason": matched["reason"] if matched else None}
            (self.console_classified if matched else self.console_unexpected).append(entry)

        self.expected_counts = {}
        for expected in EXPECTED_FAILURES:
            key = f"{expected['path']}#{expected['status']}"
            self.expected_counts[key] = {
                "http": sum(1 for f in self.http_classified if expected["path"] in f["url"] and f["status"] == expected["status"]),
                "console": sum(
                    1 for c in self.console_classified if expected["path"] in (c.get("url") or "")
                ),
                "min": expected["min"],
                "max": expected["max"],
                "reason": expected["reason"],
            }

    def _match_expected(self, url: str, status: int):
        for expected in EXPECTED_FAILURES:
            if expected["path"] in url and expected["status"] == status:
                return expected
        return None

    def report(self) -> dict:
        failed = [c for c in self.checks if c["status"] != "PASS"]
        expected_ok = all(
            counts["min"] <= counts["http"] <= counts["max"] or counts["min"] <= counts["console"] <= counts["max"]
            for counts in self.expected_counts.values()
        ) if self.expected_counts else True
        # Chaque catégorie attendue doit être observée AU MOINS une fois (prouvée réelle).
        expected_seen = all(
            (counts["http"] + counts["console"]) >= counts["min"] for counts in self.expected_counts.values()
        )
        expected_not_exceeded = all(
            max(counts["http"], counts["console"]) <= counts["max"] for counts in self.expected_counts.values()
        )
        ok = (
            not failed
            and not self.page_errors
            and not self.console_unexpected
            and not self.http_unexpected
            and expected_seen
            and expected_not_exceeded
        )
        return {
            "project": self.project,
            "viewport": self.viewport,
            "is_mobile": self.is_mobile,
            "base_url": BASE_URL,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": ok,
            "checks": self.checks,
            "checks_failed": [c["name"] for c in failed],
            "page_errors": self.page_errors,
            "http_failures_classified": self.http_classified,
            "http_failures_unexpected": self.http_unexpected,
            "console_errors_classified": self.console_classified,
            "console_errors_unexpected": self.console_unexpected,
            "expected_failure_counts": self.expected_counts,
            "screenshots": self.screenshots,
            "dialog_events": self.dialog_events,
            "conversations_created": dict(self.conversations),
            "cleanup_ids": self.cleanup_ids,
            "notes": self.notes,
            "doubles": {
                "embeddings": "fixture (mécanique UI, non sémantique)",
                "reranker": "fixture (mécanique UI, non sémantique)",
                "provider": "fake-upstream local (pas de chat natif)",
            },
        }

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------
    async def _p01_bootstrap(self, page: Page) -> None:
        # État amont déterministe : le faux fournisseur peut avoir été laissé
        # dans un autre mode par un run précédent interrompu.
        await self.upstream.set_mode("normal")
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        await self.wait_visible(page.locator('input[type="email"]'), "formulaire de connexion")
        await self.assert_no_overflow(page, "écran de connexion")
        await self.assert_true(
            "Wallia" in (await page.title()), "titre de page inattendu"
        )

    async def _p02_login_guard(self, page: Page) -> None:
        await page.locator('input[type="email"]').fill(self.credentials["email"])
        await page.locator('input[type="password"]').fill("mot-de-passe-volontairement-faux-l4b")
        await page.get_by_role("button", name="Se connecter").click()
        await self.wait_visible(page.locator('[role="alert"]'), "message d'erreur de connexion")
        text = await page.locator('[role="alert"]').first.inner_text()
        await self.assert_true("Identifiants invalides" in text, f"message d'erreur inattendu : {text!r}")

    async def _login(self, page: Page, password: str | None = None) -> None:
        await page.locator('input[type="email"]').fill(self.credentials["email"])
        await page.locator('input[type="password"]').fill(password or self.credentials["password"])
        await page.get_by_role("button", name="Se connecter").click()
        await self.poll(
            lambda: page.get_by_role("button", name="Nouvelle conversation", exact=True).count(), 1, UI_TIMEOUT_MS,
            "connexion (bouton Nouvelle conversation)",
        )

    async def _p03_login(self, page: Page) -> None:
        await self._login(page)
        await self.shot(page, "01-accueil")
        if self.is_mobile:
            await page.get_by_role("button", name="Ouvrir le menu").click()
            await self.wait_visible(page.locator(".sidebar.sidebar-open"), "drawer mobile ouvert")
            await self.assert_no_overflow(page, "drawer mobile")
            await self.shot(page, "02-drawer")
            await page.get_by_role("button", name="Fermer le menu").click()
            await self.poll(lambda: page.locator(".sidebar.sidebar-open").count(), 0, 5000, "drawer fermé")
        else:
            await self.wait_visible(page.locator(".sidebar"), "sidebar visible (desktop)")
        await self.assert_no_overflow(page, "vue initiale connectée")

    async def _p04_case_a(self, page: Page) -> None:
        await self.upstream.set_mode("normal")  # parole de double déterministe
        title_a = f"Cas A recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["A"] = conversation["id"]
        await self.rename_conversation(page, "Nouvelle conversation", title_a)
        await self.open_case(page, title_a)
        self._title_a = title_a
        await self.assert_true(
            await self.composer(page).is_visible(), "composer invisible après création du cas"
        )
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_MAIN)
        await page.get_by_title("Envoyer").click()
        await self.wait_settled_plus_one(page, before)
        answer = self.settled(page).last
        text = await answer.inner_text()
        await self.assert_true("Réponse de démonstration locale" in text, "réponse du double absente")
        sources_button = answer.get_by_role("button", name="Sources (")
        await self.assert_true(await sources_button.count() >= 1, "bouton Sources absent")
        await sources_button.first.click()
        await self.wait_visible(page.locator(".panel-sources"), "panneau des sources")
        cards = page.locator(".panel-sources .source-card")
        count = await cards.count()
        await self.assert_true(count == 2, f"nombre de sources attendu 2, obtenu {count}")
        first = cards.nth(0)
        first_text = await first.inner_text()
        await self.assert_true("Fiche démo Aster 10.10" in first_text, "titre de source 1 inattendu")
        await self.assert_true("v. 10.10" in first_text, "version de source 1 absente")
        await self.assert_true("p. 1" in first_text, "page de source 1 absente")
        await self.assert_true("logit 5.00" in first_text, "logit de source 1 inattendu")
        second_text = await cards.nth(1).inner_text()
        await self.assert_true("p. 2" in second_text, "page de source 2 absente")
        self.notes.append(f"source[1] logit=5.00 p.1 ; source[2] p.2 (corpus Aster 10.10)")
        # Citation cliquable [1] : panneau + surbrillance.
        await page.get_by_role("button", name="Fermer le panneau").click()
        await self.poll(lambda: page.locator(".panel-sources").count(), 0, 5000, "panneau fermé")
        citation = answer.locator('.cite[data-citation="1"]')
        await self.assert_true(await citation.count() >= 1, "citation [1] non cliquable")
        await citation.first.click()
        await self.wait_visible(page.locator(".panel-sources"), "panneau (via citation)")
        await self.wait_visible(
            page.locator('.panel-sources .source-card[data-source-index="1"].source-highlight'),
            "source 1 surlignée",
        )
        await self.assert_no_overflow(page, "panneau sources ouvert")
        await self.shot(page, "03-sources")
        await page.get_by_role("button", name="Fermer le panneau").click()

    async def _p05_original_link(self, page: Page, context: BrowserContext, browser) -> None:
        answer = self.settled(page).last
        await answer.get_by_role("button", name="Sources (").first.click()
        await self.wait_visible(page.locator(".panel-sources"), "panneau des sources")
        href = await page.locator(".panel-sources .source-card").first.locator("a", has_text="Ouvrir l'original").get_attribute("href")
        await self.assert_true(bool(href), "lien original absent")
        self.notes.append(f"href original = {href}")
        url = f"{BASE_URL}{href.split('#')[0]}"
        authenticated = await context.request.get(url)
        await self.assert_true(authenticated.status == 200, f"original authentifié -> HTTP {authenticated.status}")
        await self.assert_true(
            "application/pdf" in (authenticated.headers.get("content-type") or ""),
            "content-type original inattendu",
        )
        fresh = await browser.new_context()
        anonymous = await fresh.request.get(url)
        await self.assert_true(anonymous.status == 401, f"original sans session -> HTTP {anonymous.status}")
        await fresh.close()
        await page.get_by_role("button", name="Fermer le panneau").click()

    async def _p06_case_state(self, page: Page) -> None:
        await page.get_by_role("button", name="État du cas").click()
        await self.wait_visible(page.locator(".panel-case"), "panneau état du cas")
        facts_section = page.locator(".case-section", has=page.locator("h4", has_text="Faits établis"))
        values = await facts_section.locator(".case-item input").evaluate_all("els => els.map(e => e.value)")
        await self.assert_true(
            any("produit explicitement déclaré(e) : Aster" in v for v in values),
            f"fait produit absente : {values}",
        )
        await self.assert_true(
            any("version explicitement déclaré(e) : 10.10" in v for v in values),
            f"fait version absente : {values}",
        )
        statuses = await facts_section.locator(".case-item select").evaluate_all("els => els.map(e => e.value)")
        await self.assert_true(all(s == "confirmed" for s in statuses), f"statuts des faits inattendus : {statuses}")
        grid = page.locator(".panel-case .case-grid input")
        await self.assert_true((await grid.nth(0).input_value()) == "Aster", "produit du cas non prérempli")
        await self.assert_true((await grid.nth(1).input_value()) == "10.10", "version du cas non préremplie")
        # Contrôle proposé ajouté puis sauvegardé.
        checks_section = page.locator(".case-section", has=page.locator("h4", has_text="Contrôles proposés"))
        await checks_section.get_by_role("button", name="+ Ajouter").click()
        new_input = checks_section.locator(".case-item input").last
        await new_input.fill("Vérifier la sonde après redémarrage")
        await page.get_by_role("button", name="Enregistrer l'état du cas").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="État du cas enregistré").count(), 1, 8000, "toast de sauvegarde"
        )
        await self.poll(lambda: page.locator(".panel-case").count(), 0, 8000, "panneau refermé après sauvegarde")
        # Reload complet : relecture depuis le serveur.
        await page.reload(wait_until="domcontentloaded")
        await self.poll(lambda: self.chat_title(page).count(), 1, UI_TIMEOUT_MS, "rechargement connecté")
        await self.open_case(page, self._title_a)
        summary = await page.locator(".chat-case-summary").inner_text()
        await self.assert_true("produit : Aster" in summary, f"badge produit absent : {summary!r}")
        await self.assert_true("version : 10.10" in summary, f"badge version absent : {summary!r}")
        await page.get_by_role("button", name="État du cas").click()
        await self.wait_visible(page.locator(".panel-case"), "panneau état du cas (relu)")
        checks_section = page.locator(".case-section", has=page.locator("h4", has_text="Contrôles proposés"))
        proposed_values = await checks_section.locator(".case-item input").evaluate_all("els => els.map(e => e.value)")
        await self.assert_true(
            any("Vérifier la sonde après redémarrage" in v for v in proposed_values),
            f"contrôle proposé non relu : {proposed_values}",
        )
        proposed_status = await checks_section.locator(".case-item select").last.input_value()
        await self.assert_true(proposed_status == "proposed", f"statut du contrôle proposé = {proposed_status}")
        origin = await checks_section.locator(".case-item .case-origin").last.inner_text()
        await self.assert_true("saisie explicite" in origin, f"provenance inattendue : {origin!r}")
        performed = page.locator(".case-section", has=page.locator("h4", has_text="Contrôles réellement effectués"))
        performed_text = await performed.inner_text()
        await self.assert_true("Aucun élément" in performed_text, "contrôles effectués non vides")
        await self.shot(page, "04-etat-du-cas")
        await page.get_by_role("button", name="Fermer le panneau").click()

    async def _p07_attachments(self, page: Page, context: BrowserContext) -> None:
        file_input = page.locator('.composer input[type="file"]')
        await file_input.set_input_files(
            {"name": "capture-lot4b.png", "mimeType": "image/png", "buffer": PNG_1PX}
        )
        await self.poll(lambda: page.locator(".pending-files .file-chip").count(), 1, 10000, "chip après upload")
        # Dépôt (drop) réel dans le composer.
        await page.evaluate(
            """(bytes) => {
                const input = document.querySelector('.composer');
                const data = Uint8Array.from(atob(bytes), c => c.charCodeAt(0));
                const file = new File([data], 'depot-lot4b.png', { type: 'image/png' });
                const dt = new DataTransfer();
                dt.items.add(file);
                input.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
            }""",
            base64.b64encode(PNG_1PX).decode(),
        )
        await self.poll(lambda: page.locator(".pending-files .file-chip").count(), 2, 10000, "chip après dépôt")
        # Collage (paste) réel dans le textarea.
        await page.evaluate(
            """(bytes) => {
                const area = document.querySelector('.composer textarea');
                const data = Uint8Array.from(atob(bytes), c => c.charCodeAt(0));
                const file = new File([data], 'colle-lot4b.png', { type: 'image/png' });
                const dt = new DataTransfer();
                dt.items.add(file);
                const event = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
                Object.defineProperty(event, 'clipboardData', { value: dt });
                area.dispatchEvent(event);
            }""",
            base64.b64encode(PNG_1PX).decode(),
        )
        await self.poll(lambda: page.locator(".pending-files .file-chip").count(), 3, 10000, "chip après collage")
        chips_text = await page.locator(".pending-files").inner_text()
        for name in ("capture-lot4b.png", "depot-lot4b.png", "colle-lot4b.png"):
            await self.assert_true(name in chips_text, f"chip manquant : {name}")
        thumbs = await page.locator(".pending-files .file-thumb").count()
        await self.assert_true(thumbs == 3, f"aperçus image manquants ({thumbs}/3)")
        await self.wait_visible(page.locator(".pending-files .badge", has_text="vision inactive").first, "badge vision")
        await self.shot(page, "05-pieces-jointes")
        # Envoi avec pièces jointes ; l'image est affichée dans le message.
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_ATT)
        await page.get_by_title("Envoyer").click()
        await self.wait_settled_plus_one(page, before)
        user_message = page.locator(".message-user").last
        images = user_message.locator(".message-images img")
        await self.poll(lambda: images.count(), 3, 10000, "images du message")
        natural = await images.first.evaluate("el => el.naturalWidth")
        await self.assert_true(natural > 0, "aperçu image non chargé (naturalWidth=0)")
        src = await images.first.get_attribute("src")
        await self.assert_true(bool(src) and "/content" in src, f"source d'aperçu inattendue : {src}")
        content = await context.request.get(f"{BASE_URL}{src.split('#')[0]}")
        await self.assert_true(content.status == 200, f"contenu image -> HTTP {content.status}")
        await self.assert_true(
            "image/png" in (content.headers.get("content-type") or ""), "content-type image inattendu"
        )
        await self.assert_no_overflow(page, "message avec images")

    async def _p08_stop_retry(self, page: Page) -> None:
        await self.upstream.set_mode("slow")
        state_before = await self.upstream.state()
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_STOP)
        await page.get_by_title("Envoyer").click()
        await self.wait_streaming_visible(page)
        await page.get_by_title("Arrêter la génération").click()
        await self.poll(lambda: self.streaming(page).count(), 0, 20_000, "génération arrêtée")
        await self.poll(lambda: self.settled(page).count(), before + 1, 10_000, "message annulé persisté")
        cancelled = self.settled(page).last
        cancelled_text = await cancelled.inner_text()
        await self.assert_true("génération arrêtée" in cancelled_text, "badge 'génération arrêtée' absent")
        await self.assert_true(
            await cancelled.get_by_role("button", name="Relancer").count() >= 1, "bouton Relancer absent"
        )
        await self.poll_truthy(
            lambda: self._disconnect_increased(state_before), 8000, "déconnexion amont constatée après stop"
        )
        await self.shot(page, "06-stop")
        # Relance réelle une fois le fournisseur revenu au mode normal.
        await self.upstream.set_mode("normal")
        before_retry = await self.settled(page).count()
        await cancelled.get_by_role("button", name="Relancer").first.click()
        await self.wait_settled_plus_one(page, before_retry)
        text = await self.settled(page).last.inner_text()
        await self.assert_true("Réponse de démonstration locale" in text, "réponse de relance absente")
        await self.shot(page, "07-stop-relance")

    async def _disconnect_increased(self, state_before: dict) -> bool:
        state = await self.upstream.state()
        return int(state.get("disconnects", 0)) > int(state_before.get("disconnects", 0))

    async def _p09_error(self, page: Page) -> None:
        await self.upstream.set_mode("error")
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_ERROR)
        await page.get_by_title("Envoyer").click()
        await self.wait_settled_plus_one(page, before)
        error_message = self.settled(page).last
        error_text = await error_message.inner_text()
        await self.assert_true(
            "erreur serveur fournisseur (HTTP 500)" in error_text,
            f"erreur contrôlée inattendue : {error_text[:200]!r}",
        )
        await self.shot(page, "08-erreur")
        await self.upstream.set_mode("normal")
        before_retry = await self.settled(page).count()
        await error_message.get_by_role("button", name="Relancer").first.click()
        await self.wait_settled_plus_one(page, before_retry)
        text = await self.settled(page).last.inner_text()
        await self.assert_true("Réponse de démonstration locale" in text, "relance après erreur absente")

    async def _p10_xss(self, page: Page) -> None:
        await self.upstream.set_mode("xss_markdown")
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_XSS)
        await page.get_by_title("Envoyer").click()
        await self.wait_settled_plus_one(page, before)
        message = self.settled(page).last
        strong = await message.locator(".markdown strong").all_inner_texts()
        await self.assert_true(any("Gras attendu" in s for s in strong), "markdown gras non rendu")
        code = await message.locator(".markdown code").all_inner_texts()
        await self.assert_true(any("code" == c for c in code), "markdown code non rendu")
        xss_flag = await page.evaluate("() => window.__walliaXss === undefined")
        await self.assert_true(bool(xss_flag), "payload XSS exécuté (window.__walliaXss défini)")
        await self.assert_true(await message.locator(".markdown img").count() == 0, "balise <img> hostile rendue")
        await self.assert_true(await message.locator(".markdown script").count() == 0, "balise <script> hostile rendue")
        text = await message.locator(".markdown").inner_text()
        await self.assert_true("onerror" in text, "texte hostile non échappé en texte")
        await self.assert_true("[42]" in text, "citation inconnue [42] disparue")
        await self.assert_true(await message.locator('.cite[data-citation="42"]').count() == 0, "[42] rendue cliquable")
        await self.assert_true(
            await message.locator("span.cite-invalid").count() >= 1, "citation forgée #source-9 non neutralisée"
        )
        invalids = await message.locator("span.cite-invalid").all_inner_texts()
        await self.assert_true(any("forgée" in t for t in invalids), "texte de la citation forgée absent")
        hrefs = await message.locator(".markdown a").evaluate_all(
            "els => els.map(e => ({ href: e.getAttribute('href'), text: e.innerText }))"
        )
        dangerous = [h for h in hrefs if (h["href"] or "").lower().startswith(("javascript:", "data:"))]
        await self.assert_true(not dangerous, f"lien dangereux rendu : {dangerous}")
        self.notes.append(f"liens hostiles neutralisés : {hrefs}")
        cite = message.locator('.cite[data-citation="1"]')
        await self.assert_true(await cite.count() >= 1, "citation valide [1] non cliquable")
        await cite.first.click()
        await self.wait_visible(page.locator(".panel-sources"), "panneau via citation [1]")
        await self.assert_no_overflow(page, "xss + panneau")
        await self.shot(page, "09-xss-citations")
        await page.get_by_role("button", name="Fermer le panneau").click()
        await self.upstream.set_mode("normal")

    async def _p11_case_b(self, page: Page) -> None:
        title_b = f"Cas B recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["B"] = conversation["id"]
        await self.rename_conversation(page, "Nouvelle conversation", title_b)
        self._title_b = title_b
        await self.open_case(page, title_b)
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_B)
        await page.get_by_title("Envoyer").click()
        await self.wait_settled_plus_one(page, before)

    async def _p12_race_get(self, page: Page) -> None:
        title_a, title_b = self._title_a, self._title_b
        a_id = self.conversations["A"]

        async def handler(route, request):
            if request.method == "GET" and request.url == f"{BASE_URL}/api/conversations/{a_id}":
                await asyncio.sleep(2.5)
            await route.continue_()

        pattern = f"**/api/conversations/{a_id}"
        await page.route(pattern, handler)
        try:
            await self.ensure_sidebar(page)
            await page.locator(".conv-item", has_text=title_b).first.wait_for(state="attached")
            await page.locator(".conv-item", has_text=title_a).first.click()
            await asyncio.sleep(0.4)  # GET(A) désormais en vol, retardé
            await self.ensure_sidebar(page)  # mobile : le clic précédent a refermé le tiroir
            await page.locator(".conv-item", has_text=title_b).first.click()
            await self.poll(lambda: self.chat_title(page).inner_text(), title_b, 8000, "B affiché")
            await asyncio.sleep(2.6)  # la réponse tardive de A est arrivée
            current = await self.chat_title(page).inner_text()
            await self.assert_true(current == title_b, f"l'ancien GET de A a écrasé B (titre={current!r})")
            await self.close_sidebar_if_mobile(page)
        finally:
            await page.unroute(pattern, handler)
        await self.shot(page, "10-race-get")

    async def _p13_separation(self, page: Page) -> None:
        title_a, title_b = self._title_a, self._title_b
        # B est affiché : A absent, aucune image de A réutilisée, brouillon propre.
        body = await page.locator(".chat-body").inner_text()
        await self.assert_true(Q_B in body, "message de B absent de B")
        await self.assert_true("Sur l'Aster 10.10" not in body, "message de A visible dans B")
        await self.assert_true(await page.locator(".message-images").count() == 0, "images de A dans B")
        await self.assert_true((await self.composer(page).input_value()) == "", "brouillon de A fuité dans B")
        await self.composer(page).fill("brouillon B recette")
        await self.click_sidebar_item(page, title_a)
        await self.poll(lambda: self.chat_title(page).inner_text(), title_a, 8000, "retour sur A")
        await self.assert_true((await self.composer(page).input_value()) == "", "brouillon de B fuité dans A")
        body = await page.locator(".chat-body").inner_text()
        await self.assert_true(Q_B not in body, "message de B visible dans A")
        images = await page.locator(".message-images img").count()
        await self.assert_true(images == 3, f"images de A attendues 3, obtenues {images}")
        await self.composer(page).fill("brouillon A recette")
        await self.click_sidebar_item(page, title_b)
        await self.poll(lambda: self.chat_title(page).inner_text(), title_b, 8000, "retour sur B")
        value_b = await self.composer(page).input_value()
        await self.assert_true(
            value_b == "brouillon B recette", f"brouillon propre à B non conservé / fuite de A : {value_b!r}"
        )
        await self.click_sidebar_item(page, title_a)
        await self.poll(lambda: self.chat_title(page).inner_text(), title_a, 8000, "A final")
        value = await self.composer(page).input_value()
        await self.assert_true(value == "brouillon A recette", f"brouillon de A perdu : {value!r}")
        await self.composer(page).fill("")
        await self.shot(page, "11-separation")

    async def _p14_race_create(self, page: Page) -> None:
        title_a, title_b = self._title_a, self._title_b
        items_before = await self._conv_items_count(page)
        release = asyncio.Event()
        state: dict = {"held": False, "t_request": None, "t_b_click": None, "t_b_shown": None, "t_release": None}

        async def handler(route, request):
            if request.method == "POST" and request.url == f"{BASE_URL}/api/conversations" and not state["held"]:
                state["held"] = True
                state["t_request"] = time.monotonic()
                # Le POST est RÉELLEMENT retenu : aucune réponse ne peut
                # arriver avant la libération explicite par le test.
                await release.wait()
                state["t_release"] = time.monotonic()
            await route.continue_()

        pattern = "**/api/conversations"
        await page.route(pattern, handler)
        try:
            await self.ensure_sidebar(page)
            async with page.expect_response(
                lambda r: r.url == f"{BASE_URL}/api/conversations" and r.request.method == "POST"
            ) as waiter:
                await page.get_by_role("button", name="Nouvelle conversation", exact=True).click()
                await self.poll_truthy(
                    lambda: asyncio.sleep(0, result=state["held"]), 8000, "POST de création intercepté (en vol)"
                )
                # B est ouvert PENDANT que la création est retenue côté test.
                state["t_b_click"] = time.monotonic()
                await page.locator(".conv-item", has_text=title_b).first.click()
                await self.poll(lambda: self.chat_title(page).inner_text(), title_b, 8000, "B affiché pendant la création")
                state["t_b_shown"] = time.monotonic()
                release.set()  # la réponse de création est libérée APRÈS l'affichage de B
                response = await waiter.value
            created = await response.json()
            self.conversations["X_race_create"] = created["id"]
            self.cleanup_ids.append(created["id"])
            # Preuve d'ordre réelle : le POST était en vol AVANT le clic B et
            # sa réponse n'a été libérée qu'APRÈS l'affichage de B.
            t_request, t_b_click, t_b_shown, t_release = (
                state["t_request"], state["t_b_click"], state["t_b_shown"], state["t_release"],
            )
            if (
                t_request is None or t_b_click is None or t_b_shown is None or t_release is None
                or not (t_request < t_b_click <= t_b_shown <= t_release)
            ):
                raise CheckFailure(f"ordre de course non prouvé : {state}")
            self.notes.append(
                "course création : POST retenu {:.2f}s avant clic B, réponse libérée {:.2f}s après B".format(
                    t_b_click - t_request, t_release - t_b_shown
                )
            )
            await asyncio.sleep(0.8)  # la réponse tardive de création est arrivée
            await self.shot(page, "12-race-create")  # preuve visuelle de l'état final (avant assertions)
            current = await self.chat_title(page).inner_text()
            await self.assert_true(current == title_b, f"l'ancien créateur a écrasé B (titre={current!r})")
            await self.poll(
                lambda: self._conv_items_count(page), items_before + 1, 8000,
                "le nouveau cas apparaît dans la liste (conservé, non affiché)",
            )
        finally:
            release.set()  # ne jamais laisser le POST retenu (échec compris)
            await page.unroute(pattern, handler)
            await self.close_sidebar_if_mobile(page)

    async def _conv_items_count(self, page: Page) -> int:
        await self.ensure_sidebar(page)
        count = await page.locator(".conv-item").count()
        return count

    async def _logout(self, page: Page) -> None:
        await self.ensure_sidebar(page)
        await page.get_by_role("button", name="Se déconnecter").click()
        # Écran de connexion RÉEL : le formulaire est identifié par son champ
        # email (unique). L'ancien sélecteur `input[type=password]` est ambigu
        # sur la page Administration (3 champs mot de passe → strict mode
        # immédiat), ce qui n'était PAS une preuve d'un défaut de déconnexion.
        await self.poll_truthy(
            lambda: page.locator('input[type="email"]').is_visible(), UI_TIMEOUT_MS,
            "retour à l'écran de connexion (champ email visible)",
        )
        await self.wait_visible(page.get_by_role("button", name="Se connecter"), "bouton Se connecter")

    async def _p15_race_logout_upload(self, page: Page) -> None:
        title_c = f"Cas C recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["C"] = conversation["id"]
        c_id = conversation["id"]
        await self.rename_conversation(page, "Nouvelle conversation", title_c)
        self._title_c = title_c
        await self.open_case(page, title_c)
        pattern = f"**/api/conversations/{c_id}/attachments"
        state = {"used": False, "delivered": False}

        async def handler(route, request):
            if request.method == "POST" and not state["used"]:
                state["used"] = True
                # Envoie la requête MAINTENANT (session encore valide) puis
                # retient la réponse : c'est la RÉPONSE qui arrive tard.
                response = await route.fetch()
                await asyncio.sleep(3.5)
                await route.fulfill(response=response)
                state["delivered"] = True
            else:
                await route.continue_()

        await page.route(pattern, handler)
        await page.locator('.composer input[type="file"]').set_input_files(
            {"name": "tardif-lot4b.png", "mimeType": "image/png", "buffer": PNG_1PX}
        )
        await self.poll_truthy(
            lambda: asyncio.sleep(0, result=state["used"]), 5000, "requête d'upload interceptée"
        )
        await self._logout(page)  # déconnexion pendant l'upload en vol
        await self._login(page)
        await self.poll_truthy(
            lambda: asyncio.sleep(0, result=state["delivered"]), 15_000, "réponse d'upload tardive livrée à la page"
        )
        await asyncio.sleep(0.4)
        empty_state = await page.locator(".chat-empty").inner_text()
        await self.assert_true("Aucune conversation sélectionnée" in empty_state, "état vide initial inattendu")
        await self.assert_true(await page.locator(".pending-files").count() == 0, "chip tardif appliqué après re-login")
        await self.open_case(page, title_c)
        await self.assert_true(
            await page.locator(".pending-files").count() == 0, "chip tardif appliqué dans le cas C"
        )
        attachments = await self.api.json("GET", f"/api/conversations/{c_id}/attachments")
        count = len(attachments["attachments"])
        await self.assert_true(count == 1, f"pièce jointe serveur attendue 1, obtenue {count}")
        self.notes.append("upload tardif : aucune application UI, 1 pièce serveur conservée (vérité serveur)")
        await self.close_sidebar_if_mobile(page)
        await page.unroute(pattern, handler)
        await self.shot(page, "13-race-logout-upload")

    async def _p16_race_logout_stream(self, page: Page) -> None:
        title_d = f"Cas D recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["D"] = conversation["id"]
        d_id = conversation["id"]
        await self.rename_conversation(page, "Nouvelle conversation", title_d)
        self._title_d = title_d
        await self.open_case(page, title_d)
        await self.upstream.set_mode("slow")
        before = await self.settled(page).count()
        await self.composer(page).fill(Q_STOP)
        await page.get_by_title("Envoyer").click()
        await self.wait_streaming_visible(page)
        await self._logout(page)  # déconnexion pendant la génération
        await self._login(page)
        await self.poll(lambda: self.streaming(page).count(), 0, 10_000, "aucune bulle de génération après re-login")

        async def status_check():
            detail = await self.api.json("GET", f"/api/conversations/{d_id}")
            assistant = [m for m in detail["messages"] if m["role"] == "assistant"]
            return assistant[-1]["status"] if assistant else None

        status = await self.poll_truthy(
            lambda: status_check(), 15_000,
            "message assistant clôturé côté serveur",
        )
        await self.assert_true(status == "cancelled", f"statut serveur attendu 'cancelled', obtenu {status!r}")
        await self.open_case(page, title_d)
        badge = await page.locator(".message-state", has_text="génération arrêtée").count()
        await self.assert_true(badge >= 1, "badge d'annulation absent après re-login")
        await self.shot(page, "14-race-logout-stream")
        # Le streaming fonctionne de nouveau après re-login (relance réelle).
        await self.upstream.set_mode("normal")
        before2 = await self.settled(page).count()
        await self.settled(page).last.get_by_role("button", name="Relancer").first.click()
        await self.wait_settled_plus_one(page, before2)
        text = await self.settled(page).last.inner_text()
        await self.assert_true("Réponse de démonstration locale" in text, "relance post re-login absente")

    async def _p17_race_logout_create(self, page: Page) -> None:
        state = {"used": False, "delivered": False}

        async def handler(route, request):
            if request.method == "POST" and request.url == f"{BASE_URL}/api/conversations" and not state["used"]:
                state["used"] = True
                # La création est traitée AVANT la déconnexion ; seule la
                # réponse est retenue puis livrée après le re-login.
                response = await route.fetch()
                await asyncio.sleep(3.0)
                await route.fulfill(response=response)
                state["delivered"] = True
            else:
                await route.continue_()

        pattern = "**/api/conversations"
        await page.route(pattern, handler)
        await self.ensure_sidebar(page)
        async with page.expect_response(
            lambda r: r.url == f"{BASE_URL}/api/conversations" and r.request.method == "POST"
        ) as waiter:
            await page.get_by_role("button", name="Nouvelle conversation", exact=True).click()
        await self.poll_truthy(
            lambda: asyncio.sleep(0, result=state["used"]), 5000, "requête de création interceptée"
        )
        await self._logout(page)  # déconnexion pendant la création en vol
        await self._login(page)
        response = await waiter.value
        created = await response.json()
        self.conversations["X_race_logout_create"] = created["id"]
        self.cleanup_ids.append(created["id"])
        await asyncio.sleep(1.2)
        empty_state = await page.locator(".chat-empty").inner_text()
        await self.assert_true(
            "Aucune conversation sélectionnée" in empty_state,
            "la création tardive a hijacké la vue après re-login",
        )
        # La liste serveur (refresh de connexion) peut contenir le cas créé :
        # vérité serveur vérifiée via l'API, sans exigence d'affichage.
        listing = await self.api.json("GET", "/api/conversations")
        ids = [c["id"] for c in listing["conversations"]]
        await self.assert_true(created["id"] in ids, "le cas créé n'existe pas côté serveur")
        await page.unroute(pattern, handler)
        await self.shot(page, "15-race-logout-create")

    async def _p18_finalizer(self, page: Page) -> None:
        title_e = f"Cas E recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["E"] = conversation["id"]
        await self.rename_conversation(page, "Nouvelle conversation", title_e)
        self._title_e = title_e
        title_d = self._title_d
        pattern = "**/api/conversations"
        state = {"used": False}

        async def handler(route, request):
            if request.method == "GET" and request.url == f"{BASE_URL}/api/conversations" and not state["used"]:
                state["used"] = True
                await asyncio.sleep(3.0)
            await route.continue_()

        await page.route(pattern, handler)
        try:
            await self.open_case(page, title_e)
            before = await self.settled(page).count()
            await self.composer(page).fill(Q_E)
            await page.get_by_title("Envoyer").click()
            await self.wait_settled_plus_one(page, before)
            # Le finaliseur de E attend maintenant la liste retardée ; on ouvre D
            # et on démarre un NOUVEAU flux pendant cette fenêtre.
            await asyncio.sleep(0.4)
            await self.open_case(page, title_d)
            before_d = await self.settled(page).count()
            await self.composer(page).fill(Q_D2)
            await page.get_by_title("Envoyer").click()
            await self.wait_settled_plus_one(page, before_d)
            await asyncio.sleep(3.5)  # la réponse tardive de E retombe maintenant
            current = await self.chat_title(page).inner_text()
            await self.assert_true(current == title_d, f"le finaliseur tardif a changé de cas (titre={current!r})")
            await self.assert_true(await self.streaming(page).count() == 0, "flux D encore marqué en cours")
            await self.assert_true(
                await page.locator(".message-error").count() == 0, "erreur apparue après finaliseur tardif"
            )
            text = await self.settled(page).last.inner_text()
            await self.assert_true("Réponse de démonstration locale" in text, "réponse D endommagée")
            await self.shot(page, "16-race-finalizer")
            # E reste intact.
            await self.open_case(page, title_e)
            body = await page.locator(".chat-body").inner_text()
            await self.assert_true("Réponse de démonstration locale" in body, "réponse E perdue")
        finally:
            await page.unroute(pattern, handler)

    async def _p18b_delete_case(self, page: Page) -> None:
        title_f = f"Cas F recette {RUN_TAG}"
        conversation = await self.create_conversation(page)
        self.conversations["F"] = conversation["id"]
        f_id = conversation["id"]
        self.cleanup_ids.append(f_id)  # idempotent : 404 accepté si déjà supprimé
        await self.rename_conversation(page, "Nouvelle conversation", title_f)
        await self.open_case(page, title_f)
        # Suppression via l'UI réelle (corbeille + confirmation du navigateur).
        await self.ensure_sidebar(page)
        item = page.locator(".conv-item", has_text=title_f).first
        await item.get_by_title("Supprimer").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="Conversation supprimée").count(), 1, 8000, "toast suppression cas"
        )
        await self.poll(
            lambda: page.locator(".conv-item", has_text=title_f).count(), 0, 8000, "cas F retiré de la liste"
        )
        title_now = await self.chat_title(page).inner_text()
        await self.assert_true(
            title_now != title_f, f"le cas supprimé est encore affiché (titre={title_now!r})"
        )
        await self.wait_visible(page.locator(".chat-empty"), "état vide après suppression")
        status, _, _ = await self.api.call("GET", f"/api/conversations/{f_id}")
        await self.assert_true(status == 404, f"cas toujours côté serveur (HTTP {status})")
        self.notes.append(f"suppression UI du cas F vérifiée (404 serveur) ; confirms={self.dialog_events[-1:]}")
        await self.close_sidebar_if_mobile(page)
        await self.shot(page, "18b-suppression-cas")

    async def _p19_library(self, page: Page) -> None:
        await self.nav(page, "Bibliothèque & jobs")
        await self.wait_visible(page.get_by_role("heading", name="Bibliothèque documentaire"), "vue bibliothèque")
        table = page.locator("table.table").first
        await self.wait_visible(table, "table des documents")
        aster_row = table.locator("tbody tr", has_text="Aster 10.10").first
        boreas_row = table.locator("tbody tr", has_text="Boreas 9.9").first
        await self.wait_visible(aster_row, "ligne Aster")
        await self.wait_visible(boreas_row, "ligne Boreas")
        await self.assert_true("démo non officiel" in (await aster_row.inner_text()), "badge démo absent")
        await self.assert_no_overflow(page, "bibliothèque")
        await self.shot(page, "17-bibliotheque")
        # Passages réels du document Aster.
        await aster_row.get_by_role("button", name="Passages").click()
        dialog = page.get_by_role("dialog")
        await self.wait_visible(dialog, "dialogue des passages")
        # Les passages se chargent en asynchrone : attendre le contenu réel.
        await self.poll(lambda: dialog.locator(".chunk").count(), 2, 10_000, "passages chargés")
        dialog_text = await dialog.inner_text()
        await self.assert_true("Passages —" in dialog_text, "titre du dialogue inattendu")
        await self.assert_true("tension instable" in dialog_text, "passage attendu absent")
        await self.assert_true("hors tension" in dialog_text, "second passage attendu absent")
        await self.assert_true("p. 1" in dialog_text and "p. 2" in dialog_text, "pages des passages absentes")
        await self.shot(page, "18-passages")
        await dialog.get_by_role("button", name="Fermer").click()
        await self.poll(lambda: page.get_by_role("dialog").count(), 0, 5000, "dialogue fermé")
        # Métadonnées : modification réelle puis restauration.
        await boreas_row.get_by_role("button", name="Métadonnées").click()
        dialog = page.get_by_role("dialog")
        await self.wait_visible(dialog, "dialogue métadonnées")
        origin_field = dialog.get_by_label("Origine")
        await origin_field.fill("recette-lot4b")
        await dialog.get_by_role("button", name="Enregistrer les métadonnées").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="Métadonnées enregistrées").count(), 1, 8000, "toast métadonnées"
        )
        await self.wait_toast_gone(page, "Métadonnées enregistrées")
        await table.locator("tbody tr", has_text="Boreas 9.9").first.get_by_role(
            "button", name="Métadonnées"
        ).click()
        dialog = page.get_by_role("dialog")
        await self.wait_visible(dialog, "dialogue métadonnées (relu)")
        value = await dialog.get_by_label("Origine").input_value()
        await self.assert_true(value == "recette-lot4b", f"origine non relue : {value!r}")
        await dialog.get_by_label("Origine").fill("demo")
        await dialog.get_by_role("button", name="Enregistrer les métadonnées").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="Métadonnées enregistrées").count(), 1, 8000, "toast restauration"
        )
        await self.wait_toast_gone(page, "Métadonnées enregistrées")
        # Filtres réellement fonctionnels.
        search = page.locator(".filters input").first
        await search.fill("Boreas")
        await self.poll(lambda: table.locator("tbody tr").count(), 1, 8000, "filtre titre")
        row_text = await table.locator("tbody tr").first.inner_text()
        await self.assert_true("Boreas" in row_text, "filtre titre inopérant")
        await search.fill("")
        await self.poll(lambda: table.locator("tbody tr").count(), 2, 8000, "filtre titre effacé")
        scope = page.locator(".filters select").first
        await scope.select_option("demo")
        await self.poll(lambda: table.locator("tbody tr").count(), 2, 8000, "filtre périmètre démo")
        await scope.select_option("")
        # Import refusé (non-PDF) : rejet réel côté serveur.
        await page.get_by_role("button", name="Importer un PDF").click()
        dialog = page.get_by_role("dialog")
        await self.wait_visible(dialog, "dialogue import")
        await dialog.locator('input[type="file"]').set_input_files(
            {"name": "note.txt", "mimeType": "text/plain", "buffer": b"ceci n'est pas un PDF"}
        )
        await dialog.get_by_label("Titre").fill("Test refus non-PDF")
        await dialog.get_by_role("button", name="Importer et indexer").click()
        await self.poll(
            lambda: dialog.locator(".form-error").count(), 1, 10_000, "erreur d'import affichée"
        )
        error_text = await dialog.locator(".form-error").inner_text()
        await self.assert_true("PDF" in error_text and "accept" in error_text, f"message de refus inattendu : {error_text!r}")
        await self.shot(page, "19-import-refus")
        await dialog.get_by_role("button", name="Annuler").click()
        # Import réel d'un PDF valide : job créé puis document supprimé (auto-nettoyage).
        await page.get_by_role("button", name="Importer un PDF").click()
        dialog = page.get_by_role("dialog")
        await self.wait_visible(dialog, "dialogue import (PDF)")
        title = f"Recette lot4b import {RUN_TAG}"
        await dialog.locator('input[type="file"]').set_input_files(
            {
                "name": "recette-lot4b.pdf",
                "mimeType": "application/pdf",
                "buffer": minimal_pdf("Recette lot4b imported PDF."),
            }
        )
        await dialog.get_by_label("Titre").fill(title)
        await dialog.get_by_role("button", name="Importer et indexer").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="Document importé").count(), 1, 20_000, "toast import"
        )
        await self.poll(lambda: page.get_by_role("dialog").count(), 0, 8000, "dialogue import fermé")
        await self.poll(
            lambda: table.locator("tbody tr", has_text=title).count(), 1, 15_000,
            "ligne du document importé",
        )
        row = table.locator("tbody tr", has_text=title).first
        row_text = await row.inner_text()
        await self.assert_true("queued" in row_text, f"statut du document importé inattendu : {row_text[:160]!r}")
        jobs_table = page.locator("table.table").last
        jobs_text = await jobs_table.inner_text()
        await self.assert_true(title in jobs_text, "job d'ingestion absent de la table des jobs")
        await self.assert_true("queued" in jobs_text, "statut de job inattendu")
        await self.shot(page, "20-import-jobs")
        await row.get_by_role("button", name="Supprimer").click()
        await self.poll(lambda: page.locator(".toast", has_text="Document supprimé").count(), 1, 10_000, "toast suppression")
        await self.poll(
            lambda: table.locator("tbody tr", has_text=title).count(), 0, 10_000, "ligne supprimée"
        )

    async def _p20_admin(self, page: Page) -> None:
        await self.nav(page, "Administration")
        await self.wait_visible(page.get_by_role("heading", name="Administration"), "vue administration")
        # Les réglages se chargent en ASYNCHRONE : l'état initial affiche
        # « clé absente ». Attendre le badge réellement configuré (donnée du
        # serveur) AVANT toute lecture — lire trop tôt était la cause du FAIL.
        await self.poll(
            lambda: page.locator(".view .badge", has_text="clé configurée").count(), 1, 10_000,
            "réglages chargés (badge clé configurée)",
        )
        body = await page.locator(".view").inner_text()
        await self.assert_true("Embeddings (recherche)" in body, "carte embeddings absente")
        await self.assert_true("Fournisseur de chat" in body, "carte fournisseur absente")
        await self.assert_true("vision inactive" in body, "état vision inattendu")
        await self.assert_true("web : inactif" in body or "inactif" in body, "état web inattendu")
        await self.assert_true("1.1491" in body, "seuil gelé absent")
        status = await self.api.json("GET", "/api/status")
        model = status["embedding"]["model"]
        await self.assert_true(model in body, f"modèle d'embeddings {model!r} absent de la carte")
        await self.assert_true("clé configurée" in body, "clé fournisseur non configurée")
        await self.assert_true("http://fake-upstream:8099/v1" in body, "endpoint fournisseur inattendu")
        await self.assert_no_overflow(page, "administration")
        await self.shot(page, "21-administration")
        # Clé write-only : valeur synthétique envoyée, jamais réaffichée.
        key_field = page.get_by_label("Clé API", exact=False)
        await self.assert_true((await key_field.input_value()) == "", "champ clé non vide au chargement")
        synthetic = "sk-recette-lot4b-synthetique-000000000001"
        await key_field.fill(synthetic)
        await page.get_by_role("button", name="Enregistrer", exact=True).click()
        await self.poll(lambda: page.locator(".toast", has_text="Réglages enregistrés").count(), 1, 8000, "toast réglages")
        await self.poll(lambda: key_field.input_value(), "", 8000, "champ clé vidé après enregistrement")
        placeholder = await key_field.get_attribute("placeholder")
        self.notes.append(f"placeholder clé après enregistrement : {placeholder!r}")
        settings_raw = await self.api.call("GET", "/api/settings")
        await self.assert_true(
            synthetic not in settings_raw[1].decode("utf-8", "replace"), "clé synthétique renvoyée par l'API"
        )
        await self.poll(lambda: page.locator(".badge", has_text="clé configurée").count(), 1, 5000, "clé conservée")
        # Enregistrer à vide CONSERVE la clé.
        await page.get_by_role("button", name="Enregistrer", exact=True).click()
        await self.poll(lambda: page.locator(".badge", has_text="clé configurée").count(), 1, 5000, "clé conservée à vide")
        # Test de connexion au double local.
        await page.get_by_role("button", name="Tester la connexion").click()
        await self.poll(
            lambda: page.locator(".toast", has_text="Fournisseur joignable").count(), 1, 20_000, "toast test fournisseur"
        )
        await page.get_by_role("button", name="Recharger").click()
        await self.poll(lambda: key_field.input_value(), "", 8000, "champ clé vide après rechargement")

    async def _p21_password(self, page: Page) -> None:
        original = self.credentials["password"]
        temporary = original + "-L4b"
        changed = False

        async def ui_change(current: str, new: str, toast: str) -> None:
            await self.nav(page, "Administration")
            await page.get_by_label("Mot de passe actuel").fill(current)
            await page.get_by_label("Nouveau mot de passe (≥ 12 caractères)").fill(new)
            await page.get_by_label("Confirmation").fill(new)
            await page.get_by_role("button", name="Changer le mot de passe").click()
            await self.poll(lambda: page.locator(".toast", has_text="Mot de passe modifié").count(), 1, 8000, toast)
            await self.wait_toast_gone(page, "Mot de passe modifié")

        try:
            await ui_change(original, temporary, "toast changement mdp")
            changed = True
            self.credentials["password"] = temporary  # état COURANT du compte (jamais journalisé)
            await self._logout(page)
            await self._login(page, temporary)  # le NOUVEAU mot de passe fonctionne
            await ui_change(temporary, original, "toast restauration mdp")
            changed = False
            self.credentials["password"] = original
            await self._logout(page)
            await self._login(page)  # le mot de passe d'origine est rétabli pour les replays
        finally:
            if changed:
                # Panne en cours de test : le compte ne doit JAMAIS rester dans
                # un état non documenté. Restauration de secours via l'API de
                # test (même chemin d'authentification, idempotent, sans secret
                # affiché), puis reconnexion de la session indépendante.
                await self._restore_password_via_api(temporary, original)
        # Les changements de mot de passe révoquent les AUTRES sessions : la
        # session API indépendante du runner doit se reconnecter avant le
        # nettoyage (sinon 401 silencieux).
        await asyncio.to_thread(self.api.login)

    async def _restore_password_via_api(self, current: str, original: str) -> None:
        """Restauration de secours (finally) : API test indépendante, bornée."""
        try:
            self.api.credentials["password"] = current
            await asyncio.to_thread(self.api.login)
            status, _, _ = await self.api.call(
                "POST",
                "/api/auth/password",
                {"current_password": current, "new_password": original},
            )
            self.credentials["password"] = original
            self.api.credentials["password"] = original
            if status == 200:
                await asyncio.to_thread(self.api.login)
                self.notes.append("restauration mdp de secours via API : OK")
            else:
                self.notes.append(f"restauration mdp de secours via API : HTTP {status} (état à vérifier)")
        except Exception as exc:  # noqa: BLE001 — l'échec est consigné, jamais masqué
            self.notes.append(f"restauration mdp de secours impossible : {exc.__class__.__name__}")

    async def _p22_logout(self, page: Page) -> None:
        await self._logout(page)
        await self.assert_no_overflow(page, "écran de connexion final")
        anonymous = ApiClient(BASE_URL, self.credentials)
        status, _, _ = await anonymous.call("GET", "/api/conversations")
        await self.assert_true(status == 401, f"session non révoquée après logout (HTTP {status})")
        await self.shot(page, "22-deconnexion")

    async def _p23_cleanup(self) -> None:
        """Suppression RÉELLE des seuls cas créés par ce harnais.

        Un 401 (session révoquée par un changement de mot de passe) est
        détecté, reconnecté puis retenté ; un nettoyage sans droit ne peut
        JAMAIS passer silencieusement (l'ancien contrôle acceptait 0/9)."""
        async def ensure_login() -> None:
            try:
                await asyncio.to_thread(self.api.login)
            except Exception:  # noqa: BLE001 — reconnexion best effort
                pass

        if self.api.csrf is None:
            await ensure_login()
        statuses: list[tuple[str, int]] = []
        for conversation_id in self.cleanup_ids:
            status, _, _ = await self.api.call("DELETE", f"/api/conversations/{conversation_id}")
            if status == 401:
                await ensure_login()
                status, _, _ = await self.api.call("DELETE", f"/api/conversations/{conversation_id}")
            statuses.append((conversation_id, status))
        unauthorized = [cid for cid, status in statuses if status == 401]
        failed = [(cid, status) for cid, status in statuses if status not in (200, 204, 404)]
        deleted = sum(1 for _cid, status in statuses if status in (200, 204))
        already_gone = sum(1 for _cid, status in statuses if status == 404)
        self.notes.append(
            f"nettoyage : {deleted} suppressions réelles, {already_gone} déjà absents, "
            f"{len(unauthorized)} refus 401, {len(failed)} autres échecs sur {len(self.cleanup_ids)} cas"
        )
        await self.assert_true(
            not unauthorized, f"nettoyage sans droit : {len(unauthorized)} refus 401 (session non reconnectée)"
        )
        await self.assert_true(
            not failed, f"nettoyage incomplet : {[(cid[:8], status) for cid, status in failed]}"
        )
        # Résultat vérifié pour CHAQUE cas créé par le harnais : supprimé côté serveur.
        for conversation_id in self.cleanup_ids:
            status, _, _ = await self.api.call("GET", f"/api/conversations/{conversation_id}")
            await self.assert_true(
                status == 404, f"cas {conversation_id[:8]}… encore présent après nettoyage (HTTP {status})"
            )


# ---------------------------------------------------------------------------
# Entrée
# ---------------------------------------------------------------------------

PROJECTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "is_mobile": False, "has_touch": False},
    "mobile": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True},
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Recette E2E Wallia (Playwright Python)")
    parser.add_argument("--projects", default="desktop,mobile")
    args = parser.parse_args()
    wanted = [p.strip() for p in args.projects.split(",") if p.strip()]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for name in wanted:
                config = PROJECTS[name]
                recette = Recette(name, config["viewport"], is_mobile=config["is_mobile"], has_touch=config["has_touch"])
                report = await recette.run(browser)
                reports.append(report)
                out = EVIDENCE_DIR / f"lot4-e2e-{name}.json"
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{name}] rapport -> {out}", flush=True)
        finally:
            await browser.close()
    summary = {
        "generated_at": _now(),
        "base_url": BASE_URL,
        "run_tag": RUN_TAG,
        "projects": [
            {
                "project": report["project"],
                "ok": report["ok"],
                "checks_total": len(report["checks"]),
                "checks_failed": report["checks_failed"],
                "console_unexpected": len(report["console_errors_unexpected"]),
                "http_unexpected": len(report["http_failures_unexpected"]),
                "screenshots": len(report["screenshots"]),
            }
            for report in reports
        ],
        "ok": all(report["ok"] for report in reports) and len(reports) == len(wanted),
    }
    summary_path = EVIDENCE_DIR / "lot4-e2e-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
