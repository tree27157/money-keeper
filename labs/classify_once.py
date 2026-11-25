"""一次性批量分类商户（lab）。

流程：fetch_merchants() → llm_classify() → apply()。

口径与约束：
- 分类枚举固定 12 项（见 ALLOWED_CATEGORIES），代码里再做一次白名单校验；
  非法值统一落「其他」/confidence=0（契约 classify_merchant 约束 1、2、6）。
- 个人姓名/昵称/亲属称呼 → 「转账」，不计收支（caliber 第 3 条）。
- LLM 判不了 / 返回 null → 不写缓存，transactions.category 保持 NULL，
  报表归入「未分类」（caliber 第 10 条）。
- 一次 LLM 调用判完全部商户；merchant_category 写入与 transactions 回填
  在同一个数据库事务内，要么全成要么全败。
"""

import json
import os
import sys
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# 允许直接 `python labs/classify_once.py` 运行：把项目根加入 import path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.db import connect, load_dotenv  # noqa: E402

load_dotenv()  # db.py 已调用一次；这里显式复用，确保 .env 中的 DEEPSEEK_API_KEY 已加载


ALLOWED_CATEGORIES = (
    "餐饮",
    "交通",
    "购物",
    "居住",
    "娱乐",
    "医疗",
    "教育",
    "人情",
    "工资",
    "理财",
    "转账",
    "其他",
)

# LLM 明确表示「判不了」的写法：不写缓存，保持 NULL（caliber 第 10 条）。
# 其它不在枚举里的字符串才算非法值，落「其他」/confidence=0。
_UNKNOWN_CATEGORIES = {
    "",
    "未知",
    "未分类",
    "无法判断",
    "不确定",
    "unknown",
    "n/a",
    "null",
    "none",
}

_INSERT_SQL = """
    INSERT INTO merchant_category (merchant, category, confidence, source, updated_at)
    VALUES (%s, %s, %s, 'llm', now())
    ON CONFLICT (merchant) DO UPDATE
    SET category = EXCLUDED.category,
        confidence = EXCLUDED.confidence,
        source = EXCLUDED.source,
        updated_at = now()
    WHERE merchant_category.source <> 'manual'
"""

_BACKFILL_SQL = """
    UPDATE transactions t
    SET category = mc.category
    FROM merchant_category mc
    WHERE t.merchant = mc.merchant
      AND t.category IS NULL
      AND mc.confidence > 0
"""

_SYSTEM_PROMPT = f"""你是一个严谨的个人账单商户分类器。只输出 JSON，不要输出 Markdown、解释或多余文字。

固定分类枚举（category 只能取以下 12 个值之一，或 null）：
{"、".join(ALLOWED_CATEGORIES)}

判定规则：
1. 交易对方是个人姓名、昵称、亲属称呼（如“张三”“李四”“老王”“爸”“老妈”“XX(昵称)”）→ "转账"。
   「转账」专指个人往来/内部划转，不计收支；即使括号里带店铺名（如“妈 (XX家纺)”），
   只要是亲属/个人称呼也判 "转账"。
2. 明确的商户、平台、品牌、店铺按主营业务归类：
   美团/饿了么/餐厅/小吃/咖啡/奶茶 → 餐饮；
   滴滴/地铁/公交/12306/中铁/加油 → 交通；
   淘宝/京东/拼多多/超市/便利店/商场 → 购物；
   房租/物业/水电/燃气/宽带 → 居住；
   电影/演出/游戏/视频会员/KTV → 娱乐；
   医院/药房/体检 → 医疗；
   学校/培训/考试/书籍 → 教育；
   红包/礼金/请客/人情往来 → 人情；
   工资/奖金/报销 → 工资；
   基金/理财通/余额宝/证券 → 理财。
3. 对方是公司/平台，但仅凭名称无法判断具体用途 → category 为 null，不要硬猜。
4. 明确是真实消费/收入，但不属于上述 11 个具体类别 → "其他"。
5. 完全无法从商户名获得有效信息 → category 为 null，confidence 为 0。
6. 不要编造商户信息；不确定时宁可 null，不要为了填满而给高置信分类。
7. confidence 取 0.0~1.0，保留两位小数；category 为 null 时 confidence 必须为 0。
8. total 正数表示收入、负数表示支出，只作为消歧参考，不改变枚举。
9. 每个输入商户必须且只能出现一次；merchant 必须与输入完全一致（含空格、括号、emoji）。
"""


# ---------------------------------------------------------------------------
# 基础辅助
# ---------------------------------------------------------------------------

