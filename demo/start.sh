#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -d ../backend/.venv ]; then
    echo "首次运行请先执行 setup.sh"
    exit 1
fi
if [ ! -d web ]; then
    echo "缺少 web/，请先执行 setup.sh"
    exit 1
fi

export DATABASE_URL="sqlite+aiosqlite:///./data/app.db"
export STATIC_DIR="../demo/web"
mkdir -p ../backend/data

# 3秒后打开浏览器
( sleep 3 && (xdg-open http://127.0.0.1:8000 || open http://127.0.0.1:8000 || true) ) &

cd ../backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
