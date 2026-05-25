# 阿里云部署 + 定时备份 · 快速指引

> 这是一份「能复制粘贴就能跑」的精简版指引，聚焦**阿里云 ECS + 定时备份 + OSS 异地**。
>
> - 想看完整章节（含 HTTPS、调优、安全清单）：见 [DEPLOY-ALIYUN.md](DEPLOY-ALIYUN.md)
> - 想看运维 / 应急 / 故障 SOP：见 [OPERATIONS.md](OPERATIONS.md)
> - 想看脚本细节：见 [ops/README.md](ops/README.md)

---

## 0. 全流程鸟瞰

```
买 ECS → 装 Docker → 拉代码 → 配 .env.prod → 起服务
                                                  │
                                                  ▼
            装 ossutil（异地备份用）→ sudo bash ops/setup-cron.sh
                                                  │
                                                  ▼
            每日 6:25 自动备份到 /backup → 每日上传到 OSS（可选）
```

预计耗时：**新手 60 分钟、熟手 15 分钟**。

---

## 1. 买 ECS

阿里云控制台 → ECS → 创建实例：

| 项 | 推荐配置 |
|---|---|
| 规格 | `ecs.s6-c1m2.large`（2 vCPU 4 GB）起，50 人日活以上选 4 核 8G |
| 镜像 | **Ubuntu 22.04 64 位** |
| 系统盘 | ESSD 40 GB |
| 数据盘（可选） | ESSD 100 GB 挂到 `/data`（PG 数据 + 备份独立盘更稳） |
| 公网带宽 | 5 Mbps 起 |
| 安全组 | **只开 22 / 80 / 443**；SSH (22) 来源限办公网 IP/32 |
| 登录方式 | 密钥对（推荐） |

> 用域名访问且服务器在中国大陆，必须先工信部备案（10-20 天）。备案期间可先用 IP 访问。

---

## 2. 装基础环境

```bash
ssh root@<服务器公网 IP>

# 系统更新 + 基础工具
apt update && apt upgrade -y
apt install -y git curl ufw

# 装 Docker（阿里云镜像加速）
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh --mirror Aliyun
systemctl enable --now docker

# 配镜像加速 + 日志滚动
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run",
    "https://docker.nju.edu.cn"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "100m", "max-file": "3" }
}
EOF
systemctl restart docker
docker --version && docker compose version    # 验证装好
```

---

## 3. 部署应用

### 3.1 拉代码

```bash
mkdir -p /opt && cd /opt
git clone <你的私有仓库 URL> pms
cd pms/v2
```

> 没仓库的话用 WinSCP / `scp -r v2/ root@<IP>:/opt/pms-v2`。

### 3.2 配置 `.env.prod`

```bash
cp .env.prod.example .env.prod

# 生成强密钥
echo "SECRET_KEY=$(openssl rand -hex 32)"

nano .env.prod
```

必改 3 处：

```ini
POSTGRES_PASSWORD=<至少 16 位强密码>
SECRET_KEY=<上面 openssl 生成的 64 位字符串>
DEFAULT_ADMIN_PASSWORD=<临时密码，登录后立刻改>
```

**改文件权限**，防止被普通用户读到：

```bash
chmod 600 .env.prod
```

### 3.3 起服务

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

首次构建 5-10 分钟。看启动状态：

```bash
docker compose -f docker-compose.prod.yml ps          # 5 个容器应全 Up
docker compose -f docker-compose.prod.yml logs -f backend
# 看到 "Application startup complete" 就 OK，Ctrl+C 退出日志
```

### 3.4 验证

```bash
curl http://localhost/api/health
# {"status":"ok","app":"...","version":"2.0.0"}
```

浏览器打开 `http://<服务器公网 IP>`，用 `admin / <你设的密码>` 登录，**立刻改密码**。

---

## 4. 定时备份（项目自带，一行命令搞定）

项目在 `ops/` 里已经准备好备份脚本和 cron 安装器。**一行命令**完成：

```bash
cd /opt/pms/v2
chmod +x ops/*.sh
sudo bash ops/setup-cron.sh
```

这会安装：

| 路径 | 内容 |
|---|---|
| `/etc/cron.daily/pms-backup` | 每日 **06:25**（Ubuntu cron.daily 默认）备份 → `/backup/pms-db-YYYYMMDD_HHMMSS.sql.gz` + `pms-uploads-*.tar.gz` |
| `/etc/cron.d/pms-health` | 每 5 分钟跑 `health-check.sh`，backend 挂了自动重启 |
| `/etc/logrotate.d/pms` | `/var/log/pms-backup.log` 和 `pms-health.log` 每周轮转 |

备份默认策略：

