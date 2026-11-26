"""钱管家 Agent：DeepSeek function calling 循环。

工具只有两个：
- query_db：按月取数（唯一数字来源）
- write_report：用占位符模板生成报告

本文件不做任何金额/笔数/占比计算，只负责调度工具。
"""

import json
import time
from datetime import date

from app.llm import UpstreamTimeout, create_with_retry
from app.tools.query_db import query_db
from app.tools.write_report import write_report


MAX_STEPS = 6

QUERY_DB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_db",
        "description": (
            "按月聚合账单。返回 count（计入收支笔数）、count_all（含转账/理财）、"
            "income、expense、net、uncounted（转账/理财汇总）和 rows（分类/商户分组）。"
            "所有金额、笔数以此为准，不要自己计算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "月份，格式 YYYY-MM，例如 2026-11",
                },
                "category": {
                    "type": "string",
                    "description": "可选，按分类精确过滤；数据库 NULL 归为“未分类”",
                },
                "merchant": {
                    "type": "string",
                    "description": "可选，按商户精确过滤",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["category", "merchant"],
                    "description": "可选，按分类或商户分组",
                },
            },
            "required": ["month"],
        },
    },
}

WRITE_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_report",
        "description": (
            "把带占位符的 Markdown 模板渲染成月度报告，写入 reports/ 目录。"
            "模板只能使用固定占位符，禁止直接写金额或笔数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "月份，格式 YYYY-MM，例如 2026-11",
                },
                "template": {
                    "type": "string",
                    "description": (
                        "Markdown 正文，只能使用 {{total_count}}、{{income}}、"
                        "{{expense}}、{{net}}、{{uncounted_count}}、{{group_rows}} 占位符；"
                        "{{uncounted_count}} 是未计入收支的内部划转笔数"
                        "禁止写具体数字，禁止表达式（如 {{income - expense}}）"
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": "报告文件名，如 2026-11_bill.md；工具限制在 reports/ 内",
                },
            },
            "required": ["month", "template", "output_path"],
        },
    },
}

TOOLS = [QUERY_DB_SCHEMA, WRITE_REPORT_SCHEMA]
TOOL_MAP = {"query_db": query_db, "write_report": write_report}

SYSTEM = """你是钱管家（money-keeper）的账单分析助手，负责查账、解释和生成月度报告。

硬规则：
1. 金额、笔数、占比一律来自 query_db 的返回值，禁止自己算（也不要估算、四舍五入或从上下文猜数字）。query_db 没有返回的指标（例如占比、环比百分比）不要自己算；可以直接说明工具没有提供，或用文字做定性比较。
2. 涉及某个月收支/分类/商户的问题，先调用 query_db；month 使用 YYYY-MM。用户只说“11 月”时按当前年份补全；如果补全后是未来月份，先向用户确认。
3. query_db 的 count 是计入收支的笔数，count_all 含转账/理财；income/expense/net 已排除转账/理财，它们只出现在 uncounted 中。分类汇总传 group_by="category"，商户汇总传 group_by="merchant"。
4. 需要生成报告时调用 write_report。template 只能是文字/标题/列表 + 固定占位符 {{total_count}}、{{income}}、{{expense}}、{{net}}、{{uncounted_count}}、{{group_rows}}；禁止在模板中直接写金额、笔数，禁止表达式（如 {{income - expense}}）。output_path 只写文件名，如 2026-11_bill.md。
5. 工具返回 ok:false 时，向用户说明错误码和原因，不要假装成功，也不要编造结果。
6. 不要编造商户、分类、月份或文件路径；查不到数据就如实说“这个月没有数据”。
7. 不确定用户要哪个月/哪个分类时先问清楚，不要猜。
8. 不要用已知数字做额外推导（如「95 - 72 = 23」）。工具给什么就说什么；
   uncounted.count 已经是结果，不要重新算一遍。
"""


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _call_parts(call):
    """兼容 OpenAI 的 tool_call 对象和 dict 形式，返回 (name, raw_args, call_id)。"""
    if isinstance(call, dict):
        fn = call.get("function")
        if not isinstance(fn, dict):
            fn = {}
        return (
            str(fn.get("name") or ""),
            fn.get("arguments") or "{}",
            str(call.get("id") or ""),
        )

    fn = getattr(call, "function", None)
    return (
        str(getattr(fn, "name", "") or ""),
        getattr(fn, "arguments", None) or "{}",
        str(getattr(call, "id", "") or ""),
    )


