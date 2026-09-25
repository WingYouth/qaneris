"""Deterministic ``.xlsx`` validation and row streaming.

The work is deliberately split in two phases so a large workbook is never held in memory as Python
objects:

1. :func:`read_container` — extension, size, ZIP/OOXML container and ZIP-bomb limits.
2. :func:`validate_workbook` — visible/empty sheet rules, formula and merged-range rejection,
   header and identifier rules, resource limits and per-column type inference.

:func:`iter_table_rows` re-opens the workbook from the same immutable bytes with the schema decided
in phase 2, so the plan and the written rows always describe one artifact, and the materializer only
counts what it writes.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from qaneris.ingestion.excel.contracts import ExcelErrorCode, ExcelIngestionError
from qaneris.ingestion.excel.policy import DEFAULT_EXCEL_POLICY, ExcelIngestionPolicy
from qaneris.ingestion.excel.types import (
    CellKind,
    ColumnType,
    UnsupportedCellValue,
    canonical_text,
    classify_cell,
    coerce_cell,
    infer_column_type,
)

WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
CONTENT_TYPES_PART = "[Content_Types].xml"
VBA_PART = "xl/vbaproject.bin"

# Cell value types Roadshow v1 can represent. ``f`` (formula) and ``e`` (error) are rejected.
REPRESENTABLE_CELL_TYPES = frozenset({"n", "s", "b", "d"})

_XML_CHUNK_BYTES = 65_536
_XML_CHUNK_OVERLAP = 64

_ROW_REFERENCE = re.compile(rb"<(?:\w+:)?row\s+r=\"(\d+)\"")
_CELL_REFERENCE = re.compile(rb"<(?:\w+:)?c\s+r=\"([A-Za-z]+)(\d+)\"")
_MERGE_ELEMENT = re.compile(rb"<(?:\w+:)?mergeCell[\s/>]")


@dataclass(frozen=True)
class ContainerInfo:
    file_size: int
    zip_entries: int
    uncompressed_size: int


@dataclass(frozen=True)
class SheetXmlFacts:
    """Facts read from the raw worksheet XML, independent of the declared dimension."""

    row_extent: int
    column_extent: int
    has_merged_range: bool


_NO_FACTS = SheetXmlFacts(0, 0, False)


@dataclass(frozen=True)
class ColumnPlan:
    source_header: str
    column_name: str
    inferred_type: ColumnType
    nullable: bool
    all_null: bool


@dataclass(frozen=True)
class SheetPlan:
    original_name: str
    table_name: str
    columns: tuple[ColumnPlan, ...]
    row_count: int
    row_extent: int
    column_extent: int


@dataclass(frozen=True)
class WorkbookPlan:
    data: bytes
    file_size: int
    sheets: tuple[SheetPlan, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _StoredCell:
    column: int
    value: Any
    data_type: str
    number_format: str | None


@dataclass(frozen=True)
class _HeaderColumn:
    column: int
    source: str
    name: str


# --------------------------------------------------------------------------------------------
# Phase 1 — container preflight
# --------------------------------------------------------------------------------------------


def read_container(
    data: bytes, filename: str, policy: ExcelIngestionPolicy = DEFAULT_EXCEL_POLICY
) -> ContainerInfo:
    """Validate the extension, size and ZIP/OOXML container before openpyxl ever sees the file.

    The workbook is never extracted to a temporary directory and no external link is followed.
    """
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix != ".xlsx":
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_EXTENSION,
            f"Excel 导入只接受 ZIP-based OOXML .xlsx（收到扩展名：{suffix or '无'}）",
        )
    if len(data) > policy.max_file_size_bytes:
        raise ExcelIngestionError(
            ExcelErrorCode.FILE_TOO_LARGE,
            f"文件大小 {len(data)} 字节超过上限 {policy.max_file_size_bytes} 字节",
        )
    with _open_archive(data) as archive:
        names = archive.namelist()
        entries = len(names)
        if entries > policy.max_zip_entries:
            raise ExcelIngestionError(
                ExcelErrorCode.ZIP_LIMIT_EXCEEDED,
                f"ZIP 条目数 {entries} 超过上限 {policy.max_zip_entries}",
            )
        uncompressed = 0
        for item in archive.infolist():
            uncompressed += item.file_size
            if uncompressed > policy.max_uncompressed_size_bytes:
                raise ExcelIngestionError(
                    ExcelErrorCode.ZIP_LIMIT_EXCEEDED,
                    f"ZIP 解压总大小超过上限 {policy.max_uncompressed_size_bytes} 字节",
                )
        if CONTENT_TYPES_PART not in names or WORKBOOK_PART not in names:
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "缺少 OOXML 必需部件，不是有效的 .xlsx 工作簿"
            )
        if VBA_PART in {name.lower() for name in names}:
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "文件包含 VBA 宏（.xlsm），拒绝导入"
            )
        content_types = _read_part(archive, CONTENT_TYPES_PART)
        if b"macroEnabled" in content_types:
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "文件声明为宏工作簿（.xlsm），拒绝导入"
            )
        if b"spreadsheetml" not in content_types:
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "文件不是电子表格 OOXML 包"
            )
    return ContainerInfo(
        file_size=len(data), zip_entries=entries, uncompressed_size=uncompressed
    )


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, OSError) as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CONTAINER, "文件不是可读取的 ZIP/OOXML 容器"
        ) from error


def _read_part(archive: zipfile.ZipFile, part: str) -> bytes:
    try:
        return archive.read(part)
    except (KeyError, NotImplementedError, RuntimeError, OSError) as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CONTAINER, "无法读取 OOXML 必需部件"
        ) from error


# --------------------------------------------------------------------------------------------
# Phase 2 — workbook / sheet validation
# --------------------------------------------------------------------------------------------


def validate_workbook(
    data: bytes, policy: ExcelIngestionPolicy = DEFAULT_EXCEL_POLICY
) -> WorkbookPlan:
    """Validate every visible sheet and decide the SQLite schema, or fail closed."""
    with _open_archive(data) as archive:
        parts = _sheet_parts(archive)
        facts = {name: _sheet_xml_facts(archive, part) for name, part in parts.items()}
    try:
        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
    except ExcelIngestionError:
        raise
    except Exception as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CONTAINER, "无法打开 .xlsx 工作簿"
        ) from error
    try:
        return _validate_workbook(workbook, data, facts, policy)
    finally:
        workbook.close()


def _validate_workbook(
    workbook: Any,
    data: bytes,
    facts: dict[str, SheetXmlFacts],
    policy: ExcelIngestionPolicy,
) -> WorkbookPlan:
    names = list(workbook.sheetnames)
    states = {name: workbook[name].sheet_state for name in names}
    visible = [name for name in names if states[name] == "visible"]
    if len(visible) > policy.max_visible_sheets:
        raise ExcelIngestionError(
            ExcelErrorCode.SHEET_LIMIT_EXCEEDED,
            f"可见工作表数量 {len(visible)} 超过上限 {policy.max_visible_sheets}",
        )
    warnings: list[str] = []
    sheets: list[SheetPlan] = []
    table_names: set[str] = set()
    data_cells = 0
    for name in names:
        state = states[name]
        if state != "visible":
            warnings.append(f"跳过非可见工作表“{name}”（state={state}）")
            continue
        plan, counted, sheet_warnings = _validate_sheet(
            workbook[name], name, facts.get(name, _NO_FACTS), policy, table_names
        )
        warnings.extend(sheet_warnings)
        if plan is None:
            continue
        data_cells += counted
        if data_cells > policy.max_total_data_cells:
            raise ExcelIngestionError(
                ExcelErrorCode.CELL_LIMIT_EXCEEDED,
                f"非空数据单元格总数超过上限 {policy.max_total_data_cells}",
                sheet=name,
            )
        sheets.append(plan)
    if not sheets:
        raise ExcelIngestionError(
            ExcelErrorCode.NO_IMPORTABLE_SHEET, "工作簿中没有可导入的工作表"
        )
    return WorkbookPlan(
        data=data, file_size=len(data), sheets=tuple(sheets), warnings=tuple(warnings)
    )


def _validate_sheet(
    worksheet: Any,
    sheet_name: str,
    sheet_facts: SheetXmlFacts,
    policy: ExcelIngestionPolicy,
    table_names: set[str],
) -> tuple[SheetPlan | None, int, list[str]]:
    try:
        return _scan_sheet(worksheet, sheet_name, sheet_facts, policy, table_names)
    except ExcelIngestionError:
        raise
    except Exception as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CONTAINER,
            f"无法读取工作表内容（{type(error).__name__}）",
            sheet=sheet_name,
        ) from error


def _scan_sheet(
    worksheet: Any,
    sheet_name: str,
    sheet_facts: SheetXmlFacts,
    policy: ExcelIngestionPolicy,
    table_names: set[str],
) -> tuple[SheetPlan | None, int, list[str]]:
    # The extent comes from the real ``<row r>`` / ``<c r>`` references, so a stale or hostile
    # ``<dimension>`` cannot force a padding blow-up or hide trailing content.
    row_extent = sheet_facts.row_extent or (worksheet.max_row or 0)
    column_extent = sheet_facts.column_extent or (worksheet.max_column or 0)
    if row_extent > policy.max_rows_per_sheet:
        raise ExcelIngestionError(
            ExcelErrorCode.ROW_LIMIT_EXCEEDED,
            f"工作表行范围 {row_extent} 超过上限 {policy.max_rows_per_sheet}",
            sheet=sheet_name,
        )
    if column_extent > policy.max_columns_per_sheet:
        raise ExcelIngestionError(
            ExcelErrorCode.COLUMN_LIMIT_EXCEEDED,
            f"工作表列范围 {column_extent} 超过上限 {policy.max_columns_per_sheet}",
            sheet=sheet_name,
        )
    if row_extent <= 0 or column_extent <= 0:
        return None, 0, [f"跳过空工作表“{sheet_name}”"]

    header: dict[int, _HeaderColumn] = {}
    header_error: ExcelIngestionError | None = None
    shape_error: ExcelIngestionError | None = None
    formula: str | None = None
    has_content = False
    data_rows = 0
    data_cells = 0
    width = 0
    kinds: dict[int, list[CellKind]] = {}
    null_columns: set[int] = set()

    rows = worksheet.iter_rows(
        min_row=1, max_row=row_extent, min_col=1, max_col=column_extent
    )
    for row_index, raw_row in enumerate(rows, start=1):
        cells = _stored_cells(raw_row)
        if not cells:
            continue
        has_content = True
        for cell in cells:
            if cell.data_type == "f":
                formula = _coordinate(cell.column, row_index)
                break
        if formula is not None:
            break
        if row_index == 1:
            if not cells:
                # Row 1 is the header position, always. A title row or a blank leading row is a
                # rejected shape, not something to search past.
                header_error = ExcelIngestionError(
                    ExcelErrorCode.INVALID_HEADER,
                    "第 1 行（Header）不能为空",
                    sheet=sheet_name,
                )
                continue
            row_width = max(cell.column for cell in cells)
            header, header_error = _parse_header(cells, row_width, sheet_name, policy)
            if header_error is None:
                width = row_width
            continue
        data_rows += 1
        data_cells += len(cells)
        if header_error is not None:
            continue
        if shape_error is None:
            beyond = [cell for cell in cells if cell.column > width]
            if beyond:
                shape_error = ExcelIngestionError(
                    ExcelErrorCode.INVALID_HEADER,
                    "数据行出现 Header 范围之外的非空单元格",
                    sheet=sheet_name,
                    coordinate=_coordinate(beyond[0].column, row_index),
                )
        present = {cell.column: cell for cell in cells}
        for column in range(1, width + 1):
            cell = present.get(column)
            if cell is None:
                null_columns.add(column)
                continue
            kinds.setdefault(column, []).append(
                _classify_cell(cell, sheet_name, row_index, policy)
            )

    if formula is not None:
        # The coordinate is safe to report; the formula body itself is never echoed.
        raise ExcelIngestionError(
            ExcelErrorCode.FORMULA_UNSUPPORTED,
            "工作簿不支持公式，也不接受缓存计算结果；请另存为数值后重新导入",
            sheet=sheet_name,
            coordinate=formula,
        )
    if not has_content:
        return None, 0, [f"跳过空工作表“{sheet_name}”"]
    if sheet_facts.has_merged_range:
        raise ExcelIngestionError(
            ExcelErrorCode.MERGED_CELL_UNSUPPORTED, "工作表包含合并单元格", sheet=sheet_name
        )
    if header_error is not None:
        raise header_error
    if shape_error is not None:
        raise shape_error

    table_name = _table_name(sheet_name, table_names)
    columns = tuple(
        ColumnPlan(
            source_header=header[column].source,
            column_name=header[column].name,
            inferred_type=infer_column_type(kinds.get(column, [])),
            nullable=column in null_columns,
            all_null=not kinds.get(column),
        )
        for column in range(1, width + 1)
    )
    return (
        SheetPlan(
            original_name=sheet_name,
            table_name=table_name,
            columns=columns,
            row_count=data_rows,
            row_extent=row_extent,
            column_extent=width,
        ),
        data_cells,
        [],
    )


def _stored_cells(row: Any) -> list[_StoredCell]:
    cells: list[_StoredCell] = []
    for position, cell in enumerate(row, start=1):
        value = getattr(cell, "value", None)
        if value is None:
            continue
        cells.append(
            _StoredCell(
                column=position,
                value=value,
                data_type=getattr(cell, "data_type", None) or "n",
                number_format=getattr(cell, "number_format", None),
            )
        )
    return cells


def _classify_cell(
    cell: _StoredCell, sheet_name: str, row_index: int, policy: ExcelIngestionPolicy
) -> CellKind:
    coordinate = _coordinate(cell.column, row_index)
    if cell.data_type not in REPRESENTABLE_CELL_TYPES:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CELL_VALUE,
            f"单元格值类型不受支持（{cell.data_type}）",
            sheet=sheet_name,
            coordinate=coordinate,
        )
    if isinstance(cell.value, str) and len(cell.value) > policy.max_text_length:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CELL_VALUE,
            f"文本单元格超过 {policy.max_text_length} 字符上限",
            sheet=sheet_name,
            coordinate=coordinate,
        )
    try:
        return classify_cell(cell.value, cell.number_format)
    except UnsupportedCellValue as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CELL_VALUE,
            str(error),
            sheet=sheet_name,
            coordinate=coordinate,
        ) from error


def _parse_header(
    cells: list[_StoredCell], width: int, sheet_name: str, policy: ExcelIngestionPolicy
) -> tuple[dict[int, _HeaderColumn], ExcelIngestionError | None]:
    """Row 1 is the header, always. No header row is ever searched for."""
    present = {cell.column: cell for cell in cells}
    for column in range(1, width + 1):
        if column not in present:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                "Header 行存在空单元格",
                sheet=sheet_name,
                coordinate=_coordinate(column, 1),
            )
    header: dict[int, _HeaderColumn] = {}
    seen: dict[str, int] = {}
    for column in range(1, width + 1):
        cell = present[column]
        coordinate = _coordinate(column, 1)
        if cell.data_type not in REPRESENTABLE_CELL_TYPES:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                f"Header 值类型不受支持（{cell.data_type}）",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        try:
            source = canonical_text(cell.value, classify_cell(cell.value, cell.number_format))
        except UnsupportedCellValue as error:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                f"Header 值不受支持：{error}",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        name = unicodedata.normalize("NFKC", source).strip()
        if not name:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                "Header 规范化为空",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        if "\x00" in name:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                "Header 不能包含 NUL 字符",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        if len(name) > policy.max_column_name_length:
            return {}, ExcelIngestionError(
                ExcelErrorCode.INVALID_HEADER,
                f"Header 超过 {policy.max_column_name_length} 个字符上限",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        key = name.casefold()
        if key in seen:
            return {}, ExcelIngestionError(
                ExcelErrorCode.DUPLICATE_HEADER,
                f"Header 与第 {seen[key]} 列重复（按 casefold 比较）",
                sheet=sheet_name,
                coordinate=coordinate,
            )
        seen[key] = column
        header[column] = _HeaderColumn(column=column, source=source, name=name)
    return header, None


def _table_name(sheet_name: str, table_names: set[str]) -> str:
    table_name = unicodedata.normalize("NFKC", sheet_name).strip()
    if not table_name:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_TABLE_NAME, "工作表名规范化为空", sheet=sheet_name
        )
    if "\x00" in table_name:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_TABLE_NAME, "工作表名不能包含 NUL 字符", sheet=sheet_name
        )
    key = table_name.casefold()
    if key in table_names:
        raise ExcelIngestionError(
            ExcelErrorCode.DUPLICATE_TABLE_NAME,
            "工作簿内存在规范化后重名的工作表",
            sheet=sheet_name,
        )
    table_names.add(key)
    return table_name


def _coordinate(column: int, row: int) -> str:
    return f"{get_column_letter(column)}{row}"


# --------------------------------------------------------------------------------------------
# Deterministic re-read for materialization
# --------------------------------------------------------------------------------------------


def iter_table_rows(plan: WorkbookPlan, sheet: SheetPlan) -> Iterator[tuple[Any, ...]]:
    """Stream one table's rows using the schema decided by :func:`validate_workbook`.

    The workbook is re-opened from the same immutable bytes, so validation and materialization can
    never disagree about the file they describe.
    """
    workbook = load_workbook(io.BytesIO(plan.data), read_only=True, data_only=False, keep_links=False)
    try:
        worksheet = workbook[sheet.original_name]
        rows = worksheet.iter_rows(
            min_row=2, max_row=sheet.row_extent, min_col=1, max_col=sheet.column_extent
        )
        for raw_row in rows:
            values: list[Any] = []
            for position, cell in enumerate(raw_row, start=1):
                value = getattr(cell, "value", None)
                if value is None:
                    values.append(None)
                    continue
                values.append(
                    coerce_cell(
                        value,
                        getattr(cell, "number_format", None),
                        sheet.columns[position - 1].inferred_type,
                    )
                )
            if all(value is None for value in values):
                continue
            yield tuple(values)
    finally:
        workbook.close()


# --------------------------------------------------------------------------------------------
# Raw worksheet XML facts
# --------------------------------------------------------------------------------------------


def _sheet_parts(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map each sheet name to its worksheet part, using only the OOXML package metadata."""
    try:
        workbook_root = ElementTree.fromstring(_read_part(archive, WORKBOOK_PART))
        rels_root = ElementTree.fromstring(_read_part(archive, WORKBOOK_RELS_PART))
    except ElementTree.ParseError as error:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_CONTAINER, "无法解析工作簿结构"
        ) from error
    targets: dict[str, str] = {}
    for element in rels_root.iter():
        if _local_name(element.tag) != "Relationship":
            continue
        identifier = element.get("Id")
        target = element.get("Target")
        if identifier and target:
            targets[identifier] = target
    parts: dict[str, str] = {}
    for element in workbook_root.iter():
        if _local_name(element.tag) != "sheet":
            continue
        name = element.get("name")
        relationship = next(
            (value for key, value in element.attrib.items() if _local_name(key) == "id"), None
        )
        target = targets.get(relationship or "")
        if name is None or target is None:
            continue
        parts[name] = _resolve_target(target)
    return parts


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve_target(target: str) -> str:
    cleaned = target.replace("\\", "/")
    if cleaned.startswith("/"):
        return cleaned.lstrip("/")
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    return f"xl/{cleaned}"


def _sheet_xml_facts(archive: zipfile.ZipFile, part: str | None) -> SheetXmlFacts:
    """Read row/column extent and merge presence from the raw XML in one streaming pass."""
    if part is None:
        return _NO_FACTS
    try:
        handle = archive.open(part)
    except KeyError:
        return _NO_FACTS
    row_extent = 0
    column_extent = 0
    has_merged_range = False
    with handle:
        tail = b""
        while True:
            chunk = handle.read(_XML_CHUNK_BYTES)
            if not chunk:
                break
            window = tail + chunk
            if not has_merged_range and _MERGE_ELEMENT.search(window):
                has_merged_range = True
            for match in _ROW_REFERENCE.finditer(window):
                row_extent = max(row_extent, int(match.group(1)))
            for match in _CELL_REFERENCE.finditer(window):
                column_extent = max(column_extent, _column_index(match.group(1)))
                row_extent = max(row_extent, int(match.group(2)))
            tail = window[-_XML_CHUNK_OVERLAP:]
    return SheetXmlFacts(
        row_extent=row_extent, column_extent=column_extent, has_merged_range=has_merged_range
    )


def _column_index(letters: bytes) -> int:
    index = 0
    for character in letters.upper():
        index = index * 26 + (character - 64)
    return index
