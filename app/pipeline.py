"""Обработка заявки. Два входа — свободный текст и калькулятор — сходятся в одну карточку.

Текст:       форма → маскировка → ИИ → правила → подстановка → CRM
Калькулятор: параметры → правила → (ИИ только для комментария) → подстановка → CRM
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from . import privacy, rules
from .ai import AIError, AIProvider, extract_json
from .ai.prompts import calc_user_prompt, system_prompt, user_prompt
from .bitrix import Bitrix24, BitrixError
from .loki import log_event
from .schemas import LABELS, NOT_SET, AIResult, CalcRequest, Card, LeadForm

log = logging.getLogger(__name__)


@dataclass
class Result:
    card: Card
    masked_text: str
    crm: dict


def _analyze(ai: AIProvider, system: str, user: str) -> AIResult:
    """Ответ модели проверяется по схеме. Невалидный — одна повторная попытка."""
    last_err: Exception | None = None
    prompt = user
    for _ in range(2):
        raw = ai.complete(system, prompt)
        try:
            return AIResult.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as e:
            log_event(log, "ai_bad_json", f"Невалидный ответ модели: {str(e)[:200]}", logging.WARNING)
            last_err = e
            prompt = (f"{user}\n\nПрошлый ответ не прошёл проверку: {str(e)[:300]}\n"
                      "Верни только корректный JSON строго по описанным полям.")
    raise AIError("Нейросеть вернула ответ в неверном формате. Попробуйте ещё раз.") from last_err


_PRICE_RE = re.compile(r"\d[\d\s.,]*\s*(?:₽|руб|р\.|р\b|тыс|т\.р|k\b)|(?:₽|руб)", re.IGNORECASE)
_DATE_CLAIM_RE = re.compile(r"(?:свобод|занят)", re.IGNORECASE)
_PROMISE_RE = re.compile(r"(?:забронир|закреп|подтвержд|подтверж[уи]|зарезервир|гарантир|верн[уё]м|возврат)", re.IGNORECASE)
# подтверждать и бронировать дату может только фотограф; эти предложения вырезаются всегда
_CONFIRM_RE = re.compile(r"(?:подтвержд|подтверж[уи]|подтвердим|подтвердил|забронир|зарезервир)", re.IGNORECASE)
# заглушки вида [ВАШЕ ИМЯ] (настоящие метки содержат цифру: [ИМЯ_1]) и подпись «С уважением»
_PLACEHOLDER_RE = re.compile(r"\[[А-ЯЁA-Za-z ]+\]|^\s*С уважением", re.IGNORECASE)
_FALLBACK_BODY = "Здравствуйте! Спасибо за заявку."


def clean_draft(body: str, *, neutral: bool = False) -> str:
    """Выкидывает из ответа ИИ предложения с ценами и статусом даты: их пишут только правила.
    При передаче человеку (neutral) убирает ещё и обещания брони и возврата."""
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    kept = []
    for s in sentences:
        if (_PRICE_RE.search(s) or _DATE_CLAIM_RE.search(s) or _CONFIRM_RE.search(s)
                or _PLACEHOLDER_RE.search(s) or (neutral and _PROMISE_RE.search(s))):
            log.info("Из черновика убрано предложение с ценой/датой/обещанием.")
            continue
        kept.append(s)
    return " ".join(kept).strip() or _FALLBACK_BODY


_WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")


def _weekday_index(word: str | None) -> int | None:
    w = (word or "").strip().lower()
    short = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
    if w in short:
        return short[w]
    for i, stem in enumerate(_WEEKDAYS):
        if w.startswith(stem):
            return i
    return None


def resolve_shoot_date(ai_res: AIResult, today: date, text: str | None = None) -> tuple[str | None, str | None]:
    """Дата съёмки из частей, которые назвала модель. Год подбирает код: без года берётся ближайшая будущая
    дата, а если клиент назвал день недели, то та, где он совпадает с числом. Возвращает (ISO-дата, замечание)."""
    d, m = ai_res.shoot_day, ai_res.shoot_month
    if not (d and m):
        return ai_res.shoot_date, None  # модель вернула готовую ISO-дату (или даты нет)
    wd = _weekday_index(ai_res.shoot_weekday)
    year = ai_res.shoot_year
    if year and text is not None and str(year) not in text:
        year = None  # модель сама придумала год — клиент его не называл
    years = [year] if year else [today.year, today.year + 1]
    for y in years:
        try:
            cand = date(y, m, d)
        except ValueError:
            continue
        if not year and cand < today:
            continue
        if wd is not None and cand.weekday() != wd:
            continue
        return cand.isoformat(), None
    if wd is not None:
        return None, "дата: число и день недели в заявке не совпадают — уточнить у клиента"
    return None, "дата указана неверно — уточнить у клиента"


_NEXT_STEP_FALLBACK = "связаться с клиентом и уточнить детали"


def template_body(ai_res: AIResult, name_token: str | None) -> str | None:
    """Ответ, который не пишет нейросеть: на жалобу и на заявку не про фотосъёмку нужен точный тон,
    а не творчество модели. Для остальных заявок возвращает None."""
    hello = f"Здравствуйте, {name_token}." if name_token else "Здравствуйте."
    if ai_res.is_complaint:
        return (f"{hello} Спасибо, что написали, и извините за неудобства. "
                "Разберусь в ситуации лично и вернусь к вам с ответом.")
    if ai_res.lead_type == "нецелевой":
        return (f"{hello} Спасибо за обращение! К сожалению, я занимаюсь только фотосъёмкой "
                "и такие услуги не оказываю. Желаю удачи в поиске!")
    return None


def fmt_date(iso: str | None) -> str:
    if not iso:
        return NOT_SET
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def reply_block(q: rules.Quote, shoot_date: str | None) -> str:
    """Текст про дату и цену, который в черновик добавляют правила. ИИ эти цифры не пишет."""
    lines = []
    date_txt = fmt_date(shoot_date)
    if q.date_status == rules.DATE_FREE:
        lines.append(f"Дата {date_txt} у меня пока свободна.")
    elif q.date_status == rules.DATE_BUSY:
        alt = ", ".join(fmt_date(d) for d in q.nearest_free)
        tail = f" Ближайшие свободные: {alt}." if alt else " Могу предложить другие даты."
        lines.append(f"К сожалению, {date_txt} у меня уже занято.{tail}")
    if q.total:
        prefix = "от " if q.is_from else ""
        disc = ""
        if q.discounts:
            names = [label.split(" −")[0].removeprefix("скидка: ") for label, _ in q.discounts]
            disc = " с учётом скидки (" + ", ".join(names) + ")"
        lines.append(f"Предварительная стоимость — {prefix}{rules.rub(q.total)}{disc}.")
    return " ".join(lines)


def _params_dict(theme_key, hours, shoot_date, out_of_town, urgent, extra_frames=0, second=False) -> dict:
    return {"theme": theme_key, "hours": hours, "date": shoot_date, "out_of_town": out_of_town,
            "urgent": urgent, "extra_frames": extra_frames, "second_photographer": second}


def build_card(ai_res: AIResult, prices: rules.Prices, calendar: rules.Calendar, today: date,
               name_token: str | None = None, source_text: str | None = None) -> Card:
    theme_key = prices.key_by_title(ai_res.theme)
    iso, date_note = resolve_shoot_date(ai_res, today, source_text)
    if ai_res.is_complaint:
        iso, date_note = None, None  # у жалобы нет будущей съёмки; дату модель могла выдумать
    missing = ai_res.missing_data + ([date_note] if date_note else [])
    target = ai_res.lead_type == "целевой" and not ai_res.is_complaint
    q = rules.quote(prices, theme_key if target else None, ai_res.duration_hours,
                    shoot_date=iso, today=today, calendar=calendar,
                    out_of_town=ai_res.out_of_town, urgent=ai_res.urgent)
    status = rules.DATE_UNKNOWN if ai_res.is_complaint else (
        q.date_status if target else rules.date_status(calendar, prices, theme_key, iso, today))
    estimate = rules.Estimate(q.total, q.is_from, q.text()) if target else rules.Estimate(
        None, False, "не рассчитывается для этого типа заявки")
    needs_human, reason = rules.human_decision(
        prices, theme_key, ai_res.lead_type, ai_res.is_complaint, ai_res.is_nonstandard,
        status, ai_res.budget, estimate,
    )
    body = template_body(ai_res, name_token) or clean_draft(ai_res.reply_draft, neutral=needs_human)
    draft = body
    if target:
        block = reply_block(q, iso)
        if block:
            draft = f"{body}\n\n{block}"

    return Card(
        source="текст",
        theme=prices.themes[theme_key]["title"] if theme_key else NOT_SET,
        summary=ai_res.summary,
        lead_type="жалоба" if ai_res.is_complaint else ai_res.lead_type,
        shoot_date=fmt_date(iso),
        location=ai_res.location,
        duration_hours=f"{ai_res.duration_hours:g} ч" if ai_res.duration_hours else NOT_SET,
        people=ai_res.people,
        budget=rules.rub(ai_res.budget) if ai_res.budget else NOT_SET,
        key_data=ai_res.key_data,
        missing_data=missing,
        date_status=status,
        price_estimate=estimate.text,
        next_step=ai_res.next_step or _NEXT_STEP_FALLBACK,
        reply_draft=draft,
        reply_body=body,
        needs_human=needs_human,
        human_reason=reason,
        amount=estimate.amount,
        params=_params_dict(theme_key, ai_res.duration_hours, iso,
                            ai_res.out_of_town, ai_res.urgent) if target else {},
    )


def _looks_like_address(text: str) -> bool:
    return bool(privacy.ADDRESS_RE.search(text) or re.search(r"\d", text))


def lead_title(card: Card) -> str:
    if card.lead_type == "жалоба":
        return "Жалоба · " + card.summary[:60]
    if card.lead_type == "нецелевой":
        return "Нецелевая заявка · " + card.summary[:60]
    parts = [card.theme if card.theme != NOT_SET else "Новая заявка · тематика не указана"]
    if card.shoot_date != NOT_SET:
        parts.append(card.shoot_date)
    if card.location != NOT_SET and not _looks_like_address(card.location):
        parts.append(card.location)
    if card.source == "калькулятор":
        parts.append("калькулятор")
    return " · ".join(parts)


def card_as_text(card: Card, quote: rules.Quote | None = None, source_text: str = "") -> str:
    out = [f"Источник: {card.source}"]
    for key, label in LABELS.items():
        val = getattr(card, key)
        if isinstance(val, bool):
            val = "да" if val else "нет"
        if isinstance(val, list):
            val = "; ".join(val) if val else "—"
        out.append(f"{label}: {val}")
    if quote is not None and quote.total:
        out.append("")
        out.append("Расчёт клиента (предварительный, ждёт подтверждения):")
        for label, v in quote.lines + quote.discounts:
            out.append(f"  {label} — {rules.rub(v)}")
        out.append(f"  Итого — {rules.rub(quote.total)}")
    if source_text:
        # оригинал нужен фотографу, чтобы проверить пересказ нейросети
        out += ["", "Исходный текст клиента:", source_text]
    return "\n".join(out)


def _push_crm(b24: Bitrix24, name: str, phone: str, email: str, card: Card,
              quote: rules.Quote | None = None, source_text: str = "") -> dict:
    title = lead_title(card)
    lead_id = b24.create_lead(title, name, phone, email, card_as_text(card, quote, source_text), card.amount)
    crm: dict = {"status": "ok", "lead_id": lead_id, "lead_url": b24.lead_url(lead_id), "warnings": []}
    try:
        b24.create_task(
            lead_id,
            f"Ответить клиенту: {title}",
            f"Черновик ответа:\n{card.reply_draft}\n\nСледующий шаг: {card.next_step}",
            important=card.needs_human,
        )
    except BitrixError as e:
        crm["warnings"].append(f"задача не создана: {e}")
        log_event(log, "crm_warning", f"Битрикс24: задача не создана: {e}", logging.WARNING)
    try:
        mark = "❗ " if card.needs_human else ""
        b24.notify(f"{mark}Новая заявка: {title}. {card.summary}")
    except BitrixError as e:
        crm["warnings"].append(f"уведомление не отправлено: {e}")
        log_event(log, "crm_warning", f"Битрикс24: уведомление не отправлено: {e}", logging.WARNING)
    return crm


def _crm_safe(b24: Bitrix24 | None, *args, **kw) -> dict:
    if b24 is None:
        return {"status": "off", "message": "Битрикс24 не подключён (B24_URL пуст)."}
    try:
        return _push_crm(b24, *args, **kw)
    except BitrixError as e:
        log_event(log, "crm_error", f"Битрикс24: {e}", logging.ERROR)
        return {"status": "error", "message": f"В CRM не записано: {e}"}


def _report(card: Card, crm: dict, started: float, pii_masked: int, **extra) -> None:
    """Событие для мониторинга. Только категории и числа: ни текста заявки, ни имени, ни контактов."""
    log_event(
        log, "lead_processed", f"Заявка обработана: {card.source}, {card.lead_type}",
        source=card.source, lead_type=card.lead_type, theme=card.theme, date_status=card.date_status,
        needs_human=card.needs_human, human_reason=card.human_reason if card.needs_human else "",
        crm=crm.get("status", "off"), crm_warnings=len(crm.get("warnings", [])),
        pii_masked=pii_masked, ms=round((time.monotonic() - started) * 1000), **extra,
    )


def process(
    form: LeadForm,
    ai: AIProvider,
    prices: rules.Prices,
    calendar: rules.Calendar,
    today: date,
    b24: Bitrix24 | None = None,
    privacy_log: Path | None = None,
) -> Result:
    """Вход 1: заявка свободным текстом (мессенджер, форма, звонок)."""
    started = time.monotonic()
    # 1. Маскировка: имя из формы ищем и в тексте, в любом падеже
    masked = privacy.anonymize(form.text, known_names=[form.name])
    name_token = privacy.add_token(masked, privacy.NAME, form.name.split()[0]) if form.name else None
    if privacy_log:
        privacy.log_masked(masked, privacy_log)

    # 2. ИИ видит только метки
    ai_res = _analyze(
        ai,
        system_prompt(prices.titles(), today),
        user_prompt(masked.text, name_token, bool(form.phone), bool(form.email)),
    )

    # 3. Правила + 4. обратная подстановка
    card = build_card(ai_res, prices, calendar, today, name_token, masked.text)
    card = Card.model_validate(privacy.restore_obj(card.model_dump(), masked.mapping))

    # 5. CRM — сбой не роняет обработку
    crm = _crm_safe(b24, form.name, form.phone, form.email, card, source_text=form.text)
    _report(card, crm, started, sum(masked.counts().values()), ai="ok")
    return Result(card=card, masked_text=masked.text, crm=crm)


def process_calc(
    req: CalcRequest,
    ai: AIProvider | None,
    prices: rules.Prices,
    calendar: rules.Calendar,
    today: date,
    b24: Bitrix24 | None = None,
    privacy_log: Path | None = None,
) -> tuple[Result, rules.Quote]:
    """Вход 2: заявка из калькулятора. Параметры уже структурированы — ИИ нужен только для комментария."""
    started = time.monotonic()
    pii_masked = 0
    p = req.params
    theme_key = prices.key_by_title(p.theme)
    if theme_key is None:
        raise ValueError("Неизвестная тематика.")
    # Цену всегда пересчитываем на сервере: то, что прислал браузер, не используем
    q = rules.quote(prices, theme_key, p.hours, shoot_date=p.date, today=today, calendar=calendar,
                    out_of_town=p.out_of_town, urgent=p.urgent, extra_frames=p.extra_frames,
                    second_photographer=p.second_photographer)

    options = []
    if p.out_of_town:
        options.append("выезд за город")
    if p.urgent:
        options.append("срочная обработка")
    if p.second_photographer and prices.themes[theme_key]["full_day"]:
        options.append("второй фотограф")
    if p.extra_frames:
        options.append(f"+{p.extra_frames} кадров в ретушь")
    key_data = [f"часов: {q.hours:g}" if q.hours else "часов: базовый пакет", f"в ретуши {q.retouch} кадров"]
    key_data += options
    key_data.append("согласие на кадры в портфолио: " + ("да" if req.portfolio_consent else "нет"))

    masked_text = ""
    mapping: dict[str, str] = {}
    is_complaint = is_nonstandard = False
    missing: list[str] = [] if p.date else ["дата съёмки"]
    first_name = req.name.split()[0] if req.name else ""

    ai_res = None
    ai_failed = False
    if req.comment and ai is not None:
        masked = privacy.anonymize(req.comment, known_names=[req.name])
        name_token = privacy.add_token(masked, privacy.NAME, first_name) if first_name else None
        if privacy_log:
            privacy.log_masked(masked, privacy_log)
        masked_text, mapping = masked.text, masked.mapping
        pii_masked = sum(masked.counts().values())
        summary_for_ai = (f"{q.title}, дата {fmt_date(p.date)}, {q.hours or 'базовый пакет'} ч"
                          + (", " + ", ".join(options) if options else ""))
        try:
            ai_res = _analyze(ai, system_prompt(prices.titles(), today),
                              calc_user_prompt(summary_for_ai, masked.text, name_token))
        except AIError as e:
            # Сбой нейросети не должен терять заявку клиента: ведём по шаблону, комментарий уйдёт в CRM как есть
            log_event(log, "ai_error", f"Комментарий из калькулятора не разобран: {e}", logging.ERROR,
                      source="калькулятор")
            ai_failed = True
    elif req.comment:
        ai_failed = True

    if ai_res is not None:
        is_complaint, is_nonstandard = ai_res.is_complaint, ai_res.is_nonstandard
        key_data += ai_res.key_data
        missing += [m for m in ai_res.missing_data if m not in missing]
        next_step = ai_res.next_step or _NEXT_STEP_FALLBACK
    else:
        next_step = "Подтвердить дату и стоимость, договориться о предоплате"
        if ai_failed:
            key_data.append("комментарий клиента не разобран нейросетью — см. исходный текст")

    lead_type = "целевой"
    estimate = rules.Estimate(q.total, q.is_from, q.text())
    needs_human, reason = rules.human_decision(prices, theme_key, lead_type, is_complaint,
                                               is_nonstandard, q.date_status, None, estimate)
    if ai_failed and not needs_human:
        needs_human, reason = True, "комментарий клиента не разобран нейросетью"
    if ai_res is not None:
        # в калькуляторе тип заявки известен (целевой), шаблон нужен только для жалобы из комментария
        body = ((template_body(ai_res, name_token) if ai_res.is_complaint else None)
                or clean_draft(ai_res.reply_draft, neutral=needs_human))
    else:
        # шаблон: при передаче человеку — без обещания брони
        hello = f"Здравствуйте, {first_name}!" if first_name else "Здравствуйте!"
        body = (f"{hello} Спасибо за заявку: {q.title.lower()}. Фотограф свяжется с вами для уточнения деталей."
                if needs_human else
                f"{hello} Спасибо за заявку: {q.title.lower()}. Проверю детали и подтвержу бронь.")
    block = reply_block(q, p.date)
    card = Card(
        source="калькулятор",
        theme=q.title,
        summary=f"Заявка из калькулятора: {q.title.lower()}, {rules.rub(q.total)} (предварительно)",
        lead_type="жалоба" if is_complaint else lead_type,
        shoot_date=fmt_date(p.date),
        location="выезд за город" if p.out_of_town else "в городе",
        duration_hours=f"{q.hours:g} ч" if q.hours else NOT_SET,
        people=NOT_SET,
        budget=NOT_SET,
        key_data=key_data,
        missing_data=missing,
        date_status=q.date_status,
        price_estimate=q.text(),
        next_step=next_step,
        reply_draft=f"{body}\n\n{block}" if block else body,
        reply_body=body,
        needs_human=needs_human,
        human_reason=reason,
        amount=q.total,
        params=_params_dict(theme_key, p.hours, p.date, p.out_of_town, p.urgent,
                            p.extra_frames, p.second_photographer),
    )
    card = Card.model_validate(privacy.restore_obj(card.model_dump(), mapping))
    crm = _crm_safe(b24, req.name, req.phone, req.email, card, quote=q, source_text=req.comment)
    ai_state = "ok" if ai_res is not None else ("error" if ai_failed else "not_needed")
    _report(card, crm, started, pii_masked, ai=ai_state, total=q.total)
    return Result(card=card, masked_text=masked_text, crm=crm), q
