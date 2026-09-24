"""Ed25519 signing / verification for evidence and votes.

Signatures cover `kind || 0x00 || payload` so an evidence signature can never
be replayed as a vote (domain separation).
"""
from __future__ import annotations

import base64
import json
import os
from typing import Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _message(kind: str, payload: bytes) -> bytes:
    return kind.encode() + b"\x00" + payload


class Signer:
    def __init__(self, node_id: str, private_key: Ed25519PrivateKey):
        self.node_id = node_id
        self._key = private_key

    @classmethod
    def generate(cls, node_id: str) -> "Signer":
        return cls(node_id, Ed25519PrivateKey.generate())

    @classmethod
    def from_pem_file(cls, node_id: str, path: str) -> "Signer":
        with open(path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 private key")
        return cls(node_id, key)

    def sign(self, kind: str, payload: bytes) -> bytes:
        return self._key.sign(_message(kind, payload))

    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def public_key_b64(self) -> str:
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()


class KeyRegistry:
    """Public keys of all resilience nodes (distributed as a ConfigMap)."""

    def __init__(self, keys: Dict[str, Ed25519PublicKey]):
        self._keys = dict(keys)

    @classmethod
    def from_b64_map(cls, mapping: Dict[str, str]) -> "KeyRegistry":
        return cls({nid: Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))
                    for nid, b64 in mapping.items()})

    @classmethod
    def from_json_file(cls, path: str) -> "KeyRegistry":
        with open(path) as fh:
            return cls.from_b64_map(json.load(fh))

    @classmethod
    def from_env_or_default(cls) -> "KeyRegistry":
        return cls.from_json_file(os.environ.get(
            "PEER_PUBKEYS", "/etc/resilience/pubkeys/pubkeys.json"))

    def knows(self, node_id: str) -> bool:
        return node_id in self._keys

    def verify(self, node_id: str, kind: str, payload: bytes, signature: bytes) -> bool:
        key = self._keys.get(node_id)
        if key is None:
            return False
        try:
            key.verify(signature, _message(kind, payload))
            return True
        except InvalidSignature:
            return False
