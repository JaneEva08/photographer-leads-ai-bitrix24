"""Сквозная проверка пайплайна с подменённой нейросетью.

Подмена здесь только для автотестов: ручные сценарии гоняются на настоящем GigaChat.
"""
import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, rules
from app.ai import AIError
from app.bitrix import Bitrix24
from app.pipeline import process
from app.schemas import LeadForm
from tests.test_integrations import B24Mock

TODAY = date(2026, 10, 23)


class FakeAI:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def complete(self, system, user):
        self.seen.append(user)
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


WEDDING = {
    "theme": "Свадьба", "summary": "Свадебная съёмка на 8 часов", "lead_type": "целевой",
    "shoot_date": "2027-06-12", "location": "выезд за город", "out_of_town": True,
    "duration_hours": 8, "people": "пара и гости", "budget": 70000,
    "key_data": ["регистрация в 12:00"], "missing_data": ["точный адрес площадки"],
    "next_step": "Предложить другую дату или второго фотографа",
    "reply_draft": "Здравствуйте, [ИМЯ_1]! Спасибо за заявку. Напишу вам на [EMAIL_1].",
}

FAMILY = {
    "theme": "Семейная фотосессия", "summary": "Семейная съёмка в парке", "lead_type": "целевой",
    "shoot_date": "2027-05-15", "location": "парк", "duration_hours": 1.5, "people": "семья: трое",
    "budget": 10000, "next_step": "Подтвердить дату", "reply_draft": "Здравствуйте, [ИМЯ_1]!",
}


@pytest.fixture
def env():
    return dict(prices=rules.Prices.load(), calendar=rules.Calendar.load(), today=TODAY)


def wedding_form():
    from app.samples import SAMPLES
    s = next(x for x in SAMPLES if x["id"] == "wedding")
    return LeadForm(**{k: s[k] for k in ("name", "phone", "email", "text")})


def test_ai_sees_only_tokens_and_card_restored(env):
    ai = FakeAI([WEDDING])
    res = process(wedding_form(), ai, **env)
    sent = ai.seen[0]
    for raw in ("Анна", "Дмитрий", "916", "anna.test"):
        assert raw not in sent
    assert "[ИМЯ_" in sent and "[ТЕЛЕФОН_1]" in sent
    # обратная подстановка
    assert "anna.test@example.com" in res.card.reply_draft
    assert "[EMAIL_1]" not in res.card.reply_draft
    # правила: 12.06.2027 занят, свадьба → человек
    assert res.card.date_status == rules.DATE_BUSY
    assert res.card.needs_human and "свадьба" in res.card.human_reason
    assert "занято" in res.card.reply_draft
    assert res.crm["status"] == "off"


def test_price_comes_from_rules_not_ai(env):
    form = LeadForm(name="Екатерина", phone="+7 900 000-00-01", text="Семейная фотосессия 15 мая")
    res = process(form, FakeAI([FAMILY]), **env)
    assert res.card.price_estimate.startswith("9 500 ₽")
    assert "Предварительная стоимость — 9 500 ₽." in res.card.reply_draft
    assert res.card.date_status == rules.DATE_FREE
    assert not res.card.needs_human
    assert "Екатерина" in res.card.reply_draft


def test_retry_once_on_bad_json_then_error(env):
    form = LeadForm(phone="1", text="текст")
    assert process(form, FakeAI(["не json", FAMILY]), **env).card.theme == "Семейная фотосессия"
    with pytest.raises(AIError):
        process(form, FakeAI(["мусор", '{"lead_type": "непонятно"}']), **env)


def test_complaint_no_price(env):
    ans = {"summary": "Нет фото два месяца", "lead_type": "неясно", "is_complaint": True,
           "next_step": "Позвонить клиенту", "reply_draft": "Здравствуйте! Разберусь и вернусь сегодня."}
    res = process(LeadForm(phone="1", text="жалоба"), FakeAI([ans]), **env)
    assert res.card.lead_type == "жалоба" and res.card.needs_human
    assert "₽" not in res.card.reply_draft


def test_crm_push_and_failure_does_not_break(env):
    mock = B24Mock()
    b24 = Bitrix24("https://demo.bitrix24.ru/rest/1/x/", transport=httpx.MockTransport(mock))
    res = process(wedding_form(), FakeAI([WEDDING]), b24=b24, **env)
    assert res.crm["status"] == "ok" and res.crm["lead_id"] == 42
    lead = [c for c in mock.calls if c[0] == "crm.lead.add"][0][1]["fields"]
    assert lead["TITLE"].startswith("Свадьба · 12.06.2027")
    # 40 000 + 28 000 + 2 000 = 70 000, ранняя бронь свадьбы −5% → 66 500
    assert lead["NAME"] == "Анна" and lead["OPPORTUNITY"] == 66500

    bad = Bitrix24("https://demo.bitrix24.ru/rest/1/x/", transport=httpx.MockTransport(B24Mock("crm.lead.add")))
    res = process(wedding_form(), FakeAI([WEDDING]), b24=bad, **env)
    assert res.crm["status"] == "error" and res.card.summary


def test_privacy_log_written(env, tmp_path):
    log = tmp_path / "log.csv"
    process(wedding_form(), FakeAI([WEDDING]), privacy_log=log, **env)
    assert "Дмитрий" not in log.read_text(encoding="utf-8-sig")


# ---- HTTP API -------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    main._state.clear()
    monkeypatch.setitem(main._state, "ai", FakeAI([FAMILY]))
    monkeypatch.setattr(main, "get_b24", lambda: None)
    with TestClient(main.app) as c:
        yield c


def test_api_validation(client):
    r = client.post("/api/process", json={"name": "А", "text": "хочу съёмку"})
    assert r.status_code == 422 and "контакт" in r.json()["error"]


def test_api_ok_and_page(client):
    assert client.get("/").status_code == 200
    cfg = client.get("/api/config").json()
    assert len(cfg["samples"]) == 6
    r = client.post("/api/process", json={"phone": "+7 900 000-00-01", "text": "Семейная фотосессия"})
    assert r.status_code == 200
    assert r.json()["card"]["theme"] == "Семейная фотосессия"
