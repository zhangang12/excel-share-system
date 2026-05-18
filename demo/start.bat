@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ..\backend\.venv (
    echo 首次运行请先执行 setup.bat 完成环境安装。
    pause
    exit /b 1
)
if not exist web (
    echo 缺少前端构建文件，请先执行 setup.bat
    pause
    exit /b 1
)

echo ============================================
echo  项目管理系统 - 单机演示版
echo ============================================
echo.
echo 启动中...

REM 设环境变量：用本地 SQLite + 静态前端
set "DATABASE_URL=sqlite+aiosqlite:///./data/app.db"
set "STATIC_DIR=../demo/web"
set "DEFAULT_ADMIN_PASSWORD=admin123"

REM data 目录在 backend 那边，要保证存在
if not exist ..\backend\data mkdir ..\backend\data

REM 启动后 3 秒打开浏览器
start "" /min cmd /c "timeout /t 3 >nul && start http://127.0.0.1:8000"

cd ..\backend
call .venv\Scripts\activate.bat
set "STATIC_DIR=../demo/web"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo 服务已停止。按任意键退出。
pause >nul
