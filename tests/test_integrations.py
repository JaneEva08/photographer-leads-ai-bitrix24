"""GigaChat и Битрикс24 на подменённом транспорте: сеть не нужна."""
import json

import httpx
import pytest

from app.ai import AIError, extract_json
from app.ai.gigachat import GigaChatProvider
from app.bitrix import Bitrix24, BitrixError


def test_extract_json_from_fenced():
    assert extract_json('Вот ответ:\n```json\n{"a": 1}\n```') == {"a": 1}


def test_gigachat_token_then_chat_and_refresh_on_401():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/oauth"):
            assert req.headers["Authorization"] == "Basic KEY"
            assert "RqUID" in req.headers
            return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_at": 9999999999999})
        if len([c for c in calls if c.endswith("completions")]) == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": 1}'}}]})

    g = GigaChatProvider("KEY", transport=httpx.MockTransport(handler))
    assert g.complete("s", "u") == '{"ok": 1}'
    assert [c.split("/")[-1] for c in calls] == ["oauth", "completions", "oauth", "completions"]


def test_gigachat_bad_key():
    g = GigaChatProvider("BAD", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(AIError, match="ключ"):
        g.complete("s", "u")


def test_gigachat_requires_key():
    with pytest.raises(AIError):
        GigaChatProvider("")


class B24Mock:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = (fail_on,) if isinstance(fail_on, str) else (fail_on or ())

    def __call__(self, req: httpx.Request):
        method = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        body = json.loads(req.content or b"{}")
        self.calls.append((method, body))
        if method in self.fail_on:
            return httpx.Response(400, json={"error": "ACCESS_DENIED", "error_description": "нет прав"})
        result = {"user.current": {"ID": "1"}, "profile": {"ID": "1"}, "crm.lead.add": 42, "tasks.task.add": {"task": {"id": "7"}},
                  "im.notify.personal.add": 1, "crm.lead.update": True, "crm.timeline.comment.add": 5}[method]
        return httpx.Response(200, json={"result": result})


def test_bitrix_lead_fields_in_proper_places():
    mock = B24Mock()
    b = Bitrix24("https://demo.bitrix24.ru/rest/1/secret/", transport=httpx.MockTransport(mock))
    lead_id = b.create_lead("Свадьба · 12.06.2027", "Анна Королёва", "+7 916 123-45-67", "a@example.com", "карточка", 68000)
    assert lead_id == 42
    fields = [c for c in mock.calls if c[0] == "crm.lead.add"][0][1]["fields"]
    assert fields["NAME"] == "Анна" and fields["LAST_NAME"] == "Королёва"
    assert fields["PHONE"][0]["VALUE"] == "+7 916 123-45-67"
    assert fields["EMAIL"][0]["VALUE"] == "a@example.com"
    assert fields["SOURCE_ID"] == "WEB" and fields["ASSIGNED_BY_ID"] == 1
    assert fields["OPPORTUNITY"] == 68000
    assert b.lead_url(42) == "https://demo.bitrix24.ru/crm/lead/details/42/"


def test_bitrix_task_and_notify():
    mock = B24Mock()
    b = Bitrix24("https://demo.bitrix24.ru/rest/1/secret", transport=httpx.MockTransport(mock))
    assert b.create_task(42, "Ответить", "текст", important=True) == 7
    b.notify("Новая заявка")
    task = [c for c in mock.calls if c[0] == "tasks.task.add"][0][1]["fields"]
    assert task["PRIORITY"] == 2 and task["UF_CRM_TASK"] == ["L_42"] and task["RESPONSIBLE_ID"] == 1
    assert [c for c in mock.calls if c[0] == "im.notify.personal.add"][0][1]["USER_ID"] == 1


def test_bitrix_error_has_no_secret():
    b = Bitrix24("https://demo.bitrix24.ru/rest/1/supersecret/", transport=httpx.MockTransport(B24Mock(("user.current", "profile"))))
    with pytest.raises(BitrixError) as e:
        b.current_user_id()
    assert "supersecret" not in str(e.value) and "нет прав" in str(e.value)


def test_current_user_falls_back_to_profile_without_user_scope():
    def handler(req: httpx.Request):
        method = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if method == "user.current":
            return httpx.Response(401, json={"error": "insufficient_scope",
                                             "error_description": "The request requires higher privileges"})
        return httpx.Response(200, json={"result": {"ID": "5"}})
    b = Bitrix24("https://x.bitrix24.ru/rest/1/k/", transport=httpx.MockTransport(handler))
    assert b.current_user_id() == 5
