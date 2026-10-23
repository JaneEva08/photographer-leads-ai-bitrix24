"""Калькулятор: опции, скидки, ближайшие даты, заявка из калькулятора, пересчёт у фотографа."""
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, rules
from app.bitrix import Bitrix24
from app.pipeline import process_calc, reply_block
from app.schemas import CalcRequest
from tests.test_integrations import B24Mock
from tests.test_pipeline import FakeAI

TODAY = date(2026, 10, 23)  # пятница


@pytest.fixture
def prices():
    return rules.Prices.load()


@pytest.fixture
def cal():
    return rules.Calendar(full_days={"2027-06-12"}, sessions={"2027-05-16": 3, "2027-05-23": 1})


def q(prices, cal, theme, hours=None, **kw):
    return rules.quote(prices, theme, hours, today=TODAY, calendar=cal, **kw)


# ---- опции ------------------------------------------------------------------------

def test_extra_frames_and_second_photographer(prices, cal):
    r = q(prices, cal, "wedding", 6, extra_frames=20, second_photographer=True)
    # 40 000 + 2 ч × 7 000 + 6 ч × 2 500 + 20 × 300 = 75 000
    assert r.total == 75000 and r.retouch == 100


def test_second_photographer_ignored_for_session(prices, cal):
    assert q(prices, cal, "family", 1, second_photographer=True).total == 7000


def test_urgent_applies_to_options_too(prices, cal):
    # (7 000 + 10 × 300) × 1,3 = 13 000
    assert q(prices, cal, "family", 1, extra_frames=10, urgent=True).total == 13000


def test_hours_capped(prices, cal):
    assert q(prices, cal, "family", 99).hours == 14


# ---- скидки -----------------------------------------------------------------------

def test_weekday_discount_for_session(prices, cal):
    r = q(prices, cal, "family", 1, shoot_date="2027-05-19")  # среда
    assert r.total == 6300 and "будний день" in r.discounts[0][0]


def test_no_weekday_discount_on_weekend_or_without_date(prices, cal):
    assert q(prices, cal, "family", 1, shoot_date="2027-05-22").discounts == []  # суббота
    assert q(prices, cal, "family", 1).discounts == []


def test_no_weekday_discount_for_wedding(prices, cal):
    r = q(prices, cal, "wedding", 4, shoot_date="2026-11-04")  # среда, меньше 180 дней
    assert r.discounts == [] and r.total == 40000


def test_early_wedding(prices, cal):
    r = q(prices, cal, "wedding", 4, shoot_date="2027-07-10")  # суббота, > 180 дней
    assert r.total == 38000 and "ранняя бронь" in r.discounts[0][0]


def test_auto_cap_15(prices, cal):
    capped = rules.Prices(prices.themes, prices.extras, prices.rules,
                          {**prices.discounts, "weekday_percent": 12, "early_wedding_percent": 10})
    capped.themes = {**capped.themes, "wedding": {**capped.themes["wedding"], "full_day": False}}
    r = rules.quote(capped, "wedding", 4, shoot_date="2027-07-07", today=TODAY, calendar=cal)  # среда
    assert "−15%" in r.discounts[0][0] and "потолок" in r.discounts[0][0]
    assert r.total == 34000


def test_personal_discount_after_auto_and_limited(prices, cal):
    r = q(prices, cal, "family", 1, shoot_date="2027-05-19", personal_discount=10, personal_reason="постоянный клиент")
    # 7 000 − 700 = 6 300; −10% = 630 → 700 (округление вверх до 100) → 5 600
    assert r.total == 5600 and "постоянный клиент" in r.discounts[1][0]
    assert q(prices, cal, "family", 1, personal_discount=90).discounts[0][0].startswith("персональная скидка −30%")


# ---- даты ---------------------------------------------------------------------------

def test_nearest_free_for_wedding_only_weekends(prices, cal):
    r = q(prices, cal, "wedding", 4, shoot_date="2027-06-12")
    assert r.date_status == rules.DATE_BUSY
    assert all(date.fromisoformat(d).weekday() >= 4 for d in r.nearest_free)
    assert r.nearest_free[0] == "2027-06-13"


def test_nearest_free_for_session_skips_full(prices, cal):
    r = q(prices, cal, "family", 1, shoot_date="2027-05-16")
    assert r.nearest_free[:2] == ["2027-05-17", "2027-05-18"]


def test_reply_block_busy_lists_dates(prices, cal):
    r = q(prices, cal, "wedding", 4, shoot_date="2027-06-12")
    text = reply_block(r, "2027-06-12")
    assert "занято" in text and "13.06.2027" in text and "ранняя бронь свадьбы" in text


# ---- заявка из калькулятора ------------------------------------------------------------

