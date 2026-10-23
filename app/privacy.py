"""Слой анонимизации: персональные данные заменяются метками до отправки в ИИ.

Словарь «метка → значение» живёт только в памяти одного запроса.
В журнал пишутся только маскированный текст и количество меток.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --- типы меток -------------------------------------------------------------
NAME, PHONE, EMAIL, ADDRESS, NICK, LINK, INN, DOC = (
    "ИМЯ", "ТЕЛЕФОН", "EMAIL", "АДРЕС", "НИК", "ССЫЛКА", "ИНН", "ДОКУМЕНТ",
)

# --- детекторы ----------------------------------------------------------------
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zа-я]{2,}", re.IGNORECASE)
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:vk\.com|vk\.ru|t\.me|telegram\.me|instagram\.com|ok\.ru|wa\.me)/[\w.\-/?=&%]+",
    re.IGNORECASE,
)
NICK_RE = re.compile(r"(?<![\w@.])@[A-Za-z][\w]{3,31}")
PHONE_RE = re.compile(
    r"(?<![\d+])(?:\+7|8)[\s\-]*\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
)
INN_RE = re.compile(r"(?:ИНН[\s:№]*)?(?<!\d)(?:\d{12}|\d{10})(?!\d)", re.IGNORECASE)
# паспорт РФ (серия и номер) и номер банковской карты (16 цифр)
DOC_RE = re.compile(r"(?<![\d-])(?:\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}|\d{4}\s\d{6})(?![\d-])")

_STREET = (
    r"(?:ул\.|улица|улице|улицы|пр-т|пр-кт|проспект[а-я]*|пер\.|переул[а-я]+|бульвар[а-я]*|б-р|"
    r"шоссе|наб\.|набережн[а-я]+|пл\.|площад[а-я]+|мкр\.?|микрорайон[а-я]*)"
)
_HOUSE = r"(?:(?:,?\s*(?:д\.|дом|д)\s*|,?\s+)\d+[а-яА-Я]?(?!\d|[:.]\d)(?:\s*/\s*\d+)?)"
_FLAT = r"(?:,?\s*(?:кв\.|квартира|кв)\s*\d+)"
_CORP = r"(?:,?\s*(?:корп\.|корпус|к\.|стр\.)\s*\d+)"
_STREET_ADJ = r"(?:(?-i:[А-ЯЁ])[а-яё]+(?:ом|ой|ем|ий|ый|ая|ого|ей|ую|ий)\s+)?"
ADDRESS_RE = re.compile(
    rf"{_STREET_ADJ}{_STREET}(?:\s+|(?<=\.))(?-i:[А-ЯЁA-Z0-9])[\w\-ёЁ]*(?:\s+(?-i:[А-ЯЁ])[\w\-ёЁ]*)?{_HOUSE}?{_CORP}?{_FLAT}?"
    rf"|(?<![\w])(?:д\.|дом)\s*\d+[а-яА-Я]?{_CORP}?{_FLAT}",
    re.IGNORECASE,
)

# Частые русские имена и уменьшительные формы. Совпадение — только с заглавной
# буквы, чтобы не путать с обычными словами. Неоднозначные (Любовь, Надежда,
# Вера, Роман, Лев) не включены: у фотографа «love story» и «роман» — не имена.
_FIRST_NAMES = """
Александр Саша Алексей Лёша Леша Андрей Антон Артём Артем Борис Вадим Валентин Василий Вася
Виктор Витя Виталий Владимир Вова Владислав Влад Всеволод Георгий Гоша Глеб Григорий Гриша Даниил Даниэль Данил Даня
Денис Дмитрий Дима Евгений Женя Егор Иван Ваня Игорь Илья Кирилл Константин Костя Максим Макс
Марк Матвей Михаил Миша Никита Николай Коля Олег Павел Паша Пётр Петр Петя Родион Руслан
Семён Семен Сергей Серёжа Сережа Станислав Стас Степан Тимофей Тимур Фёдор Федор Федя Юрий Юра Ярослав Ян
Александра Алёна Алена Алина Алиса Алла Анастасия Настя Ангелина Анна Аня Антонина Арина Валентина Валерия Лера
Варвара Варя Вероника Виктория Вика Галина Дарья Даша Диана Ева Евгения Екатерина Катя Елена Лена
Елизавета Лиза Жанна Зоя Злата Инна Ирина Ира Карина Кира Кристина Ксения Ксюша Лариса Лилия Лидия
Людмила Люда Маргарита Рита Марина Мария Маша Милана Мила Наталья Наталия Наташа Нина Оксана Олеся Ольга Оля
Полина Регина Светлана Света Софья София Соня Таисия Тамара Татьяна Таня Ульяна Юлия Юля Яна
""".split()

# окончания падежей, которые добавляются к основе имени
_CASE_TAIL = (
    r"(?:а|я|у|ю|е|ой|ей|ом|ем|ы|и|ою|ею|ам|ям|ах|ях|ами|ями|ую|ый|ая|ых|ым|ыми"
    r"|ий|ия|ию|ием|ии|ией|ья|ье|ью|ьи|ьей|ьем|ь)?"
)


def _stem(word: str) -> str:
    """Грубая основа слова: отрезаем окончание, чтобы ловить падежи."""
    w = word.strip()
    for tail in ("ия", "ья", "ий", "ой", "ей", "ая", "ый", "а", "я", "ь", "й", "о", "е", "ы", "и"):
        if len(w) - len(tail) >= 2 and w.lower().endswith(tail):
            return w[: -len(tail)]
    return w


_FIRST_STEMS = sorted({_stem(n) for n in _FIRST_NAMES}, key=len, reverse=True)
FIRST_NAME_RE = re.compile(
    r"(?<![\w])(?:" + "|".join(re.escape(s) for s in _FIRST_STEMS) + r")" + _CASE_TAIL + r"(?![\w])"
)
# Фамилия: слово с заглавной на типичный суффикс. Считается фамилией, только
# если стоит вплотную к имени («Ольга Смирнова»), — иначе под шаблон попадают
# города и прилагательные вроде «Красногорск» и «Пушкинская».
SURNAME_RE = re.compile(
    r"(?<![\w])[А-ЯЁ][а-яё]+(?:(?:ов|ев|ёв|ин|ын)(?:а|у|ой|ым|ом|е|ых|им)?"
    r"|(?:ск|цк)(?:ий|ая|ого|ому|ой|им|ую|их)|(?:ук|юк|енко|ян|дзе|швили)"
    r"|(?:ович|евич|ьич)(?:а|у|ем|е)?|(?:овн|евн|ичн)(?:а|ы|е|у|ой))(?:-(?-i:[А-ЯЁ])[а-яё]+)?(?![\w])"
)
# Имя после слова о родственнике: «сын Лев», «дочка Вера», «жена Анна» — даже если имени нет в словаре
RELATIVE_RE = re.compile(
    r"(?<![\w])(?:сын[а-я]*|дочк[а-я]+|дочер[а-я]+|дочь|ребён[а-я]+|ребен[а-я]+|жен[аыуе]|муж[а-я]*|"
    r"мам[а-я]+|пап[а-я]+|брат[а-я]*|сестр[а-я]+|бабушк[а-я]+|дедушк[а-я]+|внук[а-я]*|внучк[а-я]+|"
    r"племянни[а-я]+|подруг[а-я]+|друг[а-я]*)\s+((?-i:[А-ЯЁ])[а-яё]+)",
    re.IGNORECASE,
)
# «меня зовут Х», «я — Х»
INTRO_RE = re.compile(r"(?:меня зовут|зовут меня|моё имя|мое имя)\s+([А-ЯЁ][а-яё]+)", re.IGNORECASE)


@dataclass
class Span:
    start: int
    end: int
    kind: str


@dataclass
class Masked:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # метка -> исходное значение

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for token in self.mapping:
            kind = token[1:].rsplit("_", 1)[0]
            out[kind] = out.get(kind, 0) + 1
        return out


def _spans(regex: re.Pattern, text: str, kind: str, group: int = 0) -> list[Span]:
    return [Span(m.start(group), m.end(group), kind) for m in regex.finditer(text)]


def _known_name_spans(text: str, known_names: list[str]) -> list[Span]:
    """Имена из полей формы ищем в тексте в любом падеже и регистре."""
    spans: list[Span] = []
    for full in known_names:
        for part in re.split(r"\s+", full or ""):
            part = part.strip(".,;:!?«»\"'")
            if len(part) < 2:
                continue
            stem = re.escape(_stem(part))
            rx = re.compile(rf"(?<![\w]){stem}{_CASE_TAIL}(?![\w])", re.IGNORECASE)
            spans += _spans(rx, text, NAME)
    return spans


def _resolve(spans: list[Span]) -> list[Span]:
    """Убираем пересечения: побеждает раньше начавшийся и более длинный."""
    spans.sort(key=lambda s: (s.start, -(s.end - s.start)))
    out: list[Span] = []
    for s in spans:
        if out and s.start < out[-1].end:
            continue
        out.append(s)
    return out


def _adjacent_surnames(text: str, spans: list[Span]) -> list[Span]:
    names = [s for s in spans if s.kind == NAME]
    out: list[Span] = []
    for m in SURNAME_RE.finditer(text):
        for n in names:
            between = text[n.end : m.start()] if n.end <= m.start() else text[m.end() : n.start]
            if between.strip() == "" and len(between) <= 2:
                out.append(Span(m.start(), m.end(), NAME))
                break
    return out


def _merge_names(text: str, spans: list[Span]) -> list[Span]:
    """«Ольга Смирнова» — одна метка, а не две. «Анна и Дмитрий» — две."""
    merged: list[Span] = []
    for s in spans:
        if (
            merged
            and s.kind == NAME
            and merged[-1].kind == NAME
            and text[merged[-1].end : s.start].strip() == ""
        ):
            merged[-1] = Span(merged[-1].start, s.end, NAME)
        else:
            merged.append(s)
    return merged


def anonymize(text: str, known_names: list[str] | None = None) -> Masked:
    known_names = [n for n in (known_names or []) if n]
    spans: list[Span] = []
    spans += _spans(EMAIL_RE, text, EMAIL)
    spans += _spans(LINK_RE, text, LINK)
    spans += _spans(NICK_RE, text, NICK)
    spans += _spans(PHONE_RE, text, PHONE)
    spans += _spans(INN_RE, text, INN)
    spans += _spans(DOC_RE, text, DOC)
    spans += _spans(ADDRESS_RE, text, ADDRESS)
    spans += _known_name_spans(text, known_names)
    spans += _spans(INTRO_RE, text, NAME, group=1)
    spans += _spans(RELATIVE_RE, text, NAME, group=1)
    spans += _spans(FIRST_NAME_RE, text, NAME)
    spans = _resolve(spans)
    # фамилия и отчество рядом с именем; два прохода ловят «Мария Ивановна Смирнова»
    for _ in range(2):
        spans = _resolve(spans + _adjacent_surnames(text, spans))
    spans = _merge_names(text, spans)

    mapping: dict[str, str] = {}
    by_value: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    parts: list[str] = []
    pos = 0
    for s in spans:
        value = text[s.start : s.end]
        key = (s.kind, value.lower())
        token = by_value.get(key)
        if token is None:
            counters[s.kind] = counters.get(s.kind, 0) + 1
            token = f"[{s.kind}_{counters[s.kind]}]"
            by_value[key] = token
            mapping[token] = value
        parts.append(text[pos : s.start])
        parts.append(token)
        pos = s.end
    parts.append(text[pos:])
    return Masked("".join(parts), mapping)


def add_token(masked: Masked, kind: str, value: str) -> str:
    """Метка для значения из поля формы (имя клиента), чтобы ИИ мог к нему обратиться."""
    for token, v in masked.mapping.items():
        if v.lower() == value.lower() and token.startswith(f"[{kind}_"):
            return token
    n = sum(1 for t in masked.mapping if t.startswith(f"[{kind}_")) + 1
    token = f"[{kind}_{n}]"
    masked.mapping[token] = value
    return token


TOKEN_RE = re.compile(r"\[(?:ИМЯ|ТЕЛЕФОН|EMAIL|АДРЕС|НИК|ССЫЛКА|ИНН|ДОКУМЕНТ)_\d+\]")


def restore(text: str, mapping: dict[str, str]) -> str:
    """Обратная подстановка. Неизвестные метки оставляем как есть — их видно в карточке."""
    if not text:
        return text
    return TOKEN_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def restore_obj(obj, mapping: dict[str, str]):
    if isinstance(obj, str):
        return restore(obj, mapping)
    if isinstance(obj, list):
        return [restore_obj(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: restore_obj(v, mapping) for k, v in obj.items()}
    return obj


def log_masked(masked: Masked, path: Path) -> None:
    """Журнал анонимизации. Исходный текст и значения не пишем — только метки."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    counts = masked.counts()
    kinds = [NAME, PHONE, EMAIL, ADDRESS, NICK, LINK, INN, DOC]
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["время", "маскированный_текст", *kinds])
        # ячейка, начинающаяся с = + - @, в Excel станет формулой
        safe = "'" + masked.text if masked.text[:1] in ("=", "+", "-", "@") else masked.text
        w.writerow([datetime.now().isoformat(timespec="seconds"), safe, *[counts.get(k, 0) for k in kinds]])
