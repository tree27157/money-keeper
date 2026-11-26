"""DeepSeek 调用封装：带总预算的指数退避重试。

参考 ai-partner/api/llm.py 的 create_with_retry：
- SDK 自身重试关闭（max_retries=0），重试只在这里做；
- 每次请求的 timeout 受整体 deadline 约束（单次最多 15s）；
- 只对超时/连接/限流/5xx 重试，其余异常直接抛出。
"""

import os
import random
import sys
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from app.tools.db import load_dotenv


load_dotenv()

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"
BASE_DELAY = 2
MAX_ATTEMPTS = 3

RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


class UpstreamTimeout(Exception):
    """上游超时或重试耗尽"""


_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
if not _API_KEY:
    raise RuntimeError("缺少 DEEPSEEK_API_KEY：请在项目根 .env 中配置")

client = OpenAI(
    api_key=_API_KEY,
    base_url=BASE_URL,
    max_retries=0,
    timeout=60,
)


def create_with_retry(
    messages,
    *,
    deadline,
    tools=None,
    request_id="",
    max_attempts=MAX_ATTEMPTS,
):
    """在 deadline（time.monotonic() 绝对时间）内调用 DeepSeek，失败按指数退避重试。"""
    for attempt in range(max_attempts):
        now = time.monotonic()
        if now >= deadline:
            raise UpstreamTimeout("总预算已耗尽")

        timeout = max(1, min(deadline - now, 15))
        started = time.monotonic()
        try:
            kwargs = {"model": MODEL, "messages": messages, "timeout": timeout}
            if tools:
                kwargs["tools"] = tools
            return client.chat.completions.create(**kwargs)

        except RETRYABLE as exc:
            took = time.monotonic() - started
            delay = BASE_DELAY * (2 ** attempt) * random.uniform(0.5, 1.5)
            remaining = max(0.0, deadline - time.monotonic())
            print(
                f"[retry] request_id={request_id or '-'} "
                f"attempt={attempt + 1}/{max_attempts} 耗时={took:.2f}s "
                f"exc={type(exc).__name__} delay={delay:.2f}s 剩余={remaining:.2f}s",
                file=sys.stderr,
                flush=True,
            )

            if attempt == max_attempts - 1:
                raise UpstreamTimeout(f"重试耗尽：{type(exc).__name__}") from exc
            if time.monotonic() + delay > deadline:
                raise UpstreamTimeout(
                    f"重试耗尽：第 {attempt + 1} 次等待 {delay:.1f}s 会超预算"
                ) from exc
            time.sleep(delay)

    raise UpstreamTimeout("重试耗尽")
