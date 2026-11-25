"""账单导入工具：import_bill。

契约：docs/tool_contracts.md 的「import_bill」一节；口径：docs/caliber.md。

关键设计（对应契约 10 条约束）：
- 解析阶段只读文件、不访问数据库；流水冲突完全交给
  ``ON CONFLICT (bill_id, transaction_id) DO NOTHING``，inserted 用
  ``cur.rowcount`` 反推，skipped = len(rows) - inserted（约束 3）。
- 导入完成后用 ``SELECT count(*)`` 的数据库真值与 file_rows 比对，
  不一致抛 COUNT_MISMATCH（约束 4）。
- bills 行、全部明细、bills 汇总更新在同一事务内；任何失败（含 BAD_ROW、
  COUNT_MISMATCH）都整单回滚（约束 5、7）。
- 金额在 Python 侧用 Decimal，返回字符串固定两位小数（约束 6）。

数据库连接：优先读环境变量 ``DATABASE_URL``；未设置时使用 libpq 默认环境
变量（PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD）。驱动优先 psycopg，
回退 psycopg2，二者需自行安装（例如 ``pip install "psycopg[binary]"``）。
"""

import csv
import io
import numbers
import os
import re
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from app.tools.db import connect

import pandas as pd


class BillImportError(Exception):
    def __init__(self, code: str, **extra):
        self.code = code
        self.extra = extra


CSV_ENCODINGS = ("utf-8", "gbk")
DIRECTION_COLUMNS = ("收/支", "收支", "收/支类型", "资金方向")
CANONICAL_COLUMNS = (
    "occurred_at",
    "merchant",
    "amount",
    "transaction_id",
    "direction",
    "raw",
)
PLATFORM_COLUMNS = {
    "wechat": {
        "time": "交易时间",
        "amount": "金额(元)",
        "merchant": "交易对方",
        "transaction_id": "交易单号",
    },
    "alipay": {
        "time": "交易时间",
        "amount": "金额",
        "merchant": "交易对方",
        "transaction_id": "交易号",
    },
}
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
INSERT_CHUNK = 500


# ---------------------------------------------------------------------------
# 基础辅助
# ---------------------------------------------------------------------------

def _error_payload(exc: BillImportError) -> dict:
    return {"ok": False, "code": exc.code, **exc.extra}


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _cell_text(value) -> str:
    """把单元格转成文本；整数型浮点去掉 .0，便于流水号/表头比对。"""
    if _is_blank(value):
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm(value) -> str:
    """列名归一化：strip + 去 BOM + 全角括号转半角（约束 2）。"""
    return (
        _cell_text(value)
        .replace("\ufeff", "")
        .replace("（", "(")
        .replace("）", ")")
        .strip()
    )


def _sample(row) -> list[str]:
    """返回文件原始表头样例（保留原始写法，仅去掉尾部空列）。"""
    values = ["" if _is_blank(cell) else _cell_text(cell) for cell in row]
    while values and values[-1] == "":
        values.pop()
    return values


def _cell_at(row, index: int | None):
    if index is None or index >= len(row):
        return None
    return row[index]


def _is_blank_row(row) -> bool:
    return all(_is_blank(cell) for cell in row)


def _raw_row_text(row, suffix: str) -> str:
    """BAD_ROW 里的 raw：csv 还原成一行 csv 文本，xlsx 用竖线连接非空单元格。"""
    if suffix == ".csv":
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(row)
        return buffer.getvalue()
    return " | ".join(_cell_text(cell) for cell in row if not _is_blank(cell))


# ---------------------------------------------------------------------------
# 读文件 + 定位表头（纯解析，不碰数据库）
# ---------------------------------------------------------------------------

def _read_csv_rows(path: Path) -> list[tuple[int, list[str]]]:
    raw = path.read_bytes()
    text = None
    for encoding in CSV_ENCODINGS:
        # "utf-8" 标签下用 utf-8-sig，兼容微信账单常见的 BOM；失败时的 tried 仍按契约返回
        decoder = "utf-8-sig" if encoding == "utf-8" else encoding
        try:
            text = raw.decode(decoder)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BillImportError("ENCODING_ERROR", tried=list(CSV_ENCODINGS))

    rows: list[tuple[int, list[str]]] = []
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        for row in reader:
            rows.append((reader.line_num, row))
    except csv.Error as exc:
        raise BillImportError("READ_FAILED", path=str(path), msg=str(exc)[:200]) from exc
    return rows


