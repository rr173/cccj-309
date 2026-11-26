"""FastAPI 应用与全部 HTTP 路由。

权限分层（接口级别互不跨读）：
- /admin/**   需会员令牌且 is_admin=1
- /me/**      任意会员令牌（领取自己的凭证）
- /ballots/{id}/vote  只认投票凭证，请求与响应均不含会员身份
- /public/**  匿名可读，结算后只有三项总数
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request

from . import service
from .config import get_settings
from .database import init_db
from .schemas import (
    BallotCreate,
    BallotOut,
    CredentialOut,
    EligibilityAction,
    EligibilityOut,
    MemberCreate,
    MemberOut,
    PublicBallotOut,
    RosterRow,
    VoteCast,
    VoteReceiptOut,
)
from .security import authenticate_member
from .service import ServiceError


def create_app(db_path: str | None = None, credential_salt: str | None = None) -> FastAPI:
    settings = get_settings()
    if db_path is not None:
        object.__setattr__(settings, "db_path", db_path)
    if credential_salt is not None:
        object.__setattr__(settings, "credential_salt", credential_salt)

    init_db(settings.db_path)

    app = FastAPI(
        title="合作社匿名表决后端",
        version="1.0.0",
        description="本地会员身份 + 服务签发投票凭证；三选项、冻结名册、停权/恢复、匿名计票。",
    )
    app.state.settings = settings

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


app = create_app()
