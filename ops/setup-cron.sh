#!/bin/bash
# 一键安装运维定时任务
#
# 用法:  sudo bash setup-cron.sh
#
# 会安装：
#   /etc/cron.daily/pms-backup        每日凌晨备份（cron.daily 默认 6:25）
#   /etc/cron.d/pms-health            每 5 分钟健康检查 → 失败时重启 backend
#   /etc/logrotate.d/pms-health       健康日志切割

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
/var/log/pms-backup.log /var/log/pms-health.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    create 0644 root root
}
EOF
echo "✓ 装好 /etc/logrotate.d/pms"

# ===== 4. 重启 cron =====
systemctl restart cron 2>/dev/null || systemctl restart crond 2>/dev/null || true

echo
echo "查看安装情况:"
echo "  ls -l /etc/cron.daily/pms-backup /etc/cron.d/pms-health /etc/logrotate.d/pms"
echo "  tail -f /var/log/pms-backup.log /var/log/pms-health.log"
