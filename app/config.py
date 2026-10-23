"""Настройки из .env. Секреты не логируются и не уходят в браузер."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "да")


@dataclass(frozen=True)
class Settings:
    gigachat_credentials: str = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
    gigachat_scope: str = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
    gigachat_model: str = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
    gigachat_ca_bundle: str = os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
    b24_url: str = os.getenv("B24_URL", "").strip()
    b24_notify_user_id: str = os.getenv("B24_NOTIFY_USER_ID", "").strip()
    admin_password: str = os.getenv("ADMIN_PASSWORD", "").strip()
    app_today: str = os.getenv("APP_TODAY", "").strip()
    demo_mode: bool = _bool("DEMO_MODE", False)
    privacy_log: Path = ROOT / os.getenv("PRIVACY_LOG", "logs/privacy_log.csv")
    loki_url: str = os.getenv("LOKI_URL", "").strip()
    loki_app: str = os.getenv("LOKI_APP", "photo-leads-ai").strip() or "photo-leads-ai"
    loki_env: str = os.getenv("LOKI_ENV", "local").strip() or "local"


settings = Settings()