def _money(value) -> str:
    """金额沿用项目口径：两位小数字符串（caliber 第 5 条）。"""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount == 0:
        return "0.00"
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _normalize_confidence(value) -> float:
    """把 LLM 给的置信度夹到 0.00~1.00；无法解析按 0 处理。"""
    try:
        conf = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    if not conf.is_finite():
        return 0.0
    conf = min(max(conf, Decimal("0")), Decimal("1"))
    return float(conf.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _normalize_entry(item) -> tuple[str, float] | None:
    """校验单条分类；返回 (category, confidence)，判不了返回 None。

    - category 为 null/“未知”等 → None（保持 NULL，caliber 第 10 条）
    - 不在 12 枚举内 → ("其他", 0.0)（契约约束 1）
    - 合法值 → confidence 夹到 0~1、保留两位
    """
    if not isinstance(item, dict):
        return None

    raw = item.get("category")
    if raw is None:
        return None

    category = str(raw).strip()
    if category.lower() in _UNKNOWN_CATEGORIES or raw is None:
        return "其他", 0.0

    if category not in ALLOWED_CATEGORIES:
        return "其他", 0.0

    return category, _normalize_confidence(item.get("confidence", 0))


def _extract_entries(payload) -> list:
    """兼容几种常见的 LLM JSON 形状，统一成带 merchant 的 entry 列表。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise RuntimeError("LLM 返回的 JSON 既不是对象也不是数组")

    for key in ("results", "merchants", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [
                {"merchant": merchant, **item}
                for merchant, item in value.items()
                if isinstance(item, dict)
            ]

    # 兼容 {merchant: {category, confidence}} 形式
    return [
        {"merchant": merchant, **item}
        for merchant, item in payload.items()
        if isinstance(item, dict)
    ]


def _build_user_prompt(merchants: list[dict]) -> str:
    items = [
        {"merchant": item["merchant"], "cnt": item["cnt"], "total": item["total"]}
        for item in merchants
    ]
    return (
        f"请一次判完下面 {len(items)} 个商户（不要遗漏、不要逐个提问）。\n"
        "只输出一个 JSON 对象，格式示例：\n"
        '{"results": [{"merchant": "输入里的商户名", "category": "餐饮", '
        '"confidence": 0.95}]}\n'
        "category 只能取枚举值或 null，confidence 为 0.0~1.0。\n"
        "商户列表（total 正数=收入、负数=支出，仅作消歧参考）：\n"
        + json.dumps(items, ensure_ascii=False, indent=2)
    )


def _ask_llm(messages: list[dict]) -> str:
    """一次 DeepSeek 调用，返回 message.content。"""
    try:
        from openai import OpenAI, OpenAIError
    except ImportError as exc:
        raise RuntimeError("缺少 openai：pip install openai") from exc

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY：请在项目根 .env 中配置")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=60,
    )
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            response_format={"type": "json_object"},
            timeout=60,
        )
    except OpenAIError as exc:
        raise RuntimeError(f"DeepSeek 调用失败：{exc}") from exc

    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# 三个主函数
# ---------------------------------------------------------------------------

def fetch_merchants() -> list[dict]:
    """查未分类的商户：merchant / cnt / total；已有人工分类的跳过"""
    with closing(connect()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.merchant, count(*) AS cnt, sum(t.amount) AS total
                FROM transactions t
                LEFT JOIN merchant_category mc ON mc.merchant = t.merchant
                WHERE t.category IS NULL
                  AND mc.merchant IS NULL
                GROUP BY t.merchant
                ORDER BY cnt DESC, t.merchant
                """
            )
            rows = cur.fetchall()

    return [
        {"merchant": merchant, "cnt": int(cnt), "total": _money(total)}
        for merchant, cnt, total in rows
    ]


def llm_classify(merchants: list[dict]) -> dict[str, dict]:
    """一次调用判 37 个 → {merchant: {'category':..., 'confidence':...}}"""
    if not merchants:
        return {}

    content = _ask_llm(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(merchants)},
        ]
    )
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM 返回不是合法 JSON：{content[:200]}") from exc

    entries = _extract_entries(payload)
    wanted = {str(item["merchant"]).strip() for item in merchants}
    mapping: dict[str, dict] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        merchant = str(entry.get("merchant", "")).strip()
        # 忽略模型编造/重复的商户；缺失的商户自然不在 mapping 里 → 保持 NULL
        if not merchant or merchant not in wanted or merchant in mapping:
            continue

        normalized = _normalize_entry(entry)
        if normalized is None:
            continue
        category, confidence = normalized
        mapping[merchant] = {"category": category, "confidence": confidence}

    for item in merchants:
        name = str(item["merchant"]).strip()
        if name and name not in mapping:
            mapping[name] = {"category": "其他", "confidence": 0.0}

    return mapping


def apply(mapping: dict[str, dict]) -> tuple[int, int]:
    """一个事务内：写 merchant_category + 回填 transactions；返回 (商户数, 回填行数)"""
    rows: list[tuple] = []
    for merchant, item in mapping.items():
        name = str(merchant).strip()
        if not name:
            continue
        normalized = _normalize_entry(item)
        if normalized is None:
            continue
        category, confidence = normalized
        rows.append((name, category, Decimal(str(confidence))))

    if not rows:
        return 0, 0

    with closing(connect()) as conn:
        with conn:  # 成功提交；任何异常回滚整批写入 + 回填
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, rows)
                cur.execute(_BACKFILL_SQL)
                backfilled = cur.rowcount

    return len(rows), backfilled


if __name__ == "__main__":
    pending = fetch_merchants()
    print(f"待分类商户：{len(pending)}")
    result = llm_classify(pending)
    print(f"拿到有效分类：{len(result)}（其余保持 NULL）")
    merchant_count, transaction_count = apply(result)
    print(f"写入 merchant_category：{merchant_count}；回填 transactions：{transaction_count}")
