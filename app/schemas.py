"""Схемы данных: форма, ответ ИИ, итоговая карточка."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

NOT_SET = "не указано"


class LeadForm(BaseModel):
    name: str = ""
    phone: str = ""
    email: str = ""
    text: str = ""

    @field_validator("name", "phone", "email", "text")
    @classmethod
    def strip(cls, v: str) -> str:
        return (v or "").strip()

    @model_validator(mode="after")
    def need_contact_and_text(self):
        if not self.text:
            raise ValueError("Заполните текст заявки.")
        if not (self.phone or self.email):
            raise ValueError("Укажите хотя бы один контакт: телефон или email.")
        if len(self.text) > 4000:
            raise ValueError("Текст заявки слишком длинный (больше 4000 символов).")
        return self


class AIResult(BaseModel):
    """То, что возвращает нейросеть. Цен и статуса даты здесь нет — их считает код."""

    theme: str = NOT_SET
    summary: str
    lead_type: Literal["целевой", "нецелевой", "неясно"]
    is_complaint: bool = False
    is_nonstandard: bool = False
    shoot_date: str | None = None
    # дата частями: год и совпадение с днём недели проверяет код (pipeline.resolve_shoot_date)
    shoot_day: int | None = None
    shoot_month: int | None = None
    shoot_year: int | None = None
    shoot_weekday: str | None = None
    location: str = NOT_SET
    out_of_town: bool = False
    urgent: bool = False
    duration_hours: float | None = None
    people: str = NOT_SET
    budget: int | None = None
    key_data: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    next_step: str = ""
    reply_draft: str = ""

    @field_validator("shoot_date", mode="before")
    @classmethod
    def empty_date(cls, v):
        """Дату принимаем только как реальную ГГГГ-ММ-ДД; «15.05.2027» приводим, мусор — в None."""
        if v in (None, "", NOT_SET, "null"):
            return None
        s = str(v).strip()
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
        if m:
            s = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return None

    @field_validator("shoot_day", "shoot_month", "shoot_year", mode="before")
    @classmethod
    def date_part(cls, v):
        if v in (None, "", NOT_SET, "null"):
            return None
        try:
            return int(str(v).strip())
        except ValueError:
            return None

    @field_validator("shoot_weekday", mode="before")
    @classmethod
    def empty_weekday(cls, v):
        return None if v in (None, "", NOT_SET, "null") else str(v)

    @field_validator("duration_hours", "budget", mode="before")
    @classmethod
    def empty_number(cls, v):
        if v in (None, "", NOT_SET, "null"):
            return None
        if isinstance(v, str):
            v = v.replace(" ", "").replace(",", ".")
        return v

    @field_validator("key_data", "missing_data", mode="before")
    @classmethod
    def as_list(cls, v):
        if v in (None, "", NOT_SET):
            return []
        if isinstance(v, str):
            return [v]
        return v


class CalcParams(BaseModel):
    """Параметры расчёта. Одинаковые для калькулятора клиента и пересчёта у фотографа."""

    theme: str
    hours: float | None = Field(default=None, ge=0.5, le=14)
    date: str | None = None
    out_of_town: bool = False
    urgent: bool = False
    extra_frames: int = Field(default=0, ge=0, le=500)
    second_photographer: bool = False

    @field_validator("date", mode="before")
    @classmethod
    def empty_date(cls, v):
        return v or None


class ManagerParams(CalcParams):
    """Пересчёт у фотографа: плюс персональная скидка. Клиенту недоступно."""

    theme: str | None = None
    personal_discount: float = Field(default=0, ge=0, le=30)
    personal_reason: str = Field(default="", max_length=120)


class CalcRequest(BaseModel):
    """Заявка из калькулятора: контакты + параметры + комментарий."""

    name: str = ""
    phone: str = ""
    email: str = ""
    comment: str = Field(default="", max_length=2000)
    consent: bool = False
    portfolio_consent: bool = False
    params: CalcParams

    @field_validator("name", "phone", "email", "comment")
    @classmethod
    def strip(cls, v: str) -> str:
        return (v or "").strip()

    @model_validator(mode="after")
    def check(self):
        if not (self.phone or self.email):
            raise ValueError("Укажите хотя бы один контакт: телефон или email.")
        if not self.consent:
            raise ValueError("Нужно согласие на обработку персональных данных.")
        return self


class Card(BaseModel):
    source: str = "текст"
    theme: str
    summary: str
    lead_type: str
    shoot_date: str
    location: str
    duration_hours: str
    people: str
    budget: str
    key_data: list[str]
    missing_data: list[str]
    date_status: str
    price_estimate: str
    next_step: str
    reply_draft: str
    needs_human: bool
    human_reason: str
    reply_body: str = ""
    amount: int | None = None
    params: dict = Field(default_factory=dict)


# Русские подписи полей — для интерфейса и комментария в CRM
LABELS = {
    "theme": "Тематика",
    "summary": "Суть запроса",
    "lead_type": "Тип заявки",
    "shoot_date": "Дата съёмки",
    "date_status": "Дата по календарю",
    "location": "Место",
    "duration_hours": "Длительность",
    "people": "Кто в кадре",
    "budget": "Бюджет",
    "price_estimate": "Ориентир стоимости",
    "key_data": "Известные детали",
    "missing_data": "Чего не хватает",
    "next_step": "Следующий шаг",
    "needs_human": "Нужен фотограф лично",
    "human_reason": "Причина",
    "reply_draft": "Черновик ответа",
}
