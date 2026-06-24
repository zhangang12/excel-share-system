#!/bin/bash
# 综合健康检查
#
# 用法:
#   bash health-check.sh                    # 全部检查
#   bash health-check.sh --quiet            # 只输出不正常项（cron 用）
#   bash health-check.sh --json             # JSON 输出
#
# 退出码: 0=全 OK，非 0=失败数量

set +e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
QUIET=0
JSON=0
[[ "$1" == "--quiet" ]] && QUIET=1
[[ "$1" == "--json"  ]] && JSON=1

FAILS=0   # 硬失败：计入退出码，upgrade.sh 据此回滚
WARNS=0   # 告警：仅提示，不计入退出码（不阻断升级，避免少量良性噪声触发回滚）
RESULTS=()

check() {
    local label="$1"
    local status="$2"   # ok|fail|warn
    local detail="$3"
    RESULTS+=("$label|$status|$detail")
    case "$status" in
        ok)   [[ "$QUIET" == "0" ]] && echo "  [OK]   $label  $detail";;
        warn) echo "  [WARN] $label  $detail"; WARNS=$((WARNS+1));;
        fail) echo "  [FAIL] $label  $detail"; FAILS=$((FAILS+1));;
    esac
}

[[ "$QUIET" == "0" && "$JSON" == "0" ]] && echo "===== 系统健康检查 $(date '+%F %T') ====="

# 1. 4 个容器都在跑
for svc in pms2_postgres pms2_backend pms2_frontend pms2_nginx; do
    state=$(docker inspect -f '{{.State.Status}}' "$svc" 2>/dev/null)
    if [[ "$state" == "running" ]]; then
        check "container/$svc" ok "running"
    else
        check "container/$svc" fail "state=$state"
    fi
done

# 2. postgres 可连
if docker exec pms2_postgres pg_isready -U pms_prod >/dev/null 2>&1; then
    check "postgres" ok "accepting connections"
else
    check "postgres" fail "pg_isready 失败"
fi

# 4. backend /api/health
HTTP_CODE=$(curl -s -o /tmp/_hc -w '%{http_code}' http://localhost/api/health 2>/dev/null)
if [[ "$HTTP_CODE" == "200" ]] && grep -q '"status":"ok"' /tmp/_hc 2>/dev/null; then
    check "api/health" ok "$(cat /tmp/_hc)"
else
    check "api/health" fail "http=$HTTP_CODE"
fi
rm -f /tmp/_hc

# 5. 磁盘
DISK=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [[ "$DISK" -ge 90 ]]; then
    check "disk-root" fail "${DISK}% used"
elif [[ "$DISK" -ge 80 ]]; then
    check "disk-root" warn "${DISK}% used"
else
    check "disk-root" ok "${DISK}% used"
fi

# 6. 内存
MEM=$(free | awk '/^Mem:/ {printf "%.0f", ($3/$2)*100}')
if [[ "$MEM" -ge 90 ]]; then
    check "memory" warn "${MEM}% used"
else
    check "memory" ok "${MEM}% used"
fi

# 7. 数据库大小
DB_SIZE=$(docker exec pms2_postgres psql -U pms_prod -d pms -tA -c \
    "SELECT pg_size_pretty(pg_database_size('pms'));" 2>/dev/null)
[[ -n "$DB_SIZE" ]] && check "db-size" ok "$DB_SIZE"

# 8. 备份新鲜度
BACKUP_DIR="${BACKUP_DIR:-/backup}"
LATEST=$(ls -t "$BACKUP_DIR"/pms-db-*.sql.gz 2>/dev/null | head -1)
if [[ -z "$LATEST" ]]; then
    check "backup-fresh" warn "$BACKUP_DIR 下无备份"
else
    AGE_HOURS=$(( ($(date +%s) - $(stat -c %Y "$LATEST")) / 3600 ))
    if [[ "$AGE_HOURS" -gt 36 ]]; then
        check "backup-fresh" warn "最近备份是 ${AGE_HOURS}h 前"
    else
        check "backup-fresh" ok "最近备份 ${AGE_HOURS}h 前"
    fi
fi

# 9. 异常日志（近 1h）
# 注意: grep -c 无匹配时已输出 "0" 并以非0退出, 不能再 `|| echo 0`(会变成 "0\n0" 致 [[ 算术语法错)。
ERR_CNT=$(docker logs --since 1h pms2_backend 2>&1 | grep -ciE 'ERROR|Traceback|Exception' || true)
ERR_CNT=$(echo "$ERR_CNT" | head -1); ERR_CNT=${ERR_CNT:-0}
if [[ "$ERR_CNT" -gt 50 ]]; then
    check "backend-errors-1h" fail "$ERR_CNT errors in last 1h"
elif [[ "$ERR_CNT" -gt 5 ]]; then
    check "backend-errors-1h" warn "$ERR_CNT errors in last 1h"
else
    check "backend-errors-1h" ok "$ERR_CNT errors"
fi

# === Output ===
if [[ "$JSON" == "1" ]]; then
    echo -n '{"fails":'$FAILS',"warns":'$WARNS',"checks":['
    first=1
    for r in "${RESULTS[@]}"; do
        IFS='|' read -r l s d <<< "$r"
        [[ "$first" == "1" ]] || echo -n ','
        first=0
        echo -n "{\"label\":\"$l\",\"status\":\"$s\",\"detail\":\"$d\"}"
    done
    echo ']}'
else
    [[ "$QUIET" == "0" || "$FAILS" -gt 0 || "$WARNS" -gt 0 ]] && \
        echo "===== $FAILS 项异常 / $WARNS 项告警(告警不阻断升级) ====="
fi

# 退出码只反映硬失败：upgrade.sh 仅在 FAIL 时回滚；告警(磁盘 80%/少量错误日志等)不回滚
exit $FAILS