def run_tool(call) -> str:
    """执行一次工具调用，永远返回 JSON 字符串；参数缺失/错误不抛异常。"""
    name, raw_args, _ = _call_parts(call)

    fn = TOOL_MAP.get(name)
    if fn is None:
        return _json({"ok": False, "code": "UNKNOWN_TOOL", "name": name})

    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError) as exc:
            return _json(
                {"ok": False, "code": "BAD_ARGS_JSON", "detail": str(exc)[:200]}
            )

    if not isinstance(args, dict):
        return _json(
            {"ok": False, "code": "BAD_ARGS", "detail": "arguments 必须是 JSON 对象"}
        )

    try:
        result = fn(**args)
    except TypeError as exc:
        # 漏传/多传/传错参数名都会走到这里
        return _json({"ok": False, "code": "BAD_ARGS", "detail": str(exc)[:300]})
    except Exception as exc:  # 工具内部异常也不能让 agent 崩
        return _json(
            {
                "ok": False,
                "code": "TOOL_ERROR",
                "detail": f"{type(exc).__name__}: {exc}"[:300],
            }
        )

    return _json(result)


def run(user_text: str, max_steps: int = MAX_STEPS, deadline_s: float = 90) -> dict:
    deadline = time.monotonic() + deadline_s
    system_content = (
        f"{SYSTEM}\n\n当前日期：{date.today().isoformat()}。"
        "用户只说月份时按当前年份补全；若补全后是未来月份，先向用户确认。"
    )
    msgs = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_text},
    ]
    trace: list[dict] = []

    for step in range(1, max_steps + 1):
        try:
            resp = create_with_retry(msgs, deadline=deadline, tools=TOOLS)
        except UpstreamTimeout as exc:
            return {
                "ok": False,
                "code": "UPSTREAM_TIMEOUT",
                "detail": str(exc),
                "steps": step,
                "trace": trace,
            }
        except Exception as exc:
            return {
                "ok": False,
                "code": "LLM_ERROR",
                "detail": f"{type(exc).__name__}: {exc}",
                "steps": step,
                "trace": trace,
            }

        if not resp.choices:
            return {
                "ok": False,
                "code": "EMPTY_CHOICES",
                "detail": "上游返回 choices 为空",
                "steps": step,
                "trace": trace,
            }
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason

        if finish == "stop":
            return {
                "ok": True,
                "answer": msg.content or "",
                "steps": step,
                "trace": trace,
            }

        if finish == "length":
            return {
                "ok": True,
                "answer": (msg.content or "") + "\n\n（回答被截断，可以让我接着说）",
                "steps": step,
                "trace": trace,
            }

        if finish == "tool_calls":
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                return {
                    "ok": False,
                    "code": "BAD_TOOL_CALLS",
                    "detail": "finish_reason=tool_calls 但 tool_calls 为空",
                    "steps": step,
                    "trace": trace,
                }

            # assistant 消息只 append 一次，带上本轮所有 tool_calls
            msgs.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        c.model_dump() if hasattr(c, "model_dump") else c
                        for c in tool_calls
                    ],
                }
            )

            for call in tool_calls:
                name, raw_args, call_id = _call_parts(call)
                result = run_tool(call)
                trace.append(
                    {
                        "step": step,
                        "tool": name,
                        "args": raw_args,
                        "result": result[:300],
                    }
                )
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    }
                )
            continue

        return {
            "ok": False,
            "code": "UNEXPECTED_FINISH",
            "finish_reason": finish,
            "steps": step,
            "trace": trace,
        }

    return {"ok": False, "code": "MAX_STEPS", "steps": max_steps, "trace": trace}
