#!/bin/bash
# 手机 APP 的死条件自检。**构建之前**跑。
#
# 这几条错了照样能编译成功，但装到手机上要么白屏、要么每个接口都失败、
# 要么按一下返回键就退出。教训来自 Windows 客户端 1.0.30/1.0.31：
# 构建绿、上传绿、清单校验绿，装上去 Cannot find module ——
# **没有一环检查过「装上能不能用」**。
#
# ⚠️ 迁到 Capacitor 之后该防的东西整个换了一批：
#    旧壳防的是「页面从服务器取不到」；新壳页面在本地，要防的是
#    「本地页面调不动服务器的 API」和「换包换出问题」。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
bad=0
ok(){ echo "  ok: $1"; }
no(){ echo "  FAIL: $1"; bad=1; }
warn(){ echo "  warn: $1"; }

MAIN=android/app/src/main/java/com/tonghui/pms/MainActivity.java
UPD=android/app/src/main/java/com/tonghui/pms/PmsUpdater.java
MANIFEST=android/app/src/main/AndroidManifest.xml
NETCFG=android/app/src/main/res/xml/network_security_config.xml
CAPCFG=capacitor.config.ts
APIBASE=../frontend/src/h5/apiBase.ts
VITECFG=../frontend/vite.config.h5.js
KEYASSET=android/app/src/main/assets/ota_public_key.pem

echo "===== APP 配置自检（Capacitor 版）====="

# ── ① 页面在本地、API 在服务器：scheme 与明文放行 ──────────────────────
# androidScheme 必须是 http。默认的 https 会把页面变成安全页面，
# 而我们的 API 是明文 http —— **混合内容拦截**，APP 里每个接口直接失败。
grep -q "androidScheme: 'http'" "$CAPCFG" \
  && ok "androidScheme=http（https 会触发混合内容拦截，API 全挂）" \
  || no "androidScheme 不是 http —— 明文 API 会被混合内容规则拦掉"

grep -q "hostname: 'localhost'" "$CAPCFG" \
  && ok "hostname=localhost（localhost 属可信来源，安全上下文仍成立）" \
  || no "hostname 不是 localhost"

grep -q 'usesCleartextTraffic="true"' "$MANIFEST" \
  && ok "清单开了 usesCleartextTraffic（服务器只有 HTTP）" \
  || no "没开 usesCleartextTraffic —— 服务器是 HTTP，装上去所有接口失败"

grep -q 'networkSecurityConfig' "$MANIFEST" \
  && ok "挂了 network_security_config（只给自家域名放行，不是全局放开）" \
  || no "没挂 network_security_config"

for d in 8.141.123.141 pms.tonghui-tech.com; do
  grep -q "$d" "$NETCFG" && ok "明文白名单含 $d" || no "明文白名单缺 $d"
done

# ── ② API 地址：本地页面用相对路径会打到本地包上（404）────────────────
grep -q "VITE_API_BASE" "$APIBASE" \
  && ok "API base 可注入（apiBase.ts）" || no "apiBase.ts 里没有可注入的 API base"
grep -q "H5_TARGET === 'app'" "$VITECFG" \
  && ok "构建区分网页版/APP 版（vite.config.h5.js）" || no "构建没区分两个目标"

if [ -f www/index.html ]; then
  ok "www/index.html 存在（Capacitor 只认 index.html，产物叫 h5.html，必须改名）"
  # APP 包里必须是绝对 API 地址；是相对 '/api' 就说明打成网页版了
  if grep -rqs "http://8.141.123.141/api" www/assets/; then
    ok "www 是 APP 版产物（API 为绝对地址）"
  else
    no "www 像是网页版产物（没有绝对 API 地址）—— 应跑 npm run build:h5:app"
  fi
else
  warn "www/ 还没构建（CI 里会现构建，本地构建前属正常）"
fi

# ── ③ Capacitor 默认没有、少了就出事的几件事 ─────────────────────────
# registerPlugin / beforeBridgeLoad 必须早于 super.onCreate：
# Bridge 在 super.onCreate 末尾才 create，晚一步这次启动就不生效（且不报错）。
if python3 - "$MAIN" <<'PY'
import sys, re
s = open(sys.argv[1]).read()
# ⚠️ 必须先剥注释再比位置：这几个名字在文档注释里也会出现（就是在解释这条规则），
#    直接 find 会命中注释，把写对了的代码判成写错（实测栽过一次）。
s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
s = re.sub(r'//[^\n]*', '', s)
sup = s.find("super.onCreate")
# ⚠️ 用 rfind 不是 find：要求**每一处** registerPlugin 都在 super.onCreate 之前。
#    用 find 的话，只要第一个插件注册对了就放行，后面新加的插件排在后面照样溜过去 ——
#    而那个插件在 JS 侧就是「调用了不存在的方法」，还查不出原因。
reg = s.rfind("registerPlugin")
pre = s.rfind("PmsUpdater.beforeBridgeLoad")
sys.exit(0 if (0 <= reg < sup and 0 <= pre < sup) else 1)
PY
then ok "插件注册与换包决策都排在 super.onCreate 之前"
else no "顺序不对：registerPlugin / beforeBridgeLoad 必须早于 super.onCreate"
fi

grep -q "canGoBack" "$MAIN" \
  && ok "返回键先在页面内后退" \
  || no "没接返回键 —— Capacitor 的 BridgeActivity 不处理硬件返回，会一按就退出 APP"

