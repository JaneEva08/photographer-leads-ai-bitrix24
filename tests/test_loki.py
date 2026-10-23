"""Отправка логов в Loki: формат запроса, маскировка, поведение при сбоях, события пайплайна.

Настоящий Loki не нужен: запросы перехватывает httpx.MockTransport.
"""
import json
import logging
import time
from datetime import date

import httpx
import pytest

from app import rules
from app.loki import LokiHandler, log_event, scrub, setup_loki
from app.pipeline import process, process_calc
from tests.test_calc import calc_req
from tests.test_pipeline import WEDDING, FakeAI, wedding_form

URL = "http://loki.test/loki/api/v1/push"
LABELS = {"app": "photo-leads-ai", "env": "test"}


class LokiMock:
    """Принимает push как Loki: проверяет формат и отвечает 204."""

    def __init__(self, status=204, error: Exception | None = None):
        self.status, self.error, self.payloads = status, error, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.error:
            raise self.error
        assert request.method == "POST" and request.url.path == "/loki/api/v1/push"
        self.payloads.append(json.loads(request.content))
        return httpx.Response(self.status, text="" if self.status < 300 else "bad request")

    def lines(self) -> list[tuple[dict, str, dict]]:
        return [(s["stream"], ts, json.loads(line))
                for p in self.payloads for s in p["streams"] for ts, line in s["values"]]


@pytest.fixture
def loki():
    """Обработчик на отдельном логгере, чтобы не задевать остальные тесты."""
    mock = LokiMock()
    h = LokiHandler(URL, LABELS, flush_interval=0.05, client=httpx.Client(transport=httpx.MockTransport(mock)))
    logger = logging.getLogger("test.loki")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(h)
    yield mock, h, logger
    logger.removeHandler(h)
    h.close()


def test_payload_format_like_lesson(loki):
    mock, h, logger = loki
    logger.info("Приложение запущено")
    h.flush()
    stream, ts, line = mock.lines()[0]
    assert stream == {"app": "photo-leads-ai", "env": "test", "level": "INFO", "event": "log"}
    assert ts.isdigit() and len(ts) == 19  # наносекунды строкой
    assert abs(int(ts) / 1e9 - time.time()) < 60
    assert line["msg"] == "Приложение запущено" and line["logger"] == "test.loki"


def test_streams_grouped_by_labels_and_sent_in_one_batch(loki):
    mock, h, logger = loki
    logger.info("раз")
    logger.info("два")
    logger.error("ошибка")
    h.flush()
    assert len(mock.payloads) == 1
    streams = {s["stream"]["level"]: len(s["values"]) for s in mock.payloads[0]["streams"]}
    assert streams == {"INFO": 2, "ERROR": 1}


def test_event_becomes_label_and_fields_go_to_json(loki):
    mock, h, logger = loki
    log_event(logger, "lead_processed", "Заявка обработана", lead_type="целевой", needs_human=True, ms=120)
    h.flush()
    stream, _, line = mock.lines()[0]
    assert stream["event"] == "lead_processed"
    assert line["lead_type"] == "целевой" and line["needs_human"] is True and line["ms"] == 120
    # поля — не метки: иначе индекс Loki разрастается
    assert "lead_type" not in stream


def test_phones_and_emails_are_masked(loki):
    mock, h, logger = loki
    logger.error("Битрикс24 вернул: клиент +7 (916) 123-45-67, anna.test@example.com")
    log_event(logger, "x", "поле", note="звонить 8 900 000 00 01")
    h.flush()
    sent = json.dumps(mock.payloads, ensure_ascii=False)
    assert "123-45-67" not in sent and "anna.test" not in sent and "000 00 01" not in sent
    assert "[ТЕЛЕФОН]" in sent and "[EMAIL]" in sent
    assert scrub("12.06.2027, 70 000 ₽, лид 42") == "12.06.2027, 70 000 ₽, лид 42"  # даты и суммы не трогаем


