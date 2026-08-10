# 多端登录入口与差异对比

> 摸清项目里所有"能登录"的入口,以及它们各自与浏览器/桌面两条主登录通道的差异。
> 结论:登录协议**只有一个**(标准 `POST /api/auth/login`),端与端的差异全部发生在「外网免闸判定」与「API 地址来源」两个维度上。

## 一、入口全景

| 端 | 前端代码 | 打包产物 | API 地址来源 | 登录页面 |
|---|---|---|---|---|
| 浏览器网页版 | `frontend/src/views/LoginView.vue` | `frontend/dist` | 相对 `/api`(nginx 代理) | LoginView |
| H5(手机浏览器) | `frontend/src/h5/H5LoginView.vue` | `frontend/dist-h5`(`h5.html`) | 相对 `/api`(同源部署) | H5LoginView |
| 手机 APP(Capacitor 壳) | 同一套 H5 代码 | `mobile/www`(由 `frontend/dist-h5-app` 产物改名而来) | **绝对地址 `http://8.141.123.141/api`**(构建时注入) | H5LoginView |
| 桌面客户端(Electron) | `frontend/src/views/LoginView.vue` | 内置 `frontend/dist` | `VITE_API_BASE=http://8.141.123.141`(桌面打包设) | LoginView |
| demo 单机演示版 | 同网页版 | `demo/web`(= `frontend/dist` 拷贝) | 相对 `/api`(本机 uvicorn 直出) | LoginView |

## 二、登录协议只有一个,各端复用同一套接口

所有端都走标准 `POST /api/auth/login` + `POST /api/auth/login/verify-gate`,无独立协议
(桌面客户端也不例外,见 `backend/app/routers/auth_router.py:83-121`)。

- 登录返回双形态:直接发 token(`TokenOut`),或命中闸门时返回 `gate_required=true + pre_token`(`GateRequiredOut`),前端见 `frontend/src/h5/H5LoginView.vue:3-4` 注释「后端一行没改,走的还是 /auth/login + /auth/login/verify-gate」。
- 免闸判定唯一权威在 `backend/app/routers/auth_router.py:88-97`:

```
ip = _client_ip(request)                                    # X-Real-IP 优先,XFF 取末段
is_desktop = request.headers.get("x-pms-client", "").startswith("desktop/")
if not u.has_role("admin"):                                 # admin 恒免闸
    cfg = gate.get_gate_config(db)
    exempt = gate.is_intranet(ip, cfg["cidrs"]) or gate.desktop_exempt(...)
    if cfg["enabled"] and not exempt:
        → issue_code() 返回 GateRequiredOut
return _issue_token(...)                                    # 否则直接发 token
```

### 各端免闸差异(关键结论)

| 维度 | admin 角色 | 内网/回环 IP | X-PMS-Client 头(桌面) | 外网浏览器/H5/APP |
|---|---|---|---|---|
| 是否免闸 | ✅ 恒免 | ✅ `gate.py:54-65` 回环+私网恒判内网,另有 cidrs 名单 | ✅ `gate.py:67-85`,device_gate 默认关 → 带客户端头就免 | ❌ 必走验证码闸(闸门开启时) |

**差异本质**:
- 浏览器网页版 / H5 / 手机 APP 三者**在服务端眼里完全一样**——都不带 `X-PMS-Client` 头,只靠 IP 判定。手机 APP 外网登录同样要过企微验证码,没有桌面客户端那条免闸通道。这是刻意设计(`H5LoginView.vue:3-4` 注释说明 H5 复用了同一条闸门)。
- 桌面客户端唯一的不同点是 axios 拦截器带 `X-PMS-Client/X-PMS-Device/X-PMS-User` 三个统计头(`frontend/src/api/index.ts:14-25`),后端据此判 `is_desktop` 免闸(`auth_router.py:90`)。
- demo 版因本机 `127.0.0.1` 恒判内网(`gate.py:58-62`),闸门对 demo 无效,`admin/admin123` 直登。

## 三、各端细节

### 1. 浏览器网页版
- 入口路由 `/login`,登录页 `frontend/src/views/LoginView.vue`。
- 外网非 admin 账号:两步闸门,第一步密码 → 返回 `pre_token` → 第二步 `verify-gate` 输码 → 发 token。
- 记住我:`REMEMBER_MINUTES = 30*24*60`(30 天),`auth_router.py:63-66` 注释「延长的只是令牌有效期,密码不落客户端」。

