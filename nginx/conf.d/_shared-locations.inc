# ⚠️⚠️ 这里被 default.conf 的 **两个 server 块（80 和 443）** 同时 include。
#    改任何一条 location 都会同时影响 HTTP 和 HTTPS —— 这正是目的：
#    两份配置各写一遍必然漂移，漏改一边就是「HTTPS 上能用、HTTP 上 404」。
#
# ⚠️ 文件名必须是 .inc **不能是 .conf**：nginx.conf 里是
#    `include /etc/nginx/conf.d/*.conf;`，叫 .conf 会被当成顶层配置加载，
#    而裸的 location 指令不能出现在 http{} 顶层 —— nginx 直接起不来。


    # 🆕 安全响应头：防点击劫持 / MIME 嗅探 / 控制 referer 泄露

    # ACME http-01 验证用
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # 🆕 企业微信「可信域名」验证文件：把 WW_verify_*.txt 放到 nginx/wecom-verify/ 即可被访问
    location ~ ^/WW_verify_.*\.txt$ {
        root /var/www/wecom;
        default_type text/plain;
        access_log off;
    }

    # 临时直接走 HTTP；申请 SSL 后请改为：
    # return 301 https://$host$request_uri;

    client_max_body_size 50M;

    # 🆕 gzip 压缩。2026-08-15 之前**整站没开**，实测明文过网：
    #      index.js 2.2MB · index.css 890KB · /api/wh/materials 334KB(863 个物料)
    #    这三样每次打开系统都要重下一遍，办公室外网上就是十几秒白屏。
    #    ⚠️ `gzip_proxied any` 是关键：本站所有内容（前端和 API）都是 proxy_pass 上来的，
    #       而 gzip_proxied 默认 off = **代理来的响应一律不压**，只写 `gzip on` 等于没开。
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;          # 5 是 CPU/压缩比的常用平衡点，再高收益很小
    gzip_min_length 1024;       # 小响应压了反而更大（还多一次 CPU）
    gzip_types
        text/plain text/css text/xml text/javascript
        application/javascript application/json application/xml
        application/wasm image/svg+xml;

    # 前端
    location / {
        proxy_pass http://frontend:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 🆕 登录限频：单独对登录接口限频防爆破（其余 /api/ 不限频，避免误伤正常高频请求）
    location = /api/auth/login {
        limit_req zone=login_limit burst=5 nodelay;
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 🆕 Agent 流式问答（SSE）：必须关掉缓冲，否则 nginx 会把整个响应攒完再发，
    #   流式当场退化成"等 17 秒再一次性出现"，等于白做。
    #   放在 location /api/ 之前，前缀更长优先匹配。
    location /api/agent/chat/stream {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
        proxy_read_timeout 300;
        gzip off;   # ⚠️ 流式必须关压缩：gzip 会攒够一块才吐，又把流式变回"等半天一次性出现"
    }

    # 后端 API
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    # 🆕 桌面客户端更新通道（安装包/清单静态目录，发布见 desktop/release.sh）
    location /desktop/ {
        alias /opt/pms/desktop/;
        autoindex off;
    }

    # 🆕 手机 APP 前端热更新通道（version.json + h5-<版本>.zip，发布见 mobile/ship.sh）
    #   ⚠️ 整段 no-cache：version.json 被缓存住的话，抬了门槛也等于没发版。
    #      zip 一个版本只下一次，不值得为它单开一条缓存规则再多一处能配错的地方。
    location /h5-ota/ {
        alias /opt/pms/h5-ota/;
        autoindex off;
    }
