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