### 2. H5(手机浏览器 / APP 共用同一套代码)
- 源码 `frontend/src/h5/`:`H5LoginView.vue` / `H5App.vue` / `session.ts` / `http.ts` / `apiBase.ts`。
- **刻意不复用 `@/api/index.ts`**:该文件 import 了 element-plus 的 `ElMessage`,一引就把整个 element-plus + vxe-table 拖进 H5 包;H5 给手机 4G 用,只有登录和助手两页,不该背这个体积(`http.ts:3-8`)。
- `http.ts` 只带 `Authorization: Bearer`(拦截器),**不带** `X-PMS-Client` → H5 与网页版在服务端不可区分,外网都走验证码闸。
- 401 处理:`localStorage.removeItem('pms_token')` + 跳 `#/login`(`http.ts:29-36`)。
- 登录页两步逻辑:`H5LoginView.vue:48-49` 命中 `gate_required` 则进第二步;`H5LoginView.vue:100-101` 验证码可重发,旧码作废。

### 3. 手机 APP(Capacitor 壳,`mobile/`)
- `capacitor.config.ts` 指向 `www/`;`mobile/www/index.html` 引 `assets/h5-*.js`,即 `frontend/dist-h5-app` 构建产物改名(入口必须叫 `index.html`,见 `mobile/check.sh:59-60`)。
- API 地址来源分叉在 `frontend/src/h5/apiBase.ts:11-13`:网页版用相对 `/api`,APP 构建时注入 `VITE_API_BASE=http://8.141.123.141`(**构建时注入,不在运行时探测**;注释警告改成绝对地址后请求跨域,后端 CORS 必须放行,见 `backend/app/main.py` 的 `_app_origins`)。
- 构建命令区分:网页 H5 = `npm run build:h5`,APP = `npm run build:h5:app`(`frontend/package.json:11-13`,后者设 `H5_TARGET=app`)。
- 部署校验:`mobile/check.sh:62-65` 检查 `www/assets/` 里含绝对 API 地址 `http://8.141.123.141/api`,否则报错「www 像是网页版产物—— 应跑 npm run build:h5:app」。
- 热更新:上传 `ops/ota-out/h5-*.zip` + `version.json` 到服务器 `h5-ota` 目录(nginx 挂载 `./h5-ota:/opt/pms/h5-ota:ro`,`docker-compose.prod.yml:77`),APP 启动时拉 manifest 决定是否换前端包。**注意:热更新只换前端代码,登录协议不变。**

### 4. 桌面客户端(Electron,`desktop/`)
- 详见 `docs/login-explore/04-desktop-login.md`。核心差异三处介入:
  1. preload 注入 `window.pmsDesktop{isDesktop,version,deviceId}`;
  2. axios 拦截器带三统计头(`frontend/src/api/index.ts:14-25`);
  3. `X-PMS-Client` 头判 `is_desktop` 免外网验证码闸(`auth_router.py:90`)。
- 桌面免闸可被伪造(`X-PMS-Client` 头 curl 一加就绕过);真正防线 `device_gate`(设备名单)默认关(`gate.py:67-85`)。

### 5. demo 单机演示版(`demo/`)
- `demo/setup.sh` 构建前端到 `demo/web`;`demo/start.sh` 设 `DATABASE_URL=sqlite+aiosqlite:///./data/app.db`、`STATIC_DIR=../demo/web`,本机 `uvicorn app.main:app --host 127.0.0.1 --port 8000`。
- 登录 `admin/admin123`,首次登录要求改密(`seed.py` 建的账号 `password_must_change=True`),`demo/README.md` 建议「演示时可继续用 admin123,直接关掉改密对话框」。
- 与生产唯一架构差异:SQLite + 无 nginx + 回环 IP → 闸门天然不触发。

## 四、nginx 与登录相关的配置

