"""Client fournisseur OpenAI-compatible (DeepSeek natif par défaut).

Contraintes : HTTPS (sauf mode local/test explicitement autorisé), domaines
autorisés explicites, redirections refusées, résolution réseau contrôlée,
streaming SSE, délais connexion/lecture/total, sortie bornée.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
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
    """Annulation explicite (stop utilisateur, déconnexion, délai)."""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class ProviderCancellation:
    """Jeton d'annulation thread-safe qui ferme réellement le transport.

    Un closer (la réponse httpx) est enregistré dès son ouverture ; `cancel()`
    le ferme immédiatement, ce qui interrompt une lecture bloquée même si le
    fournisseur est silencieux et n'a encore envoyé aucun delta.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._reason = "cancelled"
        self._closers: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            closers = list(self._closers)
        # Du plus RÉCENT au plus ancien : le closer le plus précis (shutdown du
        # socket) doit agir AVANT les fermetures génériques — un `client.close()`
        # exécuté en premier fermerait le socket et rendrait le réveil de la
        # lecture bloquée impossible.
        for closer in reversed(closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - fermeture best effort
                pass

    def attach(self, closer: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._closers.append(closer)
                return
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
        raise ProviderCancelled(self._reason)

    def detach(self, closer: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._closers.remove(closer)
            except ValueError:
                pass


@dataclass
class ProviderStatus:
    available: bool
    endpoint: str
    model: str
    reason: str | None = None


def _close_stream(stream: Any) -> None:
    """Fermeture best effort d'un flux : shutdown réel du socket puis close."""
    socket_obj = None
    try:
        socket_obj = stream.get_extra_info("socket")
    except Exception:  # noqa: BLE001 - flux déjà fermé
        socket_obj = None
    if socket_obj is not None:
        try:
            socket_obj.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        stream.close()
    except Exception:  # noqa: BLE001 - fermeture best effort
        pass


class _SocketRegistry:
    """Registre des sockets TCP du transport, utilisable AVANT les en-têtes.

    httpcore ouvre le socket AVANT d'envoyer la requête et d'attendre les
    en-têtes de réponse. En l'enregistrant dès l'ouverture, une annulation
    (stop, déconnexion, délai) peut fermer réellement le transport même si le
    fournisseur n'a encore envoyé AUCUN octet : `client.close()` seul ne
    réveille pas de façon fiable un thread bloqué sur `recv()`, alors que
    `shutdown()` le fait (constaté par sonde). Fermeture best effort, jamais
    bloquante (aucun appel réseau/base ici).

    Le registre est « fermable » : une annulation qui tombe PENDANT la bascule
    TLS (`start_tls`) reste correcte — un flux enregistré après la fermeture
    est immédiatement fermé, jamais laissé orphelin.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: list[Any] = []
        self._closed = False

    def register(self, stream: Any) -> None:
        with self._lock:
            if self._closed:
                close_now = True
            else:
                close_now = False
                self._streams.append(stream)
        if close_now:
            _close_stream(stream)

    def shutdown_all(self) -> None:
        with self._lock:
            self._closed = True
            streams = list(self._streams)
            self._streams = []
        for stream in reversed(streams):  # du plus récent au plus ancien
            _close_stream(stream)


class _TrackedStream:
    """Proxy du flux httpcore : suit AUSSI le flux renvoyé par `start_tls`.

    `SyncStream.start_tls` renvoie un NOUVEAU flux (socket TLS) et détache
    l'ancien socket : sans enregistrer le flux renvoyé, une annulation
    pré-en-têtes viserait un descripteur détaché et HTTPS ne serait pas
    réellement coupé (angle mort reproduit par
    tests/acceptance/repro_pre_headers_close.py).
    """

    def __init__(self, inner: Any, registry: _SocketRegistry) -> None:
        self._inner = inner
        self._registry = registry

    def start_tls(self, *args: Any, **kwargs: Any) -> Any:
        stream = self._inner.start_tls(*args, **kwargs)
        self._registry.register(stream)
        return _TrackedStream(stream, self._registry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TrackingNetworkBackend:
    """Proxy du backend réseau httpcore : enregistre chaque flux ouvert.

    Adaptateur minimal (le backend réel reste celui d'httpcore/httpx) ; les
    méthodes sont volontairement en `**kwargs` pour rester compatibles avec
    la signature réelle du backend installé.
    """

    def __init__(self, inner: Any, registry: _SocketRegistry) -> None:
        self._inner = inner
        self._registry = registry

    def connect_tcp(self, **kwargs: Any) -> Any:
        stream = self._inner.connect_tcp(**kwargs)
        self._registry.register(stream)
        return _TrackedStream(stream, self._registry)

    def connect_unix_socket(self, **kwargs: Any) -> Any:
        stream = self._inner.connect_unix_socket(**kwargs)
        self._registry.register(stream)
        return _TrackedStream(stream, self._registry)


_socket_tracking_warned = False


def _install_socket_tracking(client: "httpx.Client", registry: _SocketRegistry) -> bool:
    """Active l'enregistrement des sockets sur le transport httpcore du client.

    Best effort VÉRIFIÉ par test (l'invariant « stop pré-en-têtes réellement
    effectif » est testé de bout en bout) : si l'interne httpcore change, on
    refuse de casser le fournisseur — l'annulation resterait alors bornée par
    le délai — et l'incident est journalisé une fois.
    """
    global _socket_tracking_warned
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None) if pool is not None else None
    if pool is None or backend is None:
        if not _socket_tracking_warned:
            _socket_tracking_warned = True
            logging.getLogger("wallia.llm").warning(
                "suivi de socket indisponible (interne httpcore): le stop pré-en-têtes "
                "resterait borné par le délai fournisseur"
            )
        return False
    pool._network_backend = _TrackingNetworkBackend(backend, registry)
    return True


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
    if parsed.username or parsed.password:
        return False, "identifiants interdits dans l'endpoint"
    if parsed.query or parsed.fragment:
        return False, "paramètres ou fragment interdits dans l'endpoint"
    host = parsed.hostname or ""
    if not host:
        return False, "hôte manquant"
    try:
        port = parsed.port
    except ValueError:
        # `urlparse().port` lève ValueError sur un port non numérique :
        # erreur de configuration stable (422 côté API), jamais un 500.
        return False, "port invalide dans l'endpoint"
    if settings.is_production:
        default_port = 443 if parsed.scheme == "https" else 80
        if port not in (None, default_port):
            return False, f"port inattendu refusé ({port})"
    if host not in settings.provider_allowed_domains:
        return False, f"domaine non autorisé ({host})"
    if settings.is_production:
        public = _is_public_ip(host)
        if public is False:
            return False, "adresse réseau non publique refusée"
        if public is None:
            return False, "résolution DNS impossible"
    return True, None


def provider_stream_error(payload: Any) -> str:
    """Catégorie stable pour un événement `error` du flux SSE.

    Ne restitue JAMAIS le texte brut du fournisseur : seuls des codes connus
    sont traduits, tout le reste retombe sur une catégorie générique.
    """
    code = ""
    if isinstance(payload, dict):
        raw = payload.get("code") or payload.get("type") or ""
        code = str(raw).strip().lower()[:64]
    if code in ("invalid_api_key", "authentication_error", "invalid_authentication"):
        return "authentification fournisseur refusée"
    if code in ("insufficient_quota", "insufficient_balance", "quota_exceeded"):
        return "crédit ou quota fournisseur épuisé"
    if code in ("rate_limit_exceeded", "rate_limit", "too_many_requests"):
        return "limite de débit fournisseur atteinte"
    if code in ("server_error", "internal_error", "service_unavailable", "overloaded_error"):
        return "erreur serveur fournisseur"
    return "erreur signalée par le flux fournisseur"


def provider_http_error(status_code: int) -> str:
    """Erreur stable par catégorie : jamais le corps brut du fournisseur."""
    if status_code in (401, 403):
        return f"authentification fournisseur refusée (HTTP {status_code})"
    if status_code == 402:
        return "crédit ou quota fournisseur épuisé (HTTP 402)"
    if status_code == 404:
        return "endpoint fournisseur introuvable (HTTP 404)"
    if status_code == 429:
        return "limite de débit fournisseur atteinte (HTTP 429)"
    if 300 <= status_code < 400:
        return f"redirection fournisseur refusée (HTTP {status_code})"
    if status_code >= 500:
        return f"erreur serveur fournisseur (HTTP {status_code})"
    return f"requête refusée par le fournisseur (HTTP {status_code})"


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
        cancellation: ProviderCancellation | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Générateur de deltas : {'type': 'delta', 'text': str} puis {'type': 'done', 'usage': ...}.

        L'annulation (`cancellation.cancel(...)`) ferme explicitement la réponse
        httpx, y compris pendant une lecture bloquée et avant tout delta. Un flux
        qui se termine sans `[DONE]` (EOF prématuré) est une ERREUR, jamais une
        réponse complète vide.
        """
        cancel = cancellation if cancellation is not None else ProviderCancellation()
        ok, reason = validate_endpoint(self.endpoint, self.settings)
        if not ok:
            raise ProviderError("config", f"endpoint refusé: {reason}")
        payload = self._payload(messages, stream=True)
        deadline = time.monotonic() + self.timeout_s
        # Un fournisseur silencieux est borné par le DÉLAI TOTAL : le timeout de
        # lecture est plafonné par le délai fournisseur restant, jamais par le
        # seul plafond global de lecture.
        read_timeout = max(1.0, min(float(self.settings.provider_read_timeout), float(self.timeout_s)))
        timeout = httpx.Timeout(
            connect=self.settings.provider_connect_timeout,
            read=read_timeout,
            write=self.settings.provider_connect_timeout,
            pool=self.settings.provider_connect_timeout,
        )
        saw_done = False
        saw_content = False
        # Veilleur de délai TOTAL : le silence (même avant les en-têtes) est
        # réellement borné et annulable, sans dépendre de l'arrivée d'une ligne.
        watchdog = threading.Timer(self.timeout_s, lambda: cancel.cancel("timeout"))
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    # Silence AVANT les en-têtes : le socket est enregistré dès
                    # son ouverture par le transport, donc un stop/déconnexion
                    # le ferme réellement (shutdown → recv débloqué) même si
                    # aucune en-tête n'est jamais arrivée. `client.close()`
                    # reste attaché en second rideau (fermeture du pool).
                    registry = _SocketRegistry()
                    _install_socket_tracking(client, registry)
                    cancel.attach(client.close)
                    cancel.attach(registry.shutdown_all)
                    try:
                        with client.stream("POST", self._chat_url(), headers=self._headers(), json=payload) as response:
                            if cancel.cancelled:
                                raise ProviderCancelled(cancel.reason)
                            if response.status_code >= 300:
                                raise ProviderError("http", provider_http_error(response.status_code))
                            # Fermeture RÉELLE d'une lecture bloquée : `close()`
                            # seul ne réveille pas un thread bloqué sur recv() ;
                            # `shutdown()` si (constaté par sonde : réveil
                            # immédiat avec RemoteProtocolError). Indispensable
                            # face à un amont totalement silencieux.
                            network_stream = response.extensions.get("network_stream")
                            socket_obj = (
                                network_stream.get_extra_info("socket") if network_stream is not None else None
                            )

                            def _interrupt_transport() -> None:
                                if socket_obj is not None:
                                    try:
                                        socket_obj.shutdown(socket.SHUT_RDWR)
                                    except OSError:
                                        pass
                                try:
                                    response.close()
                                except Exception:  # noqa: BLE001 - fermeture best effort
                                    pass

                            cancel.attach(_interrupt_transport)
                            try:
                                for line in response.iter_lines():
                                    if cancel.cancelled:
                                        raise ProviderCancelled(cancel.reason)
                                    if cancel_check is not None and cancel_check():
                                        raise ProviderCancelled("stop")
                                    if time.monotonic() > deadline:
                                        cancel.cancel("timeout")
                                        raise ProviderCancelled("timeout")
                                    line = line.strip()
                                    if not line or not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if not data:
                                        continue
                                    if data == "[DONE]":
                                        if not saw_content:
                                            # [DONE] après un corps vide n'est PAS une
                                            # réponse réussie : flux inexploitable.
                                            raise ProviderError("empty", "réponse vide du fournisseur")
                                        saw_done = True
                                        yield {"type": "done", "usage": None}
                                        return
                                    try:
                                        parsed = json.loads(data)
                                    except json.JSONDecodeError:
                                        raise ProviderError(
                                            "protocol", "réponse fournisseur malformée (JSON invalide)"
                                        ) from None
                                    if not isinstance(parsed, dict):
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    if parsed.get("error"):
                                        # Événement `error` du flux : catégorie stable,
                                        # jamais le corps brut du fournisseur.
                                        raise ProviderError("provider", provider_stream_error(parsed.get("error")))
                                    usage = parsed.get("usage")
                                    choices = parsed.get("choices")
                                    if not choices:
                                        if isinstance(choices, list) or choices is None:
                                            if usage:
                                                yield {"type": "usage", "usage": usage}
                                            continue
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    if not isinstance(choices, list):
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    first = choices[0]
                                    if not isinstance(first, dict):
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    delta = first.get("delta")
                                    if delta is None:
                                        delta = {}
                                    if not isinstance(delta, dict):
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    text = delta.get("content")
                                    if text is not None and not isinstance(text, str):
                                        raise ProviderError("protocol", "réponse fournisseur malformée")
                                    if text:
                                        saw_content = True
                                        yield {"type": "delta", "text": text}
                                    if first.get("finish_reason") and usage:
                                        yield {"type": "usage", "usage": usage}
                            finally:
                                cancel.detach(_interrupt_transport)
                    finally:
                        cancel.detach(registry.shutdown_all)
                        cancel.detach(client.close)
                if not saw_done:
                    # EOF sans `[DONE]` : flux tronqué, jamais « complete ».
                    raise ProviderError("stream_eof", "le flux fournisseur s'est interrompu avant la fin")
            finally:
                watchdog.cancel()
        except ProviderCancelled:
            raise
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - erreurs transport → catégories stables
            if cancel.cancelled:
                raise ProviderCancelled(cancel.reason) from exc
            if isinstance(exc, httpx.ConnectError):
                raise ProviderError("connect", f"connexion au fournisseur impossible ({exc.__class__.__name__})") from exc
            if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout, httpx.PoolTimeout)):
                raise ProviderError("timeout", "délai de lecture dépassé") from exc
            if isinstance(exc, httpx.HTTPError):
                raise ProviderError("http", f"erreur réseau fournisseur ({exc.__class__.__name__})") from exc
            raise ProviderError("internal", f"erreur de transport ({exc.__class__.__name__})") from exc

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
        if response.status_code >= 300:
            return {
                "ok": False,
                "error": provider_http_error(response.status_code),
                "latency_ms": latency,
                "model": self.model,
            }
        try:
            body = response.json()
            served = body.get("model") or self.model
        except Exception:
            served = self.model
        return {"ok": True, "error": None, "latency_ms": latency, "model": served}
