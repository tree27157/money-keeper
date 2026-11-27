"""按月聚合账单（query_db）。

契约：docs/tool_contracts.md 的「query_db」一节；口径：docs/caliber.md。

实现要点（对应契约 12 条约束）：
- 时间：半开区间 ``[本月 1 号, 下月 1 号)``，避免 ``<= 月末`` 丢小数秒。
- 统一的 WHERE 片段由 ``_FILTER_SQL`` 常量给出，``_totals`` / ``_group_rows``
  共用同一份过滤条件，保证顶层汇总与分组行永远同一口径。
- 可选过滤用 ``%s::text IS NULL OR ...`` 参数化，不拼接用户输入，杜绝注入。
- 「计入收支」谓词一律写成
  ``COALESCE(category, '未分类') NOT IN ('转账', '理财')``。
  不能直接写 ``category NOT IN (...)``：SQL 三值逻辑下 ``NULL NOT IN (...)``
  为 NULL，会把「未分类」交易静默排除，违反口径第 10 条。
- 顶层 count/income/expense/net 只统计计入收支的行；转账、理财只出现在
  ``uncounted``；``count_all`` 是过滤范围内全部笔数。
- 金额、笔数全部由 SQL 聚合；Python 只做 Decimal → 两位小数字符串的格式化。

数据库连接与 import_bill 保持同一约定：``DATABASE_URL`` 优先，未设置时使用
libpq 默认环境变量（PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD）；驱动优先
psycopg，回退 psycopg2。
"""

import re
from contextlib import closing
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from app.tools.db import connect
from datetime import date, timedelta


class QueryError(Exception):
    def __init__(self, code: str, **extra):
        self.code = code
        self.extra = extra

_GROUP_LIMIT = 40

_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

# 统一的过滤条件。参数出现顺序：
# (start, end, category, category, merchant, merchant)
_FILTER_SQL = (
    "occurred_at >= %s AND occurred_at < %s"
    " AND (%s::text IS NULL OR COALESCE(category, '未分类') = %s)"
    " AND (%s::text IS NULL OR merchant = %s)"
)

# 「计入收支」谓词：必须带 COALESCE，NULL（未分类）要参与收支统计。
_COUNTED_SQL = "COALESCE(category, '未分类') NOT IN ('转账', '理财')"

_TOTALS_SQL = (
    "SELECT "
    f"count(*) FILTER (WHERE {_COUNTED_SQL}) AS counted_count, "
    "count(*) AS count_all, "
    "COALESCE(sum(amount) FILTER (WHERE amount > 0 "
    f"AND {_COUNTED_SQL}), 0) AS income, "
    "COALESCE(sum(amount) FILTER (WHERE amount < 0 "
    f"AND {_COUNTED_SQL}), 0) AS expense, "
    "COALESCE(count(*) FILTER (WHERE category IN ('转账', '理财')), 0) "
    "AS uncounted_count, "
    "COALESCE(sum(amount) FILTER (WHERE category IN ('转账', '理财')), 0) "
    "AS uncounted_sum "
    "FROM transactions WHERE " + _FILTER_SQL


)


# ---------------------------------------------------------------------------
# 基础辅助
# ---------------------------------------------------------------------------

def _error_payload(exc: QueryError) -> dict:
    return {"ok": False, "code": exc.code, **exc.extra}


def _as_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value) -> str:
    """numeric → 两位小数字符串（契约约束 8；ROUND_HALF_UP 见口径第 5 条）。"""
    amount = _as_decimal(value)
    if amount == 0:
        return "0.00"
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _filter_params(start: date, end: date, category, merchant) -> tuple:
    """顺序必须与 _FILTER_SQL 的 %s 出现顺序一致。"""
    return (start, end, category, category, merchant, merchant)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _month_range(month: str) -> tuple[date, date]:
    """'2026-11' → (2026-11-01, 2026-12-01)；格式非法抛 BAD_MONTH"""
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise QueryError("BAD_MONTH", month=month)

    year, mon = int(month[:4]), int(month[5:7])
    try:
        start = date(year, mon, 1)
        end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    except (ValueError, OverflowError) as exc:
        raise QueryError("BAD_MONTH", month=month) from exc
    return start, end


