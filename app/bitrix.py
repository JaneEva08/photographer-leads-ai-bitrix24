"""Клиент Битрикс24 через входящий вебхук (права crm, task, im).

Документация REST API: https://apidocs.bitrix24.ru/
Базовая ссылка вебхука вида https://<портал>.bitrix24.ru/rest/1/<ключ>/ — в .env как B24_URL.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))


class BitrixError(Exception):
    pass


class Bitrix24:
    def __init__(
        self,
        base_url: str,
        notify_user_id: int | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not base_url:
            raise BitrixError("Не задан B24_URL в .env.")
        self.base = base_url.rstrip("/") + "/"
        self.notify_user_id = notify_user_id
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self._user_id: int | None = None

    def call(self, method: str, payload: dict | None = None) -> dict:
        try:
            r = self.http.post(f"{self.base}{method}.json", json=payload or {})
        except httpx.HTTPError as e:
            raise BitrixError(f"Битрикс24 недоступен: {e.__class__.__name__}") from e
        try:
            data = r.json()
        except ValueError:
            raise BitrixError(f"{method}: ответ не JSON (HTTP {r.status_code})")
        if r.status_code >= 400 or "error" in data:
            # ключ вебхука в лог не пишем — только метод и текст ошибки
            msg = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
            raise BitrixError(f"{method}: {msg}")
        return data.get("result")

    def current_user_id(self) -> int:
        """Проверка вебхука: user.current. Ответственный по умолчанию — владелец вебхука."""
        if self._user_id is None:
            try:
                self._user_id = int(self.call("user.current")["ID"])
            except BitrixError as e:
                # user.current требует права user_basic; profile отвечает на правах crm/task/im
                log.warning("user.current недоступен (%s), пробую profile.", e)
                self._user_id = int(self.call("profile")["ID"])
        return self._user_id

    def create_lead(self, title: str, name: str, phone: str, email: str, comments: str, amount: int | None) -> int:
        first, _, last = (name or "").strip().partition(" ")
        fields: dict = {
            "TITLE": title,
            "NAME": first or "Без имени",
            "SOURCE_ID": "WEB",
            "ASSIGNED_BY_ID": self.current_user_id(),
            "COMMENTS": comments,
        }
        if last:
            fields["LAST_NAME"] = last
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
        if email:
            fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
        if amount:
            fields["OPPORTUNITY"] = amount
            fields["CURRENCY_ID"] = "RUB"
        return int(self.call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}))

    def create_task(self, lead_id: int, title: str, description: str, important: bool, hours: int = 2) -> int:
        deadline = (datetime.now(MSK) + timedelta(hours=hours)).isoformat(timespec="seconds")
        fields = {
            "TITLE": title,
            "DESCRIPTION": description,
            "RESPONSIBLE_ID": self.current_user_id(),
            "DEADLINE": deadline,
            "PRIORITY": 2 if important else 1,  # 2 — высокий
            "UF_CRM_TASK": [f"L_{lead_id}"],  # привязка задачи к лиду
        }
        result = self.call("tasks.task.add", {"fields": fields})
        return int(result["task"]["id"])

    def notify(self, message: str) -> None:
        user_id = self.notify_user_id or self.current_user_id()
        self.call("im.notify.personal.add", {"USER_ID": user_id, "MESSAGE": message})

    def update_lead_amount(self, lead_id: int, amount: int) -> None:
        self.call("crm.lead.update", {"id": lead_id, "fields": {"OPPORTUNITY": amount, "CURRENCY_ID": "RUB"}})

    def add_lead_comment(self, lead_id: int, text: str) -> None:
        """Комментарий в ленте лида — история пересчётов видна фотографу в карточке CRM."""
        self.call("crm.timeline.comment.add",
                  {"fields": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead", "COMMENT": text}})

    def lead_url(self, lead_id: int) -> str:
        portal = self.base.split("/rest/")[0]
        return f"{portal}/crm/lead/details/{lead_id}/"
