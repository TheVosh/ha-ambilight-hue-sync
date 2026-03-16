"""Generate a self-signed certificate matching Hue Bridge format."""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)


def get_bridge_id(mac: str) -> str:
    """Derive a Hue-style Bridge ID from a MAC address.

    Inserts FFFE in the middle: AA:BB:CC:DD:EE:FF -> AABBCCFFDDEEFF
    """
    octets = mac.replace(":", "").replace("-", "").upper()
    return f"{octets[:6]}FFFE{octets[6:12]}"


def generate_certificate(cert_dir: Path, bridge_id: str) -> tuple[Path, Path]:
    """Generate an ECDSA self-signed cert matching Hue Bridge expectations.

    Returns (cert_path, key_path).
    """
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if cert_path.exists() and key_path.exists():
        _LOGGER.debug("Certificate already exists at %s", cert_path)
        return cert_path, key_path

    os.makedirs(cert_dir, exist_ok=True)

    # Generate EC P-256 key
    private_key = ec.generate_private_key(ec.SECP256R1())

    # Build certificate matching Hue Bridge format
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Philips Hue"),
            x509.NameAttribute(NameOID.COMMON_NAME, bridge_id.lower()),
        ]
    )

    serial_number = int(bridge_id, 16)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(serial_number)
        .not_valid_before(datetime.datetime(2017, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2038, 1, 1, tzinfo=datetime.timezone.utc))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
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
        .sign(private_key, hashes.SHA256())
    )

    # Write key
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # Write cert
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    _LOGGER.info("Generated bridge certificate for %s", bridge_id)
    return cert_path, key_path
