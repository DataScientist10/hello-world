"""Request authentication for an air-gapped deployment.

There is no PKI to lean on and no OAuth server to call, so every vehicle gets a
256-bit shared secret at provisioning time and signs each request with
HMAC-SHA256. The signature covers the method, path, timestamp, nonce and a
digest of the body, which means an attacker with a tap on the depot LAN can
neither replay a request nor alter one in flight -- even when the hub is
running plain HTTP.

TLS (from the depot's own offline CA) is still recommended on top for
confidentiality; see docs/OFFLINE_DEPLOYMENT.md.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

HEADER_VEHICLE = "X-Vehicle-Id"
HEADER_TIMESTAMP = "X-Timestamp"
HEADER_NONCE = "X-Nonce"
HEADER_SIGNATURE = "X-Signature"
HEADER_RESPONSE_SIGNATURE = "X-Response-Signature"
HEADER_OPERATOR = "X-Operator-Key"
HEADER_PROVISIONING = "X-Provisioning-Key"

SIGNATURE_VERSION = "v1"
RESPONSE_SIGNATURE_VERSION = "v1-response"


class AuthError(Exception):
    """Raised when a request cannot be authenticated."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def generate_secret() -> str:
    """A fresh per-vehicle shared secret, safe to print into a provisioning file."""
    return secrets.token_hex(32)


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    """The exact bytes both sides sign.

    Including the version prefix lets us rotate the scheme later without the
    hub having to guess which variant a vehicle used.
    """
    return "\n".join([SIGNATURE_VERSION, method.upper(), path, str(timestamp), nonce, body_digest(body)])


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = canonical_string(method, path, timestamp, nonce, body).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def response_canonical_string(request_nonce: str, status: int, body: bytes) -> str:
    """The bytes both sides sign for a *response*.

    Binding the signature to the request's nonce is what stops an attacker
    replaying a genuine older response (an empty command list, say) against a
    later poll: the nonce is fresh per request and never repeats.
    """
    return "\n".join([RESPONSE_SIGNATURE_VERSION, request_nonce, str(status), body_digest(body)])


def sign_response(secret: str, request_nonce: str, status: int, body: bytes) -> str:
    message = response_canonical_string(request_nonce, status, body).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_response(secret: str, request_nonce: str, status: int, body: bytes, signature: str | None) -> bool:
    """Constant-time check that a response really came from the hub."""
    if not signature:
        return False
    expected = sign_response(secret, request_nonce, status, body)
    return hmac.compare_digest(expected, signature)


def signed_headers(vehicle_id: str, secret: str, method: str, path: str, body: bytes) -> dict[str, str]:
    """Build the four auth headers for one outbound request (used by the edge agent)."""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(12)
    return {
        HEADER_VEHICLE: vehicle_id,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(secret, method, path, timestamp, nonce, body),
    }


class NonceCache:
    """Remembers recently used nonces so a captured request cannot be replayed.

    Entries live only as long as the clock-skew window: outside it the
    timestamp check rejects the request anyway, so the cache can stay small
    (450 vehicles * a few requests/second * 300s worst case, pruned lazily).
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._last_prune = 0.0

    def check_and_add(self, vehicle_id: str, nonce: str, now: float | None = None) -> bool:
        """Return True if the nonce is fresh; False if it has been seen."""
        now = time.time() if now is None else now
        key = (vehicle_id, nonce)
        with self._lock:
            if now - self._last_prune > self._ttl:
                self._prune(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._last_prune = now

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


class Authenticator:
    """Verifies vehicle signatures and operator/provisioning keys."""

    def __init__(self, registry, config) -> None:
        self._registry = registry
        self._config = config
        self._nonces = NonceCache(config.clock_skew_tolerance_seconds)

    # -- vehicles -----------------------------------------------------------
    def authenticate_vehicle(self, headers, method: str, path: str, body: bytes, now: float | None = None):
        """Return the :class:`~iot_hub.models.Vehicle` that signed this request."""
        now = time.time() if now is None else now

        vehicle_id = headers.get(HEADER_VEHICLE)
        timestamp = headers.get(HEADER_TIMESTAMP)
        nonce = headers.get(HEADER_NONCE)
        signature = headers.get(HEADER_SIGNATURE)
        if not all([vehicle_id, timestamp, nonce, signature]):
            raise AuthError("missing authentication headers")
        if len(nonce) < 8 or len(nonce) > 64:
            raise AuthError("nonce must be 8-64 characters")

        try:
            sent_at = float(timestamp)
        except ValueError:
            raise AuthError("invalid timestamp") from None
        skew = abs(now - sent_at)
        if skew > self._config.clock_skew_tolerance_seconds:
            # A vehicle with no NTP upstream can drift; tell it what we think
            # the time is so its agent can correct itself and retry.
            raise AuthError(f"timestamp outside {self._config.clock_skew_tolerance_seconds}s window (hub time {int(now)})")

        vehicle = self._registry.get(vehicle_id)
        if vehicle is None:
            raise AuthError("unknown vehicle")
        if vehicle.status != "active":
            raise AuthError(f"vehicle is {vehicle.status}", status=403)

        expected = sign(vehicle.secret, method, path, timestamp, nonce, body)
        if not hmac.compare_digest(expected, signature):
            raise AuthError("bad signature")

        # Only burn the nonce once the signature is known good, so an attacker
        # cannot poison the cache with guessed nonces.
        if not self._nonces.check_and_add(vehicle_id, nonce, now):
            raise AuthError("replayed nonce")

        return vehicle

    # -- humans and back-office tools --------------------------------------
    def authenticate_operator(self, headers) -> None:
        configured = self._config.operator_key
        if not configured:
            raise AuthError("operator API is disabled: set HUB_OPERATOR_KEY", status=403)
        presented = headers.get(HEADER_OPERATOR) or ""
        if not hmac.compare_digest(configured, presented):
            raise AuthError("invalid operator key")

    def authenticate_provisioner(self, headers) -> None:
        configured = self._config.provisioning_key
        if not configured:
            raise AuthError("online enrollment is disabled: set HUB_PROVISIONING_KEY", status=403)
        presented = headers.get(HEADER_PROVISIONING) or ""
        if not hmac.compare_digest(configured, presented):
            raise AuthError("invalid provisioning key")

    @property
    def nonce_cache_size(self) -> int:
        return len(self._nonces)
