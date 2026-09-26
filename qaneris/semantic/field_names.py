"""Conservative Chinese labels for scanned English field names.

These are lexical hints for discovery, never governed business definitions or time axes.
Unknown tokens yield no translation so a partial name cannot be mistaken for a full match.
"""

from __future__ import annotations

import re

_TOKENS = {
    "amount": "金额",
    "category": "分类",
    "created": "创建",
    "customer": "客户",
    "date": "日期",
    "event": "事件",
    "id": "ID",
    "name": "名称",
    "order": "订单",
    "product": "商品",
    "quantity": "数量",
    "source": "来源",
    "status": "状态",
    "time": "时间",
    "type": "类型",
    "updated": "更新",
}


def field_name_aliases(path: str) -> tuple[str, ...]:
    """Return an exact whole-name hint for a simple snake_case field."""
    name = path.rsplit(".", 1)[-1].casefold()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name):
        return ()
    parts = name.split("_")
    if any(part not in _TOKENS for part in parts):
        return ()
    return ("".join(_TOKENS[part] for part in parts),)
