#!/bin/bash
# 一键启用 HTTPS：申请 Let's Encrypt 证书 + 生成带 HTTPS 的 nginx 配置 + 平滑重载。
#
# 前置条件（务必先满足）：
#   1) 域名已解析到本机公网 IP（A 记录）。
#   2) 阿里云安全组已放开 80 和 443。
#   3) 已用最新 docker-compose.prod.yml 起过一次（nginx 才有 certbot 验证目录挂载）：
#        docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
#
# 用法:
#   bash ops/enable-https.sh <域名> [邮箱]
#   例: bash ops/enable-https.sh pms.tonghui.com  ops@tonghui.com
#
# 说明：本脚本会改写 nginx/conf.d/default.conf（已自动备份）。若你用 git pull 部署，
#       建议运行后执行 `git update-index --skip-worktree nginx/conf.d/default.conf`，
#       让以后的 pull 不覆盖你的 HTTPS 配置。

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

DOMAIN="$1"
EMAIL="${2:-admin@$1}"
if [[ -z "$DOMAIN" ]]; then
    echo "用法: bash ops/enable-https.sh <域名> [邮箱]" >&2
    exit 1
fi

WEBROOT="$PROJECT_DIR/nginx/certbot-www"
LE_DIR="$PROJECT_DIR/nginx/letsencrypt"
CERTS_DIR="$PROJECT_DIR/nginx/certs"
CONF="$PROJECT_DIR/nginx/conf.d/default.conf"
mkdir -p "$WEBROOT" "$LE_DIR" "$CERTS_DIR"

echo "===== [1/5] 前置检查 ====="
if ! docker inspect -f '{{.State.Status}}' pms2_nginx 2>/dev/null | grep -q running; then
    echo "ERROR: pms2_nginx 未运行。请先 docker compose -f docker-compose.prod.yml --env-file .env.prod up -d" >&2
    exit 1
fi
# 验证 nginx 能对外提供 ACME 验证目录（写个探针文件，本机 curl 一下）
PROBE="acme-probe-$$"
echo "ok" > "$WEBROOT/.well-known/acme-challenge/$PROBE" 2>/dev/null || {
    mkdir -p "$WEBROOT/.well-known/acme-challenge"; echo "ok" > "$WEBROOT/.well-known/acme-challenge/$PROBE"; }
if ! curl -fs "http://$DOMAIN/.well-known/acme-challenge/$PROBE" | grep -q ok; then
    echo "WARN: http://$DOMAIN/.well-known/acme-challenge/ 探测失败。" >&2
    echo "      请确认：域名解析正确、安全组放开 80、nginx 已挂载 certbot-www（用最新 prod compose 起过）。" >&2
    rm -f "$WEBROOT/.well-known/acme-challenge/$PROBE"
    exit 1
fi
rm -f "$WEBROOT/.well-known/acme-challenge/$PROBE"
echo "  → 验证目录可达 ✓"

echo "===== [2/5] 申请证书（certbot webroot）====="
docker run --rm \
    -v "$WEBROOT":/var/www/certbot \
    -v "$LE_DIR":/etc/letsencrypt \
    certbot/certbot certonly --webroot -w /var/www/certbot \
    -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --no-eff-email

LIVE="$LE_DIR/live/$DOMAIN"
if [[ ! -f "$LIVE/fullchain.pem" ]]; then
    echo "ERROR: 证书未生成（$LIVE/fullchain.pem 不存在）" >&2
    exit 1
fi

echo "===== [3/5] 安装证书到 nginx/certs ====="
cp -L "$LIVE/fullchain.pem" "$CERTS_DIR/fullchain.pem"
cp -L "$LIVE/privkey.pem"  "$CERTS_DIR/privkey.pem"
echo "  → $CERTS_DIR/{fullchain,privkey}.pem ✓"

echo "===== [4/5] 生成 HTTPS 版 nginx 配置 ====="
cp -f "$CONF" "$CONF.pre-https.bak.$(date +%Y%m%d_%H%M%S)"
cat > "$CONF" <<'EOF'
# 由 ops/enable-https.sh 生成（HTTPS 启用版）
limit_req_zone $binary_remote_addr zone=login_limit:10m rate=10r/m;
limit_req_status 429;

# HTTP：仅放 ACME 续期验证，其余全部 301 跳 HTTPS
server {
    listen 80;
    server_name __DOMAIN__;
    server_tokens off;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location ~ ^/WW_verify_.*\.txt$ { root /var/www/wecom; default_type text/plain; access_log off; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name __DOMAIN__;
    server_tokens off;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    client_max_body_size 50M;

    location / {
        proxy_pass http://frontend:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /api/auth/login {
        limit_req zone=login_limit burst=5 nodelay;
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }

    location /ws/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
EOF
sed -i "s/__DOMAIN__/$DOMAIN/g" "$CONF"

echo "===== [5/5] 校验配置并重载 nginx ====="
if docker exec pms2_nginx nginx -t; then
    docker exec pms2_nginx nginx -s reload
    echo "  → HTTPS 已启用 ✓   访问 https://$DOMAIN"
else
    echo "ERROR: nginx 配置校验失败，已回滚到 HTTP 配置" >&2
    cp -f "$(ls -t "$CONF".pre-https.bak.* | head -1)" "$CONF"
    docker exec pms2_nginx nginx -s reload || true
    exit 1
fi

# 自动续期 cron（每天 3:30 尝试续期，到期前才真正续；续后拷贝证书并重载）
CRON=/etc/cron.d/pms-certbot-renew
if [[ -w /etc/cron.d ]] || [[ "$(id -u)" == "0" ]]; then
    cat > "$CRON" <<EOF
30 3 * * * root cd $PROJECT_DIR && docker run --rm -v $WEBROOT:/var/www/certbot -v $LE_DIR:/etc/letsencrypt certbot/certbot renew --webroot -w /var/www/certbot --quiet && cp -L $LIVE/fullchain.pem $CERTS_DIR/fullchain.pem && cp -L $LIVE/privkey.pem $CERTS_DIR/privkey.pem && docker exec pms2_nginx nginx -s reload >> /var/log/pms-certbot.log 2>&1
EOF
    echo "  → 已写入自动续期 cron: $CRON"
else
    echo "  → 提示：未能写入 /etc/cron.d，请手动加一条续期 cron（用 root）。"
fi

echo
echo "完成。建议接着执行（让以后 git pull 不覆盖你的 HTTPS 配置）:"
echo "  git update-index --skip-worktree nginx/conf.d/default.conf"
