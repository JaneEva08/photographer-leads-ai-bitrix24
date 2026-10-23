"""Регрессии по найденным багам: утечки анонимизатора, цены в черновике ИИ, потеря заявки при сбое ИИ,
исходный текст в CRM, пароль на админку, проверка даты от ИИ."""
import csv
import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, privacy, rules
from app.ai import AIError
from app.ai.gigachat import GigaChatProvider
from app.bitrix import Bitrix24
from app.config import Settings
from app.pipeline import clean_draft, process_calc
from app.schemas import AIResult, CalcParams, CalcRequest
from tests.test_integrations import B24Mock
from tests.test_pipeline import FAMILY, FakeAI

TODAY = date(2026, 10, 23)


# ---- анонимизатор ------------------------------------------------------------------

@pytest.mark.parametrize("text,leaked", [
    ("Мария Ивановна Смирнова позвонит", "Смирнова"),
    ("Я Кристина Волкова-Петрова", "Петрова"),
    ("Живём на Ленинском проспекте 45, кв 7", "Ленинском"),
    ("дочка Вера 3 года", "Вера"),
    ("сын Лев и жена Анна", "Лев"),
    ("Паспорт 4510 123456", "123456"),
    ("карта 4276 1234 5678 9012", "5678"),
])
def test_anonymizer_no_leaks(text, leaked):
    assert leaked not in privacy.anonymize(text).text


@pytest.mark.parametrize("text", [
    "Гуляем на набережной в субботу",
    "Пушкинская площадь рядом",
    "Съёмка 15.05.2027 в 14:30, бюджет 10000",
])
def test_anonymizer_keeps_places_dates_sums(text):
    assert privacy.anonymize(text).text == text


def test_log_has_doc_column_and_no_formula(tmp_path):
    log = tmp_path / "p.csv"
    privacy.log_masked(privacy.Masked("=SUM(A1)", {}), log)
    rows = list(csv.reader(log.open(encoding="utf-8-sig"), delimiter=";"))
    assert "ДОКУМЕНТ" in rows[0] and rows[1][1].startswith("'=")


# ---- ответ ИИ ------------------------------------------------------------------------

def test_clean_draft_drops_prices_and_date_claims():
    body = "Здравствуйте! Съёмка стоит 7 000 ₽. Дата свободна. Какой парк вам нравится?"
    assert clean_draft(body) == "Здравствуйте! Какой парк вам нравится?"


def test_clean_draft_neutral_drops_promises():
    assert "забронирую" not in clean_draft("Спасибо. Сразу забронирую дату.", neutral=True)
    assert clean_draft("Забронирую дату.", neutral=True) == "Здравствуйте! Спасибо за заявку."


@pytest.mark.parametrize("raw,expected", [
    ("2027-05-15", "2027-05-15"), ("15.05.2027", "2027-05-15"), ("завтра", None), ("2027-13-45", None),
])
def test_shoot_date_validated(raw, expected):
    r = AIResult(summary="s", lead_type="целевой", next_step="n", reply_draft="r", shoot_date=raw)
    assert r.shoot_date == expected


def test_budget_compared_with_base_package():
    prices = rules.Prices.load()
    est = rules.Estimate(9500, False, "")
    # семейная: базовый 7000, порог 0,7 × 7000 = 4900, а не от сметы 9500
    assert rules.human_decision(prices, "family", "целевой", False, False, rules.DATE_FREE, 5000, est)[0] is False
    assert rules.human_decision(prices, "family", "целевой", False, False, rules.DATE_FREE, 4000, est)[0] is True


def test_retry_gets_validation_error_in_prompt():
    from app.pipeline import _analyze
    ai = FakeAI(["не json", json.dumps({**FAMILY, "reply_draft": "ok"}, ensure_ascii=False)])
    _analyze(ai, "sys", "заявка")
    assert "не прошёл проверку" in ai.seen[1] and ai.seen[1].startswith("заявка")


# ---- калькулятор ---------------------------------------------------------------------

