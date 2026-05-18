#!/bin/bash
# ============================================================
# 同辉智能项目管理系统 —— 一键部署脚本（生产环境 / Linux + Docker）
#
# 用法:
#   bash deploy.sh           首次/更新部署
#   bash deploy.sh --logs    部署后跟随日志
#
# 首次运行会自动生成 .env.prod（含随机 SECRET_KEY），
# 提示你改密码后再次运行即可完成部署。
# ============================================================
set -e
cd "$(dirname "$0")"

COMPOSE="docker-compose.prod.yml"
ENV_FILE=".env.prod"

echo "============================================"
echo "  同辉智能项目管理系统 · 一键部署"
echo "============================================"

# ---- 1. 检查 Docker ----
if ! command -v docker >/dev/null 2>&1; then
    echo "[错误] 未检测到 Docker。请先安装："
    echo "  curl -fsSL https://get.docker.com | sh"
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "[错误] 未检测到 docker compose 插件，请升级 Docker。"
    exit 1
fi
echo "[1/4] Docker 检查通过"

# ---- 2. 准备 .env.prod ----
if [ ! -f "$ENV_FILE" ]; then
    echo "[2/4] 未找到 $ENV_FILE，正在生成..."
    cp .env.prod.example "$ENV_FILE"
    # 自动生成强随机 SECRET_KEY
    if command -v openssl >/dev/null 2>&1; then
        SECRET=$(openssl rand -hex 32)
        # 用 | 作分隔符，避免 SECRET 里的特殊字符
        sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" "$ENV_FILE"
        echo "      已自动生成随机 SECRET_KEY"
    fi
    echo ""
    echo "  >>> 请编辑 $ENV_FILE，修改以下两项后再次运行本脚本： <<<"
    echo "      - POSTGRES_PASSWORD   （改为 16 位以上强密码）"
    echo "      - DEFAULT_ADMIN_PASSWORD （管理员初始密码，登录后再改）"
    echo ""
    echo "      编辑命令:  nano $ENV_FILE"
    echo "      编辑完成后重新执行:  bash deploy.sh"
    exit 0
fi

# 校验关键项是否还是占位符
if grep -qE 'POSTGRES_PASSWORD=请改|SECRET_KEY=必须' "$ENV_FILE"; then
    echo "[错误] $ENV_FILE 中仍有未修改的占位值（POSTGRES_PASSWORD / SECRET_KEY）。"
    echo "       请先编辑：nano $ENV_FILE"
    exit 1
fi
echo "[2/4] 环境配置 $ENV_FILE 就绪"

# ---- 3. 构建并启动 ----
echo "[3/4] 构建并启动容器（首次约 5-10 分钟）..."
docker compose -f "$COMPOSE" --env-file "$ENV_FILE" up -d --build

# ---- 4. 健康检查 ----
echo "[4/4] 等待服务就绪..."
OK=0
for i in $(seq 1 45); do
    sleep 2
    if curl -fs http://localhost/api/health >/dev/null 2>&1; then
        OK=1
        break
    fi
done

echo ""
docker compose -f "$COMPOSE" --env-file "$ENV_FILE" ps
echo ""
if [ "$OK" = "1" ]; then
    echo "============================================"
    echo "  ✓ 部署成功"
    echo "    访问:  http://<服务器IP>/   （配好域名+HTTPS 后用域名）"
    echo "    默认账号见 $ENV_FILE，登录后请立即改密码"
    echo "============================================"
else
    echo "[警告] 45 秒内健康检查未通过，请查看日志排查："
    echo "  docker compose -f $COMPOSE logs -f backend"
    exit 1
fi

# ---- 可选：跟随日志 ----
if [ "$1" = "--logs" ]; then
    docker compose -f "$COMPOSE" --env-file "$ENV_FILE" logs -f
fi