def calc_req(**kw):
    base = dict(name="Анна Королёва", phone="+7 916 123-45-67", consent=True,
                params={"theme": "Свадьба", "hours": 8, "date": "2027-06-12", "out_of_town": True})
    base.update(kw)
    return CalcRequest.model_validate(base)


def test_calc_request_without_comment_needs_no_ai(prices, cal):
    res, quote = process_calc(calc_req(), None, prices, cal, TODAY)
    assert res.card.source == "калькулятор" and res.card.amount == 66500
    assert res.card.needs_human and "дата занята" in res.card.human_reason
    assert res.card.reply_draft.startswith("Здравствуйте, Анна!")
    assert res.masked_text == ""


def test_calc_request_comment_goes_through_privacy_and_ai(prices, cal):
    ai_answer = {"summary": "-", "lead_type": "целевой", "key_data": ["нужны кадры сборов невесты"],
                 "next_step": "Уточнить время сборов", "reply_draft": "[ИМЯ_1], сборы снимем!"}
    ai = FakeAI([ai_answer])
    req = calc_req(comment="Это Анна, хотим ещё сборы невесты, звоните +7 916 123-45-67")
    res, _ = process_calc(req, ai, prices, cal, TODAY)
    assert "916" not in ai.seen[0] and "Анна" not in ai.seen[0]
    assert "нужны кадры сборов невесты" in res.card.key_data
    assert res.card.reply_draft.startswith("Анна, сборы снимем!")


def test_calc_request_crm_has_breakdown(prices, cal):
    mock = B24Mock()
    b24 = Bitrix24("https://demo.bitrix24.ru/rest/1/x/", transport=httpx.MockTransport(mock))
    res, _ = process_calc(calc_req(), None, prices, cal, TODAY, b24=b24)
    lead = [c for c in mock.calls if c[0] == "crm.lead.add"][0][1]["fields"]
    assert lead["TITLE"].endswith("калькулятор") and lead["OPPORTUNITY"] == 66500
    assert "Расчёт клиента" in lead["COMMENTS"] and "ранняя бронь" in lead["COMMENTS"]


def test_calc_request_validation():
    with pytest.raises(ValueError, match="согласие"):
        calc_req(consent=False)
    with pytest.raises(ValueError, match="контакт"):
        calc_req(phone="")


# ---- HTTP ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    main._state.clear()
    monkeypatch.setattr(main, "today", lambda: TODAY)
    monkeypatch.setattr(main, "get_b24", lambda: None)
    with TestClient(main.app) as c:
        yield c


def test_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 200
    opts = client.get("/api/calc/options").json()
    assert len(opts["themes"]) == 6 and opts["discounts"]["max_auto_percent"] == 15


def test_client_quote_ignores_personal_discount(client):
    body = {"theme": "Семейная фотосессия", "hours": 1, "personal_discount": 30}
    r = client.post("/api/calc/quote", json=body).json()
    assert r["total"] == 7000 and r["discounts"] == []


def test_manager_quote_with_personal_discount(client):
    body = {"theme": "Семейная фотосессия", "hours": 1, "personal_discount": 10, "personal_reason": "портфолио"}
    r = client.post("/api/manager/quote", json=body).json()
    assert r["total"] == 6300 and "6 300 ₽" in r["reply_block"]


def test_quote_bad_theme(client):
    r = client.post("/api/calc/quote", json={"theme": "Ню"})
    assert r.status_code == 422


def test_calc_request_api(client):
    body = {"name": "Анна", "phone": "+7 900 000-00-01", "consent": True,
            "params": {"theme": "Love story", "hours": 2, "date": "2027-05-19"}}
    r = client.post("/api/calc/request", json=body)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    # 9 000 + 0,5 × 5 000 = 11 500, будний день −10% = 1 200 (вверх до 100) → 10 300
    assert r.json()["quote"]["total"] == 10300


def test_manager_save_requires_crm(client):
    r = client.post("/api/manager/save", json={"lead_id": 1, "params": {"theme": "Свадьба"}})
    assert r.status_code == 409


def test_manager_save_updates_lead(monkeypatch, client):
    mock = B24Mock()
    mock_b24 = Bitrix24("https://demo.bitrix24.ru/rest/1/x/", transport=httpx.MockTransport(mock))
    monkeypatch.setattr(main, "get_b24", lambda: mock_b24)
    r = client.post("/api/manager/save", json={"lead_id": 42, "params": {
        "theme": "Семейная фотосессия", "hours": 1, "personal_discount": 10, "personal_reason": "портфолио"}})
    assert r.status_code == 200 and r.json()["total"] == 6300
    methods = [c[0] for c in mock.calls]
    assert "crm.lead.update" in methods and "crm.timeline.comment.add" in methods
