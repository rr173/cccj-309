"""FastAPI 应用与全部 HTTP 路由。

权限分层（接口级别互不跨读）：
- /admin/**   需会员令牌且 is_admin=1
- /me/**      任意会员令牌（领取自己的凭证）
- /ballots/{id}/vote  只认投票凭证，请求与响应均不含会员身份
- /public/**  匿名可读，结算后只有三项总数

资金/拨款/付款办理接口全部在 /admin/treasury/** 下，仅管理员可用；
系统不新增任何按会员查询投票选择的入口。
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request

from . import equipment, service, treasury
from .clearing.app import create_clearing_app
from .clearing_client import ClearingClient
from .config import get_settings
from .database import init_db
from .equipment_schemas import (
    EquipmentCreate,
    HandoverIn,
    MaintenanceCreate,
    ReservationCreate,
    ReturnIn,
)
from .schemas import (
    AllocateProposalIn,
    BallotCreate,
    BallotOut,
    CredentialOut,
    DepositIn,
    EligibilityAction,
    EligibilityOut,
    GrantOut,
    MemberCreate,
    MemberOut,
    PaymentIn,
    PaymentOut,
    PoolOut,
    ProposalOut,
    PublicBallotOut,
    RevokeProposalIn,
    RosterRow,
    VoteCast,
    VoteReceiptOut,
)
from .security import authenticate_member
from .service import ServiceError

logger = logging.getLogger("coopvote")


def create_app(
    db_path: str | None = None,
    credential_salt: str | None = None,
    clearing_client: ClearingClient | None = None,
    start_reconciler: bool = True,
) -> FastAPI:
    settings = get_settings()
    if db_path is not None:
        object.__setattr__(settings, "db_path", db_path)
    if credential_salt is not None:
        object.__setattr__(settings, "credential_salt", credential_salt)

    init_db(settings.db_path)

    # 清算客户端：测试可注入直连 ASGI 的 transport；默认走 HTTP 访问独立进程
    if clearing_client is None:
        clearing_client = ClearingClient(settings.clearing_base_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动即补齐重启前遗留的受理中付款；之后后台线程定期对账
        try:
            treasury.reconcile_pending(settings.db_path, clearing_client)
        except Exception:  # noqa: BLE001 - 清算端暂不可用不应阻止启动
            logger.warning("启动对账未完成，等待清算端恢复后由后台任务补齐", exc_info=True)

        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.wait(settings.reconcile_interval_seconds):
                try:
                    treasury.reconcile_pending(settings.db_path, clearing_client)
                except Exception:  # noqa: BLE001 - 对账循环必须自愈
                    logger.warning("后台对账出错，将在下个周期重试", exc_info=True)

        worker = None
        if start_reconciler:
            worker = threading.Thread(target=_loop, name="clearing-reconciler", daemon=True)
            worker.start()
        app.state.stop_reconciler = stop_event
        try:
            yield
        finally:
            stop_event.set()
            if worker is not None:
                worker.join(timeout=5)
            clearing_client.close()

    app = FastAPI(
        title="合作社匿名表决后端",
        version="1.1.0",
        description=(
            "本地会员身份 + 服务签发投票凭证；三选项、冻结名册、停权/恢复、匿名计票；"
            "表决可绑定拨款/撤销提案，结算即按决议办理本地资金池拨款与付款。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.clearing = clearing_client

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def require_member(member=Depends(authenticate_member)):
        if member is None:
            raise ServiceError(401, "缺少或无效的会员令牌")
        return member

    def require_admin(member=Depends(require_member)):
        if not member["is_admin"]:
            raise ServiceError(403, "需要名册管理员权限")
        return member

    # ------------------------------------------------------------ 健康检查
    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ------------------------------------------------------------ 管理员：会员
    @app.post("/admin/members", response_model=MemberOut, status_code=201)
    def admin_create_member(body: MemberCreate, admin=Depends(require_admin)):
        return service.create_member(
            app.state.settings.db_path,
            app.state.settings.credential_salt,
            body.code,
            body.name,
            body.is_admin,
        )

    # ------------------------------------------------------------ 管理员：表决
    @app.post("/admin/ballots", response_model=BallotOut, status_code=201)
    def admin_create_ballot(body: BallotCreate, admin=Depends(require_admin)):
        return service.create_ballot(app.state.settings.db_path, body.title)

    @app.post("/admin/ballots/{ballot_id}/open", response_model=BallotOut)
    def admin_open_ballot(ballot_id: int, admin=Depends(require_admin)):
        return service.open_ballot(app.state.settings.db_path, ballot_id)

    @app.post("/admin/ballots/{ballot_id}/settle", response_model=PublicBallotOut)
    def admin_settle_ballot(ballot_id: int, admin=Depends(require_admin)):
        return service.settle_ballot(app.state.settings.db_path, ballot_id)

    # ------------------------------------------------------------ 管理员：资格
    @app.post("/admin/ballots/{ballot_id}/eligibility", response_model=EligibilityOut)
    def admin_change_eligibility(
        ballot_id: int, body: EligibilityAction, admin=Depends(require_admin)
    ):
        return service.set_eligibility(
            app.state.settings.db_path,
            ballot_id,
            body.member_code,
            suspend=(body.action == "suspend"),
        )

    @app.get("/admin/ballots/{ballot_id}/eligibility", response_model=list[RosterRow])
    def admin_list_eligibility(ballot_id: int, admin=Depends(require_admin)):
        # 仅返回资格状态与是否已领凭证，不含任何投票选择
        return service.list_eligibilities(app.state.settings.db_path, ballot_id)

    # ------------------------------------------------------------ 管理员：提案
    @app.post(
        "/admin/ballots/{ballot_id}/proposals/allocate",
        response_model=ProposalOut,
        status_code=201,
    )
    def admin_create_allocate_proposal(
        ballot_id: int, body: AllocateProposalIn, admin=Depends(require_admin)
    ):
        # 仅 draft 表决可绑定，开放后不得新增或更改提案
        return treasury.create_allocate_proposal(
            app.state.settings.db_path,
            ballot_id,
            body.payee,
            body.max_amount_cents,
        )

    @app.post(
        "/admin/ballots/{ballot_id}/proposals/revoke",
        response_model=ProposalOut,
        status_code=201,
    )
    def admin_create_revoke_proposal(
        ballot_id: int, body: RevokeProposalIn, admin=Depends(require_admin)
    ):
        return treasury.create_revoke_proposal(
            app.state.settings.db_path, ballot_id, body.grant_id
        )

    @app.get("/admin/ballots/{ballot_id}/proposal", response_model=ProposalOut)
    def admin_get_ballot_proposal(ballot_id: int, admin=Depends(require_admin)):
        proposal = treasury.get_proposal_by_ballot(app.state.settings.db_path, ballot_id)
        if proposal is None:
            raise ServiceError(404, "该表决未绑定提案")
        return proposal

    # ------------------------------------------------------------ 管理员：资金池
    @app.post("/admin/treasury/pool/deposit", response_model=PoolOut)
    def admin_deposit(body: DepositIn, admin=Depends(require_admin)):
        return treasury.deposit(app.state.settings.db_path, body.amount_cents)

    @app.get("/admin/treasury/pool", response_model=PoolOut)
    def admin_pool(admin=Depends(require_admin)):
        return treasury.get_pool(app.state.settings.db_path)

    # ------------------------------------------------------------ 管理员：拨款
    @app.get("/admin/treasury/grants", response_model=list[GrantOut])
    def admin_list_grants(admin=Depends(require_admin)):
        return treasury.list_grants(app.state.settings.db_path)

    @app.get("/admin/treasury/grants/{grant_id}", response_model=GrantOut)
    def admin_get_grant(grant_id: int, admin=Depends(require_admin)):
        return treasury.get_grant(app.state.settings.db_path, grant_id)

    @app.get(
        "/admin/treasury/grants/{grant_id}/payments",
        response_model=list[PaymentOut],
    )
    def admin_list_grant_payments(grant_id: int, admin=Depends(require_admin)):
        return treasury.list_payments(app.state.settings.db_path, grant_id)

    # ------------------------------------------------------------ 管理员：付款
    @app.post(
        "/admin/treasury/grants/{grant_id}/payments",
        response_model=PaymentOut,
        status_code=201,
    )
    def admin_submit_payment(
        grant_id: int, body: PaymentIn, admin=Depends(require_admin)
    ):
        return treasury.submit_payment(
            app.state.settings.db_path,
            app.state.clearing,
            grant_id,
            body.request_no,
            body.amount_cents,
        )

    @app.get("/admin/treasury/payments", response_model=list[PaymentOut])
    def admin_list_payments(admin=Depends(require_admin)):
        return treasury.list_payments(app.state.settings.db_path)

    @app.get("/admin/treasury/payments/{payment_id}", response_model=PaymentOut)
    def admin_get_payment(payment_id: int, admin=Depends(require_admin)):
        return treasury.get_payment(app.state.settings.db_path, payment_id)

    @app.post("/admin/treasury/payments/{payment_id}/reconcile", response_model=PaymentOut)
    def admin_reconcile_payment(payment_id: int, admin=Depends(require_admin)):
        return treasury.reconcile_payment(
            app.state.settings.db_path, app.state.clearing, payment_id
        )

    @app.post("/admin/treasury/reconcile")
    def admin_reconcile_all(admin=Depends(require_admin)):
        return treasury.reconcile_pending(
            app.state.settings.db_path, app.state.clearing
        )

    # ------------------------------------------------------------ 机具：管理员登记/维修
    @app.post("/admin/equipment", status_code=201)
    @app.post("/admin/machines", status_code=201, include_in_schema=False)
    @app.post("/admin/tools", status_code=201, include_in_schema=False)
    @app.post("/admin/assets", status_code=201, include_in_schema=False)
    def admin_create_equipment(body: EquipmentCreate, admin=Depends(require_admin)):
        return equipment.create_equipment(
            app.state.settings.db_path,
            body.asset_no,
            body.tags,
            body.name,
        )

    @app.get("/admin/equipment")
    @app.get("/admin/machines", include_in_schema=False)
    @app.get("/admin/tools", include_in_schema=False)
    @app.get("/admin/assets", include_in_schema=False)
    def admin_list_equipment(admin=Depends(require_admin)):
        return equipment.list_equipment(app.state.settings.db_path)

    @app.get("/admin/equipment/reservations", include_in_schema=False)
    def admin_list_equipment_reservations_before_detail(
        status: str | None = None,
        member_code: str | None = None,
        admin=Depends(require_admin),
    ):
        return equipment.list_reservations(
            app.state.settings.db_path, status=status, member_code=member_code
        )

    @app.get("/admin/equipment/by-asset/{asset_no}")
    def admin_get_equipment_by_asset(asset_no: str, admin=Depends(require_admin)):
        return equipment.get_equipment_by_asset_no(app.state.settings.db_path, asset_no)

    @app.get("/admin/equipment/{equipment_id}")
    def admin_get_equipment(equipment_id: int, admin=Depends(require_admin)):
        return equipment.get_equipment(app.state.settings.db_path, equipment_id)

    @app.post("/admin/maintenance", status_code=201)
    def admin_add_maintenance(body: MaintenanceCreate, admin=Depends(require_admin)):
        return equipment.add_maintenance(
            app.state.settings.db_path,
            body.starts_at,
            body.ends_at,
            equipment_id=body.equipment_id,
            asset_no=body.asset_no,
            reason=body.reason,
        )

    @app.post("/admin/equipment/{equipment_id}/maintenance", status_code=201)
    def admin_add_equipment_maintenance(
        equipment_id: int, body: MaintenanceCreate, admin=Depends(require_admin)
    ):
        return equipment.add_maintenance(
            app.state.settings.db_path,
            body.starts_at,
            body.ends_at,
            equipment_id=equipment_id,
            reason=body.reason,
        )

    @app.get("/admin/maintenance")
    def admin_list_maintenance(active_only: bool = False, admin=Depends(require_admin)):
        return equipment.list_maintenance(app.state.settings.db_path, active_only)

    @app.api_route(
        "/admin/maintenance/{window_id}/revoke",
        methods=["POST", "DELETE"],
    )
    def admin_revoke_maintenance(window_id: int, admin=Depends(require_admin)):
        return equipment.revoke_maintenance(app.state.settings.db_path, window_id)

    # ------------------------------------------------------------ 机具：预约与交接核对
    @app.get("/admin/reservations")
    @app.get("/admin/equipment/reservations", include_in_schema=False)
    def admin_list_reservations(
        status: str | None = None,
        member_code: str | None = None,
        admin=Depends(require_admin),
    ):
        return equipment.list_reservations(
            app.state.settings.db_path, status=status, member_code=member_code
        )

    @app.get("/admin/reservations/{reservation_id}")
    @app.get("/admin/equipment/reservations/{reservation_id}", include_in_schema=False)
    def admin_get_reservation(reservation_id: int, admin=Depends(require_admin)):
        return equipment.get_reservation(app.state.settings.db_path, reservation_id)

    @app.post("/admin/reservations/{reservation_id}/handover")
    @app.post("/admin/equipment/reservations/{reservation_id}/handover", include_in_schema=False)
    @app.post("/admin/reservations/{reservation_id}/checkout", include_in_schema=False)
    @app.post("/admin/reservations/{reservation_id}/pickup", include_in_schema=False)
    @app.post("/admin/reservations/{reservation_id}/collect", include_in_schema=False)
    def admin_handover_reservation(
        reservation_id: int,
        body: HandoverIn | None = None,
        admin=Depends(require_admin),
    ):
        body = body or HandoverIn()
        return equipment.handover(
            app.state.settings.db_path,
            reservation_id,
            equipment_ids=body.equipment_ids,
            asset_nos=body.asset_nos,
        )

    @app.get("/admin/loans")
    def admin_list_loans(
        open: bool = False,
        returned: bool = False,
        overdue: bool = False,
        admin=Depends(require_admin),
    ):
        return equipment.list_loans(
            app.state.settings.db_path,
            open_only=open,
            returned_only=returned,
            overdue_only=overdue,
        )

    @app.post("/admin/loans/{loan_id}/return")
    @app.post("/admin/equipment/loans/{loan_id}/return", include_in_schema=False)
    def admin_return_loan(loan_id: int, body: ReturnIn | None = None,
                          admin=Depends(require_admin)):
        return equipment.return_loan(app.state.settings.db_path, loan_id=loan_id)

    @app.post("/admin/equipment/{equipment_id}/return")
    def admin_return_equipment_by_pk(
        equipment_id: int,
        body: ReturnIn | None = None,
        admin=Depends(require_admin),
    ):
        return equipment.return_loan(
            app.state.settings.db_path, equipment_id=equipment_id
        )

    @app.post("/admin/returns")
    @app.post("/admin/equipment/returns", include_in_schema=False)
    def admin_return_equipment(body: ReturnIn, admin=Depends(require_admin)):
        return equipment.return_loan(
            app.state.settings.db_path,
            loan_id=body.loan_id,
            asset_no=body.asset_no,
            equipment_id=body.equipment_id,
        )

    # ------------------------------------------------------------ 会员：成套机具预约
    @app.post("/me/equipment/reservations", status_code=201)
    @app.post("/me/reservations", status_code=201, include_in_schema=False)
    @app.post("/reservations", status_code=201, include_in_schema=False)
    def me_create_equipment_reservation(
        body: ReservationCreate, member=Depends(require_member)
    ):
        return equipment.create_reservation(
            app.state.settings.db_path,
            member["id"],
            body.starts_at,
            body.ends_at,
            [item.model_dump() for item in body.items],
        )

    @app.get("/me/equipment/reservations")
    @app.get("/me/reservations", include_in_schema=False)
    @app.get("/reservations", include_in_schema=False)
    def me_list_equipment_reservations(member=Depends(require_member)):
        return equipment.list_reservations(
            app.state.settings.db_path, member_id=member["id"]
        )

    @app.get("/me/equipment/reservations/{reservation_id}")
    @app.get("/me/reservations/{reservation_id}", include_in_schema=False)
    @app.get("/reservations/{reservation_id}", include_in_schema=False)
    def me_get_equipment_reservation(
        reservation_id: int, member=Depends(require_member)
    ):
        return equipment.get_reservation(
            app.state.settings.db_path, reservation_id, member_id=member["id"]
        )

    @app.post("/me/equipment/reservations/{reservation_id}/cancel")
    @app.delete("/me/equipment/reservations/{reservation_id}", include_in_schema=False)
    @app.post("/me/reservations/{reservation_id}/cancel", include_in_schema=False)
    @app.delete("/me/reservations/{reservation_id}", include_in_schema=False)
    @app.post("/reservations/{reservation_id}/cancel", include_in_schema=False)
    @app.delete("/reservations/{reservation_id}", include_in_schema=False)
    def me_cancel_equipment_reservation(
        reservation_id: int, member=Depends(require_member)
    ):
        return equipment.cancel_reservation(
            app.state.settings.db_path, reservation_id, member["id"]
        )

    # ------------------------------------------------------------ 会员：领凭证
    @app.post("/me/ballots/{ballot_id}/credential", response_model=CredentialOut)
    def me_issue_credential(ballot_id: int, member=Depends(require_member)):
        return service.issue_credential(
            app.state.settings.db_path,
            app.state.settings.credential_salt,
            ballot_id,
            member["id"],
        )

    # ------------------------------------------------------------ 匿名：投票
    @app.post("/ballots/{ballot_id}/vote", response_model=VoteReceiptOut)
    def cast_vote(ballot_id: int, body: VoteCast):
        # 没有会员鉴权依赖：接口只凭凭证工作，且不回传任何会员字段
        return service.cast_vote(
            app.state.settings.db_path,
            app.state.settings.credential_salt,
            ballot_id,
            body.credential,
            body.choice,
        )

    # ------------------------------------------------------------ 公开端
    @app.get("/public/ballots/{ballot_id}", response_model=PublicBallotOut)
    def public_ballot(ballot_id: int):
        return service.get_public_ballot(app.state.settings.db_path, ballot_id)

    return app


# 供测试/演示直连的清算端应用工厂（独立数据库，独立进程入口见 run_clearing.py）
def build_clearing_app(db_path: str) -> FastAPI:
    return create_clearing_app(db_path)


app = create_app()
