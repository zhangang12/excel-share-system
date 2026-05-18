# 部署 · 运维 · 应急 综合手册

> 系统：项目文件管理系统 v2（FastAPI + Vue 3 + Postgres + Redis + Nginx，5 容器）
> 适用：腾讯云 / 阿里云 / 任意 Docker Linux 主机
> 配套：`ops/` 脚本目录、`DEPLOY-TENCENT.md`、`DEPLOY-ALIYUN.md`

---

## 目录

1. [架构 & 端口](#1-架构--端口)
2. [部署快速通道](#2-部署快速通道)
3. [日常运维 SOP](#3-日常运维-sop)
4. [备份与恢复](#4-备份与恢复)
5. [升级与回滚](#5-升级与回滚)
6. [监控与告警](#6-监控与告警)
7. [应急预案（故障 SOP）](#7-应急预案故障-sop)
8. [常见命令速查](#8-常见命令速查)
9. [安全清单](#9-安全清单)
10. [运维联系方式](#10-运维联系方式)

---

## 1. 架构 & 端口

```
                                     ┌──────────────┐
            Client ──HTTPS:443──► ┌─►│  nginx       │ (反代 + SSL)
                                  │  └──────┬───────┘
                                  │         │
                              ┌───┴───┐   ┌─┴────────┐
                              │frontend│   │ backend  │ uvicorn
                              └────────┘   │ FastAPI  │
                                           └─┬──┬─────┘
                                             │  │
                                ┌────────────┘  └──────┐
                                ▼                      ▼
                          ┌─────────┐            ┌─────────┐
                          │postgres │            │  redis  │
                          └─────────┘            └─────────┘
```

| 容器 | 端口（容器内） | 暴露 | 角色 |
|---|---|---|---|
| nginx | 80 / 443 | ✓ 公网 | 反代 + SSL |
| frontend | 5173 | ✗ | Vue 静态资源（生产构建后由 nginx 直接服务） |
| backend | 8000 | ✗ | FastAPI |
| postgres | 5432 | ✗ | 数据库（仅容器内可达） |
| redis | 6379 | ✗ | 缓存（仅容器内可达） |

**关键路径：**
- 项目目录：`/opt/pms/v2/`
- 备份目录：`/backup/`（cron 默认）
- 数据卷：`postgres_data` / `redis_data` / `uploads_data` / `nginx_logs`
- 日志：各容器 stdout（json-file driver，自动 100MB×3 滚动）

---

## 2. 部署快速通道

> 完整步骤见 [DEPLOY-TENCENT.md](DEPLOY-TENCENT.md) / [DEPLOY-ALIYUN.md](DEPLOY-ALIYUN.md)

### 全新部署（10 分钟流程）

```bash
# 1. 在腾讯云 / 阿里云买 Ubuntu 22.04 服务器，2 核 4G 起
# 2. SSH 进去
ssh root@<IP>

# 3. 装 Docker + 镜像加速
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
cat > /etc/docker/daemon.json <<'EOF'
{"registry-mirrors":["https://mirror.ccs.tencentyun.com","https://docker.m.daocloud.io"]}
EOF
systemctl restart docker

# 4. 拉代码
mkdir -p /opt && cd /opt
git clone <你的仓库> pms
cd pms/v2

# 5. 配 .env.prod
cp .env.prod.example .env.prod
nano .env.prod
# 必改: POSTGRES_PASSWORD / SECRET_KEY / DEFAULT_ADMIN_PASSWORD
# 生成 SECRET_KEY:  openssl rand -hex 32

# 6. 启动
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

# 7. 装运维定时任务
chmod +x ops/*.sh
sudo bash ops/setup-cron.sh

# 8. 验证
bash ops/health-check.sh
curl http://localhost/api/health
# 浏览器访问 http://<IP>，用 admin 登录后立刻改密码
```

### 域名 + HTTPS

参考 `DEPLOY-TENCENT.md` 第 7 步；推荐用腾讯云免费 SSL 一键签发。

---

## 3. 日常运维 SOP

### 每日必看（5 分钟）

```bash
cd /opt/pms/v2
bash ops/health-check.sh           # 全绿即可
tail -20 /var/log/pms-backup.log   # 看昨晚备份是否成功
ls -lt /backup/ | head -5          # 确认有当日 .sql.gz
```

### 每周

- 看磁盘 `df -h`，>80% 警告，>90% 必须处理
- 看登录审计 `bash ops/db-shell.sh -c "SELECT username,action,created_at FROM audit_logs ORDER BY id DESC LIMIT 20;"`
- 手动跑一次 `bash ops/diagnose.sh`，把诊断包归档（出问题时对比）

### 每月

- `apt update && apt upgrade -y`（建议先打快照）
- `docker image prune -af`（清理悬空镜像）
- 验证一次备份能恢复（在测试环境跑 `ops/restore.sh`）
- SSL 证书有效期检查（acme.sh 自动续，腾讯云免费证书 1 年）

### 自动化（已由 setup-cron.sh 装好）

```
/etc/cron.daily/pms-backup    每日 6:25 备份 → /backup/
/etc/cron.d/pms-health        每 5 分钟健康检查，失败自动重启 backend
/etc/logrotate.d/pms          每周切割运维日志
```

---

## 4. 备份与恢复

### 备份策略

| 项 | 频率 | 位置 | 保留 |
|---|---|---|---|
| Postgres `pg_dump` | 每日 | `/backup/pms-db-YYYYMMDD.sql.gz` | 30 天 |
| uploads volume | 每日 | `/backup/pms-uploads-YYYYMMDD.tar.gz` | 30 天 |
| 异地备份（COS） | 每日 | `cos://<bucket>/db/` | 90 天（在 COS 配生命周期） |

### 手动备份

```bash
bash ops/backup.sh                                   # 本机
COS_BUCKET=cos://xxx/db/ bash ops/backup.sh --upload-cos
```

### 恢复

```bash
# 1. 列出可用备份
bash ops/restore.sh --list

# 2. 恢复（脚本会先做"恢复前快照"防反悔）
bash ops/restore.sh /backup/pms-db-20260514_030000.sql.gz

# 同时恢复 uploads:
bash ops/restore.sh /backup/pms-db-20260514_030000.sql.gz \
    --uploads /backup/pms-uploads-20260514_030000.tar.gz
```

**输入 `yes I am sure` 才会真执行**，避免误删。

---

## 5. 升级与回滚

### 安全升级（推荐方式）

```bash
cd /opt/pms/v2
bash ops/upgrade.sh
```

`upgrade.sh` 流程：
1. 备份当前 db + uploads
2. `git pull` 拉新代码
3. `docker compose up -d --build`
4. 等 backend 起来（60s 超时）
5. 跑 `health-check.sh`
6. **任何一步失败自动回滚**到升级前 commit

### 手工升级

```bash
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
bash ops/health-check.sh
```

### 回滚

```bash
bash ops/rollback.sh --list           # 看最近 10 次 commit
bash ops/rollback.sh                  # 回退一版（HEAD~1）
bash ops/rollback.sh --to abc1234     # 回到指定 commit
```

### 数据库 schema 变更（⚠ 重要）

当前用 `Base.metadata.create_all` 自动建表，**不会改旧表 schema**。
如果以后 `models.py` 改了字段（加列、改类型），需要：

1. **少量变更**（加新列）→ 手工执行 SQL：
   ```bash
   bash ops/db-shell.sh
   ALTER TABLE projects ADD COLUMN new_col VARCHAR(64);
   ```
2. **大量变更** → 接入 Alembic 迁移工具，不在此手册范围内

升级前先在测试环境验证 schema 兼容性。

---

## 6. 监控与告警

### 应用层

`ops/health-check.sh` 检查 9 项，cron 每 5 分钟跑一次。当 backend 挂时自动重启。

```bash
bash ops/health-check.sh --json    # 接入外部监控（Prometheus/Zabbix）
```

### 平台层（云厂商免费）

- **腾讯云云监控（CM）** / **阿里云云监控** — CPU / 内存 / 磁盘 / 带宽超阈值短信
- 推荐阈值：CPU > 85%（5min）、内存 > 90%、磁盘 > 85%、公网出流量异常上升

### 应用错误率

```bash
# 近 1 小时 backend ERROR 数
docker logs --since 1h pms2_backend 2>&1 | grep -cE 'ERROR|Exception'
```

> 后续可接 Sentry / 阿里云 ARMS。

---

## 7. 应急预案（故障 SOP）

> 顺序：**先收日志（diagnose.sh）→ 再尝试自愈 → 必要时回滚 → 联系开发**

### 7.1 网站打不开（白屏 / 502 / 拒绝连接）

```bash
# 1. 立刻收集诊断
bash ops/diagnose.sh

# 2. 看 5 个容器哪个挂了
docker compose -f docker-compose.prod.yml ps

# 3. 各种情况
#  - nginx Exit → docker compose restart nginx
#  - backend Exit → bash ops/logs.sh backend errors
#  - postgres Exit → 多半磁盘满了，df -h；清旧备份
docker compose -f docker-compose.prod.yml restart <服务名>
```

如 30 秒后仍不行：

```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

### 7.2 登录后空白页

| 现象 | 原因 | 处理 |
|---|---|---|
| 网络请求全 502 | backend 挂 | `bash ops/logs.sh backend errors` |
| 401 反复跳登录 | SECRET_KEY 变了/token 过期 | 重新登录；如果改过 .env 要重启 backend |
| 接口 500 | backend 异常 | 看日志，必要时 `ops/rollback.sh` |

### 7.3 admin 忘密码

```bash
bash ops/reset-admin-password.sh admin
# 输入新密码 → 立即生效
```

也支持重置任意账号：`bash ops/reset-admin-password.sh <username>`

### 7.4 磁盘满

```bash
df -h
du -sh /backup /var/lib/docker /var/log/* | sort -rh | head -10

# 清旧备份
find /backup -name 'pms-*' -mtime +14 -delete

# 清悬空镜像
docker image prune -af
docker system prune -af --volumes      # 谨慎，包括未挂的 volume
```

### 7.5 数据被误删 / 误改

```bash
# 1. 立刻停 backend / nginx 防止继续写
docker compose -f docker-compose.prod.yml stop backend nginx

# 2. 用昨晚备份恢复
bash ops/restore.sh /backup/pms-db-<最近且不含误操作的>.sql.gz

# 3. 启回服务
docker compose -f docker-compose.prod.yml up -d
```

数据丢失最长 24 小时（备份频率），如要更短，加 `0 */6 * * *` 每 6 小时跑 `backup.sh`。

### 7.6 数据库无法启动

```bash
docker logs pms2_postgres --tail 100

# 常见：磁盘满 → 清 /backup
# 常见：数据文件损坏 → 从备份恢复（容器还能起来时用 restore.sh；起不来则需手工建库重导）
# 极少：volume 权限错 → 用 root 容器查 ls -la /var/lib/docker/volumes/v2_postgres_data/_data/
```

### 7.7 被攻击 / 异常流量

```bash
# 1. 看 nginx 访问日志
docker exec pms2_nginx tail -100 /var/log/nginx/access.log

# 2. 临时封 IP（用云厂商安全组更稳，或 ufw 临时）
ufw deny from <恶意 IP>

# 3. 看是否需要改密钥
# 如果 SECRET_KEY 可能泄露 → 改 .env.prod 后重启，所有 token 失效
```

### 7.8 服务器宕机（云厂商故障）

1. 控制台看实例状态，如果是云端宕机，等恢复
2. 永久故障 → 在另一台服务器上：
   ```bash
   git clone <仓库> /opt/pms
   cd /opt/pms/v2
   cp <旧 .env.prod> .
   # 把最近的备份拉到本机
   bash ops/restore.sh <备份文件>
   ```
3. 改 DNS A 记录指向新 IP

---

## 8. 常见命令速查

```bash
cd /opt/pms/v2

# 状态
docker compose -f docker-compose.prod.yml ps
bash ops/health-check.sh

# 日志（推荐用 logs.sh）
bash ops/logs.sh backend          # 跟 backend
bash ops/logs.sh backend errors   # 只 grep 错误
bash ops/logs.sh backend 500      # 最近 500 行
bash ops/logs.sh all              # 跟所有容器
bash ops/logs.sh nginx

# 重启
docker compose -f docker-compose.prod.yml restart backend
docker compose -f docker-compose.prod.yml restart   # 全部

# 启停
docker compose -f docker-compose.prod.yml stop      # 停
docker compose -f docker-compose.prod.yml start     # 起
docker compose -f docker-compose.prod.yml down      # 停并删容器（保留 volume）
docker compose -f docker-compose.prod.yml down -v   # ⚠ 也删 volume，数据全丢

# 进容器
docker compose -f docker-compose.prod.yml exec backend bash
docker compose -f docker-compose.prod.yml exec postgres bash
bash ops/db-shell.sh              # 直接进 psql

# 数据库快查
bash ops/db-shell.sh -c "SELECT count(*) FROM projects;"
bash ops/db-shell.sh -c "SELECT username, last_login FROM users ORDER BY last_login DESC NULLS LAST LIMIT 10;"
bash ops/db-shell.sh -c "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"

# 一次性诊断打包
bash ops/diagnose.sh
```

---

## 9. 安全清单

部署后 / 每月复查一遍：

- [ ] admin 首次登录已改密码（不是 `admin123`）
- [ ] `.env.prod` 的 `SECRET_KEY` 是用 `openssl rand -hex 32` 生成的
- [ ] `.env.prod` 的 `POSTGRES_PASSWORD` 是 16 位以上强密码
- [ ] `.env.prod` 文件权限是 600：`chmod 600 .env.prod`
- [ ] 服务器 SSH 用密钥登录，禁用密码：`/etc/ssh/sshd_config` 里 `PasswordAuthentication no`
- [ ] 云厂商安全组 SSH (22 端口) 来源限制到办公网 IP/32
- [ ] 公网只开 22 / 80 / 443，数据库端口（5432）不暴露
- [ ] HTTPS 已启用，HTTP 跳转 HTTPS
- [ ] 系统包定期更新：`apt update && apt upgrade`
- [ ] 备份每日跑（`tail /var/log/pms-backup.log`），且至少一份在异地（COS / 另一台机器）
- [ ] 测试环境验证过 `ops/restore.sh` 真能恢复（不是纸面方案）
- [ ] 监控告警接收人配好（短信 / 邮件 / 微信 / 钉钉）
- [ ] 离职人员 admin 账号已禁用
- [ ] 不在 git 里提交 `.env.prod`（应在 `.gitignore`）

---

## 10. 运维联系方式

> 请在这里填写你公司的实际联系人

| 角色 | 姓名 | 电话 | 邮箱 | 说明 |
|---|---|---|---|---|
| 运维负责人 | | | | 服务器、网络、备份 |
| 后端开发 | | | | 业务逻辑、数据库问题 |
| 前端开发 | | | | 界面问题 |
| 云厂商支持 | 腾讯云 | 4009-100-100 | — | 工单优先 |
| 域名管理 | | | | DNS 解析、备案 |

**应急升级顺序：**
1. 现场运维先收 `diagnose.sh` 包
2. 尝试 `health-check.sh` + 重启服务
3. 仍不行 → 用脚本 `rollback.sh` 回上一版
4. 仍不行 → 联系后端开发，把诊断包发过去

---

## 附录 A：目录结构

```
/opt/pms/v2/
├── .env.prod                       # 生产环境变量（不入 git）
├── docker-compose.prod.yml
├── DEPLOY-TENCENT.md               # 腾讯云部署
├── DEPLOY-ALIYUN.md                # 阿里云部署
├── OPERATIONS.md                   # 本文档
├── backend/   frontend/  nginx/
└── ops/                            # 运维脚本
    ├── README.md
    ├── backup.sh
    ├── restore.sh
    ├── health-check.sh
    ├── upgrade.sh
    ├── rollback.sh
    ├── reset-admin-password.sh
    ├── diagnose.sh
    ├── setup-cron.sh
    ├── db-shell.sh
    └── logs.sh
```

## 附录 B：定时任务一览（setup-cron.sh 安装后）

```
/etc/cron.daily/pms-backup     -> 每日 6:25 备份
/etc/cron.d/pms-health         -> 每 5 分钟健康检查 + 自愈
/etc/logrotate.d/pms           -> 运维日志按周切割
```

## 附录 C：数据保留策略

| 数据 | 位置 | 保留 |
|---|---|---|
| 业务数据（postgres） | volume `postgres_data` | 永久 |
| 用户上传（Excel） | volume `uploads_data` | 永久 |
| 备份（本机） | `/backup/` | 30 天 |
| 备份（COS） | `cos://...` | 90 天（在 COS 配生命周期） |
| nginx 访问日志 | volume `nginx_logs` | 自然滚动 |
| 容器 stdout 日志 | json-file driver | 100MB × 3 文件 = 300MB/容器 |
| 审计日志（audit_logs 表） | 业务库 | 永久（如表过大可手工归档） |
