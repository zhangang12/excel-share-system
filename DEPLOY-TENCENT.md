# 腾讯云部署指南

> 本系统使用 Docker Compose 部署，包含 5 个容器：postgres / redis / backend / frontend / nginx
> 推荐配置：**轻量应用服务器 2 核 4G** 起步即可；50 人以上日活建议 4 核 8G
> 适用产品：**CVM（云服务器）** 或 **Lighthouse（轻量应用服务器，性价比更高）**

---

## 第 1 步：买服务器

### 方案 A：Lighthouse 轻量应用服务器（推荐中小项目）

https://console.cloud.tencent.com/lighthouse

| 项 | 推荐 |
|---|---|
| **地域** | 离用户最近的（广州 / 上海 / 北京） |
| **套餐** | 2 核 4G 6M 带宽（约 ¥60/月，新人首年常有折扣） |
| **镜像** | 应用镜像 → **Docker CE** （已预装 Docker）；或纯系统 Ubuntu 22.04 |
| **存储** | 80 GB SSD（套餐自带） |

### 方案 B：CVM（云服务器，功能更全）

https://console.cloud.tencent.com/cvm

| 项 | 推荐 |
|---|---|
| **付费模式** | 包年包月（便宜稳定）或 按量计费（先试用） |
| **地域 + 可用区** | 广州三区 / 上海二区（按位置选） |
| **实例规格** | **S5.MEDIUM4（2 vCPU 4 GB）** 起步；流量大可升级到 SA3.MEDIUM4 |
| **镜像** | 公共镜像 → Ubuntu Server 22.04 LTS 64位 |
| **系统盘** | SSD 50 GB |
| **数据盘** | 增强型 SSD 100 GB（建议挂到 `/data` 给 Postgres） |
| **网络** | 默认 VPC，公网带宽按量计费 5 Mbps 或包月 5 Mbps 起 |
| **安全组** | 放行 **22 / 80 / 443**（详见下） |
| **登录方式** | SSH 密钥对（推荐） |

### 1.3 安全组（防火墙）规则

控制台 → 安全组 → 入站规则添加：

| 类型 | 来源 | 协议端口 | 备注 |
|---|---|---|---|
| 自定义 | 你的办公网公网 IP/32 | TCP:22 | SSH（限制来源更安全） |
| HTTP | 0.0.0.0/0 | TCP:80 | 网站 |
| HTTPS | 0.0.0.0/0 | TCP:443 | 网站 |

> ⚠ Lighthouse 的"防火墙规则"在 Lighthouse 控制台单独配置，不在 CVM 安全组里。

### 1.4 ICP 备案

如果用**域名**访问且服务器在**中国大陆**，必须工信部备案（首次 10-20 天）。
- 入口：https://console.cloud.tencent.com/beian
- 备案期间可以**先用公网 IP 直接访问**测试

> 香港 / 海外节点不需要备案，但国内访问延迟稍高。

---

## 第 2 步：连服务器装基础环境

```bash
ssh ubuntu@<服务器公网 IP>     # CVM 默认 ubuntu；Lighthouse 默认 root
# 切到 root（如不是 root）
sudo -i
```

### 2.1 系统更新

```bash
apt update && apt upgrade -y
apt install -y git curl ufw nano
```

### 2.2 装 Docker（如果用 Lighthouse Docker 应用镜像可跳过）

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
systemctl enable --now docker
docker --version
docker compose version
```

### 2.3 配 Docker 国内镜像加速

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "100m", "max-file": "3" }
}
EOF
systemctl restart docker
```

> `mirror.ccs.tencentyun.com` 是腾讯云内网镜像，腾讯云服务器拉取**几乎不限速**且**不走公网流量**。

### 2.4 系统防火墙（可选，安全组之外再加一层）

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw --force enable
```

---

## 第 3 步：把代码传到服务器

### 方式 A：用 Git（推荐）

```bash
cd /opt
git clone <你的私有 Git 仓库 URL> pms
cd pms/v2
```

### 方式 B：从本地传（Windows）

```bat
:: 本地 cmd / PowerShell
scp -r D:\opencode-project\EXCEL共享维护系统1500\EXCEL共享维护系统\v2 ubuntu@<IP>:/opt/pms-v2
```

或者用 **WinSCP**（图形界面）/ **腾讯云控制台→文件上传**。

---

## 第 4 步：配置环境变量

```bash
cd /opt/pms/v2          # 或 /opt/pms-v2
cp .env.prod.example .env.prod
nano .env.prod
```

### 关键配置（**必须改**）

```bash
# 用这条命令生成强 SECRET_KEY，复制粘贴
openssl rand -hex 32
```

`.env.prod` 改成：

```ini
POSTGRES_USER=pms_prod
POSTGRES_PASSWORD=<至少 16 位强密码>
POSTGRES_DB=pms
SECRET_KEY=<刚才生成的 64 位字符串>
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=<临时密码，登录后立刻改>
ACCESS_TOKEN_EXPIRE_MINUTES=480
```

---

## 第 5 步：（可选）把 Postgres 数据挂到独立数据盘

如果买了 CVM 数据盘（Lighthouse 通常用不到）：

```bash
fdisk -l                            # 看磁盘，假设是 /dev/vdb
mkfs.ext4 /dev/vdb
mkdir -p /data
mount /dev/vdb /data
echo "/dev/vdb /data ext4 defaults,nofail 0 2" >> /etc/fstab

