import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Dict, Any
import requests

from config.settings import settings

logger = logging.getLogger(__name__)


def b64url_encode(data: bytes) -> str:
    """Encode bytes to URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data_str: str) -> bytes:
    """Decode URL-safe base64 with missing padding handled."""
    padded = data_str + "=" * ((4 - len(data_str) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def hash_password(password: str, iterations: int = 100_000) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with secure random salt."""
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
            salt = bytes.fromhex(salt_hex)
            expected_key = bytes.fromhex(key_hex)
            actual_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual_key, expected_key)
        except Exception as e:
            logger.error(f"Error parsing password hash: {e}")
            return False

    # Plain text fallback (for initial .env default password)
    return hmac.compare_digest(plain_password.encode("utf-8"), stored_hash_or_plain.encode("utf-8"))


def create_session_token(payload: Dict[str, Any], secret_key: str, expire_hours: int = 72) -> str:
    """
    Creates a cryptographically signed session token (standard HS256 JWT format).
    Does not require external JWT dependencies.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    token_payload = dict(payload)
    token_payload["iat"] = now
    token_payload["exp"] = now + (expire_hours * 3600)

    header_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = b64url_encode(json.dumps(token_payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def verify_session_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """
    Verifies token signature and expiration in constant time.
    Returns payload dictionary if valid, None if invalid or expired.
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.strip().split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    expected_sig_b64 = b64url_encode(expected_sig)

    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        logger.warning("Session token signature verification failed.")
        return None

    try:
        payload_json = b64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)

        exp = payload.get("exp")
        if exp is not None and int(exp) < int(time.time()):
            logger.info("Session token has expired.")
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
        logger.warning(f"Google token verification failed with status {resp.status_code}: {resp.text}")
        raise ValueError("Ungültiger oder abgelaufener Google ID Token.")

    data = resp.json()

    # 1. Verify audience if client_id configured
    if client_id:
        aud = data.get("aud")
        if aud != client_id:
            raise PermissionError("Google Token Audience stimmt nicht mit der Client-ID überein.")

    # 2. Verify email verification flag
    email_verified = str(data.get("email_verified", "")).lower() in ("true", "1")
    if not email_verified:
        raise PermissionError("Die Google E-Mail-Adresse ist bei Google nicht verifiziert.")

    # 3. Enforce strict email restriction
    token_email = (data.get("email") or "").strip().lower()
    target_email = allowed_email.strip().lower()

    if token_email != target_email:
        logger.warning(f"Access denied for unauthorized Google account: {token_email} (Allowed: {target_email})")
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


class AuthManager:
    """Central authentication and session coordinator for AURA QUANT AI."""

    def __init__(self):
        self.secret_key = settings.AUTH_SECRET_KEY
        self.expire_hours = settings.AUTH_SESSION_EXPIRE_HOURS
        self.username = settings.AUTH_USERNAME
        self.password = settings.AUTH_PASSWORD
        self.google_allowed_email = settings.GOOGLE_ALLOWED_EMAIL
        self.google_client_id = settings.GOOGLE_CLIENT_ID
        self.auth_enabled = settings.AUTH_ENABLED

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
        if not user_match:
            logger.warning(f"Authentication failed: Unknown username '{username}'.")
            return None

        if not verify_password(password, self.password):
            logger.warning("Authentication failed: Invalid password.")
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

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Verify session token."""
        return verify_session_token(token, self.secret_key)


auth_manager = AuthManager()
