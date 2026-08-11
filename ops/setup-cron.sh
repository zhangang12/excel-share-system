#!/bin/bash
# 一键安装运维定时任务
#
# 用法:  sudo bash setup-cron.sh
#
# 会安装：
#   /etc/cron.daily/pms-backup        每日凌晨备份（cron.daily 默认 6:25）
#   /etc/cron.d/pms-health            每 5 分钟健康检查 → 失败时重启 backend
#   /etc/logrotate.d/pms-health       健康日志切割
#   /etc/cron.d/pms-certrenew         每天两次续期 HTTPS 证书

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

[[ "$(id -u)" != "0" ]] && { echo "需 root: sudo bash $0"; exit 1; }

# ===== 1. 每日备份 =====
cat > /etc/cron.daily/pms-backup <<EOF
#!/bin/bash
exec >> /var/log/pms-backup.log 2>&1
bash $SCRIPT_DIR/backup.sh
EOF
chmod +x /etc/cron.daily/pms-backup
echo "✓ 装好 /etc/cron.daily/pms-backup"

# ===== 2. 每 5 分钟健康检查（失败时自愈）=====
cat > /etc/cron.d/pms-health <<EOF
# m h dom mon dow user cmd
*/5 * * * * root bash $SCRIPT_DIR/health-check.sh --quiet >> /var/log/pms-health.log 2>&1 || (echo "[\$(date)] health failed, restarting backend"; cd $PROJECT_DIR && docker compose -f docker-compose.prod.yml --env-file .env.prod restart backend) >> /var/log/pms-health.log 2>&1
EOF
echo "✓ 装好 /etc/cron.d/pms-health"

# ===== 3. 日志轮转 =====
cat > /etc/logrotate.d/pms <<'EOF'
/var/log/pms-backup.log /var/log/pms-health.log /var/log/pms-certrenew.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    create 0644 root root
}
EOF
echo "✓ 装好 /etc/logrotate.d/pms"

# ===== 3.1 🆕 nginx 访问日志轮转（2026-08-11 日志改为落盘后必须有，否则磁盘迟早被撑爆）=====
# 背景：原来 nginx 日志只进 docker logs，**一发版重启容器就清零**——
#   2026-08-11 要查三天前"谁是用客户端登录的"，日志已经没了，只能靠推断。
#   改成 bind mount 落盘（docker-compose.prod.yml: ./nginx/logs:/var/log/nginx）后要自己轮转。
# ⚠️ postrotate 里的 `nginx -s reopen` 不能省：logrotate 是把文件改名，
#    nginx 还攥着旧的文件句柄继续往里写，不发信号的话新文件永远是空的，
#    而被改名的旧文件还在悄悄变大——磁盘照样满，日志照样查不到。
cat > /etc/logrotate.d/pms-nginx <<EOF
$PROJECT_DIR/nginx/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
    sharedscripts
    postrotate
        docker exec pms2_nginx nginx -s reopen 2>/dev/null || true
    endscript
}
EOF
echo "✓ 装好 /etc/logrotate.d/pms-nginx（nginx 访问日志 daily 轮转，留 14 天）"

# ===== 3.5 HTTPS 证书续期（每天 3:17 和 15:17）=====
# ⚠️ 为什么一天两次：Let's Encrypt 官方就是这么建议的 —— 万一某次因网络或服务端
#    问题失败，当天还有第二次机会。certbot 自己判断「剩余 <30 天」才真的去续，
#    平时就是空跑，没有额外开销。
# ⚠️ 错开整点：整点是全网 ACME 请求高峰，容易撞限流。
cat > /etc/cron.d/pms-certrenew <<EOF
# m h dom mon dow user cmd
17 3,15 * * * root bash $SCRIPT_DIR/renew-cert.sh >> /var/log/pms-certrenew.log 2>&1
EOF
echo "✓ 装好 /etc/cron.d/pms-certrenew"

# ===== 4. 重启 cron =====
systemctl restart cron 2>/dev/null || systemctl restart crond 2>/dev/null || true

echo
echo "查看安装情况:"
echo "  ls -l /etc/cron.daily/pms-backup /etc/cron.d/pms-health /etc/cron.d/pms-certrenew /etc/logrotate.d/pms"
echo "  tail -f /var/log/pms-backup.log /var/log/pms-health.log /var/log/pms-certrenew.log"
echo
echo "⚠️ 证书续期装完先跑一次演练（走完整条链路但不真签发）:"
echo "  bash $SCRIPT_DIR/renew-cert.sh --dry-run"
