#!/bin/bash
# APP 的死条件自检。构建**之前**跑 —— 这几条错了照样能编译成功，
# 但装到手机上要么白屏、要么每次都要重新登录。
#
# 教训来自 Windows 客户端 1.0.30/1.0.31：构建绿、上传绿、清单校验绿，
# 装上去 Cannot find module —— 因为**没有一环检查「装上能不能用」**。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
bad=0
ok(){ echo "  ok: $1"; }
no(){ echo "  FAIL: $1"; bad=1; }

MAIN=app/src/main/java/com/tonghui/pms/MainActivity.kt
MANIFEST=app/src/main/AndroidManifest.xml
NETCFG=app/src/main/res/xml/network_security_config.xml

echo "===== APP 配置自检 ====="

# ① 服务器只有 HTTP。安卓 9+ 默认禁明文，不放行 → 一打开就是「连接不上服务器」
grep -q 'usesCleartextTraffic="true"' "$MANIFEST" \
  && ok "清单开了 usesCleartextTraffic（服务器只有 HTTP，不开必然连不上）" \
  || no "清单没开 usesCleartextTraffic —— 服务器是 HTTP，装上去必然白屏"

grep -q 'networkSecurityConfig' "$MANIFEST" \
  && ok "挂了 network_security_config（只给自家域名放行，不是全局放开）" \
  || no "没挂 network_security_config"

for d in 8.141.123.141 pms.tonghui-tech.com; do
  grep -q "$d" "$NETCFG" && ok "明文白名单含 $d" || no "明文白名单缺 $d"
done

# ② H5 把登录令牌存 localStorage，不开 DOM storage 每次都要重登
grep -q 'domStorageEnabled = true' "$MAIN" \
  && ok "开了 domStorageEnabled（令牌存 localStorage，不开每次都要重新登录）" \
  || no "没开 domStorageEnabled —— 会导致每次打开都要重新登录"

grep -q 'javaScriptEnabled = true' "$MAIN" \
  && ok "开了 JavaScript" || no "没开 JavaScript，Vue 页面根本跑不起来"

# ③ 入口地址必须指向 H5（网页端在手机上没法用）
URL=$(grep -oE 'APP_URL = "[^"]+"' "$MAIN" | cut -d'"' -f2)
case "$URL" in
  */h5/) ok "入口指向 H5：$URL" ;;
  *)     no "入口不是 H5：$URL" ;;
esac
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then
  ok "入口 $URL 当前可访问"
else
  echo "  warn: $URL 现在访问不到（HTTP $CODE；CI 到不了内网属正常，不阻断）"
fi

# ④ 返回键要在 H5 里后退，不是一按就退出 APP
grep -q 'canGoBack' "$MAIN" \
  && ok "返回键先在页面内后退，退到头才退出" || no "返回键没处理，会一按就退出 APP"

# ⑤ 连不上时必须有可重试的页面，不能只留白屏
if grep -q 'R.id.offline' "$MAIN" && grep -q 'R.id.retry' "$MAIN"; then
  ok "有离线页 + 重试按钮（白屏是最让人摸不着头脑的状态）"
else
  no "缺离线页或重试按钮"
fi

# ⑥ 附件下载要交给系统下载器，WebView 自己存不了
grep -q 'setDownloadListener' "$MAIN" \
  && ok "下载交给系统下载器" || no "没接下载，点附件不会有反应"

echo
if [ "$bad" = 0 ]; then echo "✅ 配置自检通过"; else echo "❌ 自检失败，不要打包"; exit 1; fi
