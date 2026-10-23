"""Общий интерфейс провайдера ИИ. Чтобы сменить модель, достаточно новой реализации."""
from __future__ import annotations

import json
import re
from typing import Protocol


class AIError(Exception):
    """Понятная пользователю ошибка ИИ."""


class AIProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        """Вернуть сырой текст ответа модели."""
        ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(raw: str) -> dict:
    """Достаём JSON из ответа: модели любят обрамлять его ```json и пояснениями."""
    text = _FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе модели нет JSON")
    return json.loads(text[start : end + 1])
