#!/bin/bash
# HTTPS 证书自动续期。由 /etc/cron.d/pms-certrenew 每天跑两次（见 ops/setup-cron.sh）。
#
#   bash ops/renew-cert.sh              续期（没到期就什么都不做，certbot 自己判断）
#   bash ops/renew-cert.sh --dry-run    走一遍完整流程但不真的签发（装完必须先跑这个）
#   bash ops/renew-cert.sh --force      强制续期（排障用，注意 Let's Encrypt 有频率限制）
#
# ⚠️⚠️ **光跑 `certbot renew` 是不够的**，这是这个脚本存在的全部理由：
#    certbot 只会更新 `nginx/letsencrypt/live/<域名>/` 下的文件，
#    而 nginx 容器挂载的是 **`nginx/certs/`**（见 docker-compose.prod.yml）。
#    不把新证书拷过去、不 reload，nginx 会一直用着旧证书直到过期 ——
#    **续期"成功"了，网站照样在到期那天挂掉**，而且日志里一片绿。
#
# ⚠️ 续期走 webroot（HTTP-01），要求 80 端口能被外网访问到
#    `/.well-known/acme-challenge/`。所以 80 端口那个 server 块里的
#    ACME location **不能删**，也不能整体 301 跳 HTTPS 把它挡掉。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

DOMAIN="${CERT_DOMAIN:-www.tonghuizhineng.top}"
WEBROOT="$ROOT/nginx/certbot-www"
LE_DIR="$ROOT/nginx/letsencrypt"
CERTS_DIR="$ROOT/nginx/certs"
LIVE="$LE_DIR/live/$DOMAIN"

MODE=""
case "${1:-}" in
  --dry-run) MODE="--dry-run" ;;
  --force)   MODE="--force-renewal" ;;
  "")        ;;
  *) echo "未知参数: $1"; exit 1 ;;
esac

log() { echo "[$(date '+%F %T')] $*"; }

log "开始续期检查（域名 $DOMAIN${MODE:+，模式 $MODE}）"

[[ -d "$LIVE" ]] || { log "ERROR: 找不到 $LIVE，这台机器还没签过证书"; exit 1; }

# 记录续期前的有效期，用来判断到底有没有换新（certbot 没到期时会跳过，这是正常的）
before=$(openssl x509 -in "$CERTS_DIR/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
log "当前证书到期: ${before:-未知}"

docker run --rm \
    -v "$WEBROOT":/var/www/certbot \
    -v "$LE_DIR":/etc/letsencrypt \
    certbot/certbot renew --webroot -w /var/www/certbot \
    --non-interactive $MODE 2>&1 | sed 's/^/  certbot| /'
rc=${PIPESTATUS[0]}
if [[ $rc -ne 0 ]]; then
    log "ERROR: certbot 退出码 $rc，续期失败"
    exit 1
fi

if [[ -n "$MODE" && "$MODE" == "--dry-run" ]]; then
    log "dry-run 结束：整条链路（含 HTTP-01 验证）走通，未真正签发"
    exit 0
fi

# ── 关键的第二步：把新证书装到 nginx 真正读的位置 ──
if [[ ! -f "$LIVE/fullchain.pem" ]]; then
    log "ERROR: $LIVE/fullchain.pem 不存在"
    exit 1
fi
cp -L "$LIVE/fullchain.pem" "$CERTS_DIR/fullchain.pem"
cp -L "$LIVE/privkey.pem"  "$CERTS_DIR/privkey.pem"
chmod 600 "$CERTS_DIR/privkey.pem"

after=$(openssl x509 -in "$CERTS_DIR/fullchain.pem" -noout -enddate 2>/dev/null | cut -d= -f2)

if [[ "$before" == "$after" ]]; then
    log "证书还没到续期窗口（剩余 >30 天），未更换 —— 这是正常的，不用管"
    exit 0
fi

log "证书已更新: $before → $after"

# ── 第三步：reload。⚠️ 不 reload 的话 nginx 还捧着内存里的旧证书 ──
if ! docker exec pms2_nginx nginx -t 2>&1 | tail -1 | grep -q successful; then
    log "ERROR: 新证书装上后 nginx -t 不通过，**没有 reload**，线上仍是旧证书"
    exit 1
fi
docker exec pms2_nginx nginx -s reload
log "nginx 已 reload，新证书生效"

# 复检：真的从 443 取一次证书，确认换的是这一张
serving=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [[ "$serving" == "$after" ]]; then
    log "复检通过：443 上对外提供的就是新证书（到期 $serving）"
else
    log "WARN: 443 上取到的证书到期是 '$serving'，与预期 '$after' 不符，去看看"
    exit 1
fi
