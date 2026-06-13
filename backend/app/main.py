"""主入口"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

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
    aftersales_router, finance_router, feedback_router,
)
from .errors import register_exception_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)  # 🆕 v3：给存量表补新增列（幂等）
    async with SessionLocal() as db:
        await seed(db)
        await run_data_migrations(db)
    log.info("启动完成 · DB=%s", settings.database_url.split("@")[-1])
    yield
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

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": settings.app_name, "version": "2.0.0"}

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