- **登录限频**:`nginx/conf.d/default.conf:2-3` 定义 `login_limit` zone(10r/m,burst 5),`nginx/conf.d/_shared-locations.inc:39-41` 只挂在 `location = /api/auth/login` 上,`limit_req_status 429`。**verify-gate 不在此限频内**(走通用 `location /api/`)。
- **X-Real-IP 覆写**:nginx 在每个 proxy location 都 `proxy_set_header X-Real-IP $remote_addr`,这是后端 `_client_ip()` 取真实 IP 的依据(`_shared-locations.inc:43`),外部伪造不了。
- **企微可信域名验证**:`nginx/conf.d/_shared-locations.inc:17-22` 对 `~ ^/WW_verify_.*\.txt$` 服务 `nginx/wecom-verify/` 目录(`docker-compose.prod.yml:75` 挂 `/var/www/wecom:ro`)。这是企业微信「可信域名」验证文件,供 H5 调企微相关能力用,与登录鉴权无关。
- **SSL**:`nginx/certs` + ACME(http-01,`_shared-locations.inc:13-15`),`enable-https.sh`/`renew-cert.sh` 管理,`docker-compose.prod.yml:73-74` 挂载。

## 五、ops/ 里与登录、账号相关的线索

- `ops/reset-admin-password.sh`:应急重置 admin 密码(docker exec 进 `pms2_backend`,改 `password_hash`、`is_active=True`、`password_must_change=False`)。这是 admin 忘密码/被锁的唯一恢复通道。
- `ops/release.sh` / `upgrade.sh` / `backup.sh` / `restore.sh`:发版与备份,不涉及登录协议。
- `ops/ota-out/`:手机 APP 前端热更新包(`h5-*.zip` + `version.json`),`mobile/ship.sh` 上传。
- **ops 里没有独立的"登录配置"脚本**——外网闸门开关/内网名单/设备名单全部走「管理 → 外网访问」页面(写 `app_settings`),运行时实时读库(`gate.py:96-127`),不发版即可调。

## 六、种子账号:创建与生效范围

- **创建位置**:`backend/app/seed.py:60-103`,每次启动 lifespan 里 `await seed(db)`(`backend/app/main.py:85`)。
- 账号内容:
  - `admin`:`settings.default_admin_username/password`,默认 `admin / admin123`,`config.py` 可改(`seed.py:63-85`);
  - `manager`:硬编码 `manager / manager123`(`seed.py:87-103`);
  - 两者 `password_must_change=True`,配 admin/manager 角色(角色表也在 seed 里预填,`seed.py:15-16,51`)。
- **生效范围**:**所有环境都建**——生产 postgres、开发 SQLite、demo SQLite 只要跑 `app.main` lifespan 就执行(幂等,账号已存在则跳过)。也就是说「种子账号」在生产上是真实账号,不是演示专属。
- 改密码方式:登录后「我的」里改,或 `ops/reset-admin-password.sh`。seed 不覆盖已存在账号的密码。

## 七、反例 / 被排除的猜想

1. **「H5 有自己的登录协议」**——不成立。H5LoginView 顶部注释明说「后端一行没改,走的还是 /auth/login + /auth/login/verify-gate」(`H5LoginView.vue:3-4`)。H5 与网页版、APP、桌面端共用同一套 auth_router 接口。
2. **「手机 APP 免验证码」**——不成立。APP 走 H5 的 `http.ts`,不带 `X-PMS-Client` 头,服务端视角与浏览器无异;只有内网 IP 或 admin 才免闸。
3. **「verify-gate 有独立限频」**——不成立。nginx 限频只挂了 `= /api/auth/login` 一个 location(`_shared-locations.inc:39`),`/api/auth/login/verify-gate` 走通用 `/api/` 无限频。验码限频实际靠 `gate.py:129-138` 的「同账号 1 条/分」发码限频兜底(错码只计 fail_count,不再锁)。
4. **「demo 是独立登录体系」**——不成立。demo 只是换了 SQLite 数据库 + 去掉 nginx,登录接口与权限体系与生产完全同一套,仅因回环 IP 天然免闸。

## 八、一处值得留意的不一致

H5 端 401 时 `http.ts:29-36` 会 `localStorage.removeItem('pms_token')` 并跳登录;而网页版 `LoginView.vue` 是否做同样清理、以及桌面端 preload 注入的 token 管理,与 H5 不是同一份代码(`@/api/index.ts` 与 `h5/http.ts` 是两份独立 axios 实例)。排查「登录态被清/没清」时先分清当前端用的是哪一份。
