"""Отправка логов в Grafana Loki напрямую, без агента Promtail: POST /loki/api/v1/push.

Как устроено:
- Это обычный обработчик стандартного logging. Всё, что приложение пишет через logging,
  попадает и в консоль, и в Loki. Отдельная функция в каждом эндпоинте не нужна.
- Приложение не ждёт Loki. Запись кладётся в очередь, фоновый поток отправляет пачками
  раз в секунду. Loki упал или тормозит — заявки клиентов всё равно обрабатываются,
  записи для Loki теряются, в консоль пишется предупреждение не чаще раза в минуту.
- Метки (labels) — только с короткими списками значений: app, env, level, event. Loki индексирует
  метки, и метка с тысячами значений (id лида, телефон) раздувает индекс. Подробности события
  идут в саму строку лога в формате JSON и достаются в Grafana через `| json`.
- Персональные данные в Loki не уходят. Заявки пишутся событиями с категориями, без текста
  и контактов, а телефоны и email в любой строке маскируются ещё раз на всякий случай.
"""
from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from typing import Any

import httpx

from .privacy import EMAIL_RE, PHONE_RE

# httpx пишет в лог каждый свой запрос, в том числе запрос в Loki: без фильтра получился бы бесконечный цикл.
# uvicorn.access содержит IP-адреса посетителей — это лишние персональные данные.
SKIP_LOGGERS = ("httpx", "httpcore", "uvicorn.access", __name__)


def scrub(text: str) -> str:
    """Вторая линия защиты: маскирует телефоны и email, если они случайно попали в сообщение."""
    return PHONE_RE.sub("[ТЕЛЕФОН]", EMAIL_RE.sub("[EMAIL]", text))


def log_event(logger: logging.Logger, event: str, message: str, level: int = logging.INFO, **fields: Any) -> None:
    """Бизнес-событие: короткое имя (станет меткой event) + поля для дашборда.
    В поля кладём только категории и числа: тип заявки, статус CRM, время обработки. Не текст и не контакты."""
    logger.log(level, message, extra={"event": event, "fields": fields})


class LokiHandler(logging.Handler):
    def __init__(
        self,
        url: str,
        labels: dict[str, str],
        *,
        timeout: float = 3.0,
        batch_size: int = 200,
        flush_interval: float = 1.0,
        max_queue: int = 10_000,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__()
        self.url = url
        self.labels = {k: str(v) for k, v in labels.items()}
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.sent = 0
        self.dropped = 0
        self._q: queue.Queue = queue.Queue(max_queue)
        self._client = client or httpx.Client(timeout=timeout)
        self._last_warning = 0.0
        self._stop = threading.Event()
        self.addFilter(lambda r: not r.name.startswith(SKIP_LOGGERS))
        self._thread = threading.Thread(target=self._run, name="loki-sender", daemon=True)
        self._thread.start()

    # --- запись в очередь (выполняется в потоке запроса, должна быть мгновенной) ---

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = self._entry(record)
        except Exception:  # noqa: BLE001 — сбой форматирования не должен ронять приложение
            self.handleError(record)
            return
        try:
            self._q.put_nowait(entry)
        except queue.Full:
            self.dropped += 1

    def _entry(self, record: logging.LogRecord) -> tuple[tuple, list[str]]:
        labels = dict(self.labels, level=record.levelname, event=getattr(record, "event", "log"))
        line: dict[str, Any] = {"msg": scrub(record.getMessage()), "logger": record.name}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                line[k] = scrub(v) if isinstance(v, str) else v
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            line["error"] = scrub(f"{type(exc).__name__}: {exc}")
        ts = str(int(record.created * 1_000_000_000))  # Loki ждёт наносекунды строкой
        return tuple(sorted(labels.items())), [ts, json.dumps(line, ensure_ascii=False, default=str)]

    # --- отправка (фоновый поток) ---

    def _run(self) -> None:
        while True:
            batch = self._take_batch()
            if batch:
                self._send(batch)
                for _ in batch:
                    self._q.task_done()
            elif self._stop.is_set():
                return

    def _take_batch(self) -> list:
        batch: list = []
        deadline = time.monotonic() + self.flush_interval
        while len(batch) < self.batch_size:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                batch.append(self._q.get(timeout=min(left, 0.2)))
            except queue.Empty:
                if self._stop.is_set():
                    break
        return batch

    def build_payload(self, batch: list) -> dict:
        streams: dict[tuple, list] = {}
        for key, value in batch:
            streams.setdefault(key, []).append(value)
        return {"streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]}

    def _send(self, batch: list) -> None:
        try:
            r = self._client.post(self.url, json=self.build_payload(batch))
        except Exception as e:  # noqa: BLE001 — фоновый поток не должен умирать ни от какой ошибки
            self._fail(len(batch), f"Loki недоступен ({type(e).__name__}): {e}")
            return
        if r.status_code >= 300:
            self._fail(len(batch), f"Loki ответил {r.status_code}: {r.text[:200]}")
            return
        self.sent += len(batch)

    def _fail(self, n: int, reason: str) -> None:
        self.dropped += n
        now = time.monotonic()
        if now - self._last_warning > 60:
            self._last_warning = now
            # не через logging: иначе сообщение о сбое Loki само встанет в очередь в Loki
            print(f"[loki] {reason}. Записей потеряно всего: {self.dropped}", file=sys.stderr)

    # --- завершение ---

    def flush(self, timeout: float = 5.0) -> None:
        """Дождаться отправки накопленного (для остановки приложения и тестов)."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        self.flush()
        self._stop.set()
        self._thread.join(timeout=self.flush_interval + 1)
        self._client.close()
        super().close()


def setup_loki(url: str, labels: dict[str, str], level: int = logging.INFO) -> LokiHandler | None:
    """Подключает отправку в Loki к корневому логгеру. Пустой адрес — Loki выключен, приложение работает как раньше."""
    if not url:
        return None
    handler = LokiHandler(url, labels)
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)
    # httpx пишет в консоль строку на каждую отправку в Loki (раз в секунду) — прячем только их
    logging.getLogger("httpx").addFilter(lambda r: url not in r.getMessage())
    return handler
