"""按月生成账单报告（write_report）。

契约：docs/tool_contracts.md 的「write_report」一节。
数字唯一来源是 query_db(month, group_by="category")；本模块只做
「取数 → 占位符替换 → 落盘」，不做任何计算或总结。
"""

import re
from pathlib import Path

from app.tools.query_db import query_db


REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"   # money-keeper/reports

_MAX_TEMPLATE_BYTES = 100 * 1024
_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")
_PLACEHOLDERS = {"total_count", "income", "expense", "net", "group_rows", "uncounted_count"}


class ReportError(Exception):
    def __init__(self, code: str, **extra):
        self.code = code
        self.extra = extra


def _error_payload(exc: ReportError) -> dict:
    return {"ok": False, "code": exc.code, **exc.extra}


def _group_table(rows: list[dict]) -> str:
    """把 query_db 的 rows 渲染成 Markdown 表格；数字原样使用，不重算。"""
    lines = [
        "| 分类 | 笔数 | 收入 | 支出 | 净额 |",
        "|---|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {row['key']} | {row['count']} | {row['income']} | "
        f"{row['expense']} | {row['net']} |"
        for row in rows
    )
    return "\n".join(lines)


def _values(month: str) -> dict[str, str]:
    """调 query_db(month, group_by='category')，把结果转成占位符字典"""
    data = query_db(month, group_by="category")
    if not data.get("ok"):
        extra = {k: v for k, v in data.items() if k not in ("ok", "code")}
        raise ReportError(str(data.get("code", "BAD_MONTH")), **extra)

    values: dict[str, str] = {}
    if data.get("count") is not None:
        values["total_count"] = str(data["count"])
    for key in ("income", "expense", "net"):
        if data.get(key) is not None:
            values[key] = str(data[key])
    if "rows" in data:
        values["group_rows"] = _group_table(data["rows"] or [])
    values["uncounted_count"] = str((data.get("uncounted") or {}).get("count", 0))
    return values


def _render(template: str, values: dict[str, str]) -> str:
    """正则找 {{name}}，逐个替换；未知占位符抛 UNKNOWN_PLACEHOLDER"""
    def replace(match: re.Match) -> str:
        name = match.group(1).strip()
        if name not in _PLACEHOLDERS:
            raise ReportError("UNKNOWN_PLACEHOLDER", placeholder=name)
        if name not in values or values[name] is None:
            raise ReportError("PLACEHOLDER_MISS_DATA", placeholder=name)
        return values[name]

    # 用回调做替换：遇到未知占位符直接抛错，正文不会带着它落盘。
    return _PLACEHOLDER_RE.sub(replace, template)


def write_report(month: str, template: str, output_path: str) -> dict:
    template_bytes = len(template.encode("utf-8"))
    if template_bytes > _MAX_TEMPLATE_BYTES:
        return {"ok": False, "code": "TEMPLATE_TOO_LARGE", "bytes": template_bytes}

    # 契约第 5 条：先 resolve，再判断是否仍在 REPORTS_DIR 内；写盘也用 resolve 后的路径。
    # 按契约字面，Path(output_path).resolve() 的相对路径以进程 cwd 为基准；
    # 调用方应传绝对路径，或相对 cwd 且落在 reports/ 下的路径。
    allowed_dir = REPORTS_DIR.resolve()
    raw = Path(output_path)
    requested = (allowed_dir / raw if not raw.is_absolute() else raw).resolve()
    if not requested.is_relative_to(allowed_dir):
        return {
            "ok": False,
            "code": "PATH_DENIED",
            "requested": str(output_path),
            "allowed_dir": str(allowed_dir),
        }

    try:
        values = _values(month)
        rendered = _render(template, values)
    except ReportError as exc:
        return _error_payload(exc)

    requested.parent.mkdir(parents=True, exist_ok=True)

    try:
        # "w" 覆盖写，不追加；newline="" 原样保留模板里的换行
        with requested.open("w", encoding="utf-8", newline="") as fh:
            fh.write(rendered)
    except (OSError, UnicodeError) as exc:
        msg = getattr(exc, "strerror", None) or str(exc)
        return {
            "ok": False,
            "code": "WRITE_FAILED",
            "path": str(requested),
            "errno": getattr(exc, "errno", None),
            "msg": msg[:200],
        }

    return {
        "ok": True,
        "month": month,
        "file_path": str(requested),
        "rendered": True,
    }
