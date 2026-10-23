"""Провайдер GigaChat API.

Схема авторизации: Authorization key (Basic) → временный access token (~30 минут)
→ запросы к модели с Bearer-токеном. Для HTTPS нужен корневой сертификат
НУЦ Минцифры (https://www.gosuslugi.ru/crt), путь к нему — GIGACHAT_CA_BUNDLE_FILE.
"""
from __future__ import annotations

import logging
import ssl
import time
from pathlib import Path
import uuid

import httpx

from .base import AIError

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


class GigaChatProvider:
    name = "GigaChat"

    def __init__(
        self,
        credentials: str,
        scope: str = "GIGACHAT_API_PERS",
        model: str = "GigaChat",
        ca_bundle: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not credentials:
            raise AIError("Не задан GIGACHAT_CREDENTIALS в .env.")
        self.credentials = credentials
        self.scope = scope
        self.model = model
        if ca_bundle and not Path(ca_bundle).is_file():
            raise AIError(f"Файл сертификата не найден: {ca_bundle}. Проверьте GIGACHAT_CA_BUNDLE_FILE.")
        verify: str | bool = ca_bundle or True
        try:
            self.http = httpx.Client(verify=verify, timeout=timeout, transport=transport)
        except (OSError, ssl.SSLError) as e:
            raise AIError("Не удалось прочитать сертификат GigaChat (GIGACHAT_CA_BUNDLE_FILE).") from e
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        try:
            r = self.http.post(
                OAUTH_URL,
                headers={
                    "Authorization": f"Basic {self.credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self.scope},
            )
        except httpx.ConnectError as e:
            log.error("GigaChat OAuth: нет соединения: %s", e)
            raise AIError(
                "Сервис GigaChat недоступен. Проверьте интернет и сертификат НУЦ Минцифры "
                "(GIGACHAT_CA_BUNDLE_FILE)."
            ) from e
        except httpx.HTTPError as e:
            log.error("GigaChat OAuth: %s", e)
            raise AIError("Сервис GigaChat недоступен.") from e
        if r.status_code == 401:
            raise AIError("GigaChat не принял ключ авторизации. Проверьте GIGACHAT_CREDENTIALS и GIGACHAT_SCOPE.")
        if r.status_code >= 400:
            log.error("GigaChat OAuth %s: %s", r.status_code, r.text[:300])
            raise AIError(f"GigaChat вернул ошибку авторизации ({r.status_code}).")
        data = r.json()
        self._token = data["access_token"]
        # expires_at приходит в миллисекундах
        self._expires_at = data.get("expires_at", (time.time() + 1800) * 1000) / 1000
        return self._token

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        for attempt in (1, 2):
            token = self._get_token()
            try:
                r = self.http.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    json=payload,
                )
            except httpx.TimeoutException as e:
                raise AIError("GigaChat не ответил вовремя. Попробуйте ещё раз.") from e
            except httpx.HTTPError as e:
                log.error("GigaChat: %s", e)
                raise AIError("Сервис GigaChat недоступен.") from e
            if r.status_code == 401 and attempt == 1:
                self._token = None  # токен истёк — получаем новый и повторяем
                continue
            if r.status_code == 429:
                raise AIError("Слишком много запросов к GigaChat. Подождите минуту.")
            if r.status_code >= 400:
                log.error("GigaChat %s: %s", r.status_code, r.text[:300])
                raise AIError(f"GigaChat вернул ошибку ({r.status_code}).")
            return r.json()["choices"][0]["message"]["content"]
        raise AIError("Не удалось авторизоваться в GigaChat.")
