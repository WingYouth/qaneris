"""Recognize workspace catalog questions before choosing a query datasource."""

import re


def is_workspace_inventory_question(question: str) -> bool:
    compact = re.sub(r"\s+", "", question).casefold()
    if "工作区" not in compact:
        return False
    inventory = r"(?:数据源|数据库|数据集|数据表|表|数据)"
    return bool(
        re.search(rf"(?:有哪些|有什么|列出|查看|展示|包含|包括).{{0,8}}{inventory}", compact)
        or re.search(rf"{inventory}.{{0,8}}(?:有哪些|有什么|列出|查看|展示)", compact)
    )


def is_selected_source_inventory_question(question: str) -> bool:
    """Treat references to the selected Excel file as a request for its scanned contents."""
    compact = re.sub(r"\s+", "", question).casefold()
    source = r"(?:当前|这个|这份|该|选中(?:的)?|上传(?:的)?)?(?:excel|工作簿|电子表格)"
    description = r"(?:是什么|是啥|有什么|有哪些|包含|包括|列出|展示)"
    contents = r"(?:数据|内容|工作表|表|字段)"
    return bool(
        re.search(rf"{source}.{{0,6}}{description}.{{0,4}}{contents}", compact)
        or re.search(rf"{source}.{{0,4}}{contents}.{{0,4}}{description}", compact)
    )