- 存路径：`/backup/`（可用 `BACKUP_DIR=...` 改）
- 保留：**30 天**（可用 `KEEP_DAYS=14` 改）
- 内容：Postgres 完整 `pg_dump` + uploads volume 打包

### 4.1 手动跑一次验证

```bash
bash ops/backup.sh
ls -lh /backup/
```

应能看到当前时间戳的 `pms-db-*.sql.gz` 和 `pms-uploads-*.tar.gz`。

### 4.2 看每日备份日志

```bash
tail -20 /var/log/pms-backup.log
```

---

## 5. OSS 异地备份（强烈推荐）

> 本机磁盘坏 / ECS 被回收 / 误删 `/backup`，本地备份就全没了。
> 把每日备份**同步到一个 OSS 桶**是关键的最后保险。

### 5.1 在阿里云创建 OSS 桶

控制台 → 对象存储 OSS → 创建 Bucket：

| 项 | 推荐 |
|---|---|
| 名字 | `<公司前缀>-pms-backup-<随机后缀>` |
| 区域 | **与 ECS 同区域**（同区域内网传输免流量费） |
| 存储类型 | **低频访问** 或 **归档**（更便宜，备份场景适用） |
| 读写权限 | **私有**（绝不能公开） |
| 服务端加密 | 推荐开（OSS 完全托管，免密码） |

控制台 → 访问控制 RAM → 创建子账号：

- 用户名：`pms-backup-uploader`
- 权限：**只授** `AliyunOSSFullAccess` 或自定义策略只允许这个 Bucket 写入
- 记下 **AccessKey ID** 和 **Secret**（页面只显示一次）

### 5.2 在 ECS 装 ossutil + 配密钥

```bash
# 安装 ossutil（官方一键脚本）
curl https://gosspublic.alicdn.com/ossutil/install.sh | sudo bash

# 交互配置：填刚才的 AK/SK + endpoint（参考 OSS 控制台桶详情）
ossutil config
# Endpoint:   oss-cn-shanghai.aliyuncs.com   (按你 Bucket 所在区域)
# AccessKey ID:      <填 RAM 子账号 AK>
# AccessKey Secret:  <填 RAM 子账号 SK>
# 其它回车默认

# 验证：列出 Bucket
ossutil ls
# 应能看到 oss://<你的桶名>/
```

### 5.3 手动跑一次 OSS 上传

```bash
OSS_BUCKET=oss://<你的桶名>/pms-backups/ bash ops/backup.sh --upload-oss
```

成功后到 OSS 控制台 Bucket 内能看到 `pms-db-*.sql.gz`。

### 5.4 加进每日 cron

修改 `setup-cron.sh` 装好的那个 cron 文件，让它每日不仅本机备份还顺手上传 OSS：

```bash
# 用 OSS 上传版本替换 cron.daily/pms-backup
cat > /etc/cron.daily/pms-backup <<'EOF'
#!/bin/bash
exec >> /var/log/pms-backup.log 2>&1
export OSS_BUCKET=oss://<你的桶名>/pms-backups/
bash /opt/pms/v2/ops/backup.sh --upload-oss
EOF
chmod +x /etc/cron.daily/pms-backup
```

> 注意把 `<你的桶名>` 和 `/opt/pms/v2/` 路径换成实际的。

明天 6:25 自动跑、出问题看 `/var/log/pms-backup.log`。

### 5.5 OSS 生命周期（自动清旧）

OSS 控制台 → Bucket → 基础设置 → 生命周期 → 新建规则：

- 前缀：`pms-backups/`
- 行为：**90 天后自动删除**（或转归档存储再删）

这样异地备份不会无限堆积。

---

## 6. 验证备份能恢复（部署当天必须做一次！）

**没验证过的备份不算备份。** 在测试环境（或先停业务窗口）跑一次：

```bash
# 看可用备份
bash ops/restore.sh --list

# 选最近一份恢复
bash ops/restore.sh /backup/pms-db-20260524_062500.sql.gz
# 会提示输入 "yes I am sure" 才真执行
# 同时它会先做"恢复前快照"防反悔
```

成功后用浏览器登录验证数据正常即可。

---

## 7. 日常 3 件事

| 频率 | 做什么 | 命令 |
|---|---|---|
| 每日早晨 5 分钟 | 看健康 + 看昨晚备份 | `bash ops/health-check.sh && tail -10 /var/log/pms-backup.log && ls -lt /backup \| head -5` |
| 每周 | 看磁盘 / 看审计 | `df -h && bash ops/db-shell.sh -c "SELECT username,action,created_at FROM audit_logs ORDER BY id DESC LIMIT 20;"` |
| 每月 | 测试一次恢复、`apt upgrade` | 在测试机跑 `restore.sh` |

