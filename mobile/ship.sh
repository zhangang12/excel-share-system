#!/bin/bash
# ============================================================
# 手机 APP 前端热更新发版 —— 改完前端一条命令让手机上的人收到
#
#   构建 APP 版 H5 → 打 zip → 算 sha256 → 私钥签名 → 上传服务器 → 最后写 version.json
#
# 跟桌面客户端 desktop/ship.sh 是同一个模型：包先到位，清单最后写。
#
# 用法:
#   bash mobile/ship.sh                        版本号 = 日期+序号（如 2026.08.05-1）
#   bash mobile/ship.sh --set-version 3.1.0    指定版本号
#   bash mobile/ship.sh --min-shell 2.1.0      本包要求的最低 APK 版本（默认沿用上一次）
#   bash mobile/ship.sh --notes "修了xxx"      更新说明
#   bash mobile/ship.sh --dry-run              只打印，不上传
#
# 前置:
#   .deploy.local        服务器地址（与 desktop/release.sh 同一份）
#   .deploy.local.ota.pem 热更新签名私钥（gitignored，只在发版机器上）
#
# ⚠️ 顺序不能反：**必须先把 zip 传上去，再写 version.json**。
#    反过来会出现「清单说有 X 但服务器上没有 X」——所有手机都在下一个不存在的包。
#
# ⚠️ min_shell 只在「新前端用了老 APK 没有的原生能力」时才抬。
#    抬了之后老 APK 不再热更新，会提示用户换 APK —— 那意味着要重新发 APK 给所有人，
#    别为了一次普通前端改动就抬。
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

CONF="$PROJECT_DIR/.deploy.local"
KEY="$PROJECT_DIR/.deploy.local.ota.pem"
OUT="$PROJECT_DIR/ops/ota-out"
SET_VERSION=""
MIN_SHELL=""
NOTES=""
DRY=0
PACK_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --set-version) SET_VERSION="$2"; shift 2 ;;
    --min-shell)   MIN_SHELL="$2"; shift 2 ;;
    --notes)       NOTES="$2"; shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    # 只产出本地包与清单，不碰服务器。CI 和自测走这条。
    --pack-only)   PACK_ONLY=1; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

say() { echo -e "\n\033[1;36m── $* ──\033[0m"; }
run() { if [[ $DRY == 1 ]]; then echo "[dry-run] $*"; else eval "$@"; fi; }
# macOS 是 shasum，Linux(CI) 是 sha256sum
sha256of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
             else shasum -a 256 "$1" | awk '{print $1}'; fi; }

