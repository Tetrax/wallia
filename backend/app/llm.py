"""Client fournisseur OpenAI-compatible (DeepSeek natif par défaut).

Contraintes : HTTPS (sauf mode local/test explicitement autorisé), domaines
autorisés explicites, redirections refusées, résolution réseau contrôlée,
streaming SSE, délais connexion/lecture/total, sortie bornée.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings


class ProviderError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = message


class ProviderCancelled(Exception):
    pass


@dataclass
class ProviderStatus:
    available: bool
    endpoint: str
    model: str
    reason: str | None = None


def _is_public_ip(host: str) -> bool | None:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not ip.is_global:
            return False
    return True


def validate_endpoint(endpoint: str, settings: Settings) -> tuple[bool, str | None]:
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False, "endpoint illisible"
    if parsed.scheme not in ("https", "http"):
        return False, "schéma non supporté"
    if parsed.scheme == "http" and settings.is_production:
        return False, "HTTPS exigé en production"
    host = parsed.hostname or ""
    if not host:
        return False, "hôte manquant"
    if host not in settings.provider_allowed_domains:
        return False, f"domaine non autorisé ({host})"
    if settings.is_production:
        public = _is_public_ip(host)
        if public is False:
            return False, "adresse réseau non publique refusée"
        if public is None:
            return False, "résolution DNS impossible"
    return True, None


class Provider:
    def __init__(self, settings: Settings, endpoint: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout_s: float | None = None) -> None:
        self.settings = settings
        self.endpoint = (endpoint or settings.provider_endpoint).rstrip("/")
        self.model = model or settings.provider_model
        self._api_key = api_key if api_key is not None else settings.provider_api_key()
        self.timeout_s = timeout_s or settings.provider_total_timeout

    @property
    def api_key(self) -> str | None:
        value = (self._api_key or "").strip()
        if not value or value.startswith("#"):
            return None
        return value

    def status(self) -> ProviderStatus:
        if self.api_key is None:
            return ProviderStatus(False, self.endpoint, self.model, "clé API absente")
        ok, reason = validate_endpoint(self.endpoint, self.settings)
        if not ok:
            return ProviderStatus(False, self.endpoint, self.model, reason)
        return ProviderStatus(True, self.endpoint, self.model, None)

    def _chat_url(self) -> str:
        if self.endpoint.endswith("/chat/completions"):
            return self.endpoint
        return f"{self.endpoint}/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.api_key
        if key is None:
            raise ProviderError("config", "clé API non configurée")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _payload(self, messages: list[dict[str, Any]], stream: bool, max_tokens: int | None = None) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens or self.settings.provider_max_tokens,
            "temperature": 0.2,
        }

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Générateur de deltas : {'type': 'delta', 'text': str} puis {'type': 'done', 'usage': ...}."""
        ok, reason = validate_endpoint(self.endpoint, self.settings)
        if not ok:
            raise ProviderError("config", f"endpoint refusé: {reason}")
        payload = self._payload(messages, stream=True)
        deadline = time.monotonic() + self.timeout_s
        timeout = httpx.Timeout(
            connect=self.settings.provider_connect_timeout,
            read=self.settings.provider_read_timeout,
            write=self.settings.provider_connect_timeout,
            pool=self.settings.provider_connect_timeout,
        )
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                with client.stream("POST", self._chat_url(), headers=self._headers(), json=payload) as response:
                    if response.status_code >= 400:
                        body = ""
                        try:
                            body = response.read().decode("utf-8", "replace")[:600]
                        except Exception:
                            pass
                        raise ProviderError("http", f"fournisseur HTTP {response.status_code}: {_safe_error_body(body)}")
                    for line in response.iter_lines():
                        if cancel_check is not None and cancel_check():
                            raise ProviderCancelled()
                        if time.monotonic() > deadline:
                            raise ProviderError("timeout", "délai total dépassé")
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            yield {"type": "done", "usage": None}
                            return
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = parsed.get("usage")
                        choices = parsed.get("choices") or []
                        if not choices:
                            if usage:
                                yield {"type": "usage", "usage": usage}
                            continue
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield {"type": "delta", "text": text}
                        if choices[0].get("finish_reason") and usage:
                            yield {"type": "usage", "usage": usage}
        except httpx.ConnectError as exc:
            raise ProviderError("connect", f"connexion au fournisseur impossible ({exc.__class__.__name__})") from exc
        except httpx.ReadTimeout as exc:
            raise ProviderError("timeout", "délai de lecture dépassé") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("http", f"erreur réseau fournisseur ({exc.__class__.__name__})") from exc

    def test_connection(self) -> dict[str, Any]:
        """Appel minimal réel (non streaming) pour vérifier la configuration."""
        status = self.status()
        if not status.available:
            return {"ok": False, "error": status.reason, "latency_ms": None, "model": self.model}
        payload = self._payload([{"role": "user", "content": "ping"}], stream=False, max_tokens=1)
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=self.settings.provider_connect_timeout,
                                                    read=30.0, write=10.0, pool=10.0),
                              follow_redirects=False) as client:
                response = client.post(self._chat_url(), headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"erreur réseau ({exc.__class__.__name__})", "latency_ms": None, "model": self.model}
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {_safe_error_body(response.text[:600])}",
                "latency_ms": latency,
                "model": self.model,
            }
        try:
            body = response.json()
            served = body.get("model") or self.model
        except Exception:
            served = self.model
        return {"ok": True, "error": None, "latency_ms": latency, "model": served}


def _safe_error_body(body: str) -> str:
    """Réduit un corps d'erreur fournisseur à un message sûr (pas de secret, pas de dump)."""
    text = " ".join(body.split())
    for marker in ("sk-", "Bearer "):
        if marker in text:
            text = text.replace(marker, "[masqué]")
    return text[:300]
