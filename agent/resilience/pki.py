"""Key material generation (used by scripts/gen-certs.py, the simulator and tests).

Per resilience node we create:
  * an X.509 certificate (ECDSA P-256) signed by the platform CA, CN=agent-x,
    usable for both TLS server and client auth (mTLS);
  * an Ed25519 signing key for evidence / votes.
Public Ed25519 keys of all nodes go into one registry (pubkeys.json).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
from typing import Dict, Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .crypto import Signer


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "cr-platform"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _write(path: str, data: bytes, private: bool = False) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
    if private:
        os.chmod(path, 0o600)


def make_ca(days: int = 365):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(_name("cr-platform-ca")).issuer_name(_name("cr-platform-ca"))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_leaf(ca_key, ca_cert, cn: str, dns: Iterable[str], days: int = 365):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder()
            .subject_name(_name(cn)).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def agent_dns_names(agent_name: str, namespace: str = "resilience") -> list:
    return [agent_name, f"{agent_name}.{namespace}", f"{agent_name}.{namespace}.svc",
            f"{agent_name}.{namespace}.svc.cluster.local", "localhost"]


def generate(outdir: str, agents: Dict[str, str], namespace: str = "resilience") -> Dict[str, str]:
    """agents: node_id -> agent_name. Writes:
         outdir/ca.crt, outdir/ca.key, outdir/pubkeys.json
         outdir/<agent_name>/{ca.crt,tls.crt,tls.key,signing.key}
       Returns the public-key registry (node_id -> base64 Ed25519 key)."""
    os.makedirs(outdir, exist_ok=True)
    ca_key, ca_cert = make_ca()
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    _write(os.path.join(outdir, "ca.crt"), ca_pem)
    _write(os.path.join(outdir, "ca.key"), _pem_key(ca_key), private=True)
    pubkeys = {}
    for node_id, agent_name in agents.items():
        d = os.path.join(outdir, agent_name)
        os.makedirs(d, exist_ok=True)
        key, cert = make_leaf(ca_key, ca_cert, agent_name, agent_dns_names(agent_name, namespace))
        _write(os.path.join(d, "ca.crt"), ca_pem)
        _write(os.path.join(d, "tls.crt"), cert.public_bytes(serialization.Encoding.PEM))
        _write(os.path.join(d, "tls.key"), _pem_key(key), private=True)
        signer = Signer.generate(node_id)
        _write(os.path.join(d, "signing.key"), _pem_key(signer._key), private=True)
        pubkeys[node_id] = signer.public_key_b64()
    with open(os.path.join(outdir, "pubkeys.json"), "w") as fh:
        json.dump(pubkeys, fh, indent=2)
    return pubkeys
