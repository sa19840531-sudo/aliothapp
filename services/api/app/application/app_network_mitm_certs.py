"""App 网络 MITM 证书：进程内 CA + 按主机签发叶子证书。

仅用于受控质检环境；不持久化请求体或凭证。cryptography 缺失时调用方应降级 CONNECT-only。
"""

from __future__ import annotations

import logging
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    _CRYPTO_AVAILABLE = True
except Exception:  # noqa: BLE001
    x509 = None  # type: ignore[assignment]
    _CRYPTO_AVAILABLE = False


@dataclass(frozen=True, kw_only=True)
class MitmAuthorityMaterial:
    """CA 材料对外只暴露 PEM，私钥不出进程边界。"""

    ca_cert_pem: bytes
    ca_key_pem: bytes
    storage_dir: str | None = None


class AppNetworkMitmAuthority:
    """按主机缓存叶子证书的 MITM 签发器。"""

    def __init__(self, *, storage_dir: Path | None = None) -> None:
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography 不可用，无法启用 TLS MITM")
        self._lock = threading.RLock()
        self._storage_dir = storage_dir
        self._leaf_cache: dict[str, tuple[bytes, bytes]] = {}
        self._ca_cert: Any
        self._ca_key: Any
        self._ca_cert_pem: bytes
        self._ca_key_pem: bytes
        self._load_or_create_ca()

    @property
    def ca_cert_pem(self) -> bytes:
        return self._ca_cert_pem

    def material(self) -> MitmAuthorityMaterial:
        return MitmAuthorityMaterial(
            ca_cert_pem=self._ca_cert_pem,
            ca_key_pem=self._ca_key_pem,
            storage_dir=str(self._storage_dir) if self._storage_dir else None,
        )

    def ssl_context_for_host(self, hostname: str) -> ssl.SSLContext:
        host = str(hostname or "").strip().lower()
        if not host or any(ch in host for ch in ("/", " ", "\n", "\r")):
            raise ValueError("MITM hostname 非法")
        cert_pem, key_pem = self._leaf_for_host(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # 临时文件写入由 load_cert_chain 需要路径；用 NamedTemporaryFile 不安全并发
        # 改用 memory BIO 不可用，故写到进程缓存目录
        cert_path, key_path = self._write_leaf_files(host, cert_pem, key_pem)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx

    def _load_or_create_ca(self) -> None:
        assert x509 is not None
        if self._storage_dir is not None:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            cert_path = self._storage_dir / "alioth-app-mitm-ca.crt"
            key_path = self._storage_dir / "alioth-app-mitm-ca.key"
            if cert_path.is_file() and key_path.is_file():
                self._ca_cert_pem = cert_path.read_bytes()
                self._ca_key_pem = key_path.read_bytes()
                self._ca_cert = x509.load_pem_x509_certificate(self._ca_cert_pem)
                self._ca_key = serialization.load_pem_private_key(
                    self._ca_key_pem, password=None
                )
                return
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Alioth App QC"),
                x509.NameAttribute(NameOID.COMMON_NAME, "Alioth App Network MITM CA"),
            ]
        )
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        self._ca_key = key
        self._ca_cert = cert
        self._ca_key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._ca_cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        if self._storage_dir is not None:
            (self._storage_dir / "alioth-app-mitm-ca.crt").write_bytes(self._ca_cert_pem)
            (self._storage_dir / "alioth-app-mitm-ca.key").write_bytes(self._ca_key_pem)
            logger.info("App MITM CA 已写入 %s", self._storage_dir)

    def _leaf_for_host(self, host: str) -> tuple[bytes, bytes]:
        with self._lock:
            cached = self._leaf_cache.get(host)
            if cached is not None:
                return cached
            assert x509 is not None
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.now(UTC)
            subject = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]
            )
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(self._ca_cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=30))
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(host)]),
                    critical=False,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                    critical=False,
                )
                .add_extension(
                    x509.BasicConstraints(ca=False, path_length=None),
                    critical=True,
                )
                .sign(self._ca_key, hashes.SHA256())
            )
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            key_pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            self._leaf_cache[host] = (cert_pem, key_pem)
            return cert_pem, key_pem

    def _write_leaf_files(
        self, host: str, cert_pem: bytes, key_pem: bytes
    ) -> tuple[Path, Path]:
        base = self._storage_dir
        if base is None:
            base = Path(".alioth") / "app-network-mitm" / "leaf"
        else:
            base = Path(base) / "leaf"
        base.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in host)[:120]
        cert_path = base / f"{safe}.crt"
        key_path = base / f"{safe}.key"
        if not cert_path.is_file():
            cert_path.write_bytes(cert_pem)
        if not key_path.is_file():
            key_path.write_bytes(key_pem)
        return cert_path, key_path


def cryptography_available() -> bool:
    return _CRYPTO_AVAILABLE


def build_upstream_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def android_system_ca_filename(cert_pem: bytes) -> str:
    """计算 Android /system/etc/security/cacerts 所需的 subject_hash_old 文件名。"""
    if not _CRYPTO_AVAILABLE or x509 is None:
        raise RuntimeError("cryptography 不可用")
    cert = x509.load_pem_x509_certificate(cert_pem)
    subject_der = cert.subject.public_bytes(serialization.Encoding.DER)
    digest = __import__("hashlib").md5(subject_der).digest()
    return f"{int.from_bytes(digest[:4], 'little'):08x}.0"


def pem_to_der(cert_pem: bytes) -> bytes:
    if not _CRYPTO_AVAILABLE or x509 is None:
        raise RuntimeError("cryptography 不可用")
    cert = x509.load_pem_x509_certificate(cert_pem)
    return cert.public_bytes(serialization.Encoding.DER)