更详细的运维流程见 [OPERATIONS.md 第 3 章](OPERATIONS.md#3-日常运维-sop)。

---

## 8. 故障极速自救

| 现象 | 第一步 |
|---|---|
| 网站打不开 | `docker compose -f docker-compose.prod.yml ps` 看哪个容器挂；`bash ops/diagnose.sh` 收诊断包 |
| admin 忘密码 | `bash ops/reset-admin-password.sh admin` |
| 磁盘满 | `df -h` → `find /backup -mtime +14 -delete` → `docker image prune -af` |
| 数据被误改 | `docker compose stop backend nginx` → `bash ops/restore.sh <昨晚备份>` |
| 升级出错 | `bash ops/rollback.sh` |

完整应急 SOP 见 [OPERATIONS.md 第 7 章](OPERATIONS.md#7-应急预案故障-sop)。

---

## 9. 域名 + HTTPS（可选）

域名 A 记录指向 ECS 公网 IP 后：

```bash
# 改 server_name
nano nginx/conf.d/default.conf
# 把 server_name _;  改成你的域名

# 用 acme.sh 签免费证书（阿里云也有免费 SSL，控制台一键签更省事）
curl https://get.acme.sh | sh -s email=你的邮箱
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
docker compose -f docker-compose.prod.yml stop nginx
~/.acme.sh/acme.sh --issue --standalone -d <你的域名>
~/.acme.sh/acme.sh --install-cert -d <你的域名> \
  --key-file       /opt/pms/v2/nginx/certs/privkey.pem \
  --fullchain-file /opt/pms/v2/nginx/certs/fullchain.pem \
  --reloadcmd     "docker compose -f /opt/pms/v2/docker-compose.prod.yml restart nginx"

# 启用 HTTPS：编辑 nginx/conf.d/default.conf 取消 HTTPS 段注释 + HTTP 段加 301 跳 HTTPS
docker compose -f docker-compose.prod.yml restart nginx
```

完整 HTTPS 章节见 [DEPLOY-ALIYUN.md 第 7 步](DEPLOY-ALIYUN.md#第-7-步配置域名--https)。

---

## 10. 部署后检查清单

照单核对一遍：

- [ ] 浏览器能访问 `http://<IP>`、`/api/health` 返回 OK
- [ ] `admin` 已改默认密码
- [ ] `.env.prod` 权限是 `600`：`ls -l .env.prod`
- [ ] `SECRET_KEY` 是 `openssl rand -hex 32` 生成的（不是模板里的占位符）
- [ ] `POSTGRES_PASSWORD` ≥ 16 位强密码
- [ ] 安全组只开 22/80/443
- [ ] SSH 用密钥登录、`sshd_config` 里 `PasswordAuthentication no`
- [ ] `bash ops/setup-cron.sh` 已跑
- [ ] `bash ops/backup.sh` 手动跑过一次，`/backup` 有文件
- [ ] OSS 桶已建 + ossutil 配好 + cron 里写了 `--upload-oss`
- [ ] `bash ops/restore.sh` 在测试机或停机窗口跑通过
- [ ] OSS 生命周期已设（自动清 90 天前的）
- [ ] 阿里云云监控告警：CPU > 85% / 内存 > 90% / 磁盘 > 85% 发短信
- [ ] HTTPS 已启用（如有域名）

全部勾上 = 这套部署稳了。

---

## 11. 关于"存量数据"的注意事项

如果是**升级既有线上系统**（不是从零部署）：

1. **先备份再动**：`bash ops/backup.sh`
2. **升级走 `upgrade.sh`，失败自动回滚**：`bash ops/upgrade.sh`
3. 本次代码改动（v2 → 表头 tooltip + 权限克隆）**未改数据库 schema**，可直接升级，无需迁移
4. 升级后到任意项目详情页用 `admin` 或 `manager` 账号，验证「克隆权限」按钮可见 + 表头 hover 显示完整字段名 = 升级成功

---

## 附：脚本命令速查

```bash
cd /opt/pms/v2

# 状态/日志
docker compose -f docker-compose.prod.yml ps
bash ops/health-check.sh
bash ops/logs.sh backend

# 备份/恢复
bash ops/backup.sh
OSS_BUCKET=oss://xxx/db/ bash ops/backup.sh --upload-oss
bash ops/restore.sh --list
bash ops/restore.sh /backup/pms-db-XXXX.sql.gz

# 升级/回滚
bash ops/upgrade.sh
bash ops/rollback.sh --list

# 重启
docker compose -f docker-compose.prod.yml restart backend
docker compose -f docker-compose.prod.yml restart

# 数据库
bash ops/db-shell.sh
bash ops/db-shell.sh -c "SELECT count(*) FROM projects;"

# 应急
bash ops/diagnose.sh                          # 打包诊断
bash ops/reset-admin-password.sh admin        # 改 admin 密码
```