def _read_xlsx_rows(path: Path) -> list[tuple[int, list]]:
    try:
        # 不硬编码 header 行（约束 9）：整表读进来后按特征词找表头
        frame = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as exc:  # noqa: BLE001 - 损坏/加密/非 xlsx 统一转结构化错误
        raise BillImportError("READ_FAILED", path=str(path), msg=str(exc)[:200]) from exc

    rows: list[tuple[int, list]] = []
    # header=None 时第 1 行对应工作表第 1 行，行号即 Excel 行号
    for offset, values in enumerate(frame.itertuples(index=False, name=None), start=1):
        rows.append((offset, list(values)))
    return rows


def _locate_header(rows, time_name: str, amount_name: str) -> tuple[int | None, list]:
    """找到同时含「时间列」「金额列」的那一行；找不到时返回最像表头的一行。"""
    wanted_time = _norm(time_name)
    wanted_amount = _norm(amount_name)
    best_hits = 0
    best_row: list = []
    first_non_empty: list = []

    for index, (_, row) in enumerate(rows):
        cells = [_norm(cell) for cell in row]
        if not first_non_empty and any(cells):
            first_non_empty = row
        hits = (wanted_time in cells) + (wanted_amount in cells)
        if hits == 2:
            return index, row
        if hits > best_hits:
            best_hits, best_row = hits, row

    if not best_row and first_non_empty:
        best_row = first_non_empty
    return None, best_row


def _header_positions(header_row) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, cell in enumerate(header_row):
        key = _norm(cell)
        if key and key not in positions:
            positions[key] = index
    return positions


def _read_file(path: Path, platform: str) -> pd.DataFrame:      # 抛 BillImportError
    if not path.is_file():
        raise BillImportError("FILE_NOT_FOUND", path=str(path))

    suffix = path.suffix.lower()
    if suffix == ".csv":
        raw_rows = _read_csv_rows(path)
    elif suffix == ".xlsx":
        raw_rows = _read_xlsx_rows(path)
    else:
        raise BillImportError(
            "UNSUPPORTED_TYPE", path=str(path), allowed=[".csv", ".xlsx"]
        )

    mapping = PLATFORM_COLUMNS[platform]
    header_index, header_row = _locate_header(
        raw_rows, mapping["time"], mapping["amount"]
    )
    positions = _header_positions(header_row)
    missing = [name for name in mapping.values() if _norm(name) not in positions]
    if header_index is None or missing:
        raise BillImportError(
            "MISSING_COLUMN",
            missing=missing,
            file_header_sample=_sample(header_row),
        )

    time_at = positions[_norm(mapping["time"])]
    amount_at = positions[_norm(mapping["amount"])]
    merchant_at = positions[_norm(mapping["merchant"])]
    transaction_at = positions[_norm(mapping["transaction_id"])]

    direction_at = next(
        (
            positions[_norm(name)]
            for name in DIRECTION_COLUMNS
            if _norm(name) in positions
        ),
        None,
    )

    if direction_at is None:
        raise BillImportError(
            "MISSING_COLUMN",
            missing=["收/支"],
            file_header_sample=_sample(header_row),
        )

    records: list[dict] = []
    line_numbers: list[int] = []
    for line_number, row in raw_rows[header_index + 1:]:
        if _is_blank_row(row):              # 末尾空行/中间空行不计入 file_rows
            continue
        records.append(
            {
                "occurred_at": _cell_at(row, time_at),
                "merchant": _cell_at(row, merchant_at),
                "amount": _cell_at(row, amount_at),
                "transaction_id": _cell_at(row, transaction_at),
                "direction": _cell_at(row, direction_at),
                "raw": _raw_row_text(row, suffix),
            }
        )
        line_numbers.append(line_number)

    data = {name: [record[name] for record in records] for name in CANONICAL_COLUMNS}
    # dtype=object：禁止 pandas 把流水号/金额列推断成 float，避免精度与类型意外
    frame = pd.DataFrame(data, columns=list(CANONICAL_COLUMNS), dtype=object)
    frame.index = pd.Index(line_numbers, name="line")
    return frame


