"""Where the data lives.

Two backends, one tiny interface:

    read_all()                    -> {collection: {doc_id: document}}
    write(collection, id, doc)    -> create or replace one document
    delete(collection, id)        -> remove one document

The whole dataset is small (a college register: a few thousand documents at
most), so the app loads it once at start-up, keeps it in memory, and writes
through on every change. That removes the need for queries altogether -- and
with them, the composite-index problems that come from querying Firestore.

* ``LocalStore``     - one JSON file. No setup, works offline. This is what you
                       use on your laptop and what makes tampering easy to
                       demonstrate: open the file, change a record, run verify.
* ``FirestoreStore`` - Firebase Firestore over its REST API, using nothing but
                       the standard library. Used on Render, where the disk is
                       wiped on every deploy.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("bcoe.store")

#: The collections this app keeps. Kept short and obvious on purpose.
COLLECTIONS = ("students", "sessions", "records", "blocks")

PAGE_SIZE = 300


class StoreError(RuntimeError):
    """Any backend failure, normalised into one exception type."""


class Store(abc.ABC):
    """The interface both backends implement."""

    name = "store"

    @abc.abstractmethod
    def read_all(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Everything, as {collection: {doc_id: document}}."""

    @abc.abstractmethod
    def write(self, collection: str, doc_id: str, document: dict[str, Any]) -> None:
        """Create or replace one document."""

    @abc.abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """Remove one document."""

    def health(self) -> dict[str, Any]:
        try:
            data = self.read_all()
        except StoreError as exc:
            return {"backend": self.name, "ok": False, "error": str(exc)}
        return {
            "backend": self.name,
            "ok": True,
            "counts": {name: len(data.get(name, {})) for name in COLLECTIONS},
        }


# ============================================================================
# Local JSON file
# ============================================================================
class LocalStore(Store):
    """A single JSON file, written atomically."""

    name = "local-json"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def read_all(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StoreError(f"Cannot read {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StoreError(f"{self.path} does not contain a JSON object.")
        return {name: dict(docs) for name, docs in data.items() if isinstance(docs, dict)}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)          # atomic on every platform

    def write(self, collection: str, doc_id: str, document: dict[str, Any]) -> None:
        with self._lock:
            data = self.read_all()
            data.setdefault(collection, {})[doc_id] = document
            self._write_all(data)

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            data = self.read_all()
            if doc_id in data.get(collection, {}):
                del data[collection][doc_id]
                self._write_all(data)


# ============================================================================
# Firebase Firestore (REST, standard library only)
# ============================================================================
def to_firestore_value(value: Any) -> dict[str, Any]:
    """Python value -> Firestore value."""
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"nullValue": None}
    if isinstance(value, list):
        return {"arrayValue": {"values": [to_firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: to_firestore_value(v) for k, v in value.items()}}}
    return {"stringValue": str(value)}


