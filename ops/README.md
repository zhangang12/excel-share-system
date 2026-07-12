# ops/ 运维脚本工具集

所有脚本默认从 `v2/` 项目目录运行；脚本会自己 `cd` 到 `..` 找 `docker-compose.prod.yml`。

## 一次性设置

```bash
chmod +x ops/*.sh
sudo bash ops/setup-cron.sh        # 装好每日备份 + 5 分钟健康检查
```

## 脚本一览

| 脚本 | 作用 | 典型用法 |
|---|---|---|
| `release.sh` | **本地一键发版**（在你自己电脑跑）：SSH 进服务器自动执行 `upgrade.sh` | `bash ops/release.sh` |
| `backup.sh` | 备份 DB + uploads 到 `/backup/`，按天滚动 | `bash ops/backup.sh` |
| `restore.sh` | 从备份恢复（先快照当前 → 重建 db → 写入） | `bash ops/restore.sh /backup/pms-db-20260514.sql.gz` |
| `health-check.sh` | 检查：4 个容器、pg、API、磁盘、内存、错误日志 | `bash ops/health-check.sh` |
| `enable-https.sh` | 一键申请 Let's Encrypt 证书并启用 HTTPS（443） | `bash ops/enable-https.sh pms.你的域名.com you@mail.com` |
| `upgrade.sh` | 安全升级：备份 → git pull → rebuild → 健康检查 → 失败自动回滚 | `bash ops/upgrade.sh` |
| `rollback.sh` | 回滚到指定 commit 或上一版 | `bash ops/rollback.sh --list` |
| `reset-admin-password.sh` | 应急重置任意账号密码 | `bash ops/reset-admin-password.sh admin` |
| `diagnose.sh` | 一键打包诊断信息（日志、配置、db 状态） | `bash ops/diagnose.sh` |
| `setup-cron.sh` | 安装定时任务（备份 + 自愈） | `sudo bash ops/setup-cron.sh` |
| `db-shell.sh` | 进 psql | `bash ops/db-shell.sh` |
| `logs.sh` | 跟容器日志 | `bash ops/logs.sh backend` |

## 本地一键发版（release.sh）

`release.sh` 跑在**你自己的电脑**上（不是服务器），通过 SSH 触发服务器端的 `upgrade.sh`，
一条命令完成「(可选)推代码 → SSH 进服务器 → 备份 → 拉码 → 重建 → 健康检查 → 失败自动回滚」。

```bash
# 首次：填服务器信息（该文件已 gitignore，不入库；用 SSH 密钥免密登录）
cp .deploy.local.example .deploy.local && nano .deploy.local
ssh-copy-id user@服务器            # 若还没配免密登录

# 之后每次发版：
bash ops/release.sh               # 部署 GitHub 上最新 main
bash ops/release.sh --push        # 先把本地提交推到 main 再部署
bash ops/release.sh --logs        # 部署成功后跟随后端日志
bash ops/release.sh --health      # 只在服务器跑一次健康检查
bash ops/release.sh --dry-run     # 只看会执行什么，不真跑
```

发版失败时服务器会自动回滚（线上仍是旧版可用），`release.sh` 会把退出码含义打印出来。

## 退出码约定

- `0` — 正常
- `1` — 业务失败（备份失败、健康检查不过；upgrade.sh 已自动回滚）
- `2` — 系统失败（依赖工具缺失、回滚也失败，需人工介入）

## 配置

所有脚本读 `v2/.env.prod` 取 `POSTGRES_USER/POSTGRES_DB` 等；不要改硬编码。

环境变量覆盖：

```bash
BACKUP_DIR=/data/backup KEEP_DAYS=14 bash ops/backup.sh
COS_BUCKET=cos://pms-backup-xxx/db/ bash ops/backup.sh --upload-cos   # 腾讯云
OSS_BUCKET=oss://pms-backup-xxx/db/ bash ops/backup.sh --upload-oss   # 阿里云
```
