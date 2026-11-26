"""清算模拟端 HTTP 应用与路由，可独立进程启动（见 run_clearing.py）。"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import service
from .db import init_db
from .service import ClearingError


class PaymentSubmit(BaseModel):
    ref: str = Field(min_length=1, max_length=128)
    payee: str = Field(min_length=1, max_length=128)
    amount_cents: int = Field(gt=0)


class SimControl(BaseModel):
    offline: Optional[bool] = None
    drop_after_debit: Optional[bool] = None
    fail_next: Optional[bool] = None


def create_clearing_app(db_path: str) -> FastAPI:
    init_db(db_path)
    app = FastAPI(
        title="本地清算模拟端",
        version="1.0.0",
        description="独立持久化的伪银行：付款去重、结果查询、扣款后断连/宕机模拟。",
    )
    app.state.db_path = db_path

    @app.exception_handler(ClearingError)
    async def clearing_error_handler(request: Request, exc: ClearingError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # 注意：ConnectionDropped 故意不注册处理器——让它穿透 ASGI 应用，
    # 由服务端中断连接（真实部署表现为连接重置），模拟“扣款后响应断开”。

    @app.get("/health")
    def health():
        return service.health(app.state.db_path)

    @app.post("/payments", status_code=201)
    def submit_payment(body: PaymentSubmit):
        return service.submit_payment(
            app.state.db_path, body.ref, body.payee, body.amount_cents
        )

    @app.get("/payments/{ref}")
    def get_payment(ref: str):
        return service.get_payment(app.state.db_path, ref)

    # ------------------------------------------------ 故障注入（仅本地演示/测试）
    @app.post("/sim/control")
    def sim_control(body: SimControl):
        return service.set_sim_control(
            app.state.db_path,
            offline=body.offline,
            drop_after_debit=body.drop_after_debit,
            fail_next=body.fail_next,
        )

    return app
