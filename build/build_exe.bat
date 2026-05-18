@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Build standalone .exe (first run: 8-15 min)
echo ============================================

REM ---- 0. Check Python / Node ----
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install Node.js 20 LTS from nodejs.org
    pause
    exit /b 1
)
echo Python:
python --version
echo Node:
node --version

REM ---- 1. Build frontend ----
echo.
echo [1/4] Building frontend dist...
pushd ..\frontend
REM always run npm install (idempotent; fixes incomplete node_modules)
call npm install --registry=https://registry.npmmirror.com
if errorlevel 1 (
    echo frontend npm install failed
    popd
    pause
    exit /b 1
)
call npm run build
if errorlevel 1 (
    echo frontend build failed
    popd
    pause
    exit /b 1
)
popd

REM ---- 2. Prepare backend venv + deps + pyinstaller ----
echo.
echo [2/4] Preparing backend env...
pushd ..\backend
if not exist .venv-build (
    python -m venv .venv-build
)
call .venv-build\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo backend deps install failed
    popd
    pause
    exit /b 1
)
python -m pip install --quiet pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo PyInstaller install failed
    popd
    pause
    exit /b 1
)
popd

REM ---- 3. PyInstaller package ----
echo.
echo [3/4] PyInstaller packaging (please wait)...
call ..\backend\.venv-build\Scripts\activate.bat
pyinstaller pms.spec --noconfirm --clean
if errorlevel 1 (
    echo packaging failed
    pause
    exit /b 1
)

REM ---- 4. Collect artifacts ----
echo.
echo [4/4] Collecting artifacts...
if exist out rmdir /s /q out
mkdir out
copy /y dist\pms-demo.exe out\ >nul

REM Copy bundled Chinese readme to out folder (PowerShell handles Unicode filename)
if exist readme_zh.txt powershell -NoProfile -Command "Copy-Item -LiteralPath 'readme_zh.txt' -Destination ('out\' + [char]0x4F7F + [char]0x7528 + [char]0x8BF4 + [char]0x660E + '.txt')"

REM Display size
for %%I in (out\pms-demo.exe) do set EXESIZE=%%~zI
set /a EXESIZE_MB=%EXESIZE% / 1048576

echo.
echo ============================================
echo   Build complete!
echo.
echo   Artifact: %CD%\out\pms-demo.exe
echo   Size:     %EXESIZE_MB% MB
echo.
echo   Test: go to out\ and double-click pms-demo.exe
echo   Share: zip the whole out\ folder and send to customer
echo ============================================
pause
