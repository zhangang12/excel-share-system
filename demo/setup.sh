#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "=== 安装后端 ==="
cd ../backend
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "=== 安装前端 ==="
cd ../frontend
[ -d node_modules ] || npm install --registry=https://registry.npmmirror.com

echo "=== 构建前端 ==="
npm run build

cd ../demo
rm -rf web
cp -r ../frontend/dist web
mkdir -p data

echo "✅ 完成。运行 ./start.sh 启动演示。"
