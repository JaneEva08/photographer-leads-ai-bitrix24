"""Веб-приложение.

/        — калькулятор для клиента: расчёт → проверка заявки → отправка
/admin   — рабочее место фотографа: разбор заявки текстом, карточка, пересчёт со скидкой

/admin и служебные API (/api/process, /api/manager/*, /api/config) закрываются паролем
ADMIN_PASSWORD из .env (логин admin). Пусто — доступ открыт: только для локального запуска.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import rules
from .ai import AIError
from .ai.gigachat import GigaChatProvider
from .bitrix import Bitrix24, BitrixError
from .config import settings
from .loki import log_event, setup_loki
from .pipeline import fmt_date, process, process_calc, reply_block
from .samples import SAMPLES
from .schemas import LABELS, CalcParams, CalcRequest, LeadForm, ManagerParams

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
# LOKI_URL пуст — отправки в Loki нет, всё как раньше
_loki = setup_loki(settings.loki_url, {"app": settings.loki_app, "env": settings.loki_env})

STATIC = Path(__file__).resolve().parent.parent / "static"

_state: dict = {}


def get_ai():
    if "ai" not in _state:
        _state["ai"] = GigaChatProvider(
            credentials=settings.gigachat_credentials,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
            ca_bundle=settings.gigachat_ca_bundle or None,
        )
    return _state["ai"]


def get_b24() -> Bitrix24 | None:
    if not settings.b24_url:
        return None
    if "b24" not in _state:
        uid = int(settings.b24_notify_user_id) if settings.b24_notify_user_id else None
        _state["b24"] = Bitrix24(settings.b24_url, notify_user_id=uid)
    return _state["b24"]


def today() -> date:
    """APP_TODAY (ГГГГ-ММ-ДД) фиксирует «сегодня» для демо и протокола тестирования."""
    if settings.app_today:
        try:
            return date.fromisoformat(settings.app_today)
        except ValueError:
            log.error("APP_TODAY задан неверно (нужно ГГГГ-ММ-ДД), использую настоящую дату.")
    return date.today()


_basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    if not settings.admin_password:
        return
    ok = creds is not None and secrets.compare_digest(
        creds.password.encode("utf-8"), settings.admin_password.encode("utf-8"))
    if not ok:
        raise HTTPException(status_code=401, detail="Нужен пароль фотографа.",
                            headers={"WWW-Authenticate": 'Basic realm="photo-leads admin"'})


def check_webhook() -> None:
    if not settings.admin_password:
        log.warning("ADMIN_PASSWORD не задан: /admin и /api/manager/* открыты всем. Для сервера задайте пароль.")
    b24 = get_b24()
    if b24 is None:
        log.info("Битрикс24 не подключён: B24_URL пуст.")
        return
    try:
        uid = b24.current_user_id()
        log.info("Вебхук Битрикс24 работает, ответственный — пользователь ID %s.", uid)
    except BitrixError as e:
        log.error("Вебхук Битрикс24 не работает: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_webhook()
    log_event(log, "app_started", "Приложение запущено", gigachat_key=bool(settings.gigachat_credentials),
              bitrix24=bool(settings.b24_url), demo_mode=settings.demo_mode, loki=bool(_loki))
    yield
    if _loki is not None:
        log_event(log, "app_stopped", "Приложение остановлено")
        logging.getLogger().removeHandler(_loki)
        _loki.close()


app = FastAPI(title="ИИ-разбор заявок фотографа", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _bad(e: ValidationError | ValueError) -> JSONResponse:
    if isinstance(e, ValidationError):
        err = e.errors()[0]
        msg = str(err.get("msg", "Неверные данные")).removeprefix("Value error, ")
    else:
        msg = str(e)
    # текст ошибки — наш, без ввода пользователя: pydantic не кладёт значение поля в msg
    log_event(log, "bad_input", f"Неверные данные: {msg}", logging.WARNING)
    return JSONResponse({"error": msg}, status_code=422)


def _quote(p: CalcParams, personal: float = 0, reason: str = "") -> rules.Quote:
    prices = rules.Prices.load()
    key = prices.key_by_title(p.theme) if p.theme else None
    if p.theme and key is None:
        raise ValueError("Неизвестная тематика.")
    return rules.quote(prices, key, p.hours, shoot_date=p.date, today=today(),
                       calendar=rules.Calendar.load(), out_of_town=p.out_of_town, urgent=p.urgent,
                       extra_frames=p.extra_frames, second_photographer=p.second_photographer,
                       personal_discount=personal, personal_reason=reason)


# ---- страницы ----------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin():
    return FileResponse(STATIC / "admin.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "gigachat_key": bool(settings.gigachat_credentials), "bitrix24": bool(settings.b24_url)}


# ---- калькулятор клиента -----------------------------------------------------------

@app.get("/api/calc/options")
def calc_options():
    prices = rules.Prices.load()
    return {
        "themes": [{"key": k, **{f: t[f] for f in ("title", "base_hours", "base_price", "retouch", "extra_hour", "full_day")}}
                   for k, t in prices.themes.items()],
        "extras": prices.extras,
        "discounts": prices.discounts,
        "max_hours": prices.rules.get("max_hours", 14),
        "today": today().isoformat(),
        "demo_mode": settings.demo_mode,
    }


@app.post("/api/calc/quote")
async def calc_quote(request: Request):
    """Расчёт для клиента. Персональную скидку отсюда передать нельзя."""
    try:
        p = CalcParams.model_validate(await request.json())
        return _quote(p).as_dict()
    except (ValidationError, ValueError) as e:
        return _bad(e)


@app.post("/api/calc/request")
async def calc_request(request: Request):
    try:
        req = CalcRequest.model_validate(await request.json())
    except ValidationError as e:
        return _bad(e)
    ai = None
    if req.comment:
        try:
            ai = get_ai()
        except AIError as e:
            log_event(log, "ai_error", f"GigaChat недоступен, комментарий уйдёт в CRM без разбора: {e}",
                      logging.ERROR, source="калькулятор")
    try:
        # синхронные вызовы GigaChat и Битрикс24 — в потоке, чтобы не блокировать остальные запросы
        result, q = await asyncio.to_thread(
            process_calc, req, ai, rules.Prices.load(), rules.Calendar.load(), today(),
            get_b24(), settings.privacy_log)
    except ValueError as e:
        return _bad(e)
    body = {
        "status": "sent",
        "message": "Фотограф проверит дату и подтвердит стоимость в течение 2 часов в рабочее время. Ответ придёт на телефон или email из заявки.",
        "quote": q.as_dict(),
    }
    if settings.demo_mode:
        # Только для демо: показать, как заявку увидит фотограф
        body["card"] = result.card.model_dump()
        body["crm"] = result.crm
        body["masked_text"] = result.masked_text
    return body


# ---- рабочее место фотографа ----------------------------------------------------------

@app.get("/api/config", dependencies=[Depends(require_admin)])
def config():
    return {"demo_mode": settings.demo_mode, "labels": LABELS, "samples": SAMPLES, "crm": bool(settings.b24_url)}


@app.post("/api/process", dependencies=[Depends(require_admin)])
async def process_lead(request: Request):
    try:
        form = LeadForm.model_validate(await request.json())
    except ValidationError as e:
        return _bad(e)
    try:
        result = await asyncio.to_thread(
            lambda: process(form, ai=get_ai(), prices=rules.Prices.load(), calendar=rules.Calendar.load(),
                            today=today(), b24=get_b24(), privacy_log=settings.privacy_log))
    except AIError as e:
        _state.pop("ai", None)
        log_event(log, "ai_error", f"Заявка не разобрана: {e}", logging.ERROR, source="текст")
        raise HTTPException(status_code=502, detail=str(e))
    body = {"card": result.card.model_dump(), "crm": result.crm}
    if settings.demo_mode:
        body["masked_text"] = result.masked_text
    return body


@app.post("/api/manager/quote", dependencies=[Depends(require_admin)])
async def manager_quote(request: Request):
    """Пересчёт у фотографа, с персональной скидкой. Возвращает новый абзац для черновика."""
    try:
        p = ManagerParams.model_validate(await request.json())
        q = _quote(p, p.personal_discount, p.personal_reason)
    except (ValidationError, ValueError) as e:
        return _bad(e)
    return {**q.as_dict(), "reply_block": reply_block(q, p.date)}


class SaveRequest(BaseModel):
    lead_id: int
    params: ManagerParams


@app.post("/api/manager/save", dependencies=[Depends(require_admin)])
async def manager_save(request: Request):
    """Записать пересчёт в лид: новая сумма + комментарий с расшифровкой в ленте."""
    b24 = get_b24()
    if b24 is None:
        return JSONResponse({"error": "Битрикс24 не подключён."}, status_code=409)
    try:
        req = SaveRequest.model_validate(await request.json())
        q = _quote(req.params, req.params.personal_discount, req.params.personal_reason)
    except (ValidationError, ValueError) as e:
        return _bad(e)
    if not q.total:
        return JSONResponse({"error": "Не выбрана тематика — сумма не посчитана."}, status_code=422)
    lines = [f"{label} — {rules.rub(v)}" for label, v in q.lines + q.discounts]
    text = (f"Пересчёт фотографа: {rules.rub(q.total)}. Дата {fmt_date(req.params.date)} — {q.date_status}.\n"
            + "\n".join(lines))
    try:
        await asyncio.to_thread(b24.update_lead_amount, req.lead_id, q.total)
        await asyncio.to_thread(b24.add_lead_comment, req.lead_id, text)
    except BitrixError as e:
        log_event(log, "crm_error", f"Пересчёт не сохранён в Битрикс24: {e}", logging.ERROR)
        return JSONResponse({"error": f"Не сохранено в Битрикс24: {e}"}, status_code=502)
    log_event(log, "manager_saved", "Фотограф сохранил пересчёт в лид", total=q.total,
              personal_discount=req.params.personal_discount > 0)
    return {"status": "ok", "total": q.total}


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
