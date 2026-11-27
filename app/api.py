"""钱管家 Web 层：把 agent.run 包成 HTTP 接口。"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agent import run
from app.llm import UpstreamTimeout


app = FastAPI(title="钱管家", version="1.0.0")


class ChatIn(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat")
def chat(payload: ChatIn):
    """同步阻塞调用，用 def 而不是 async def —— agent.run 内部是阻塞的。"""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        result = run(payload.text)
    except UpstreamTimeout as exc:
        raise HTTPException(status_code=504, detail=f"上游超时：{exc}") from exc

    # agent.run 目前会把 UpstreamTimeout 收敛成 ok:false，这里同样映射成 504。
    if not result.get("ok") and result.get("code") == "UPSTREAM_TIMEOUT":
        raise HTTPException(
            status_code=504,
            detail=result.get("detail") or "上游超时",
        )

    return result
