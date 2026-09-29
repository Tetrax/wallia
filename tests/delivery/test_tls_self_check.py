"""Test de la validation de paire TLS de l'activateur (clés/certs ÉPHÉMÈRES).

Couvre le défaut central du point 1 de la revue : `validate_pair` comparait la
clé PUBLIQUE du certificat au DER PRIVÉ de la clé privée et rejetait donc une
vraie paire. Ici, une paire auto-signée est générée dans le répertoire
temporaire du test (JAMAIS de TLS réel, aucune mutation système) et validée par
`deployment/wallia-tls-activate.sh --self-check` (sans privilège).

Nécessite le binaire `openssl` ; sinon le test est ignoré (skip explicite).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVATE = REPO_ROOT / "deployment" / "wallia-tls-activate.sh"
HOST = "wallia.valdev.me"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl requis")


def generate_pair(root: Path, name: str) -> tuple[Path, Path]:
    key = root / f"{name}.key.pem"
    cert = root / f"{name}.cert.pem"
    proc = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "2",
            "-subj", f"/CN={HOST}",
            "-addext", f"subjectAltName=DNS:{HOST}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return cert, key


def self_check(cert: Path, key: Path, host: str = HOST) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ACTIVATE), "--self-check", "--cert", str(cert), "--key", str(key), "--host", host],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_paire_valide_acceptee(tmp_path: Path) -> None:
    cert, key = generate_pair(tmp_path, "valide")
    proc = self_check(cert, key)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["host"] == HOST
    assert payload["fingerprint_sha256"]


def test_cle_etrangere_refusee(tmp_path: Path) -> None:
    cert, _ = generate_pair(tmp_path, "cert")
    _, other_key = generate_pair(tmp_path, "autre")
    proc = self_check(cert, other_key)
    assert proc.returncode != 0
    assert "ne correspond pas au certificat" in proc.stderr


def test_hote_non_couvert_refuse(tmp_path: Path) -> None:
    cert, key = generate_pair(tmp_path, "valide")
    proc = self_check(cert, key, host="autre.valdev.me")
    assert proc.returncode != 0
    assert "ne correspond pas à l'hôte" in proc.stderr


def test_certificat_futur_refuse(tmp_path: Path) -> None:
    """notBefore dans le futur : PAS « pas encore expiré » ⇒ refus explicite.

    Le certificat futur est construit avec `cryptography` (déjà disponible) ;
    le test est ignoré si la bibliothèque est absente. Aucune mutation système.
    """
    pytest.importorskip("cryptography")
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "futur.cert.pem"
    key_path = tmp_path / "futur.key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    proc = self_check(cert_path, key_path)
    assert proc.returncode != 0
    assert "PAS ENCORE VALIDE" in proc.stderr
    assert "notBefore" in proc.stderr
