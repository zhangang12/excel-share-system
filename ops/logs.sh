#!/bin/bash
# 看日志快捷脚本
#
# 用法:
#   bash logs.sh                  # 跟 backend 日志（默认）
#   bash logs.sh nginx            # 跟 nginx
#   bash logs.sh postgres         # 跟 postgres
#   bash logs.sh all              # 跟所有容器
#   bash logs.sh backend 500      # 显示 backend 最近 500 行
#   bash logs.sh backend errors   # 只看 backend 的 error/exception
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

SVC="${1:-backend}"
ARG="${2:-tail}"

# 容器名映射
case "$SVC" in
    all) docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f --tail 100; exit;;
    backend|api) C=pms2_backend;;
    frontend|web) C=pms2_frontend;;
    nginx) C=pms2_nginx;;
    db|postgres|pg) C=pms2_postgres;;
    redis) C=pms2_redis;;
    *) C="$SVC";;
esac

case "$ARG" in
    tail) exec docker logs -f --tail 100 "$C";;
    errors|err) docker logs "$C" 2>&1 | grep -iE 'error|exception|traceback|warning' | tail -50;;
    [0-9]*) exec docker logs --tail "$ARG" "$C";;
    *) exec docker logs --tail 100 "$C";;
esac
