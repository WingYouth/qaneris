"""Deterministic normalization of intent-layer time facts."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, timedelta

from qaneris.contracts.query import TimeRange
from qaneris.semantic.models import RuleExtraction

_RECENT = re.compile(r"(?:最近|近)(\d+|[一二三四五六七八九十百]+)(天|日|个月|月|年)")


def normalize_time_range(rules: RuleExtraction, *, today: date | None = None) -> TimeRange | None:
    """Normalize facts already extracted from the question; never infer a physical time field."""
    if rules.explicit_dates:
        start = rules.explicit_dates[0]
        end = rules.explicit_dates[-1]
        return TimeRange(start=min(start, end), end=max(start, end))
    expression = rules.time_expression
    if not expression:
        return None
    current = today or datetime.now(UTC).date()
    if expression == "今年":
        return TimeRange(start=date(current.year, 1, 1), end=current)
    if expression == "去年":
        return TimeRange(start=date(current.year - 1, 1, 1), end=date(current.year - 1, 12, 31))
    if expression in {"本月", "这个月"}:
        return TimeRange(start=date(current.year, current.month, 1), end=current)
    if expression == "上个月":
        previous = _shift_month(current, -1)
        return TimeRange(
            start=date(previous.year, previous.month, 1),
            end=date(previous.year, previous.month, calendar.monthrange(previous.year, previous.month)[1]),
        )
    if expression in {"本周", "这周"}:
        return TimeRange(start=current - timedelta(days=current.weekday()), end=current)
    if expression == "上周":
        end = current - timedelta(days=current.weekday() + 1)
        return TimeRange(start=end - timedelta(days=6), end=end)
    match = _RECENT.fullmatch(expression)
    if match:
        count = _count(match.group(1))
        unit = match.group(2)
        if unit in {"天", "日"}:
            start = current - timedelta(days=count - 1)
        elif unit in {"个月", "月"}:
            start = _shift_month(current, -count)
        else:
            start = _shift_year(current, -count)
        return TimeRange(start=start, end=current)
    return None


def _shift_month(value: date, offset: int) -> date:
    absolute = value.year * 12 + value.month - 1 + offset
    year, zero_based_month = divmod(absolute, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _shift_year(value: date, offset: int) -> date:
    year = value.year + offset
    return date(year, value.month, min(value.day, calendar.monthrange(year, value.month)[1]))


def _count(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if value == "百":
        return 100
    if "百" in value:
        hundreds, remainder = value.split("百", 1)
        return digits.get(hundreds, 1) * 100 + (_count(remainder) if remainder else 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return digits[value]