# ---------------------------------------------------------------------------
# 行解析（纯解析，不碰数据库）
# ---------------------------------------------------------------------------

def _parse_time(value) -> datetime:
    if _is_blank(value):
        raise ValueError("时间为空")

    if isinstance(value, pd.Timestamp):
        parsed = value.to_pydatetime()
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        parsed = None
        for candidate in (text, text.replace("/", "-")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("无法解析时间")

    # 口径表第 1 条：账单本地墙钟时间，不做时区换算，直接落 timestamp
    return parsed.replace(tzinfo=None)


def _norm_direction(value) -> str:
    return _cell_text(value).replace(" ", "")


def _parse_amount(value, direction) -> Decimal:
    if _is_blank(value):
        raise ValueError("金额为空")
    if isinstance(value, bool):
        raise ValueError("金额不是数值")

    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, numbers.Integral):
        amount = Decimal(int(value))
    elif isinstance(value, numbers.Real):
        # 约束 10：xlsx 数值金额不在这里补零/格式化，交给 numeric(12,2)
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        text = text.replace("¥", "").replace("￥", "").replace("$", "")
        text = text.replace(",", "").replace("，", "").replace("元", "").strip()
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1].strip()
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("金额格式非法") from exc

    if not amount.is_finite():
        raise ValueError("金额不是有限数值")

    direction_text = _norm_direction(direction)
    if direction_text == "支出":
        return -abs(amount)
    if direction_text == "收入":
        return abs(amount)
    # "/"、"不计收支"、空值：符号来源是「收/支」列，不是金额正负（口径表第 1、8 条）。
    # 这类中性交易由后续 classify_merchant 判分类，导入阶段保留文件原值，category 留 NULL。
    return amount


def _to_rows(df: pd.DataFrame, platform: str) -> list[tuple]:   # 抛 BillImportError
    if platform not in PLATFORM_COLUMNS:
        raise BillImportError(
            "BAD_PLATFORM", platform=platform, allowed=list(PLATFORM_COLUMNS)
        )

    rows: list[tuple] = []
    for line, occurred_at, merchant, amount, transaction_id, direction, raw in (
        df.itertuples(index=True, name=None)
    ):
        raw_text = "" if raw is None else str(raw)

        try:
            occurred = _parse_time(occurred_at)
        except (TypeError, ValueError) as exc:
            raise BillImportError(
                "BAD_ROW", line=int(line), raw=raw_text, reason="时间解析失败"
            ) from exc

        try:
            amount_value = _parse_amount(amount, direction)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise BillImportError(
                "BAD_ROW", line=int(line), raw=raw_text, reason="金额解析失败"
            ) from exc

        merchant_text = _cell_text(merchant)
        if not merchant_text:
            raise BillImportError(
                "BAD_ROW", line=int(line), raw=raw_text, reason="交易对方为空"
            )

        transaction_text = _cell_text(transaction_id)
        if not transaction_text:
            raise BillImportError(
                "BAD_ROW", line=int(line), raw=raw_text, reason="交易单号为空"
            )

        neutral = _norm_direction(direction) not in ("支出", "收入")
        rows.append((transaction_text, merchant_text, amount_value, occurred, "转账" if neutral else None))
    return rows


# ---------------------------------------------------------------------------
# 数据库写入
# ---------------------------------------------------------------------------



def _ensure_bill(conn, platform: str, period_date: date, file_name: str) -> tuple[int, bool]:
    """取账单 id；返回 (bill_id, duplicate)。

    约束 8：UNIQUE(platform, period) 冲突不是错误，回查已有 bill_id 继续插明细。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bills (platform, file_name, period)
            VALUES (%s, %s, %s)
            ON CONFLICT (platform, period) DO NOTHING
            RETURNING id
            """,
            (platform, file_name, period_date),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0]), False

        cur.execute(
            "SELECT id FROM bills WHERE platform = %s AND period = %s",
            (platform, period_date),
        )
        row = cur.fetchone()
        if row is None:
            # 理论不可达：唯一键刚刚冲突过
            raise RuntimeError(f"账单回查失败：platform={platform}, period={period_date}")
        return int(row[0]), True


