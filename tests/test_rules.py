from datetime import date

import pytest

from app import rules

TODAY = date(2026, 10, 23)


@pytest.fixture
def prices():
    return rules.Prices.load()


@pytest.fixture
def cal():
    return rules.Calendar(full_days={"2027-06-12"}, sessions={"2027-05-16": 3, "2027-05-23": 1})


# ---- цена ----------------------------------------------------------------------

@pytest.mark.parametrize("key,amount", [
    ("individual", 6000), ("family", 7000), ("kids", 6500),
    ("love_story", 9000), ("wedding", 40000), ("event", 10000),
])
def test_base_price_each_theme(prices, key, amount):
    e = rules.estimate_price(prices, key, prices.themes[key]["base_hours"])
    assert e.amount == amount and not e.is_from


def test_extra_hours(prices):
    # свадьба 8 ч: 40 000 + 4 ч × 7 000
    assert rules.estimate_price(prices, "wedding", 8).amount == 68000
    # семейная 1,5 ч: 7 000 + 0,5 × 5 000
    assert rules.estimate_price(prices, "family", 1.5).amount == 9500


def test_shorter_than_base_is_base(prices):
    assert rules.estimate_price(prices, "wedding", 2).amount == 40000


def test_out_of_town_and_urgent(prices):
    # (7 000 + 2 000) × 1,3 = 11 700
    e = rules.estimate_price(prices, "family", 1, out_of_town=True, urgent=True)
    assert e.amount == 11700
    assert "выезд" in e.text and "срочная" in e.text


def test_unknown_duration_is_from(prices):
    e = rules.estimate_price(prices, "love_story", None)
    assert e.amount == 9000 and e.is_from and e.text.startswith("от ")


def test_no_theme_no_price(prices):
    e = rules.estimate_price(prices, None, 2)
    assert e.amount is None


def test_key_by_title(prices):
    assert prices.key_by_title("Свадьба") == "wedding"
    assert prices.key_by_title("love story") == "love_story"
    assert prices.key_by_title("не указано") is None


# ---- дата ----------------------------------------------------------------------

def test_date_unknown(prices, cal):
    assert rules.date_status(cal, prices, "family", None, TODAY) == rules.DATE_UNKNOWN
    assert rules.date_status(cal, prices, "family", "не дата", TODAY) == rules.DATE_UNKNOWN


def test_date_past(prices, cal):
    assert rules.date_status(cal, prices, "family", "2026-01-10", TODAY) == rules.DATE_PAST


def test_full_day_busy_for_everyone(prices, cal):
    assert rules.date_status(cal, prices, "wedding", "2027-06-12", TODAY) == rules.DATE_BUSY
    assert rules.date_status(cal, prices, "family", "2027-06-12", TODAY) == rules.DATE_BUSY


def test_sessions_limit(prices, cal):
    assert rules.date_status(cal, prices, "family", "2027-05-16", TODAY) == rules.DATE_BUSY  # уже 3
    assert rules.date_status(cal, prices, "family", "2027-05-23", TODAY) == rules.DATE_FREE  # 1 из 3


def test_wedding_not_on_day_with_sessions(prices, cal):
    assert rules.date_status(cal, prices, "wedding", "2027-05-23", TODAY) == rules.DATE_BUSY
    assert rules.date_status(cal, prices, "wedding", "2027-05-22", TODAY) == rules.DATE_FREE


# ---- передача человеку ----------------------------------------------------------

def _decide(prices, **kw):
    args = dict(theme_key="family", lead_type="целевой", is_complaint=False, is_nonstandard=False,
                status=rules.DATE_FREE, budget=None, estimate=rules.Estimate(7000, False, ""))
    args.update(kw)
    return rules.human_decision(prices, **args)


def test_simple_session_no_human(prices):
    assert _decide(prices) == (False, "не требуется")


@pytest.mark.parametrize("kw,needle", [
    ({"is_complaint": True}, "жалоба"),
    ({"theme_key": "wedding"}, "свадьба"),
    ({"theme_key": "event"}, "мероприятие"),
    ({"is_nonstandard": True}, "нестандартный"),
    ({"status": rules.DATE_BUSY}, "дата занята"),
    ({"budget": 4000}, "бюджет"),
])
def test_human_reasons(prices, kw, needle):
    need, reason = _decide(prices, **kw)
    assert need and needle in reason


def test_budget_within_30_percent_is_fine(prices):
    assert _decide(prices, budget=5000)[0] is False  # 5000 ≥ 0,7 × 7000


def test_nontarget_busy_date_no_human(prices):
    assert _decide(prices, lead_type="нецелевой", status=rules.DATE_BUSY, theme_key=None)[0] is False