def _totals(conn, start, end, category, merchant) -> dict:
    """返回 count / count_all / income / expense / uncounted_count / uncounted_sum"""
    with conn.cursor() as cur:
        cur.execute(_TOTALS_SQL, _filter_params(start, end, category, merchant))
        row = cur.fetchone()

    counted, count_all, income, expense, uncounted_count, uncounted_sum = row
    return {
        "count": int(counted),
        "count_all": int(count_all),
        "income": _as_decimal(income),
        "expense": _as_decimal(expense),
        "uncounted_count": int(uncounted_count),
        "uncounted_sum": _as_decimal(uncounted_sum),
    }

_COVERAGE_SQL = """
    SELECT min(occurred_at)::date AS start,
           max(occurred_at)::date AS end,
           count(*) AS n
    FROM transactions
    WHERE occurred_at >= %s AND occurred_at < %s
"""


def _coverage(conn, start: date, end: date) -> dict:
    """数据覆盖范围。不受 category/merchant 过滤影响，反映整月原始数据窗口。"""
    with conn.cursor() as cur:
        cur.execute(_COVERAGE_SQL, (start, end))
        first, last, n = cur.fetchone()

    month_days = (end - start).days
    if n == 0 or first is None:
        return {"start": None, "end": None, "days": 0,
                "month_days": month_days, "complete": False}

    last_day = end - timedelta(days=1)
    return {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "days": (last - first).days + 1,
        "month_days": month_days,
        "complete": first == start and last == last_day,
    }

def _group_rows(conn, start, end, by, category, merchant) -> list[dict]:
    """ by ∈ {'category','merchant'}；按 abs(net) 降序；不含转账/理财 """
    if by == "category":
        key_expr = "COALESCE(category, '未分类')"
    elif by == "merchant":
        key_expr = "merchant"
    else:
        raise QueryError("BAD_GROUP_BY", got=by)

    # key_expr 只来自上面的二选一白名单，用户输入永远走 %s 参数绑定。
    # GROUP BY 与 SELECT 使用同一个 key_expr，保证表达式一字不差。
    sql = (
        f"SELECT {key_expr} AS key, "
        "count(*) AS count, "
        "COALESCE(sum(amount) FILTER (WHERE amount > 0), 0) AS income, "
        "COALESCE(sum(amount) FILTER (WHERE amount < 0), 0) AS expense, "
        "COALESCE(sum(amount), 0) AS net "
        "FROM transactions "
        f"WHERE {_FILTER_SQL} "
        f"AND {_COUNTED_SQL} "
        f"GROUP BY {key_expr} "
        "ORDER BY abs(COALESCE(sum(amount), 0)) DESC "
        f"LIMIT {_GROUP_LIMIT}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, _filter_params(start, end, category, merchant))
        rows = cur.fetchall()

    return [
        {
            "key": str(key),
            "count": int(count),
            "income": _money(income),
            "expense": _money(expense),
            "net": _money(net),
        }
        for key, count, income, expense, net in rows
    ]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def query_db(month, category=None, merchant=None, group_by=None) -> dict:
    try:
        start, end = _month_range(month)
        if group_by is not None and group_by not in ("category", "merchant"):
            raise QueryError("BAD_GROUP_BY", got=group_by)
    except QueryError as exc:
        return _error_payload(exc)

    with closing(connect()) as conn:
        coverage = _coverage(conn, start, end)
        totals = _totals(conn, start, end, category, merchant)
        rows = (
            _group_rows(conn, start, end, group_by, category, merchant)
            if group_by is not None
            else []
        )

    income = totals["income"]
    expense = totals["expense"]
    # 契约约束 1/5：net = income + expense，且顶层汇总不受 group_by 影响。
    # 两个分量都来自 SQL 聚合，这里是 Decimal 精确相加，不涉及浮点。
    net = income + expense

    return {
        "ok": True,
        "month": month,
        "count": totals["count"],
        "count_all": totals["count_all"],
        "coverage": coverage,
        "income": _money(income),
        "expense": _money(expense),
        "net": _money(net),
        "uncounted": {
            "count": totals["uncounted_count"],
            "sum": _money(totals["uncounted_sum"]),
        },
        "rows": rows,
        "rows_truncated": len(rows) >= _GROUP_LIMIT,
    }