def from_firestore_value(value: dict[str, Any]) -> Any:
    """Firestore value -> Python value."""
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return value["booleanValue"]
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return [from_firestore_value(v) for v in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            key: from_firestore_value(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    return None


def to_firestore_document(document: dict[str, Any]) -> dict[str, Any]:
    return {"fields": {key: to_firestore_value(value) for key, value in document.items()}}


def from_firestore_document(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: from_firestore_value(value)
        for key, value in (document.get("fields") or {}).items()
    }


class FirestoreStore(Store):
    """Firestore through its REST API.

    Only three operations are used -- list a collection, write a document,
    delete a document -- and no queries at all. Queries are what drag in
    composite indexes, which have to be created by hand in the Firebase
    console; listing documents works on any project with no setup.
    """

    name = "firestore"
    base_url = "https://firestore.googleapis.com/v1"

    def __init__(self, project_id: str, credentials: dict[str, Any], timeout: int = 20) -> None:
        self.project_id = project_id
        self.credentials = credentials
        self.timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.RLock()

    # -- authentication -------------------------------------------------
    def _access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.time() < self._token_expires_at - 60:
                return self._token
            self._token, expires_in = service_account_access_token(self.credentials)
            self._token_expires_at = time.time() + float(expires_in or 3600)
            return self._token

    @property
    def documents_url(self) -> str:
        return (
            f"{self.base_url}/projects/{self.project_id}"
            f"/databases/(default)/documents"
        )

    def _request(self, method: str, url: str, payload: Any = None, *, retry: bool = True) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        token = self._access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 403) and retry:
                self._access_token(force_refresh=True)
                return self._request(method, url, payload, retry=False)
            if exc.code == 404:
                return {}
            raise StoreError(f"Firestore {method} failed ({exc.code}): {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise StoreError(f"Cannot reach Firestore: {exc.reason}") from exc

    # -- the three operations ------------------------------------------
    def read_all(self) -> dict[str, dict[str, dict[str, Any]]]:
        data: dict[str, dict[str, dict[str, Any]]] = {}
        for collection in COLLECTIONS:
            documents: dict[str, dict[str, Any]] = {}
            page_token = ""
            while True:
                url = f"{self.documents_url}/{collection}?pageSize={PAGE_SIZE}"
                if page_token:
                    url += "&pageToken=" + urllib.parse.quote(page_token)
                page = self._request("GET", url)
                if not isinstance(page, dict):
                    break
                for item in page.get("documents") or []:
                    doc_id = str(item.get("name", "")).rsplit("/", 1)[-1]
                    documents[doc_id] = from_firestore_document(item)
                page_token = page.get("nextPageToken") or ""
                if not page_token:
                    break
            data[collection] = documents
        return data

    def write(self, collection: str, doc_id: str, document: dict[str, Any]) -> None:
        url = f"{self.documents_url}/{collection}/{urllib.parse.quote(str(doc_id))}"
        self._request("PATCH", url, to_firestore_document(document))

    def delete(self, collection: str, doc_id: str) -> None:
        url = f"{self.documents_url}/{collection}/{urllib.parse.quote(str(doc_id))}"
        self._request("DELETE", url)


# ============================================================================
# Choosing a backend
# ============================================================================
def load_service_account() -> dict[str, Any] | None:
    """Read the Firebase service account from the environment.

    Accepts either the JSON itself (what Render needs) or the path to a JSON
    file (easier locally).
    """
    value = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not value:
        return None
    if value.startswith("{"):
        return json.loads(value)
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # Not a file and not JSON: report it as invalid JSON, which is what it is.
    return json.loads(value)


def build_store(settings) -> tuple[Store, dict[str, Any]]:
    """Return the best available store, plus a short report for the UI.

    A misconfigured Firebase must never stop the app from booting: it falls
    back to the local file and says why.
    """
    report: dict[str, Any] = {"requested": settings.storage_backend, "fallback": False, "notes": []}
    backend = (settings.storage_backend or "auto").lower()
    wants_cloud = backend in ("auto", "firestore")

    if wants_cloud and (settings.firebase_project_id or os.environ.get("FIREBASE_SERVICE_ACCOUNT")):
        try:
            credentials = load_service_account()
            if not credentials:
                raise StoreError("No FIREBASE_SERVICE_ACCOUNT value found.")
            project_id = settings.firebase_project_id or credentials.get("project_id", "")
            if not project_id:
                raise StoreError("No FIREBASE_PROJECT_ID value found.")
            store = FirestoreStore(project_id, credentials)
            store.read_all()                     # prove we can actually reach it
            report["notes"].append(f"Connected to Firestore project {project_id}.")
            return store, report
        except Exception as exc:                  # noqa: BLE001 - never fail to boot
            report["fallback"] = True
            report["notes"].append(f"Firestore unavailable: {exc}")
            log.warning("Firestore unavailable (%s); using the local JSON file", exc)
    elif backend == "firestore":
        report["fallback"] = True
        report["notes"].append("Firestore requested but no project id was set.")

    store = LocalStore(settings.storage_path)
    report["notes"].append(f"Using the local file {settings.storage_path}.")
    return store, report


# ----------------------------------------------------------------------------
# Service-account signing (RS256) and the OAuth token exchange.
#
# Firestore needs an OAuth2 access token, and getting one means signing a JWT
# with the service account's RSA key. This is the only piece of the project
# that is not worth writing again from memory: it was checked against OpenSSL
# (`openssl dgst -sha256 -verify`) and is kept exactly as it was proved.
# ----------------------------------------------------------------------------
class JWTError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Tiny ASN.1 DER reader
# --------------------------------------------------------------------------
def _der_read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one TLV triple. Returns (tag, value_bytes, next_offset)."""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        num_bytes = length & 0x7F
        length = int.from_bytes(data[offset : offset + num_bytes], "big")
        offset += num_bytes
    value = data[offset : offset + length]
    return tag, value, offset + length


def _der_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def parse_pkcs8_rsa_private_key(pem: str) -> tuple[int, int]:
    """Extract ``(n, d)`` -- the RSA modulus and private exponent -- from a PKCS#8 PEM.

    Structure we walk::

        PrivateKeyInfo ::= SEQUENCE {
            version Integer,
            privateKeyAlgorithm AlgorithmIdentifier,
            privateKey OCTET STRING   -- contains RSAPrivateKey
        }
        RSAPrivateKey ::= SEQUENCE {
            version, modulus(n), publicExponent(e), privateExponent(d), ...
        }

    We only need ``n`` and ``d`` to sign.
    """
    body = "".join(
        line.strip()
        for line in pem.splitlines()
        if "-----" not in line and line.strip()
    )
    der = base64.b64decode(body)

    tag, seq, _ = _der_read(der, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE at the top of the key")

    offset = 0
    _, _version, offset = _der_read(seq, offset)
    _, _algorithm, offset = _der_read(seq, offset)
    tag, octet, _ = _der_read(seq, offset)
    if tag != 0x04:
        raise JWTError("Expected an OCTET STRING wrapping the RSA key")

    offset = 0
    tag, rsa_seq, _ = _der_read(octet, 0)
    if tag != 0x30:
        raise JWTError("Expected an ASN.1 SEQUENCE inside the private key")

    offset = 0
    _, _v, offset = _der_read(rsa_seq, offset)          # version
    _, n_bytes, offset = _der_read(rsa_seq, offset)     # modulus
    _, _e_bytes, offset = _der_read(rsa_seq, offset)    # public exponent
    _, d_bytes, offset = _der_read(rsa_seq, offset)     # private exponent

    return _der_int(n_bytes), _der_int(d_bytes)


# --------------------------------------------------------------------------
# RSASSA-PKCS1-v1_5 signing
# --------------------------------------------------------------------------
def _pkcs1_v15_pad(message: bytes, key_length: int) -> bytes:
    """EMSA-PKCS1-v1_5 encoding: 0x00 || 0x01 || PS(0xFF...) || 0x00 || DigestInfo."""
    padding_length = key_length - len(message) - 3
    if padding_length < 8:
        raise JWTError("RSA key is too small for a SHA-256 signature")
    return b"\x00\x01" + b"\xff" * padding_length + b"\x00" + message


def rsa_sha256_sign(message: bytes, n: int, d: int) -> bytes:
    """Sign with RSA: signature = EM^d mod n."""
    key_length = (n.bit_length() + 7) // 8
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    encoded = _pkcs1_v15_pad(digest_info, key_length)
    signature = pow(int.from_bytes(encoded, "big"), d, n)
    return signature.to_bytes(key_length, "big")


def rsa_sha256_verify(message: bytes, signature: bytes, n: int, e: int = 65537) -> bool:
    """Verify a signature. Used by our own self-test so the maths is proven."""
    key_length = (n.bit_length() + 7) // 8
    if len(signature) != key_length:
        return False
    recovered = pow(int.from_bytes(signature, "big"), e, n).to_bytes(key_length, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    return recovered == _pkcs1_v15_pad(digest_info, key_length)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_signed_jwt(
    *,
    client_email: str,
    private_key_pem: str,
    private_key_id: str,
    token_uri: str = "https://oauth2.googleapis.com/token",
    scope: str = "https://www.googleapis.com/auth/datastore",
    lifetime: int = 3600,
) -> str:
    """Build a Google service-account JWT assertion."""
    n, d = parse_pkcs8_rsa_private_key(private_key_pem)

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": private_key_id}
    claims = {
        "iss": client_email,
        "scope": scope,
        "aud": token_uri,
        "iat": now,
        "exp": now + lifetime,
    }

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")

    signature = rsa_sha256_sign(signing_input, n, d)
    return signing_input.decode("ascii") + "." + _b64url(signature)


def exchange_jwt_for_token(
    jwt: str, token_uri: str = "https://oauth2.googleapis.com/token", timeout: int = 30
) -> dict[str, Any]:
    """Trade the JWT assertion for an OAuth 2.0 access token."""
    payload = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        token_uri,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.read().decode("utf-8", errors="replace")
        raise JWTError(f"Google token endpoint rejected the assertion: {detail}") from exc
    except Exception as exc:  # pragma: no cover - network dependent
        raise JWTError(f"Could not reach the Google token endpoint: {exc}") from exc


def service_account_access_token(credentials: dict[str, Any]) -> tuple[str, float]:
    """Return ``(access_token, expiry_epoch)`` for a service-account dict."""
    jwt = build_signed_jwt(
        client_email=credentials["client_email"],
        private_key_pem=credentials["private_key"],
        private_key_id=credentials.get("private_key_id", ""),
        token_uri=credentials.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    response = exchange_jwt_for_token(
        jwt, credentials.get("token_uri", "https://oauth2.googleapis.com/token")
    )
    if "access_token" not in response:
        raise JWTError(f"No access_token in the token response: {response}")
    expires_in = float(response.get("expires_in", 3600))
    return response["access_token"], time.time() + expires_in


