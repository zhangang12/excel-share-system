@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo  项目管理系统 - 单机版首次安装
echo ============================================
echo.

REM ---- 检查 Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python
    echo 请安装 Python 3.10+：https://www.python.org/downloads/
    echo 安装时勾选 "Add Python to PATH"
    pause
    exit /b 1
)
echo [1/5] Python 已就绪
python --version

REM ---- 检查 Node ----
where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Node.js
    echo 请安装 Node.js 20 LTS：https://nodejs.org/
    pause
    exit /b 1
)
echo [2/5] Node.js 已就绪
node --version

REM ---- 安装后端依赖 ----
echo.
echo [3/5] 安装后端依赖（首次约 2 分钟）...
cd ..\backend
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [错误] 后端依赖安装失败
    pause
    exit /b 1
)
cd ..\demo

REM ---- 安装前端依赖 ----
echo.
echo [4/5] 安装前端依赖（首次约 2-3 分钟）...
cd ..\frontend
if not exist node_modules (
    call npm install --registry=https://registry.npmmirror.com
    if errorlevel 1 (
        echo [错误] 前端依赖安装失败
        pause
        exit /b 1
    )
)

REM ---- 构建前端 ----
echo.
echo [5/5] 构建前端（生成 dist/）...
call npm run build
if errorlevel 1 (
    echo [错误] 前端构建失败
    pause
    exit /b 1
)
cd ..\demo

REM ---- 把构建产物复制到 demo/web ----
echo.
echo 复制前端到演示目录...
if exist web rmdir /s /q web
mkdir web
xcopy /e /q ..\frontend\dist\* web\

REM ---- data 目录 ----
if not exist data mkdir data

echo.
echo ============================================
echo  ✅ 安装完成！
echo.
echo  接下来双击 start.bat 启动演示
echo ============================================
pause
