import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple
import requests

from config.settings import settings

logger = logging.getLogger(__name__)

VALID_GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def b64url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data_str: str) -> bytes:
    """Decode URL-safe base64 with missing padding handled."""
    padded = data_str + "=" * ((4 - len(data_str) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def hash_password(password: str, iterations: int = 600_000) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with secure random salt (OWASP 2024+ standard)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2:sha256:{iterations}${salt.hex()}${dk.hex()}"


def verify_password(plain_password: str, stored_hash_or_plain: str) -> bool:
    """
    Verify plain password against PBKDF2 hash or plain string from config.
    Uses constant-time comparison to prevent timing side-channel attacks.
    """
    if not plain_password or not stored_hash_or_plain:
        return False

    if stored_hash_or_plain.startswith("pbkdf2:sha256:"):
        try:
            parts = stored_hash_or_plain.split("$")
            if len(parts) != 3:
                return False
            _, salt_hex, key_hex = parts
            iter_part = parts[0].split(":")[-1]
            iterations = int(iter_part)
            if iterations < 10_000 or iterations > 5_000_000:
                return False
            salt = bytes.fromhex(salt_hex)
            expected_key = bytes.fromhex(key_hex)
            actual_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual_key, expected_key)
        except Exception as e:
            logger.error(f"Error parsing password hash: {e}")
            return False

    # Plain text fallback (for initial .env password before hashing)
    return hmac.compare_digest(plain_password.encode("utf-8"), stored_hash_or_plain.encode("utf-8"))


def create_session_token(payload: Dict[str, Any], secret_key: str, expire_hours: int = 72) -> str:
    """
    Creates a cryptographically signed session token (standard HS256 JWT format)
    with a unique jti claim for server-side revocation support.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    token_payload = dict(payload)
    token_payload["iat"] = now
    token_payload["exp"] = now + (expire_hours * 3600)
    token_payload["jti"] = secrets.token_hex(16)

    header_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = b64url_encode(json.dumps(token_payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def verify_session_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """
    Verifies JWT header (strict HS256), signature in constant time, and required expiration.
    Returns payload dictionary if valid, None if invalid or expired.
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.strip().split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, sig_b64 = parts

    # 1. Verify HMAC-SHA256 signature first (constant time)
    try:
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    except UnicodeEncodeError:
        return None

    expected_sig = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    expected_sig_b64 = b64url_encode(expected_sig)

    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        logger.warning("Session token signature verification failed.")
        return None

    # 2. Strictly validate header algorithm & type (prevents alg confusion / none attacks)
    try:
        header = json.loads(b64url_decode(header_b64).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
            logger.warning("Session token rejected: invalid JWT header.")
            return None

        payload = json.loads(b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None

        now = int(time.time())
        exp = payload.get("exp")
        if exp is None or not isinstance(exp, (int, float)) or int(exp) < now:
            logger.info("Session token missing expiration or expired.")
            return None

        iat = payload.get("iat")
        if iat is not None and isinstance(iat, (int, float)) and int(iat) > now + 60:
            logger.warning("Session token rejected: issued in the future.")
            return None

        return payload
    except Exception as e:
        logger.warning(f"Failed to decode valid token payload: {e}")
        return None


def verify_google_id_token(id_token: str, allowed_email: str = "oubou68@gmail.com", client_id: str = "") -> Dict[str, Any]:
    """
    Validates a Google ID Token (from Google Sign-In / GIS).
    Enforces strict access control: ONLY allowed_email is permitted!

    Returns user dictionary if authenticated, or raises ValueError / PermissionError.
    """
    token_str = (id_token or "").strip()
    if not token_str:
        raise ValueError("Google ID Token darf nicht leer sein.")

    # Live verification via Google tokeninfo endpoint
    try:
        resp = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token_str},
            timeout=8.0
        )
    except requests.RequestException as e:
        logger.error(f"Google tokeninfo request failed: {e}")
        raise ValueError("Verbindung zum Google-Authentifizierungsserver fehlgeschlagen.")

    if resp.status_code != 200:
        logger.warning(f"Google token verification failed with status {resp.status_code}")
        raise ValueError("Ungültiger oder abgelaufener Google ID Token.")

    data = resp.json()

    # 1. Verify issuer if provided
    iss = data.get("iss")
    if iss and iss not in VALID_GOOGLE_ISSUERS:
        raise PermissionError("Ungültiger Google Token Aussteller (iss).")

    # 2. Verify expiration if provided
    exp = data.get("exp")
    if exp is not None:
        try:
            if int(exp) < int(time.time()):
                raise ValueError("Google ID Token ist abgelaufen.")
        except (ValueError, TypeError) as e:
            if "abgelaufen" in str(e):
                raise
            raise ValueError("Ungültiges Ablaufdatum im Google ID Token.")

    # 3. Verify audience if client_id configured
    if client_id:
        aud = data.get("aud")
        if aud != client_id:
            raise PermissionError("Google Token Audience stimmt nicht mit der Client-ID überein.")
    else:
        logger.warning("SECURITY: GOOGLE_CLIENT_ID is empty; audience verification is skipped.")

    # 4. Verify email verification flag
    email_verified = str(data.get("email_verified", "")).lower() in ("true", "1")
    if not email_verified:
        raise PermissionError("Die Google E-Mail-Adresse ist bei Google nicht verifiziert.")

    # 5. Enforce strict email restriction
    token_email = (data.get("email") or "").strip().lower()
    target_email = allowed_email.strip().lower()

    if token_email != target_email:
        logger.warning(f"Access denied for unauthorized Google account: {token_email}")
        raise PermissionError(
            f"Zugriff verweigert: Nur das autorisierte Google-Konto '{target_email}' hat Zugriff. "
            f"Angemeldet als '{token_email}'."
        )

    return {
        "sub": data.get("sub", ""),
        "email": token_email,
        "name": data.get("name", token_email.split("@")[0]),
        "picture": data.get("picture", ""),
        "auth_provider": "google",
        "role": "admin"
    }


class LoginRateLimiter:
    """Thread-safe sliding-window brute-force rate limiter per client IP."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> List[float]:
        cutoff = now - self.window_seconds
        recent = [ts for ts in self._attempts[key] if ts > cutoff]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent

    def check_allowed(self, key: str) -> Tuple[bool, int]:
        """Returns (is_allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            recent = self._prune(key, now)
            if len(recent) >= self.max_attempts:
                retry_after = max(1, int(self.window_seconds - (now - recent[0])))
                return False, retry_after
            return True, 0

    def record_failure(self, key: str):
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._attempts[key].append(now)

    def reset(self, key: str):
        with self._lock:
            self._attempts.pop(key, None)

    def clear_all(self):
        with self._lock:
            self._attempts.clear()


class AuthManager:
    """Central authentication, rate-limiting, and session coordinator for AURA QUANT AI."""

    def __init__(self):
        self.secret_key = settings.AUTH_SECRET_KEY
        self.expire_hours = settings.AUTH_SESSION_EXPIRE_HOURS
        self.username = settings.AUTH_USERNAME
        # Hash plaintext password in memory so raw credentials are not retained
        raw_pwd = settings.AUTH_PASSWORD
        if raw_pwd and not raw_pwd.startswith("pbkdf2:sha256:"):
            self._password_hash = hash_password(raw_pwd, iterations=100_000)
        else:
            self._password_hash = raw_pwd
        self.google_allowed_email = settings.GOOGLE_ALLOWED_EMAIL
        self.google_client_id = settings.GOOGLE_CLIENT_ID
        self.auth_enabled = settings.AUTH_ENABLED

        # Server-side token revocation registry: jti -> exp_timestamp
        self._revoked_jtis: Dict[str, int] = {}
        self._revocation_lock = threading.Lock()
        self.rate_limiter = LoginRateLimiter(
            max_attempts=settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS,
            window_seconds=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS
        )

    def authenticate_local(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Verify username and password against configured admin credentials."""
        if not self.auth_enabled:
            return {
                "username": username or "admin",
                "email": self.google_allowed_email,
                "role": "admin",
                "auth_provider": "local"
            }

        user_match = hmac.compare_digest(
            (username or "").strip().lower().encode("utf-8"),
            self.username.strip().lower().encode("utf-8")
        )
        # Always run PBKDF2 verification even on unknown username to prevent timing user-enumeration
        pwd_match = verify_password(password or "", self._password_hash)

        if not (user_match and pwd_match):
            logger.warning("Authentication failed: Invalid credentials.")
            return None

        return {
            "username": self.username,
            "email": self.google_allowed_email,
            "role": "admin",
            "auth_provider": "local"
        }

    def authenticate_google(self, id_token: str) -> Dict[str, Any]:
        """Verify Google ID token and check restriction to allowed email."""
        return verify_google_id_token(
            id_token=id_token,
            allowed_email=self.google_allowed_email,
            client_id=self.google_client_id
        )

    def create_token(self, user_info: Dict[str, Any]) -> str:
        """Issue signed session token for authenticated user."""
        payload = {
            "sub": user_info.get("username") or user_info.get("email"),
            "email": user_info.get("email"),
            "name": user_info.get("name") or user_info.get("username"),
            "role": user_info.get("role", "admin"),
            "auth_provider": user_info.get("auth_provider", "local")
        }
        return create_session_token(payload, self.secret_key, self.expire_hours)

    def revoke_token(self, token: Optional[str]):
        """Revokes a session token immediately on the server so it cannot be reused."""
        if not token:
            return
        payload = verify_session_token(token, self.secret_key)
        if not payload:
            return
        jti = payload.get("jti")
        exp = int(payload.get("exp", int(time.time()) + 3600))
        if jti:
            now = int(time.time())
            with self._revocation_lock:
                # Prune expired entries
                self._revoked_jtis = {k: v for k, v in self._revoked_jtis.items() if v >= now}
                self._revoked_jtis[jti] = exp

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Verify session token and ensure it has not been revoked."""
        payload = verify_session_token(token, self.secret_key)
        if not payload:
            return None
        jti = payload.get("jti")
        if jti:
            with self._revocation_lock:
                if jti in self._revoked_jtis:
                    logger.info("Rejected revoked session token.")
                    return None
        return payload


auth_manager = AuthManager()