def test_exception_type_and_message_without_traceback(loki):
    mock, h, logger = loki
    try:
        raise ValueError("сломалось")
    except ValueError:
        logger.exception("сбой")
    h.flush()
    assert mock.lines()[0][2]["error"] == "ValueError: сломалось"


def test_httpx_logs_are_not_sent(loki):
    mock, h, _ = loki
    h.handle(logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: POST %s", (URL,), None))
    h.handle(logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "127.0.0.1 GET /", None, None))
    h.flush()
    assert mock.payloads == []


@pytest.mark.parametrize("mock", [LokiMock(error=httpx.ConnectError("refused")), LokiMock(status=400)])
def test_loki_down_does_not_break_app(mock, capsys):
    h = LokiHandler(URL, LABELS, flush_interval=0.05, client=httpx.Client(transport=httpx.MockTransport(mock)))
    logger = logging.getLogger("test.loki.down")
    logger.propagate = False
    logger.addHandler(h)
    started = time.monotonic()
    for i in range(100):
        logger.info("событие %s", i)
    assert time.monotonic() - started < 0.5  # запись в лог не ждёт Loki
    h.flush()
    assert h.dropped == 100 and h.sent == 0
    assert "[loki]" in capsys.readouterr().err  # предупреждение в консоль, без исключения
    logger.removeHandler(h)
    h.close()


def test_full_queue_drops_instead_of_blocking():
    gate = []

    def slow(request):
        gate.append(1)
        time.sleep(0.3)
        return httpx.Response(204)

    h = LokiHandler(URL, LABELS, max_queue=5, batch_size=1, flush_interval=0.01,
                    client=httpx.Client(transport=httpx.MockTransport(slow)))
    rec = logging.LogRecord("test", logging.INFO, "", 0, "x", None, None)
    started = time.monotonic()
    for _ in range(50):
        h.handle(rec)
    assert time.monotonic() - started < 0.2
    assert h.dropped >= 40
    h.close()


def test_setup_without_url_does_nothing():
    before = list(logging.getLogger().handlers)
    assert setup_loki("", LABELS) is None
    assert logging.getLogger().handlers == before


# ---- события пайплайна -----------------------------------------------------------------

def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def test_lead_event_has_categories_but_no_personal_data(caplog):
    caplog.set_level(logging.INFO)
    env = dict(prices=rules.Prices.load(), calendar=rules.Calendar.load(), today=date(2026, 10, 23))
    process(wedding_form(), FakeAI([WEDDING]), **env)
    [rec] = _events(caplog, "lead_processed")
    f = rec.fields
    assert f["source"] == "текст" and f["lead_type"] == "целевой" and f["theme"] == "Свадьба"
    assert f["needs_human"] is True and "свадьба" in f["human_reason"]
    assert f["crm"] == "off" and f["pii_masked"] >= 3 and f["ms"] >= 0
    dump = json.dumps(f, ensure_ascii=False) + rec.getMessage()
    for raw in ("Анна", "Дмитрий", "916", "anna.test", "Подмосков"):
        assert raw not in dump


def test_calc_event_and_ai_state(caplog):
    caplog.set_level(logging.INFO)
    prices, cal = rules.Prices.load(), rules.Calendar.load()
    process_calc(calc_req(), None, prices, cal, date(2026, 10, 23))
    [rec] = _events(caplog, "lead_processed")
    assert rec.fields["source"] == "калькулятор" and rec.fields["ai"] == "not_needed"
    assert rec.fields["total"] > 0


def test_bad_input_logged_as_warning(caplog):
    from fastapi.testclient import TestClient

    from app import main
    caplog.set_level(logging.INFO)
    with TestClient(main.app) as c:
        c.post("/api/calc/quote", json={"theme": "Нет такой", "hours": 1})
    assert _events(caplog, "app_started")
    [rec] = _events(caplog, "bad_input")
    assert rec.levelno == logging.WARNING
