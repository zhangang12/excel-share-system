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
| `backup.sh` | 备份 DB + uploads 到 `/backup/`，按天滚动 | `bash ops/backup.sh` |
| `restore.sh` | 从备份恢复（先快照当前 → 重建 db → 写入） | `bash ops/restore.sh /backup/pms-db-20260514.sql.gz` |
| `health-check.sh` | 检查 9 项：5 个容器、pg/redis、API、磁盘、内存、错误日志 | `bash ops/health-check.sh` |
| `upgrade.sh` | 安全升级：备份 → git pull → rebuild → 健康检查 → 失败自动回滚 | `bash ops/upgrade.sh` |
| `rollback.sh` | 回滚到指定 commit 或上一版 | `bash ops/rollback.sh --list` |
| `reset-admin-password.sh` | 应急重置任意账号密码 | `bash ops/reset-admin-password.sh admin` |
| `diagnose.sh` | 一键打包诊断信息（日志、配置、db 状态） | `bash ops/diagnose.sh` |
| `setup-cron.sh` | 安装定时任务（备份 + 自愈） | `sudo bash ops/setup-cron.sh` |
| `db-shell.sh` | 进 psql | `bash ops/db-shell.sh` |
| `logs.sh` | 跟容器日志 | `bash ops/logs.sh backend` |

## 退出码约定

- `0` — 正常
- `1` — 业务失败（备份失败、健康检查不过）
- `2` — 系统失败（依赖工具缺失、回滚也失败）

## 配置

所有脚本读 `v2/.env.prod` 取 `POSTGRES_USER/POSTGRES_DB` 等；不要改硬编码。

环境变量覆盖：

```bash
BACKUP_DIR=/data/backup KEEP_DAYS=14 bash ops/backup.sh
COS_BUCKET=cos://pms-backup-xxx/db/ bash ops/backup.sh --upload-cos
```