def _calc_req(comment=""):
    return CalcRequest(name="Анна", phone="+7 900 000-00-01", consent=True, comment=comment,
                       params=CalcParams(theme="Семейная фотосессия", hours=1, date="2027-05-19"))


class BrokenAI:
    name = "broken"

    def complete(self, system, user):
        return "это не json"


def test_calc_ai_failure_still_creates_lead_with_original_comment():
    mock = B24Mock()
    b24 = Bitrix24("https://x.bitrix24.ru/rest/1/k/", transport=httpx.MockTransport(mock))
    res, _ = process_calc(_calc_req("Хотим у моря"), BrokenAI(), rules.Prices.load(), rules.Calendar.load(),
                          TODAY, b24)
    assert res.crm["status"] == "ok"
    assert res.card.needs_human and "не разобран" in res.card.human_reason
    comments = [c for c in mock.calls if c[0] == "crm.lead.add"][0][1]["fields"]["COMMENTS"]
    assert "Исходный текст клиента:" in comments and "Хотим у моря" in comments


def test_template_draft_no_booking_promise_when_human_needed():
    prices, cal = rules.Prices.load(), rules.Calendar.load()
    req = CalcRequest(name="Анна", phone="+7 900 000-00-01", consent=True,
                      params=CalcParams(theme="Свадьба", hours=8, date="2027-06-12"))
    res, _ = process_calc(req, None, prices, cal, TODAY)
    assert res.card.needs_human and "подтвержу бронь" not in res.card.reply_draft


# ---- пароль на админку ---------------------------------------------------------------

@pytest.fixture
def secured(monkeypatch):
    main._state.clear()
    monkeypatch.setattr(main, "settings", Settings(admin_password="s3cret"))
    monkeypatch.setattr(main, "get_b24", lambda: None)
    with TestClient(main.app) as c:
        yield c


def test_admin_requires_password(secured):
    for method, url in [("get", "/admin"), ("get", "/api/config"), ("post", "/api/process"),
                        ("post", "/api/manager/quote"), ("post", "/api/manager/save")]:
        r = getattr(secured, method)(url)
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"], url
    assert secured.get("/admin", auth=("admin", "bad")).status_code == 401
    assert secured.get("/admin", auth=("admin", "s3cret")).status_code == 200


def test_public_calculator_stays_open(secured):
    assert secured.get("/").status_code == 200
    assert secured.get("/api/calc/options").status_code == 200


# ---- GigaChat: сертификат ------------------------------------------------------------

def test_missing_ca_bundle_is_clear_error(tmp_path):
    with pytest.raises(AIError, match="GIGACHAT_CA_BUNDLE_FILE"):
        GigaChatProvider("key", ca_bundle=str(tmp_path / "nope.cer"))


def test_app_today_override(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(app_today="2026-12-01"))
    assert main.today() == date(2026, 12, 1)


# ---- дата из частей: год и день недели считает код ------------------------------------

def _ai(**kw):
    kw.setdefault("lead_type", "целевой")
    kw.setdefault("reply_draft", "r")
    return AIResult(summary="s", next_step="n", **kw)


@pytest.mark.parametrize("kw,expected,note", [
    ({"shoot_day": 15, "shoot_month": 5, "shoot_weekday": "суббота"}, "2027-05-15", False),  # 15.05.2026 уже прошло
    ({"shoot_day": 15, "shoot_month": 5}, "2027-05-15", False),
    ({"shoot_day": 13, "shoot_month": 5, "shoot_weekday": "суббота"}, None, True),  # 13.05.2027 — четверг
    ({"shoot_day": 12, "shoot_month": 6, "shoot_year": 2027}, "2027-06-12", False),
    ({"shoot_day": 31, "shoot_month": 2}, None, True),
    ({"shoot_date": "2027-05-15"}, "2027-05-15", False),
    ({}, None, False),
])
def test_resolve_shoot_date(kw, expected, note):
    from app.pipeline import resolve_shoot_date
    iso, n = resolve_shoot_date(_ai(**kw), TODAY)
    assert iso == expected and bool(n) is note