# Docker 目录搬到数据盘
mkdir -p /data/docker
systemctl stop docker
rsync -aP /var/lib/docker/ /data/docker/
mv /var/lib/docker /var/lib/docker.bak
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

用 `admin / <你的临时密码>` 登录，**立刻去个人菜单改密码**。

---

## 第 7 步：配置域名 + HTTPS

### 7.1 域名解析

腾讯云 DNSPod 控制台：https://console.cloud.tencent.com/cns

- 域名记录 → 添加 **A 记录**，主机记 `pms`（或 `@`），值填服务器公网 IP，TTL 600。
- 解析 2-10 分钟生效；`ping pms.your-domain.com` 能看到服务器 IP 即 OK。

### 7.2 改 Nginx 配置

```bash
cd /opt/pms/v2
nano nginx/conf.d/default.conf
```

把 `server_name _;` 改成 `server_name pms.your-domain.com;`，保存。

### 7.3 SSL 证书（推荐方式 1：腾讯云免费证书，简单）

控制台 → SSL 证书 → 免费证书 → 申请 → 选 DNS 验证 → 部署到域名。
下载 Nginx 格式证书包，解压：
- `xxx_bundle.crt` → 重命名为 `fullchain.pem`，传到 `/opt/pms/v2/nginx/certs/`
- `xxx.key` → 重命名为 `privkey.pem`，传到同目录

腾讯云免费证书 1 年有效，到期前 30 天可一键续。

### 7.3 SSL 证书（方式 2：acme.sh + Let's Encrypt，自动续 90 天）

```bash
curl https://get.acme.sh | sh -s email=你的邮箱
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt

# 临时停 nginx 让出 80
docker compose -f docker-compose.prod.yml stop nginx

# 申请
~/.acme.sh/acme.sh --issue --standalone -d pms.your-domain.com

# 部署证书
~/.acme.sh/acme.sh --install-cert -d pms.your-domain.com \
  --key-file       /opt/pms/v2/nginx/certs/privkey.pem \
  --fullchain-file /opt/pms/v2/nginx/certs/fullchain.pem \
  --reloadcmd     "docker compose -f /opt/pms/v2/docker-compose.prod.yml restart nginx"
```

证书 90 天，acme.sh 自动续期，不用管。

### 7.4 启用 HTTPS

编辑 `nginx/conf.d/default.conf`：
- 取消 `listen 443 ssl;` 整段注释
- HTTP 段加 `return 301 https://$host$request_uri;` 强制跳转

```bash
docker compose -f docker-compose.prod.yml restart nginx
```

浏览器访问 `https://pms.your-domain.com` 应该是绿锁。

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

### 8.2 上传到腾讯云对象存储 COS（推荐异地备份）

1. 控制台开通 COS：https://console.cloud.tencent.com/cos
2. 新建 Bucket，例如 `pms-backup-1234567890`，所属地域选**和服务器相同**（内网走流量便宜或免费）
3. 装 coscli（官方命令行工具）：

```bash
wget https://github.com/tencentyun/coscli/releases/latest/download/coscli-linux -O /usr/local/bin/coscli
chmod +x /usr/local/bin/coscli
coscli config init       # 交互式配置 SecretId/SecretKey（密钥管理：https://console.cloud.tencent.com/cam/capi）
```

把上传命令加到 cron 末尾：

```bash
# 在 /etc/cron.daily/pms-backup 文件末尾追加
coscli cp /backup/pms-${DATE}.sql.gz cos://pms-backup-1234567890/db/
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
git pull        # 或 scp 新代码
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

# 进数据库
docker compose -f docker-compose.prod.yml exec postgres psql -U pms_prod pms

# 完全停服
docker compose -f docker-compose.prod.yml down

# ⚠ 完全清理含数据（谨慎）
docker compose -f docker-compose.prod.yml down -v
```

