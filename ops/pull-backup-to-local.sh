#!/bin/bash
# 把【生产服务器】的备份(数据库 + 上传文件 uploads)定时拉取到【本地服务器】留存——异地冷备。
#
# 运行位置：在【本地服务器】(Linux / NAS / WSL / Git Bash)上跑，通过 SSH 从云端 rsync 拉取。
#           pull 模型：本地主动发起，云端无需开放任何额外端口、本地无需公网 IP。
#
# 前置：本地服务器能【免密】SSH 登录云端生产机：
#         ssh-keygen -t ed25519              # 本地生成密钥(若没有)
#         ssh-copy-id root@8.141.123.141     # 公钥送到云端
#
# 用法：
#   REMOTE=root@8.141.123.141 LOCAL_DIR=/data/pms-backup bash pull-backup-to-local.sh
#   RUN_REMOTE_BACKUP=1 REMOTE=root@8.141.123.141 bash pull-backup-to-local.sh   # 拉取前先让云端跑一次新备份
#
# 可配置(环境变量)：
#   REMOTE             云端 SSH 目标，如 root@8.141.123.141      (必填)
#   REMOTE_BACKUP_DIR  云端备份目录，默认 /backup
#   REMOTE_OPS_DIR     云端 ops 目录(触发远程备份用)，默认 /root/excel-share-system-main/ops
#   LOCAL_DIR          本地存放目录，默认 ./pms-backup-local
#   KEEP_DAYS          本地保留天数，默认 90 (异地冷备建议比云端 30 天更久)
#   SSH_KEY            指定私钥路径(可选)
#   SSH_PORT           SSH 端口，默认 22
#   RUN_REMOTE_BACKUP  =1 时先 SSH 到云端跑 backup.sh 生成最新备份再拉
#
# 退出码: 0=成功 1=连接/拉取失败 2=完整性校验失败
set -e
REMOTE="${REMOTE:?需设 REMOTE，如 REMOTE=root@8.141.123.141}"
REMOTE_BACKUP_DIR="${REMOTE_BACKUP_DIR:-/backup}"
REMOTE_OPS_DIR="${REMOTE_OPS_DIR:-/root/excel-share-system-main/ops}"
LOCAL_DIR="${LOCAL_DIR:-./pms-backup-local}"
KEEP_DAYS="${KEEP_DAYS:-90}"
SSH_PORT="${SSH_PORT:-22}"
SSH_OPT="-p $SSH_PORT -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"
[[ -n "$SSH_KEY" ]] && SSH_OPT="$SSH_OPT -i $SSH_KEY"

log(){ echo "[$(date '+%F %T')] $*"; }
mkdir -p "$LOCAL_DIR"

# 1) (可选) 先让云端生成一份最新备份
if [[ "$RUN_REMOTE_BACKUP" == "1" ]]; then
    log "触发云端生成最新备份…"
    ssh $SSH_OPT "$REMOTE" "bash $REMOTE_OPS_DIR/backup.sh" \
        || { echo "ERROR: 远程 backup.sh 执行失败" >&2; exit 1; }
fi

# 2) rsync 增量拉取(只拉 pms-*.gz；--ignore-existing 已下载过的同名文件跳过，省流量；断点续传)
log "从 $REMOTE:$REMOTE_BACKUP_DIR 拉取到 $LOCAL_DIR …"
rsync -az --partial --ignore-existing -e "ssh $SSH_OPT" \
    --include='pms-db-*.sql.gz' --include='pms-uploads-*.tar.gz' --exclude='*' \
    "$REMOTE:$REMOTE_BACKUP_DIR/" "$LOCAL_DIR/" \
    || { echo "ERROR: rsync 拉取失败(检查免密SSH/网络)" >&2; exit 1; }

# 3) 完整性校验：最新的 db / uploads 备份必须能 gzip -t 通过且非空
latest_db=$(ls -t "$LOCAL_DIR"/pms-db-*.sql.gz 2>/dev/null | head -1)
latest_up=$(ls -t "$LOCAL_DIR"/pms-uploads-*.tar.gz 2>/dev/null | head -1)
for f in "$latest_db" "$latest_up"; do
    [[ -z "$f" ]] && { echo "WARN: 未拉到某类备份(可能云端尚未生成)"; continue; }
    if ! gzip -t "$f" 2>/dev/null; then echo "ERROR: 完整性校验失败 $f" >&2; exit 2; fi
    log "✓ 校验通过 $(basename "$f") ($(du -h "$f"|cut -f1))"
done

# 4) 本地滚动清理
find "$LOCAL_DIR" -name 'pms-db-*.sql.gz'      -mtime +${KEEP_DAYS} -delete 2>/dev/null || true
find "$LOCAL_DIR" -name 'pms-uploads-*.tar.gz' -mtime +${KEEP_DAYS} -delete 2>/dev/null || true
n_db=$(ls "$LOCAL_DIR"/pms-db-*.sql.gz 2>/dev/null | wc -l)
n_up=$(ls "$LOCAL_DIR"/pms-uploads-*.tar.gz 2>/dev/null | wc -l)
log "完成。本地现存 数据库备份 ${n_db} 份 / 文件备份 ${n_up} 份 (保留 ${KEEP_DAYS} 天)"