grep -q "setDownloadListener" "$MAIN" \
  && ok "附件下载交给系统下载器" || no "没接下载，点附件不会有反应"

grep -q "setDecorFitsSystemWindows(getWindow(), false)" "$MAIN" \
  && ok "开了 edge-to-edge（否则 env(safe-area-inset-*) 恒为 0，刘海适配白做）" \
  || no "没开 edge-to-edge，H5 的安全区适配拿不到值"

grep -q "RECORD_AUDIO" "$MANIFEST" \
  && ok "声明了麦克风权限（WebView 没有 Web Speech API，语音走原生桥）" \
  || no "没声明 RECORD_AUDIO，APP 里语音用不了"
# ⚠️ WebView 的 getUserMedia 录音要 RECORD_AUDIO **加** MODIFY_AUDIO_SETTINGS，
#    缺后者 Chromium 直接拒——系统设置里给了麦克风也没用（华为机上实证过）
grep -q "MODIFY_AUDIO_SETTINGS" "$MANIFEST" \
  && ok "声明了 MODIFY_AUDIO_SETTINGS（云端录音 getUserMedia 需要）" \
  || no "没声明 MODIFY_AUDIO_SETTINGS，云端语音在 APP 里必然报权限错"

# ── ③.5 资源 XML 必须合法 ─────────────────────────────────────────────
# ⚠️ **XML 注释里不能出现 `--`**。写了照样过 IDE、也过 git，但 mergeReleaseResources
#    会以一堆 SAXParseException 栈直接失败，而错误信息埋在几十行栈里很难一眼看到。
#    实测栽过一次：注释里写了「与 --h5-blue 同值」，CI 打包整个挂掉。
if python3 - <<'PY'
import xml.dom.minidom, glob, sys
bad = []
for f in glob.glob('android/app/src/main/res/**/*.xml', recursive=True) + \
         ['android/app/src/main/AndroidManifest.xml']:
    try:
        xml.dom.minidom.parse(f)
    except Exception as e:
        bad.append(f"{f}: {e}")
if bad:
    print("\n".join("    " + b for b in bad))
sys.exit(1 if bad else 0)
PY
then ok "资源 XML 全部合法（注释里不能有 -- ，否则资源合并直接失败）"
else no "有非法 XML（见上），资源合并会失败"
fi

# ── ③.6 应用图标：错了照样能编译，装上才发现是默认图标 ─────────────────
if python3 - <<'PY'
import os, sys
try:
    from PIL import Image
except ImportError:
    print("    warn: 没装 Pillow，跳过图标尺寸校验"); sys.exit(0)
# 密度 → (传统图标边长, 自适应前景边长=108dp 换算)
EXP = {"mdpi": (48, 108), "hdpi": (72, 162), "xhdpi": (96, 216),
       "xxhdpi": (144, 324), "xxxhdpi": (192, 432)}
bad = []
for d, (leg, fg) in EXP.items():
    base = f"android/app/src/main/res/mipmap-{d}"
    for name, want in (("ic_launcher", leg), ("ic_launcher_round", leg),
                       ("ic_launcher_foreground", fg), ("ic_launcher_monochrome", fg)):
        p = f"{base}/{name}.png"
        if not os.path.exists(p):
            bad.append(f"缺 {p}"); continue
        s = Image.open(p).size
        if s != (want, want):
            bad.append(f"{p} 是 {s}，应为 {want}×{want}")
# ⚠️ 自适应前景**必须透明底**：画了底色的话，启动器裁切时会露出一圈方角
for d in EXP:
    p = f"android/app/src/main/res/mipmap-{d}/ic_launcher_foreground.png"
    if os.path.exists(p):
        im = Image.open(p).convert("RGBA")
        if im.getpixel((0, 0))[3] != 0:
            bad.append(f"{p} 四角不透明 —— 自适应前景不能自己画底")
print("\n".join("    " + b for b in bad))
sys.exit(1 if bad else 0)
PY
then ok "应用图标齐全（5 档密度 × 传统/圆形/自适应/单色），前景透明底"
else no "应用图标有问题（见上）—— 重跑 python3 mobile/tools/make-icons.py"
fi

# ── ④ 热更新：验不了签就等于谁都能往 APP 里推 JS ──────────────────────
if [ -f "$KEYASSET" ] && grep -q "BEGIN PUBLIC KEY" "$KEYASSET"; then
  ok "APK 里有热更新验签公钥"
else
  no "缺 $KEYASSET —— 没公钥时 APP 拒绝一切热更新（有意的失败即拒绝）"
fi
grep -q "越界路径" "$UPD" \
  && ok "解压挡了 zip-slip" || no "解压没挡 zip-slip：包里写 ../.. 能写到私有目录之外"
grep -q "TRIAL_TIMEOUT_MS" "$MAIN" \
  && ok "新包有试用期（起不来自动回滚，不让用户对着白屏）" || no "没有试用/回滚机制"

# ── ⑤ 后端得放行 APP 的来源，否则每个接口都是 CORS 失败 ─────────────
grep -q '"http://localhost"' ../backend/app/main.py \
  && ok "后端 CORS 放行了 http://localhost（APP 的页面来源）" \
  || no "后端 CORS 没放行 http://localhost —— APP 里每个接口都失败，且手机上看不到报错"

echo
if [ "$bad" = 0 ]; then echo "✅ 配置自检通过"; else echo "❌ 自检失败，不要打包"; exit 1; fi
