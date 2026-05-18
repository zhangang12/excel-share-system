# 阿里云 ECS 部署指南

> 本系统使用 Docker Compose 部署，包含 5 个容器：postgres / redis / backend / frontend / nginx
> 推荐配置：**ECS 共享型 s6（2 核 4G）即可起步**；50 人以上日活建议 4 核 8G

---

## 第 1 步：买 ECS

### 1.1 登录阿里云控制台

https://ecs.console.aliyun.com → 创建实例

### 1.2 配置选择

| 项 | 推荐 |
|---|---|
| **付费模式** | 包年包月（便宜稳定）或 按量付费（先试用） |
| **地域** | 离用户最近的（华东 1 / 华北 2 / 华南 1） |
| **实例规格** | `ecs.s6-c1m2.large`（2 vCPU 4 GB）起步 |
| **镜像** | Ubuntu 22.04 64位 |
| **系统盘** | ESSD 40 GB |
| **数据盘** | ESSD 100 GB（专门挂载到 `/data` 给 Postgres 用） |
| **网络** | 默认 VPC，公网带宽 5 Mbps 起 |
| **安全组** | 开放端口 **22（SSH） / 80（HTTP） / 443（HTTPS）** |
| **登录方式** | 密钥对（推荐）或 root 密码 |

### 1.3 备案

如果用**域名**访问，且服务器在中国大陆，**必须工信部备案**（10-20 天）。备案期间可以先用 IP 访问。

---

## 第 2 步：连服务器装基础环境

```bash
ssh root@<服务器公网 IP>
```

### 2.1 系统更新

```bash
apt update && apt upgrade -y
apt install -y git curl ufw
```

### 2.2 装 Docker（用阿里云镜像源加速）

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh --mirror Aliyun
systemctl enable --now docker
docker --version
docker compose version
```

### 2.3 配 Docker Hub 国内镜像（防止拉镜像慢）

```bash
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
```

### 2.4 防火墙（如果你想让阿里云安全组之外再加一层）

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw --force enable
```

---

## 第 3 步：把代码上传到服务器

### 方式 A：用 Git（推荐）

```bash
cd /opt
git clone <你的私有 Git 仓库 URL> pms
cd pms/v2
```

### 方式 B：手动上传（Windows 可用 WinSCP / scp）

```bat
:: 本地 cmd
scp -r D:\opencode-project\EXCEL共享维护系统1500\EXCEL共享维护系统\v2 root@<IP>:/opt/pms-v2
```

---

## 第 4 步：配置环境变量

```bash
cd /opt/pms/v2          # 或 /opt/pms-v2
cp .env.prod.example .env.prod
nano .env.prod
```

### 关键配置（**必须改**）

```bash
# 用这条命令生成强 SECRET_KEY，复制粘贴到 .env.prod
openssl rand -hex 32
```

把 `.env.prod` 里改成：

```ini
POSTGRES_USER=pms_prod
POSTGRES_PASSWORD=<至少 16 位强密码>
POSTGRES_DB=pms
SECRET_KEY=<刚才生成的 64 位字符串>
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=<改成你自己的临时密码，登录后再改>
ACCESS_TOKEN_EXPIRE_MINUTES=480
```

---

## 第 5 步：（可选）把 Postgres 数据挂到独立数据盘

如果买了数据盘（推荐），先挂载：

```bash
# 查看磁盘
fdisk -l
# 假设是 /dev/vdb
mkfs.ext4 /dev/vdb
mkdir -p /data
mount /dev/vdb /data
echo "/dev/vdb /data ext4 defaults,nofail 0 2" >> /etc/fstab

# 把 Docker volume 改到 /data
mkdir -p /data/docker
systemctl stop docker
mv /var/lib/docker/* /data/docker/
ln -s /data/docker /var/lib/docker
systemctl start docker
```

---

## 第 6 步：启动服务

```bash
cd /opt/pms/v2
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

首次构建约 5-10 分钟。

### 看启动状态

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f backend
```

5 个容器都应该 `Up`。看到 `Application startup complete` 就 OK。

### 浏览器访问

```
http://<服务器公网 IP>
```

用 `admin / <你设置的临时密码>` 登录，立刻改密码。

---

## 第 7 步：配置域名 + HTTPS

### 7.1 域名解析

阿里云"云解析"→ 把你的域名 A 记录指向 ECS 公网 IP。

### 7.2 改 Nginx 配置

```bash
cd /opt/pms/v2
nano nginx/conf.d/default.conf
```

