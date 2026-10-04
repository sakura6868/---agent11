"""Fail closed on unsafe cloud configuration without exposing secret values."""
from __future__ import annotations
import os


def is_production() -> bool:
    return os.getenv("APP_ENV", "").lower() == "production" or os.getenv("RENDER", "").lower() == "true"


def demo_login_enabled() -> bool:
    default = "0" if is_production() else "1"
    return os.getenv("ENABLE_DEMO_LOGIN", default).lower() in {"1", "true", "yes"}


def validate_production_configuration() -> None:
    if not is_production():
        return
    errors = []
    for name in ("AUTH_TOKEN_SECRET", "ADMIN_API_TOKEN"):
        value = os.getenv(name, "").strip()
        if len(value) < 32 or len(set(value)) < 8 or value.lower().startswith(("replace-with-", "change-me", "example")):
            errors.append(name + " must contain an independently generated secret of at least 32 characters")
    auth, admin = os.getenv("AUTH_TOKEN_SECRET", "").strip(), os.getenv("ADMIN_API_TOKEN", "").strip()
    if auth and admin and auth == admin:
        errors.append("user and administrator secrets must be different")
    if os.getenv("DEV_ADMIN_QUICK_LOGIN", "").lower() in {"1", "true", "yes"}:
        errors.append("DEV_ADMIN_QUICK_LOGIN must be disabled in production")
    if "*" in [v.strip() for v in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")]:
        errors.append("production CORS requires explicit origins")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
