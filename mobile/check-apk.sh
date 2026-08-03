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
  unzip -l "$APK" | grep -q "classes.dex" \
    && ok "有 classes.dex（代码真的编进去了）" || no "没有 classes.dex"
  unzip -l "$APK" | grep -q "resources.arsc" \
    && ok "有 resources.arsc（布局/明文放行配置在里面）" || no "没有资源表"
  # ⚠️ **不能**用 META-INF/*.RSA 判有没有签名。
  #    那是 v1(JAR) 签名的痕迹；minSdk≥24 的包 AGP 只做 v2/v3 签名，
  #    签名块在 APK 尾部、不是文件条目，一 grep 就误判成「没签名」。
  #    实测栽过一次：包其实签好了，检查却把它拦下来。用 apksigner 才准。
  :
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