把 `server_name _;` 改成 `server_name pms.your-company.com;`，保存。

### 7.3 申请免费 SSL 证书（acme.sh + Let's Encrypt）

```bash
# 装 acme.sh
curl https://get.acme.sh | sh -s email=你的邮箱
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt

# 临时停掉 nginx 占用 80 端口
docker compose -f docker-compose.prod.yml stop nginx

# 申请证书（独立模式，acme.sh 自己起一个临时 HTTP 服务）
~/.acme.sh/acme.sh --issue --standalone -d pms.your-company.com

# 部署证书到本地目录
~/.acme.sh/acme.sh --install-cert -d pms.your-company.com \
  --key-file       /opt/pms/v2/nginx/certs/privkey.pem \
  --fullchain-file /opt/pms/v2/nginx/certs/fullchain.pem \
  --reloadcmd     "docker compose -f /opt/pms/v2/docker-compose.prod.yml restart nginx"
```

### 7.4 启用 HTTPS

编辑 `nginx/conf.d/default.conf`：
- 取消 HTTPS 段注释
- HTTP 段改成 301 跳转到 HTTPS（取消 `return 301 ...` 那行注释）

```bash
docker compose -f docker-compose.prod.yml restart nginx
```

浏览器访问 `https://pms.your-company.com` 应该是绿锁了。

证书 90 天有效，acme.sh 自动续。

---

## 第 8 步：备份

### 8.1 数据库每日备份

```bash
mkdir -p /backup
cat > /etc/cron.daily/pms-backup <<'EOF'
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
docker exec pms2_postgres pg_dump -U pms_prod pms | gzip > /backup/pms-${DATE}.sql.gz
# 保留最近 30 份
ls -1t /backup/pms-*.sql.gz | tail -n +31 | xargs -r rm
EOF
chmod +x /etc/cron.daily/pms-backup
```

### 8.2 备份到 OSS（可选）

阿里云 OSS 装好 ossutil，加到上面 cron 末尾：

```bash
ossutil cp /backup/pms-${DATE}.sql.gz oss://your-bucket/pms-backups/
```

---

## 日常运维

```bash
# 看运行状态
docker compose -f docker-compose.prod.yml ps

# 看日志
docker compose -f docker-compose.prod.yml logs -f backend --tail 100
docker compose -f docker-compose.prod.yml logs -f nginx

# 重启某个服务
docker compose -f docker-compose.prod.yml restart backend

# 更新代码后重新部署
git pull        # 或 scp 新代码上去
docker compose -f docker-compose.prod.yml up -d --build

# 进入数据库
docker compose -f docker-compose.prod.yml exec postgres psql -U pms_prod pms

# 完全停掉
docker compose -f docker-compose.prod.yml down

# 完全清理（包含数据！谨慎）
docker compose -f docker-compose.prod.yml down -v
```

---

## 监控建议

- **阿里云云监控**：免费监控 CPU/内存/磁盘/网络，可设阈值告警
- **应用层**：浏览器访问 `http://<IP>/api/health` 应该返回 `{"status":"ok",...}`
- **日志收集**：可选，可对接阿里云日志服务 SLS

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 80/443 打不开 | 安全组放行 / `docker compose ps` 看 nginx 是否 Up |
| 登录后空白 | F12 看 Network；多半是 backend 没起来 → `logs backend` |
| 上传 Excel 失败 | nginx `client_max_body_size 50M` 是否够 |
| WebSocket 连不上 | nginx 配置里 `/ws/` 那段必须有 `Upgrade/Connection` 头 |
| 数据库连接失败 | `.env.prod` 里 `POSTGRES_PASSWORD` 和 `DATABASE_URL` 一致 |

---

## 性能调优（流量大了再考虑）

- backend 多 worker：`Dockerfile.prod` 里 `--workers` 改大
- Postgres：`shared_buffers / effective_cache_size` 调到内存的 25-75%
- Redis：开启 maxmemory-policy
- Nginx：加 gzip / 静态资源缓存
- CDN：阿里云 CDN 把前端静态资源加速

---

## 安全清单

- [ ] 首次登录立刻改 admin 密码
- [ ] SECRET_KEY 已用 `openssl rand` 重新生成
- [ ] POSTGRES_PASSWORD 是强密码
- [ ] HTTPS 已启用
- [ ] 阿里云安全组只开 22/80/443
- [ ] SSH 用密钥登录、禁用 root 密码登录
- [ ] 定期 `apt update && apt upgrade`
- [ ] 数据库每日备份且备份在另一台机器/OSS
