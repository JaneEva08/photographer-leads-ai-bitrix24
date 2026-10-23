"""Бизнес-правила фотографа: цена по прайсу, скидки, статус даты, передача человеку.

Всё здесь детерминировано — нейросеть сюда не заходит. ИИ только извлекает
параметры из текста, а дату и цену проверяет и считает этот модуль.
Калькулятор клиента, разбор текста и пересчёт у фотографа используют один
и тот же расчёт `quote()`, поэтому цена везде одинаковая.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Статусы даты
DATE_FREE = "свободна"
DATE_BUSY = "занята"
DATE_UNKNOWN = "не указана"
DATE_PAST = "в прошлом"


def load_json(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def rub(x: int) -> str:
    sign = "−" if x < 0 else ""
    return f"{sign}{abs(x):,} ₽".replace(",", " ")


def _round100(x: float) -> int:
    return int(math.ceil(x / 100.0 - 1e-9) * 100)


@dataclass
class Prices:
    themes: dict
    extras: dict
    rules: dict
    discounts: dict = field(default_factory=dict)

    @classmethod
    def load(cls, data: dict | None = None) -> "Prices":
        data = data or load_json("prices.json")
        return cls(themes=data["themes"], extras=data["extras"], rules=data["rules"],
                   discounts=data.get("discounts", {}))

    def titles(self) -> list[str]:
        return [t["title"] for t in self.themes.values()]

    def key_by_title(self, title: str | None) -> str | None:
        if not title:
            return None
        norm = title.strip().lower()
        for key, t in self.themes.items():
            if t["title"].lower() == norm or key == norm:
                return key
        return None


@dataclass
class Calendar:
    full_days: set[str] = field(default_factory=set)
    sessions: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, data: dict | None = None) -> "Calendar":
        data = data or load_json("busy_dates.json")
        return cls(full_days=set(data.get("full_days", [])), sessions=dict(data.get("sessions", {})))


# ---- дата -----------------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def date_status(calendar: Calendar, prices: Prices, theme_key: str | None,
                shoot_date: str | None, today: date) -> str:
    """Свободна ли дата для этой тематики.

    Свадьба и мероприятие — только в полностью свободный день.
    Фотосессии — не больше max_sessions_per_day и не в закрытый день.
    """
    d = _parse_date(shoot_date)
    if d is None:
        return DATE_UNKNOWN
    if d < today:
        return DATE_PAST
    iso = d.isoformat()
    if iso in calendar.full_days:
        return DATE_BUSY
    booked = calendar.sessions.get(iso, 0)
    full_day_theme = theme_key is not None and prices.themes[theme_key]["full_day"]
    if full_day_theme:
        return DATE_BUSY if booked > 0 else DATE_FREE
    return DATE_BUSY if booked >= prices.rules["max_sessions_per_day"] else DATE_FREE


def nearest_free(calendar: Calendar, prices: Prices, theme_key: str | None,
                 shoot_date: str | None, today: date, count: int = 3, horizon: int = 90) -> list[str]:
    """Ближайшие свободные даты после занятой. Для свадьбы и мероприятия — только пт–вс."""
    d = _parse_date(shoot_date)
    if d is None:
        return []
    full_day = theme_key is not None and prices.themes[theme_key]["full_day"]
    out: list[str] = []
    for i in range(1, horizon + 1):
        cand = d + timedelta(days=i)
        if cand < today or (full_day and cand.weekday() < 4):
            continue
        if date_status(calendar, prices, theme_key, cand.isoformat(), today) == DATE_FREE:
            out.append(cand.isoformat())
            if len(out) == count:
                break
    return out


# ---- расчёт ---------------------------------------------------------------------

@dataclass
class Quote:
    theme_key: str | None
    title: str
    hours: float | None
    lines: list[tuple[str, int]]
    discounts: list[tuple[str, int]]
    total: int | None
    is_from: bool
    retouch: int
    date_status: str
    nearest_free: list[str]
    personal_reason: str = ""

    def text(self) -> str:
        if self.total is None:
            return "нужно уточнить тематику"
        parts = [f"{label} — {rub(v)}" for label, v in self.lines + self.discounts]
        prefix = "от " if self.is_from else ""
        s = f"{prefix}{rub(self.total)} ({'; '.join(parts)}; в ретуши {self.retouch} кадров)"
        if self.is_from:
            s += ". Длительность не указана — посчитан базовый пакет"
        return s

    def as_dict(self) -> dict:
        return {
            "theme": self.theme_key, "title": self.title, "hours": self.hours,
            "lines": [{"label": l, "amount": v} for l, v in self.lines],
            "discounts": [{"label": l, "amount": v} for l, v in self.discounts],
            "total": self.total, "is_from": self.is_from, "retouch": self.retouch,
            "date_status": self.date_status, "nearest_free": self.nearest_free,
            "text": self.text(),
        }


def quote(
    prices: Prices,
    theme_key: str | None,
    hours: float | None = None,
    *,
    shoot_date: str | None = None,
    today: date | None = None,
    calendar: Calendar | None = None,
    out_of_town: bool = False,
    urgent: bool = False,
    extra_frames: int = 0,
    second_photographer: bool = False,
    personal_discount: float = 0,
    personal_reason: str = "",
) -> Quote:
    """Полный расчёт. Даты и скидки по дате учитываются, только если передан today."""
    status = DATE_UNKNOWN
    free: list[str] = []
    if today is not None and calendar is not None:
        status = date_status(calendar, prices, theme_key, shoot_date, today)
        if status == DATE_BUSY:
            free = nearest_free(calendar, prices, theme_key, shoot_date, today)

    if theme_key is None:
        return Quote(None, "не указано", hours, [], [], None, False, 0, status, free)

    t = prices.themes[theme_key]
    ex = prices.extras
    is_from = hours is None
    h = max(float(hours or 0), t["base_hours"])
    h = min(h, prices.rules.get("max_hours", 14))

    lines: list[tuple[str, int]] = [(f"{t['title']}: {t['base_hours']:g} ч", t["base_price"])]
    if h > t["base_hours"]:
        extra_h = h - t["base_hours"]
        lines.append((f"доп. {extra_h:g} ч", _round100(extra_h * t["extra_hour"])))
    if out_of_town:
        lines.append(("выезд за город", ex["out_of_town"]))
    if second_photographer and t["full_day"]:
        lines.append((f"второй фотограф, {h:g} ч", _round100(h * ex["second_photographer_hour"])))
    frames = max(0, int(extra_frames or 0))
    if frames:
        lines.append((f"доп. {frames} кадров в ретушь", frames * ex["extra_retouch_frame"]))
    subtotal = sum(v for _, v in lines)
    if urgent:
        lines.append((f"срочная обработка +{ex['urgent_percent']}%", _round100(subtotal * ex["urgent_percent"] / 100)))
    gross = sum(v for _, v in lines)

    # автоматические скидки — только с известной датой
    disc = prices.discounts
    auto: list[tuple[str, float]] = []
    d = _parse_date(shoot_date)
    if d is not None and today is not None and d >= today:
        if not t["full_day"] and d.weekday() < 5 and disc.get("weekday_percent"):
            auto.append(("будний день", disc["weekday_percent"]))
        if theme_key == "wedding" and (d - today).days > disc.get("early_wedding_days", 10**6):
            auto.append(("ранняя бронь свадьбы", disc["early_wedding_percent"]))
    discounts: list[tuple[str, int]] = []
    total = gross
    if auto:
        pct = min(sum(p for _, p in auto), disc.get("max_auto_percent", 100))
        names = " + ".join(n for n, _ in auto)
        capped = " (потолок)" if pct < sum(p for _, p in auto) else ""
        amount = _round100(gross * pct / 100)
        discounts.append((f"скидка: {names} −{pct:g}%{capped}", -amount))
        total -= amount
    personal = max(0.0, min(float(personal_discount or 0), disc.get("max_personal_percent", 30)))
    if personal:
        amount = _round100(total * personal / 100)
        reason = f" ({personal_reason.strip()})" if personal_reason.strip() else ""
        discounts.append((f"персональная скидка −{personal:g}%{reason}", -amount))
        total -= amount

    return Quote(theme_key, t["title"], None if is_from else h, lines, discounts, total, is_from,
                 t["retouch"] + frames, status, free, personal_reason)


@dataclass
class Estimate:
    amount: int | None
    is_from: bool
    text: str


def estimate_price(prices: Prices, theme_key: str | None, duration_hours: float | None,
                   out_of_town: bool = False, urgent: bool = False, **kw) -> Estimate:
    """Короткая форма quote() для карточки."""
    q = quote(prices, theme_key, duration_hours, out_of_town=out_of_town, urgent=urgent, **kw)
    return Estimate(q.total, q.is_from, q.text())


# ---- передача человеку ------------------------------------------------------------

def human_decision(
    prices: Prices,
    theme_key: str | None,
    lead_type: str,
    is_complaint: bool,
    is_nonstandard: bool,
    status: str,
    budget: int | None,
    estimate: Estimate,
) -> tuple[bool, str]:
    """Окончательное решение «нужен ли человек». Возвращает (да/нет, причина)."""
    reasons: list[str] = []
    if is_complaint:
        reasons.append("жалоба или конфликт")
    if (theme_key is not None and prices.themes[theme_key]["full_day"] and lead_type != "нецелевой"
            and not is_complaint):
        reasons.append(f"{prices.themes[theme_key]['title'].lower()}: крупный заказ, ведёт фотограф")
    if is_nonstandard:
        reasons.append("нестандартный запрос")
    if status == DATE_BUSY and lead_type != "нецелевой":
        reasons.append("дата занята — предложить другую")
    base = prices.themes[theme_key]["base_price"] if theme_key is not None else None
    if budget is not None and base is not None and budget < base * prices.rules["low_budget_ratio"]:
        reasons.append("бюджет заметно ниже стоимости пакета")
    if not reasons:
        return False, "не требуется"
    return True, "; ".join(reasons)
