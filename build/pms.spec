# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 规格：把 backend + 前端 dist 打成单 .exe
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).resolve()           # v2/build
BACKEND = ROOT.parent / "backend"
FRONTEND_DIST = ROOT.parent / "frontend" / "dist"

# 校验前端已构建
if not FRONTEND_DIST.exists():
    raise SystemExit(
        f"前端 dist 不存在: {FRONTEND_DIST}\n"
        "请先在 frontend 目录跑：npm install && npm run build"
    )

# 把整个 app/ 包当成数据 + 模块都引入
APP_DIR = BACKEND / "app"

# 数据：前端 + 后端 app 目录
datas = [
    (str(FRONTEND_DIST), "web"),
]

# hiddenimports：动态导入的包，逐个收集（缺失的包容错跳过）
hiddenimports = []
for _pkg in ("uvicorn", "fastapi", "sqlalchemy", "aiosqlite", "openpyxl",
             "xlrd", "starlette", "anyio", "pydantic", "pydantic_settings",
             "bcrypt", "jwt", "multipart", "python_multipart", "email_validator"):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass
hiddenimports += ["app", "app.main", "app.config", "app.database", "app.models",
                  "app.schemas", "app.auth", "app.deps", "app.errors",
                  "app.seed", "app.utils",
                  "app.routers", "app.routers.auth_router", "app.routers.admin_router",
                  "app.routers.projects_router", "app.routers.datasheets_router",
                  "app.routers.excel_router", "app.routers.overview_router",
                  "app.routers.field_perm_router", "app.routers.ws_router"]

datas += collect_data_files("openpyxl")

block_cipher = None

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.testing", "PIL", "scipy"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pms-demo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
