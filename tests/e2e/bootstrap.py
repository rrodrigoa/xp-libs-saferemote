"""Generate disposable CA, role certificates and fixture credentials."""

import base64
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def write(folder, name, value, secret=False):
    path = Path(folder, name)
    path.write_bytes(value)
    path.chmod(0o400 if secret else 0o444)
    os.chown(path, 10001, 10001)


def bootstrap():
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "SafeRemote fixture CA")]
    )

    def builder(name, key):
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(hours=4))
        )

    ca = (
        builder(subject, ca_key)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    for role in ("control-plane", "endpoint", "azurite", "runner"):
        folder = f"/{role}"
        write(folder, "ca.pem", ca.public_bytes(serialization.Encoding.PEM))
        if role == "runner":
            continue
        key = ec.generate_private_key(ec.SECP256R1())
        usages = [ExtendedKeyUsageOID.SERVER_AUTH]
        if role == "control-plane":
            usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        cert = (
            builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)]), key)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(role)]), critical=False
            )
            .add_extension(x509.ExtendedKeyUsage(usages), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        write(folder, "tls.pem", cert.public_bytes(serialization.Encoding.PEM))
        write(
            folder,
            "tls.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            secret=True,
        )

    signer = ed25519.Ed25519PrivateKey.generate()
    write("/control-plane", "plan.key", signer.private_bytes_raw(), secret=True)
    write("/endpoint", "plan.pub", signer.public_key().public_bytes_raw())
    write("/runner", "plan.pub", signer.public_key().public_bytes_raw())
    storage = base64.b64encode(secrets.token_bytes(32))
    for role in ("control-plane", "azurite"):
        write(f"/{role}", "storage.key", storage, secret=True)
    for label in ("owner", "outsider", "approver"):
        token = secrets.token_urlsafe(32).encode()
        for role in ("control-plane", "runner"):
            write(f"/{role}", label, token, secret=True)
    for folder in ("/journal", "/reports"):
        os.chown(folder, 10001, 10001)
    Path("/tmp/ready").touch()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    bootstrap()
