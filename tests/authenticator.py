"""A software authenticator, so the passkey ceremonies are tested for real.

Ported from Vigilo's test/authenticator.ts. Passkey verification is the one
part of sign-in that cannot be checked by inspection: either the server accepts
a signature it should and refuses one it should not, or it does not, and only
running both proves which. This builds the same bytes a phone would, and the
tests drive the real routes with them.

A fixture, not a library: ES256, "none" attestation, resident keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


@dataclass
class Credential:
    credential_id: bytes
    key: ec.EllipticCurvePrivateKey
    rp_id: str
    user_handle: bytes
    sign_count: int = 0


@dataclass
class SoftAuthenticator:
    origin: str
    credentials: list[Credential] = field(default_factory=list)
    user_verified: bool = True

    def _cose_key(self, key: ec.EllipticCurvePrivateKey) -> bytes:
        nums = key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: nums.x.to_bytes(32, 'big'),
                -3: nums.y.to_bytes(32, 'big'),
            }
        )

    def _flags(self, attested: bool) -> int:
        f = FLAG_UP | FLAG_BE | FLAG_BS
        if self.user_verified:
            f |= FLAG_UV
        if attested:
            f |= FLAG_AT
        return f

    def create(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """navigator.credentials.create() for the JSON options the server sent."""
        rp_id = options['rp']['id']
        user_handle = unb64u(options['user']['id'])
        key = ec.generate_private_key(ec.SECP256R1())
        cred_id = os.urandom(32)
        client_data = json.dumps(
            {
                'type': 'webauthn.create',
                'challenge': options['challenge'],
                'origin': origin or self.origin,
                'crossOrigin': False,
            }
        ).encode()
        attested = b'\x00' * 16 + struct.pack('>H', len(cred_id)) + cred_id + self._cose_key(key)
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(True)])
            + struct.pack('>I', 0)
            + attested
        )
        att_obj = cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': auth_data})
        self.credentials.append(Credential(cred_id, key, rp_id, user_handle))
        return {
            'id': b64u(cred_id),
            'rawId': b64u(cred_id),
            'type': 'public-key',
            'response': {
                'clientDataJSON': b64u(client_data),
                'attestationObject': b64u(att_obj),
                'transports': ['internal', 'hybrid'],
            },
            'clientExtensionResults': {},
            'authenticatorAttachment': 'platform',
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        credential: Credential | None = None,
        origin: str | None = None,
        tamper: bool = False,
    ) -> dict[str, Any]:
        """navigator.credentials.get(). Uses the allow-list, else any credential for the RP."""
        rp_id = options.get('rpId')
        allowed = {unb64u(c['id']) for c in options.get('allowCredentials') or []}
        if credential is None:
            pool = [c for c in self.credentials if c.rp_id == rp_id]
            if allowed:
                pool = [c for c in pool if c.credential_id in allowed]
            if not pool:
                raise LookupError('no matching credential on this authenticator')
            credential = pool[-1]
        credential.sign_count += 1
        client_data = json.dumps(
            {
                'type': 'webauthn.get',
                'challenge': options['challenge'],
                'origin': origin or self.origin,
                'crossOrigin': False,
            }
        ).encode()
        auth_data = (
            hashlib.sha256(credential.rp_id.encode()).digest()
            + bytes([self._flags(False)])
            + struct.pack('>I', credential.sign_count)
        )
        sig = credential.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        if tamper:
            sig = sig[:-1] + bytes([sig[-1] ^ 0x01])
        return {
            'id': b64u(credential.credential_id),
            'rawId': b64u(credential.credential_id),
            'type': 'public-key',
            'response': {
                'clientDataJSON': b64u(client_data),
                'authenticatorData': b64u(auth_data),
                'signature': b64u(sig),
                'userHandle': b64u(credential.user_handle),
            },
            'clientExtensionResults': {},
            'authenticatorAttachment': 'platform',
        }
