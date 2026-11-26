"""钱管家 CLI。

用法：
    python -m app.cli "看看 11 月账单"
"""

import argparse
import sys

from app.agent import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="钱管家：用自然语言查账单、生成报告")
    parser.add_argument("text", nargs="+", help='要问的问题，例如 "看看 11 月账单"')
    args = parser.parse_args(argv)
    user_text = " ".join(args.text)

    try:
        result = run(user_text)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if result.get("ok"):
        print(result.get("answer") or "")
    else:
        code = result.get("code", "ERROR")
        detail = result.get("detail") or result.get("finish_reason") or ""
        if code == "MAX_STEPS":
            detail = f"超过最大步数 {result.get('steps', '?')}"
        print(f"[{code}] {detail}".strip())

    trace = result.get("trace") or []
    print("\n--- trace ---")
    if not trace:
        print("(无工具调用)")
    else:
        for item in trace:
            print(f"[step {item.get('step')}] {item.get('tool')}")
            print(f"  args: {item.get('args')}")
            print(f"  result: {item.get('result')}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
