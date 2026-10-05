"""FastAPI 入口：HTTP API + 静态操作页面。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import ScheduleRequest
from .service import handle_request

BASE = Path(__file__).resolve().parent

app = FastAPI(
    title="跨时区发布窗口编排",
    version="1.0.0",
    description="输入服务依赖、候选时间窗、团队禁发、维护冻结与回滚耗时，"
                "输出满足依赖先后/间隔/资源占用约束的可行排程，并解释每个冲突。",
)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={
        "status": "invalid_request",
        "errors": [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()],
    })


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/schedule")
def schedule(req: ScheduleRequest):
    status, payload = handle_request(req)
    return JSONResponse(status_code=status, content=payload)


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