---

## 监控

### 腾讯云原生监控（免费）

控制台 → 云监控（CM） → 默认对 CVM/Lighthouse 监控 CPU/内存/磁盘/带宽，可设置阈值告警发短信/邮件。

### 应用健康检查

```bash
curl -s http://localhost/api/health
# 应返回 {"status":"ok",...}
```

可以写个简单脚本加 cron 自检：

```bash
cat > /etc/cron.d/pms-health <<'EOF'
*/5 * * * * root curl -fs http://localhost/api/health > /dev/null || (date; docker compose -f /opt/pms/v2/docker-compose.prod.yml restart backend) >> /var/log/pms-health.log
EOF
```

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 80/443 打不开 | 安全组 + Lighthouse 防火墙是否都放行；`docker compose ps` 看 nginx 是否 Up |
| 登录后空白 | F12 看 Network；多半是 backend 没起来 → `logs backend` |
| 上传 Excel 失败 | nginx `client_max_body_size 50M` 是否够 |
| WebSocket 连不上 | nginx 配置里 `/ws/` 段必须有 `Upgrade/Connection` 头 |
| 数据库连接失败 | `.env.prod` 里 `POSTGRES_PASSWORD` 和 `DATABASE_URL` 一致 |
| 拉镜像慢/超时 | 确认 `daemon.json` 里 mirror 已生效：`docker info \| grep -A 3 Mirrors` |
| 域名无法访问但 IP 可以 | DNS 是否已生效 `dig pms.xxx.com`；ICP 备案是否完成 |

---

## 性能调优（流量大了再考虑）

- backend 多 worker：`backend/Dockerfile.prod` 把 `--workers 1` 改 `--workers 2~4`
- Postgres：`shared_buffers / effective_cache_size` 调到内存的 25%-75%
- Redis：`maxmemory + allkeys-lru` 策略
- Nginx：加 `gzip` 静态资源压缩，长缓存 `expires 30d`
- 腾讯云 **CDN**：把 `assets/*` 这种静态资源接入，回源到 CVM；前端首屏快很多

---

## 成本估算（参考价，2026 年 5 月）

| 资源 | 规格 | 月费（约） |
|---|---|---|
| Lighthouse 套餐 | 2 核 4G + 80G SSD + 6M | ¥60 |
| CVM S5.MEDIUM4 包月 | 2 核 4G + 50G + 5M | ¥120-150 |
| 数据盘（仅 CVM） | 增强 SSD 100G | ¥35 |
| 域名 .com | / | ¥55/年 |
| COS 备份 | 1GB 存 + 10GB 流量/月 | < ¥5 |
| SSL 证书 | 免费 | ¥0 |

**最小可用配置：Lighthouse 2 核 4G 套餐 ≈ ¥60/月 + 域名 ¥5/月** 起。

---

## 安全清单（上线前过一遍）

- [ ] admin 首次登录已改密码
- [ ] SECRET_KEY 已用 `openssl rand` 重新生成（**不要用示例值**）
- [ ] POSTGRES_PASSWORD 是 16 位以上强密码
- [ ] HTTPS 已启用且 HTTP 跳转 HTTPS
- [ ] 安全组 SSH 来源限制为办公网 IP（不要 0.0.0.0/0）
- [ ] SSH 用密钥登录、`/etc/ssh/sshd_config` 关 `PasswordAuthentication`
- [ ] `apt update && apt upgrade` 定期跑（建议加 unattended-upgrades）
- [ ] 数据库每日备份且至少一份在 COS（异地）
- [ ] 腾讯云监控告警通知人配好（短信/邮件/微信）
- [ ] ICP 备案信息正确（域名实际指向服务器）

---

## 与阿里云版本差异速查

| 项 | 阿里云 | 腾讯云 |
|---|---|---|
| 服务器入口 | ECS 控制台 | CVM / Lighthouse 控制台 |
| DNS 解析 | 云解析 DNS | DNSPod（云解析） |
| 对象存储 | OSS + ossutil | COS + coscli |
| 镜像加速 | `registry.cn-xxx.aliyuncs.com` | `mirror.ccs.tencentyun.com` |
| 备案系统 | 阿里云 ICP 备案 | 腾讯云 ICP 备案（互不通用） |
| SSL 免费证书 | 阿里云 SSL 一键签发 | 腾讯云 SSL 一键签发 |

> 已在阿里云 ECS 备案的网站，迁到腾讯云要做"接入备案"（保留备案号，转到腾讯云）；只需在腾讯云备案系统提交，1-3 天审核。
