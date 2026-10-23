"""Демо-поток событий для дашборда: имитирует день работы приложения без GigaChat и Битрикс24.

События идут с меткой app="photo-leads-demo", поэтому не смешиваются с настоящими:
на дашборде переключите «Приложение» вверху на photo-leads-demo.

Запуск из корня проекта, когда стек мониторинга поднят:
    python monitoring/demo_events.py            # 60 событий раз в 1–3 секунды
    python monitoring/demo_events.py --fast     # 200 событий сразу

Данные синтетические: ни имён, ни телефонов в событиях нет, как и в настоящем приложении.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.loki import LokiHandler, log_event  # noqa: E402

THEMES = ["Свадьба", "Семейная фотосессия", "Индивидуальная фотосессия", "Детская фотосессия",
          "Love story", "Мероприятие"]
TYPES = [("целевой", 70), ("нецелевой", 10), ("жалоба", 5), ("неясно", 15)]
REASONS = ["свадьба: крупный заказ, ведёт фотограф", "дата занята — предложить другую",
           "бюджет заметно ниже стоимости пакета", "нестандартный запрос"]


def one(log: logging.Logger) -> None:
    roll = random.random()
    if roll < 0.06:
        log_event(log, "ai_error", "Заявка не разобрана: GigaChat не ответил за 30 секунд", logging.ERROR,
                  source="текст")
        return
    if roll < 0.09:
        log_event(log, "crm_error", "Битрикс24: 503 Service Unavailable", logging.ERROR)
        return
    if roll < 0.15:
        log_event(log, "bad_input", "Неверные данные: Укажите телефон или email", logging.WARNING)
        return
    if roll < 0.18:
        log_event(log, "manager_saved", "Фотограф сохранил пересчёт в лид", total=random.choice([9500, 18000, 66500]),
                  personal_discount=random.random() < 0.3)
        return
    source = random.choice(["калькулятор", "калькулятор", "текст"])
    lead_type = "целевой" if source == "калькулятор" else random.choices(
        [t for t, _ in TYPES], [w for _, w in TYPES])[0]
    needs_human = lead_type == "жалоба" or random.random() < 0.3
    log_event(
        log, "lead_processed", f"Заявка обработана: {source}, {lead_type}",
        source=source, lead_type=lead_type,
        theme=random.choice(THEMES) if lead_type == "целевой" else "не указано",
        date_status=random.choice(["свободна", "занята", "не указана"]),
        needs_human=needs_human,
        human_reason=("жалоба или конфликт" if lead_type == "жалоба" else random.choice(REASONS)) if needs_human else "",
        crm="ok", crm_warnings=0, pii_masked=random.randint(0, 5) if source == "текст" else random.randint(0, 2),
        ms=random.randint(1500, 9000) if source == "текст" else random.randint(5, 4000),
        ai="ok",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3100/loki/api/v1/push")
    ap.add_argument("--count", type=int)
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()
    count = args.count or (200 if args.fast else 60)

    handler = LokiHandler(args.url, {"app": "photo-leads-demo", "env": "demo"})
    log = logging.getLogger("demo")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler())

    log_event(log, "app_started", "Демо: приложение запущено")
    for _ in range(count):
        one(log)
        if not args.fast:
            time.sleep(random.uniform(1, 3))
    handler.close()
    print(f"Готово: отправлено {handler.sent}, потеряно {handler.dropped}.")
    if handler.dropped:
        print("Loki не принял записи — проверьте, что стек запущен: docker compose ps (в папке monitoring).")


if __name__ == "__main__":
    main()
