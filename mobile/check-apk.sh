#!/bin/bash
# 打开刚产出的 APK 看里面对不对。
# 「构建成功」和「装上能用」是两件事 —— Windows 客户端 1.0.30 就是全流程绿灯
# 却装不上，因为没有一环打开包看过。
set -euo pipefail
APK="${1:?用法: check-apk.sh <apk 路径>}"
[ -f "$APK" ] || { echo "❌ 找不到 $APK —— 产物路径变了？"; exit 1; }
bad=0
ok(){ echo "  ok: $1"; }
no(){ echo "  FAIL: $1"; bad=1; }

echo "===== APK 内容校验 ====="
echo "  文件：$APK（$(du -h "$APK" | cut -f1)）"

AAPT=$(ls "${ANDROID_HOME:-/nonexistent}"/build-tools/*/aapt2 2>/dev/null | sort -V | tail -1 || true)
if [ -n "$AAPT" ]; then
  DUMP=$("$AAPT" dump badging "$APK" 2>/dev/null || true)
  echo "$DUMP" | grep -q "package: name='com.tonghui.pms'" \
    && ok "包名 com.tonghui.pms" || no "包名不对"
  echo "$DUMP" | grep -q "uses-permission: name='android.permission.INTERNET'" \
    && ok "有联网权限" || no "没有联网权限，APP 打不开任何页面"
  echo "$DUMP" | grep -q "launchable-activity" \
    && ok "有启动入口（桌面上能点开）" || no "没有 launcher activity，装上桌面看不到图标"
  echo "$DUMP" | grep -q "application-label" \
    && ok "有应用名" || no "没有应用名"
else
  echo "  warn: 找不到 aapt2，跳过 badging 检查"
fi

if command -v unzip >/dev/null 2>&1; then
  # ⚠️ 先把清单落成文件再 grep。写成 `unzip -l ... | grep -q` 的话，
  #    grep 命中即退出会让 unzip 收到 SIGPIPE，在 set -o pipefail 下整条管道判失败 ——
  #    **包是好的，检查却把它拦下来**（打热更新包时实测栽过一次）。
  LIST=$(mktemp)
  unzip -l "$APK" > "$LIST"

  grep -q "classes.dex" "$LIST" \
    && ok "有 classes.dex（代码真的编进去了）" || no "没有 classes.dex"
  grep -q "resources.arsc" "$LIST" \
    && ok "有 resources.arsc（布局/明文放行配置在里面）" || no "没有资源表"

  # 🆕 Capacitor 版的关键：前端包**在 APK 里**。少了它装上就是白屏，
  #    而这恰恰是「编译成功」完全看不出来的一类错。
  grep -q "assets/public/index.html" "$LIST" \
    && ok "内置前端包在（assets/public/index.html）" \
    || no "APK 里没有内置前端包 —— 装上必定白屏（漏了 npx cap sync？）"
  grep -q "assets/public/assets/" "$LIST" \
    && ok "前端资源目录在" || no "前端只有入口没有资源，装上还是白屏"

  # 热更新验签公钥：没有它 APP 会拒绝一切热更新（有意的失败即拒绝），
  # 但那意味着这个包发出去之后**再也推不动前端**，只能重发 APK。
  grep -q "assets/ota_public_key.pem" "$LIST" \
    && ok "热更新验签公钥在（没有它这个包将永远收不到热更新）" \
    || no "缺 assets/ota_public_key.pem —— 这个包发出去就再也推不了前端"

  # 打成网页版产物的话 API 是相对路径，APP 里每个接口都 404 在本地包上
  if command -v unzip >/dev/null 2>&1; then
    if unzip -p "$APK" 'assets/public/assets/*.js' 2>/dev/null | grep -q "http://8.141.123.141/api"; then
      ok "内置前端是 APP 版（API 为绝对地址）"
    else
      no "内置前端像是网页版（API 是相对路径）—— APP 里所有接口会 404"
    fi
  fi
  rm -f "$LIST"
fi

# 签名：用 apksigner 验，这是唯一准确的判据
SIGNER=$(ls "${ANDROID_HOME:-/nonexistent}"/build-tools/*/apksigner 2>/dev/null | sort -V | tail -1 || true)
if [ -n "$SIGNER" ]; then
  if "$SIGNER" verify --print-certs "$APK" >/tmp/_sig 2>&1; then
    ok "已签名（apksigner 验过；没签名的 APK 手机拒绝安装）"
    grep -qiE "v2|v3" /tmp/_sig 2>/dev/null && ok "用的是 v2/v3 签名方案" || true
  else
    no "签名校验不通过：$(head -2 /tmp/_sig | tr '\n' ' ')"
  fi
else
  echo "  warn: 找不到 apksigner，跳过签名校验"
fi

echo
if [ "$bad" = 0 ]; then echo "✅ APK 校验通过"; else echo "❌ 这个包别发"; exit 1; fi
