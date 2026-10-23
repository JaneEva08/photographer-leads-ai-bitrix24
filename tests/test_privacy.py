import csv

import pytest

from app import privacy as p


def masked(text, names=None):
    return p.anonymize(text, names)


# ---- телефоны -----------------------------------------------------------------

@pytest.mark.parametrize("phone", [
    "+7 999 123-45-67", "8 (916) 1234567", "89161234567", "+7-916-123-45-67",
    "+7(916)123 45 67", "8 916 123 45 67",
])
def test_phone_formats(phone):
    m = masked(f"Звоните {phone}, жду.")
    assert m.text == "Звоните [ТЕЛЕФОН_1], жду."
    assert m.mapping["[ТЕЛЕФОН_1]"] == phone


def test_email_nick_link():
    m = masked("Почта olga@test.ru, ник @olga_test, страница vk.com/id12345 и https://t.me/olga_photo")
    assert "[EMAIL_1]" in m.text and "[НИК_1]" in m.text
    assert m.text.count("[ССЫЛКА_") == 2
    assert "olga" not in m.text


def test_inn():
    m = masked("ИНН 7701234567, для ИП 500100732259")
    assert "[ИНН_1]" in m.text and "[ИНН_2]" in m.text
    assert "7701234567" not in m.text


@pytest.mark.parametrize("addr", [
    "ул. Лесной, д. 5, кв. 12", "ул.Лесная 5", "пр-т Мира, д. 10/2, корп. 3, кв. 7",
    "улица Новый Арбат, дом 1",
])
def test_addresses(addr):
    m = masked(f"Приезжайте: {addr}.")
    assert m.mapping.get("[АДРЕС_1]") == addr


# ---- имена --------------------------------------------------------------------

def test_full_name_one_token():
    m = masked("Меня зовут Ольга Смирнова.")
    assert m.text == "Меня зовут [ИМЯ_1]."
    assert m.mapping["[ИМЯ_1]"] == "Ольга Смирнова"


def test_full_name_with_patronymic():
    m = masked("Иванов Пётр Сергеевич просит перезвонить")
    assert m.text == "[ИМЯ_1] просит перезвонить"


def test_couple_two_tokens():
    m = masked("Мы Анна и Дмитрий, женимся летом")
    assert m.text == "Мы [ИМЯ_1] и [ИМЯ_2], женимся летом"


def test_child_name_in_case():
    m = masked("сыну Мише 3 года, дочке Дашеньке нет")  # уменьшительно-ласкательные не все
    assert "Мише" not in m.text


def test_form_name_any_case_and_lowercase():
    m = masked("Это снова ольга, Ольгой меня зовут, спросите Смирновой", names=["Ольга Смирнова"])
    for w in ("ольга", "Ольгой", "Смирновой"):
        assert w not in m.text


def test_same_value_same_token():
    m = masked("+7 999 123-45-67 или +7 999 123-45-67")
    assert m.text == "[ТЕЛЕФОН_1] или [ТЕЛЕФОН_1]"


# ---- не маскируем то, что нужно для разбора --------------------------------------

def test_keeps_dates_money_places():
    text = ("Свадьба 12 июня 2027, регистрация в 12:00, 8 часов, бюджет 70 000 ₽, "
            "Подмосковье, живём в Красногорске, снимать на Пушкинской набережной, love story, роман")
    m = masked(text)
    assert m.text == text
    assert m.mapping == {}


# ---- подстановка и журнал --------------------------------------------------------

def test_restore_roundtrip():
    src = "Меня зовут Ольга Смирнова, телефон 8 (926) 765-43-21"
    m = masked(src)
    draft = "Здравствуйте, [ИМЯ_1]! Перезвоню на [ТЕЛЕФОН_1]. Метка [ИМЯ_9] неизвестна."
    out = p.restore(draft, m.mapping)
    assert "Ольга Смирнова" in out and "8 (926) 765-43-21" in out and "[ИМЯ_9]" in out


def test_add_token_for_form_name():
    m = masked("Хотим фотосессию")
    t = p.add_token(m, p.NAME, "Екатерина")
    assert t == "[ИМЯ_1]" and m.mapping[t] == "Екатерина"


def test_log_has_no_raw_values(tmp_path):
    log = tmp_path / "privacy_log.csv"
    m = masked("Ольга Смирнова, +7 999 123-45-67, olga@test.ru")
    p.log_masked(m, log)
    content = log.read_text(encoding="utf-8-sig")
    for raw in ("Ольга", "Смирнова", "999", "olga@"):
        assert raw not in content
    rows = list(csv.reader(content.splitlines(), delimiter=";"))
    assert rows[0][:2] == ["время", "маскированный_текст"]
    assert rows[1][2:5] == ["1", "1", "1"]  # имя, телефон, email
