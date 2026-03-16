"""Tests for certificate generation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# Load certificate.py directly to avoid pulling in homeassistant via __init__.py
_cert_module_path = (
    Path(__file__).parent.parent / "custom_components" / "hue_entertainment" / "certificate.py"
)
_spec = importlib.util.spec_from_file_location("certificate", _cert_module_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

generate_certificate = _mod.generate_certificate
get_bridge_id = _mod.get_bridge_id

VALID_BRIDGE_ID = "AABBCCFFFE112233"


class TestGetBridgeId:
    def test_colon_separator(self):
        assert get_bridge_id("AA:BB:CC:DD:EE:FF") == "AABBCCFFFEDDEEFF"

    def test_correct_fffe_insertion(self):
        # First 6 hex chars of MAC + FFFE + last 6 hex chars
        result = get_bridge_id("AA:BB:CC:DD:EE:FF")
        assert result == "AABBCCFFFEDDEEFF"

    def test_dash_separator(self):
        result = get_bridge_id("AA-BB-CC-DD-EE-FF")
        assert result == get_bridge_id("AA:BB:CC:DD:EE:FF")

    def test_output_is_uppercase(self):
        result = get_bridge_id("aa:bb:cc:dd:ee:ff")
        assert result == result.upper()

    def test_length_is_16(self):
        result = get_bridge_id("AA:BB:CC:DD:EE:FF")
        assert len(result) == 16


class TestGenerateCertificate:
    def test_files_created(self, tmp_path):
        cert_path, key_path = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        assert cert_path.exists()
        assert key_path.exists()

    def test_returns_correct_paths(self, tmp_path):
        cert_path, key_path = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        assert cert_path == tmp_path / "cert.pem"
        assert key_path == tmp_path / "key.pem"

    def test_cert_is_valid_pem(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert is not None

    def test_key_is_valid_ec_pem(self, tmp_path):
        _, key_path = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)

    def test_key_is_p256(self, tmp_path):
        _, key_path = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert isinstance(key.curve, ec.SECP256R1)

    def test_cn_is_bridge_id_lowercase(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == VALID_BRIDGE_ID.lower()

    def test_country_and_org(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        country = cert.subject.get_attributes_for_oid(NameOID.COUNTRY_NAME)[0].value
        org = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value
        assert country == "NL"
        assert org == "Philips Hue"

    def test_self_signed(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert.subject == cert.issuer

    def test_serial_number(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert.serial_number == int(VALID_BRIDGE_ID, 16)

    def test_validity_window(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        assert cert.not_valid_before_utc.year == 2017
        assert cert.not_valid_after_utc.year == 2038

    def test_basic_constraints_ca(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.critical is True
        assert bc.value.ca is True

    def test_key_usage(self, tmp_path):
        cert_path, _ = generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ku.critical is True
        assert ku.value.digital_signature is True
        assert ku.value.key_cert_sign is True
        assert ku.value.crl_sign is True
        assert ku.value.key_encipherment is False
        assert ku.value.data_encipherment is False

    def test_idempotent(self, tmp_path):
        generate_certificate(tmp_path, VALID_BRIDGE_ID)
        cert_content = (tmp_path / "cert.pem").read_bytes()
        key_content = (tmp_path / "key.pem").read_bytes()

        generate_certificate(tmp_path, VALID_BRIDGE_ID)

        assert (tmp_path / "cert.pem").read_bytes() == cert_content
        assert (tmp_path / "key.pem").read_bytes() == key_content

    def test_creates_missing_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "certs"
        assert not nested.exists()
        generate_certificate(nested, VALID_BRIDGE_ID)
        assert (nested / "cert.pem").exists()
