"""First-install credentials come only from controlled runtime configuration."""
from dotenv import dotenv_values

from app.config import _ENV_FILE, settings

_raw = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False) if _ENV_FILE.is_file() else {}

ADMIN_USERNAME = (
    "admin" if settings.local_admin_mode
    else (_raw.get("AUTH_USERNAME") or settings.auth_username or "admin").strip()
)
ADMIN_PASSWORD = _raw.get("AUTH_PASSWORD") or settings.auth_password or ""
REGISTRATION_SECRET = (
    _raw.get("AUTH_REGISTRATION_SECRET") or settings.auth_registration_secret or ""
).strip()
del _raw

SMTP_CONFIG = {
    "host": "",
    "port": 465,
    "security": "ssl",
    "username": "",
    "from_address": "",
}
