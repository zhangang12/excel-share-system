"""主入口"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from .config import settings
from .database import Base, engine, SessionLocal
from . import models  # noqa: F401
from .seed import seed
from .data_migration import run_all as run_data_migrations, ensure_schema_columns
from .routers import (
    auth_router, admin_router, projects_router, datasheets_router,
    excel_router, overview_router, field_perm_router, ws_router,
    attachments_router, messages_router, orders_router, sales_router,
    logistics_router, collab_router, downstream_router,
    aftersales_router, finance_router, feedback_router, reports_router,
    warehouse_router, export_router, user_feedback_router,
    produce_router, leads_router, purchase_mgmt_router, oa_router,
)
from .errors import register_exception_handlers
from .database import get_db
from .deps import require_admin_or_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from .overdue import overdue_scheduler
    # 🆕 生产用 --workers 4：4 个进程各自独立跑一遍启动流程。ensure_schema_columns 是
    # "先查列是否存在、不存在再 ALTER TABLE ADD COLUMN"，seed 是"先查角色/用户是否存在、
    # 不存在再 INSERT"——两者都不是原子操作。4 个 worker 几乎同时启动时会一起判定"不存在"，
    # 然后一起执行 ALTER/INSERT，后面几个必然撞上 DuplicateColumn/UniqueViolation 并抛出未捕获
    # 异常，导致该 worker 进程启动失败退出——外部表现就是 nginx 间歇性 502（只有落在挂掉的
    # worker 上的请求出错，其余 worker 正常，所以是部分请求偶发，不是整站瘫痪）。
    # 用 Postgres 会话级 advisory lock 把这段串行化：同一时刻只有一个 worker 在跑，
    # 其余 worker 排队等它跑完，等到自己时该加的列/该建的角色都已存在，直接跳过，无竞态。
    lock_conn = None
    if engine.dialect.name == "postgresql":
        lock_conn = await engine.connect()
        await lock_conn.execute(text("SELECT pg_advisory_lock(872193641)"))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await ensure_schema_columns(engine)  # 🆕 v3：给存量表补新增列（幂等）
        async with SessionLocal() as db:
            await seed(db)
            await run_data_migrations(db)
    finally:
        if lock_conn is not None:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(872193641)"))
            await lock_conn.close()
    files_path = Path(settings.files_dir).resolve()
    log.info("启动完成 · DB=%s · 文件目录=%s (可写=%s)",
             settings.database_url.split("@")[-1], files_path,
             os.access(files_path, os.W_OK))
    task = asyncio.create_task(overdue_scheduler())  # 🆕 v3 M15 逾期每日提醒
    yield
    task.cancel()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="2.0.0",
        description="类飞书多维表格的项目进度管理系统",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(auth_router.router)
    app.include_router(admin_router.router)
    app.include_router(projects_router.router)
    app.include_router(datasheets_router.router)
    app.include_router(excel_router.router)
    app.include_router(overview_router.router)
    app.include_router(field_perm_router.router)
    app.include_router(ws_router.router)
    # 🆕 v3 增量路由
    app.include_router(attachments_router.router)
    app.include_router(messages_router.router)
    app.include_router(orders_router.router)
    app.include_router(sales_router.router)
    app.include_router(logistics_router.router)
    app.include_router(collab_router.router)
    app.include_router(downstream_router.router)
    app.include_router(aftersales_router.router)
    app.include_router(finance_router.router)
    app.include_router(feedback_router.router)
    app.include_router(reports_router.router)
    app.include_router(warehouse_router.router)
    app.include_router(export_router.router)
    app.include_router(user_feedback_router.router)  # 🆕 用户反馈小助手
    app.include_router(produce_router.router)  # 🆕 生产部分组派发（钣金组/装配组）
    app.include_router(leads_router.router)    # 🆕 销售线索跟踪
    app.include_router(purchase_mgmt_router.router)  # 🆕 采购管理模块
    app.include_router(oa_router.router)  # 🆕 OA 审批模块

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": settings.app_name, "version": "2.0.0"}

    # 🆕 v3 M15：逾期扫描手动/cron 触发（管理层）
    @app.post("/api/internal/overdue-scan")
    async def overdue_scan_now(
        current=Depends(require_admin_or_manager),
        db=Depends(get_db),
    ):
        from .overdue import scan_overdue
        return await scan_overdue(db)

    # 🆕 尾款到期提醒手动/cron 触发（管理层）：到期前14天起，每条台账只提醒一次
    @app.post("/api/internal/balance-due-scan")
    async def balance_due_scan_now(
        current=Depends(require_admin_or_manager),
        db=Depends(get_db),
    ):
        from .overdue import scan_balance_due
        return await scan_balance_due(db)

    # ===== 演示模式：托管前端静态资源 =====
    static_path = Path(settings.static_dir).resolve()
    if static_path.exists():
        # /assets, /favicon 等
        assets = static_path / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(static_path / "index.html")

        # SPA fallback：其它非 API 路径都返回 index.html，让 vue-router 接管
        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            # 跳过 api / ws / docs / openapi
            for prefix in ("api/", "ws/", "docs", "openapi.json", "redoc"):
                if full_path.startswith(prefix):
                    return JSONResponse({"detail": "not found"}, status_code=404)
            f = static_path / full_path
            if f.is_file():
                return FileResponse(f)
            # 否则回到 SPA 入口
            return FileResponse(static_path / "index.html")

    return app


app = create_app()