def test_wrong_weekday_goes_to_missing_data_not_to_price():
    from app.pipeline import build_card
    ai = _ai(theme="Семейная фотосессия", shoot_day=13, shoot_month=5, shoot_weekday="суббота", duration_hours=1.5)
    card = build_card(ai, rules.Prices.load(), rules.Calendar.load(), TODAY)
    assert card.shoot_date == "не указано" and card.date_status == rules.DATE_UNKNOWN
    assert any("не совпадают" in m for m in card.missing_data)


def test_complaint_ignores_invented_date_and_has_no_wedding_reason():
    from app.pipeline import build_card
    ai = _ai(theme="Свадьба", lead_type="неясно", is_complaint=True, shoot_date="2026-08-23")
    card = build_card(ai, rules.Prices.load(), rules.Calendar.load(), TODAY)
    assert card.shoot_date == "не указано" and card.needs_human
    assert card.human_reason == "жалоба или конфликт"


# ---- черновик ------------------------------------------------------------------------

def test_clean_draft_drops_date_confirmation_and_placeholders():
    body = ("Здравствуйте! Мы подтверждаем выбранную вами дату. Какой стиль вам нравится? "
            "С уважением, [ВАШЕ ИМЯ]")
    assert clean_draft(body) == "Здравствуйте! Какой стиль вам нравится?"


def test_clean_draft_keeps_tokens_and_asking_client_to_confirm():
    body = "Здравствуйте, [ИМЯ_1]! Подтвердите, пожалуйста, удобное время."
    assert clean_draft(body) == body


# ---- заголовок лида ---------------------------------------------------------------------

def test_lead_title_has_no_address():
    from app.pipeline import lead_title
    from app.schemas import Card
    c = Card(theme="Детская фотосессия", summary="s", lead_type="целевой", shoot_date="не указано",
             location="ул. Лесной, д. 5, кв. 12", duration_hours="не указано", people="x", budget="не указано",
             key_data=[], missing_data=[], date_status="не указана", price_estimate="", next_step="",
             reply_draft="", needs_human=False, human_reason="")
    assert lead_title(c) == "Детская фотосессия"


# ---- шаблонные ответы -------------------------------------------------------------------

def test_complaint_gets_template_not_model_text():
    from app.pipeline import build_card
    ai = _ai(lead_type="неясно", is_complaint=True, reply_draft="Извини, уточни дату свадьбы!")
    card = build_card(ai, rules.Prices.load(), rules.Calendar.load(), TODAY, "[ИМЯ_1]")
    assert "извините" in card.reply_draft and "Извини," not in card.reply_draft and "[ИМЯ_1]" in card.reply_draft
    assert "дату свадьбы" not in card.reply_draft


def test_nontarget_gets_polite_refusal():
    from app.pipeline import build_card
    ai = _ai(lead_type="нецелевой", reply_draft="Подберём вам видеографа!")
    card = build_card(ai, rules.Prices.load(), rules.Calendar.load(), TODAY)
    assert "только фотосъёмкой" in card.reply_draft and "видеограф" not in card.reply_draft


def test_incomplete_model_answer_does_not_break_card():
    from app.pipeline import build_card
    ai = AIResult(summary="s", lead_type="целевой", theme="Семейная фотосессия")  # нет next_step и reply_draft
    card = build_card(ai, rules.Prices.load(), rules.Calendar.load(), TODAY)
    assert card.next_step and card.reply_draft


def test_year_invented_by_model_is_ignored():
    from app.pipeline import resolve_shoot_date
    ai = _ai(shoot_day=15, shoot_month=5, shoot_year=2026, shoot_weekday="суббота")
    assert resolve_shoot_date(ai, TODAY, "Суббота 15 мая, в парке") == ("2027-05-15", None)
    # а год, который клиент написал, принимается
    assert resolve_shoot_date(_ai(shoot_day=12, shoot_month=6, shoot_year=2027), TODAY, "свадьба 12 июня 2027")[0] == "2027-06-12"
