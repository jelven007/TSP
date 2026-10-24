"""SMTP email notification adapter.

Transport failures are isolated from alert persistence and SSE delivery. SMTP credentials
are supplied by the caller from ``secrets_store`` and never logged here.
"""
from __future__ import annotations

import logging
import os
import smtplib
import time
from contextlib import suppress
from email.message import EmailMessage
from email.utils import parseaddr

from app.bootstrap_defaults import SMTP_CONFIG

logger = logging.getLogger(__name__)

SECURITY_MODES = {"ssl", "starttls", "none"}
_MAX_ATTEMPTS = 2


def is_valid_email(address: str) -> bool:
    """Small dependency-free mailbox validation suitable for configuration checks."""
    parsed = parseaddr((address or "").strip())[1]
    if parsed != (address or "").strip() or parsed.count("@") != 1:
        return False
    local, domain = parsed.rsplit("@", 1)
    return bool(local and domain and "." in domain and " " not in parsed)


def is_configured(config: dict) -> bool:
    """Return whether the non-secret fields are sufficient to attempt delivery."""
    sender = str(config.get("from_address") or config.get("username") or "").strip()
    recipients = config.get("to_addresses") or []
    return bool(config.get("host") and sender and recipients)


def get_registration_smtp(recipient: str) -> tuple[dict, str]:
    """Resolve the registration transport without exposing its password to clients."""
    from app import secrets_store
    from app.config import settings
    from app.services import preferences

    if settings.auth_smtp_host.strip():
        config = {
            "host": settings.auth_smtp_host.strip(),
            "port": settings.auth_smtp_port,
            "security": settings.auth_smtp_security.strip().lower(),
            "username": settings.auth_smtp_username.strip(),
            "from_address": (
                settings.auth_smtp_from_address.strip()
                or settings.auth_smtp_username.strip()
            ),
            "to_addresses": [recipient] if recipient else [],
        }
        return config, settings.auth_smtp_password
    recipients = [recipient] if recipient else []
    config = preferences.get_email_smtp_config()
    if config["host"]:
        return {**config, "to_addresses": recipients}, secrets_store.get_email_smtp_password()
    # Full deployments may already configure their transport through TSP_CELL_*.
    cell_host = os.environ.get("TSP_CELL_SMTP_HOST", "").strip()
    if settings.multitenancy_enabled and cell_host:
        username = os.environ.get("TSP_CELL_SMTP_USER", "").strip()
        security = os.environ.get("TSP_CELL_SMTP_SECURITY", "tls").strip()
        return {
            "host": cell_host,
            "port": int(os.environ.get("TSP_CELL_SMTP_PORT", "465")),
            "security": "ssl" if security == "tls" else security,
            "username": username,
            "from_address": os.environ.get("TSP_CELL_SMTP_FROM", "").strip() or username,
            "to_addresses": recipients,
        }, os.environ.get("TSP_CELL_SMTP_PASSWORD", "")
    return {**SMTP_CONFIG, "to_addresses": recipients}, ""


def get_notification_smtp(recipient: str | None = None) -> tuple[dict, str]:
    """Use the system registration transport with editable notification recipients.

    Historical SMTP settings remain the registration fallback when AUTH_SMTP is unset.
    Notifications can override recipients but never the registration transport.
    """
    from app.services import auth, preferences

    config, password = get_registration_smtp("")
    config = {**config, "to_addresses": preferences.get_email_smtp_config()["to_addresses"]}
    if config["host"] and not config["to_addresses"]:
        config = {
            **config,
            "to_addresses": (
                auth.registered_emails()
                if recipient is None
                else [recipient] if is_valid_email(recipient) else []
            ),
        }
    return config, password


def _authenticate(smtp: smtplib.SMTP, username: str, password: str) -> None:
    """Log in with AUTH LOGIN first.

    NetEase mailboxes (126/163) advertise AUTH PLAIN but reject it with 535
    for client authorization codes, so smtplib's default preference fails
    even when the code is valid.
    """
    smtp.ehlo_or_helo_if_needed()
    if "LOGIN" in (smtp.esmtp_features.get("auth") or "").split():
        smtp.user, smtp.password = username, password
        code, resp = smtp.auth("LOGIN", smtp.auth_login, initial_response_ok=False)
        if 200 <= code < 300:
            return
        raise smtplib.SMTPAuthenticationError(code, resp)
    smtp.login(username, password)


def send_email(
    config: dict,
    password: str,
    subject: str,
    body: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bool:
    """Send one UTF-8 plain-text email through SSL, STARTTLS, or plain SMTP."""
    if not is_configured(config):
        return False

    host = str(config.get("host") or "").strip()
    try:
        port = int(config.get("port", 465))
    except (TypeError, ValueError):
        return False
    security = str(config.get("security") or "ssl")
    username = str(config.get("username") or "").strip()
    sender = str(config.get("from_address") or username).strip()
    recipients = [str(item).strip() for item in config.get("to_addresses", [])]
    if (
        not 1 <= port <= 65535
        or security not in SECURITY_MODES
        or not is_valid_email(sender)
        or not recipients
        or any(not is_valid_email(item) for item in recipients)
    ):
        return False

    message = EmailMessage()
    message["Subject"] = str(subject or "TickFlow 通知")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(str(body or ""))

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        smtp = None
        try:
            if security == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                smtp = smtplib.SMTP(host, port, timeout=10)
                if security == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
            if username:
                _authenticate(smtp, username, password)
            smtp.send_message(message)
            # Delivery already succeeded; a failed QUIT must not retry and duplicate the email.
            try:
                smtp.quit()
            except Exception:
                with suppress(Exception):
                    smtp.close()
            return True
        except Exception as exc:  # SMTP/network errors must not escape
            last_err = str(exc)
            if smtp is not None:
                with suppress(Exception):
                    smtp.close()
        if attempt < max_attempts:
            time.sleep(1)

    logger.warning("邮件推送最终失败(已尝试 %d 次): %s", max_attempts, last_err)
    return False