def _upsert(conn, bill_id: int, rows: list[tuple]) -> int:       # 返回 inserted 行数
    """插入明细并返回 inserted。

    约束 3：冲突判定完全交给 ON CONFLICT DO NOTHING，
    inserted 只来自 cur.rowcount；这里不做任何冲突预查询。
    """
    inserted = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[start:start + INSERT_CHUNK]
            placeholders = ",".join(["(%s, %s, %s, %s, %s, %s)"] * len(chunk))
            params: list = []
            for transaction_id, merchant, amount, occurred_at, category in chunk:
                params.extend((bill_id, transaction_id, merchant, amount, occurred_at, category))
            # placeholders 只由常量模板拼成，所有数据走参数绑定
            cur.execute(
                "INSERT INTO transactions "
                "(bill_id, transaction_id, merchant, amount, occurred_at, category) "
                f"VALUES {placeholders} "
                "ON CONFLICT (bill_id, transaction_id) DO NOTHING",
                params,
            )
            inserted += cur.rowcount
    return inserted


def _db_totals(conn, bill_id: int) -> tuple[int, Decimal]:
    """数据库真值：账单下明细总笔数与金额合计（用于 COUNT_MISMATCH 与汇总）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), COALESCE(sum(amount), 0) "
            "FROM transactions WHERE bill_id = %s",
            (bill_id,),
        )
        count, total = cur.fetchone()
    if not isinstance(total, Decimal):
        total = Decimal(str(total))
    return int(count), total


def _update_bill_totals(
    conn, bill_id: int, total_count: int, total_amount: Decimal
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bills SET total_count = %s, total_amount = %s WHERE id = %s",
            (total_count, total_amount, bill_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"bills 汇总更新失败：bill_id={bill_id}")


def _format_money(value) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount == 0:
        return "0.00"
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(quantized, "f")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def import_bill(file_path: str, platform: str, period: str) -> dict:
    # 参数校验与解析都在数据库事务之外：解析阶段不查库（约束 3）
    try:
        if not isinstance(platform, str) or platform not in PLATFORM_COLUMNS:
            raise BillImportError(
                "BAD_PLATFORM", platform=platform, allowed=list(PLATFORM_COLUMNS)
            )
        if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
            raise BillImportError("BAD_PERIOD", period=period, expected="YYYY-MM")
        try:
            period_date = date(int(period[:4]), int(period[5:7]), 1)
        except ValueError as exc:
            raise BillImportError(
                "BAD_PERIOD", period=period, expected="YYYY-MM"
            ) from exc

        path = Path(file_path)
        frame = _read_file(path, platform)
        rows = _to_rows(frame, platform)
        file_rows = len(frame)              # 表头之后非空数据行数（约束 1）
    except BillImportError as exc:
        return _error_payload(exc)

    try:
        with closing(connect()) as conn:
            with conn:                      # 成功才提交，异常回滚整单（约束 7）
                bill_id, duplicate = _ensure_bill(
                    conn, platform, period_date, path.name
                )
                inserted = _upsert(conn, bill_id, rows)

                # 约束 4：actual 必须来自数据库 count(*)，不能用内存值自校验。
                # 按字面以 file_rows 为 expected：若文件内部本身有重复流水号，
                # ON CONFLICT 会少插，这里判 COUNT_MISMATCH 并整单回滚。
                actual_rows, total_amount = _db_totals(conn, bill_id)
                if actual_rows != file_rows:
                    raise BillImportError(
                        "COUNT_MISMATCH", expected=file_rows, actual=actual_rows
                    )

                _update_bill_totals(conn, bill_id, actual_rows, total_amount)
    except BillImportError as exc:
        return _error_payload(exc)

    return {
        "ok": True,
        "bill_id": bill_id,
        "duplicate": duplicate,
        "inserted": inserted,
        "skipped": len(rows) - inserted,
        "total_amount": _format_money(total_amount),
    }