[[ -f "$CONF" || $PACK_ONLY == 1 ]] || { echo "❌ 缺 .deploy.local（服务器地址）"; exit 1; }
[[ -f "$KEY" ]]  || { echo "❌ 缺 $KEY —— 没有私钥签不了名，而 APP 拒绝安装没签名的包。
   首次生成：openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out $KEY
   然后把公钥更新进 APK 并重新发 APK：
   openssl rsa -in $KEY -pubout -out mobile/android/app/src/main/assets/ota_public_key.pem"; exit 1; }

if [[ -f "$CONF" ]]; then set -a; . "$CONF"; set +a; fi
DEPLOY_PORT="${DEPLOY_PORT:-22}"

if [[ $PACK_ONLY == 0 ]]; then
  : "${DEPLOY_PATH:?请在 .deploy.local 填 DEPLOY_PATH}"
  # SSH/SCP 组装：与 desktop/release.sh 完全同一套，优先用 ~/.ssh/config 的 Host 别名
  if [[ -n "${DEPLOY_SSH_ALIAS:-}" ]]; then
    SSH=(ssh -o ConnectTimeout=10 "$DEPLOY_SSH_ALIAS"); SCP=(scp -o ConnectTimeout=10)
    TARGET="$DEPLOY_SSH_ALIAS"
  else
    : "${DEPLOY_HOST:?请在 .deploy.local 填 DEPLOY_HOST 或 DEPLOY_SSH_ALIAS}"
    : "${DEPLOY_USER:?请在 .deploy.local 填 DEPLOY_USER}"
    SSH=(ssh -p "$DEPLOY_PORT" -o ConnectTimeout=10); SCP=(scp -P "$DEPLOY_PORT" -o ConnectTimeout=10)
    if [[ -n "${DEPLOY_KEY:-}" ]]; then
      SSH+=(-i "${DEPLOY_KEY/#\~/$HOME}"); SCP+=(-i "${DEPLOY_KEY/#\~/$HOME}")
    fi
    SSH+=("${DEPLOY_USER}@${DEPLOY_HOST}"); TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
  fi
  REMOTE_DIR="$DEPLOY_PATH/h5-ota"
fi

# ---------- 1. 版本号 ----------
if [[ -n "$SET_VERSION" ]]; then
  VERSION="$SET_VERSION"
else
  # 日期 + 当天序号：一天发多次也不会撞号，而且一眼看得出是哪天的包
  BASE="$(date +%Y.%m.%d)"
  N=1
  while [[ -f "$OUT/h5-${BASE}-${N}.zip" ]]; do N=$((N+1)); done
  VERSION="${BASE}-${N}"
fi

# min_shell 默认沿用服务器上现有清单的值，避免手滑把门槛降回去
if [[ -z "$MIN_SHELL" ]]; then
  MIN_SHELL="$(curl -fsS --max-time 10 "http://${DEPLOY_HOST:-8.141.123.141}/h5-ota/version.json" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("min_shell","2.0.0"))' 2>/dev/null || true)"
  [[ -n "$MIN_SHELL" ]] || MIN_SHELL="2.0.0"
fi

say "版本 $VERSION（要求 APK ≥ $MIN_SHELL）"

# ---------- 2. 构建 APP 版 H5 ----------
say "构建 APP 版 H5（相对 base + 绝对 API 地址）"
run "cd '$PROJECT_DIR/frontend' && npm run build:h5:app"

DIST="$PROJECT_DIR/frontend/dist-h5-app"
[[ $DRY == 1 ]] || [[ -f "$DIST/h5.html" ]] || { echo "❌ 构建产物里没有 h5.html"; exit 1; }

# ---------- 3. 打包 ----------
say "打 zip"
mkdir -p "$OUT"
STAGE="$OUT/stage-$VERSION"
ZIP="$OUT/h5-${VERSION}.zip"
if [[ $DRY == 0 ]]; then
  rm -rf "$STAGE" "$ZIP"; mkdir -p "$STAGE"
  cp -R "$DIST"/. "$STAGE"/
  # Capacitor 的本地服务器认 index.html，构建产物叫 h5.html
  mv "$STAGE/h5.html" "$STAGE/index.html"
  ( cd "$STAGE" && zip -qr "$ZIP" . -x '.*' )
  [[ -f "$ZIP" ]] || { echo "❌ zip 没生成"; exit 1; }
  # 开包自检：包里必须有入口，别把一个没有 index.html 的包发出去。
  # ⚠️ 不要写成 `unzip -l ... | grep -q`：grep -q 命中即退出，unzip 收到 SIGPIPE，
  #    在 set -o pipefail 下整条管道判失败 —— **包是好的，检查却把它拦下来**。
  #    （跟当初用 grep META-INF/*.RSA 误判 APK 没签名是同一类错。）先落文件再查。
  unzip -l "$ZIP" > "$OUT/.list.txt"
  grep -q " index.html$" "$OUT/.list.txt" || { echo "❌ 包里没有 index.html"; exit 1; }
  rm -f "$OUT/.list.txt"
  echo "  $ZIP（$(du -h "$ZIP" | cut -f1)）"
fi

# ---------- 4. 签名 ----------
# 签的是 sha256 的十六进制文本本身，APP 那边用同样的字节验（PmsUpdater.verifySignature）
say "签名"
if [[ $DRY == 0 ]]; then
  SHA="$(sha256of "$ZIP")"
  SIG="$(printf '%s' "$SHA" | openssl dgst -sha256 -sign "$KEY" | base64 | tr -d '\n')"
  echo "  sha256=$SHA"
  # 立刻用公钥验一遍自己签的东西 —— 私钥换过、公钥没同步进 APK 的话，
  # 这里就该失败，而不是等包发出去手机静默拒绝更新
  PUB="$PROJECT_DIR/mobile/android/app/src/main/assets/ota_public_key.pem"
  printf '%s' "$SHA" > "$OUT/.sha.txt"
  printf '%s' "$SIG" | base64 -d > "$OUT/.sig.bin"
  if openssl dgst -sha256 -verify "$PUB" -signature "$OUT/.sig.bin" "$OUT/.sha.txt" >/dev/null 2>&1; then
    echo "  ok: 签名能被 APK 里的公钥验过"
  else
    echo "❌ 签名验不过 APK 里的公钥 —— 私钥和公钥不是一对，发出去手机会拒绝更新"; exit 1
  fi
  rm -f "$OUT/.sha.txt" "$OUT/.sig.bin"
fi

# ---------- 5. 写清单 ----------
if [[ $DRY == 0 ]]; then
  cat > "$OUT/version.json" <<EOF
{
  "version": "$VERSION",
  "url": "h5-${VERSION}.zip",
  "sha256": "$SHA",
  "sig": "$SIG",
  "min_shell": "$MIN_SHELL",
  "notes": $(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$NOTES")
}
EOF
  python3 -m json.tool "$OUT/version.json" >/dev/null || { echo "❌ 清单不是合法 JSON"; exit 1; }
  echo "  ok: $OUT/version.json"
fi

# ---------- 6. 上传：先包，后清单 ----------
if [[ $PACK_ONLY == 1 ]]; then
  say "只打包（--pack-only），不上传"
  [[ $DRY == 0 ]] && python3 -m json.tool "$OUT/version.json"
  echo
  echo "✅ 包与清单已产出在 $OUT/"
  exit 0
fi

say "上传 → ${TARGET}:${REMOTE_DIR}/"
if [[ $DRY == 1 ]]; then
  echo "[dry-run] ${SSH[*]} mkdir -p '$REMOTE_DIR'"
  echo "[dry-run] ${SCP[*]} $ZIP $TARGET:$REMOTE_DIR/"
  echo "[dry-run] ${SCP[*]} $OUT/version.json $TARGET:$REMOTE_DIR/   # ⚠️ 必须在 zip 之后"
  exit 0
fi

"${SSH[@]}" "mkdir -p '$REMOTE_DIR'"
"${SCP[@]}" "$ZIP" "$TARGET:$REMOTE_DIR/"

# ⚠️ 传完先把包**从公网下回来**核对，再写清单。
#    少了这一步，清单可能指向一个传了一半的文件 —— 而那时所有手机都会去下它。
REMOTE_URL="http://${DEPLOY_HOST:-8.141.123.141}/h5-ota/h5-${VERSION}.zip"
TMPZ="$OUT/.remote-check.zip"
curl -fsS --max-time 180 -o "$TMPZ" "$REMOTE_URL"
RSHA="$(sha256of "$TMPZ")"; rm -f "$TMPZ"
[[ "$RSHA" == "$SHA" ]] || { echo "❌ 服务器上的包与本地对不上（$RSHA）—— 清单不写，先查上传"; exit 1; }
echo "  ok: 公网上取回的包校验一致"

"${SCP[@]}" "$OUT/version.json" "$TARGET:$REMOTE_DIR/"
echo "  ok: 清单已更新"

# ---------- 7. 复检 ----------
say "复检线上清单"
curl -fsS --max-time 10 "http://${DEPLOY_HOST:-8.141.123.141}/h5-ota/version.json" | python3 -m json.tool
echo
echo "✅ 发布完成。手机端下次打开 APP 时后台拉取，再下一次打开生效。"
echo "   （不是立刻换 —— 新包要先试用一次确认起得来，见 PmsUpdater 的试用/回滚）"
