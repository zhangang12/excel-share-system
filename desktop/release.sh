#!/bin/bash
# ============================================================
# 桌面客户端一键打包 + 上传 —— 本地跑一条命令完成发布
#
#   流程：版本号 bump（desktop/package.json）
#        → VITE_API_BASE=http://8.141.123.141 打包前端（frontend/dist）
#        → 拷贝 frontend/dist → desktop/app/（打进安装包的内置页面）
#        → electron-builder 打 Windows NSIS 安装包（desktop/dist/）
#        → SSH 建服务器目录 $DEPLOY_PATH/desktop-releases/
#        → scp 上传 *.exe / latest.yml / *.blockmap / version.json
#        → nginx 已把 /desktop/ 映射到该目录，客户端下一轮检查即收到更新
#
# 用法:
#   bash desktop/release.sh                        版本号 +patch（1.0.0→1.0.1）后打包上传
#   bash desktop/release.sh --set-version 1.2.0    指定版本号
#   bash desktop/release.sh --min-version 1.1.0    改 version.json 最低版本（强制旧客户端更新）后再传
#   bash desktop/release.sh --dry-run              只打印将执行的命令，不打包不上传
#
# 首次使用：同 ops/release.sh，读仓库根 .deploy.local（gitignored）。
#
# 🆕 图标（待用户提供 logo）：把 1024x1024 PNG 放到 desktop/build/icon.png 即可，
#    electron-builder 会自动转成 ico 打进安装包；缺省时用 Electron 默认图标。
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DESKTOP_DIR="$SCRIPT_DIR"
cd "$PROJECT_DIR"

# ---- 参数 ----
SET_VERSION=""; MIN_VERSION=""; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --set-version) SET_VERSION="$2"; shift 2 ;;
    --min-version) MIN_VERSION="$2"; shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    -h|--help)     grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）"; exit 1 ;;
  esac
done

# ---- 读配置（gitignore 的 .deploy.local），与 ops/release.sh 保持一致 ----
CONF=".deploy.local"
if [[ ! -f "$CONF" ]]; then
  echo "✗ 没找到 $CONF"
  echo "  首次使用请执行： cp .deploy.local.example .deploy.local  然后填服务器信息"
  exit 1
fi
set -a; source "$CONF"; set +a
DEPLOY_PORT="${DEPLOY_PORT:-22}"
: "${DEPLOY_PATH:?请在 $CONF 填 DEPLOY_PATH（服务器上项目目录）}"

# ---- 组装 SSH/SCP 命令：优先用 ~/.ssh/config 里的 Host 别名（同 ops/release.sh）----
if [[ -n "${DEPLOY_SSH_ALIAS:-}" ]]; then
  SSH=(ssh -o ConnectTimeout=10 "$DEPLOY_SSH_ALIAS")
  SCP=(scp -o ConnectTimeout=10)
  TARGET="$DEPLOY_SSH_ALIAS"
else
  : "${DEPLOY_HOST:?请在 $CONF 填 DEPLOY_HOST 或 DEPLOY_SSH_ALIAS}"
  : "${DEPLOY_USER:?请在 $CONF 填 DEPLOY_USER}"
  SSH=(ssh -p "$DEPLOY_PORT" -o ConnectTimeout=10)
  SCP=(scp -P "$DEPLOY_PORT" -o ConnectTimeout=10)
  if [[ -n "${DEPLOY_KEY:-}" ]]; then
    SSH+=(-i "${DEPLOY_KEY/#\~/$HOME}")
    SCP+=(-i "${DEPLOY_KEY/#\~/$HOME}")
  fi
  SSH+=("${DEPLOY_USER}@${DEPLOY_HOST}")
  TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
fi

REMOTE_DIR="$DEPLOY_PATH/desktop-releases"

# ---- dry-run：只打印将执行的命令 ----
if [[ "$DRY" == "1" ]]; then
  echo "[dry-run] (cd desktop && npm version ${SET_VERSION:-patch} --no-git-tag-version)"
  [[ -n "$MIN_VERSION" ]] && echo "[dry-run] 更新 desktop/version.json 的 min_version → $MIN_VERSION"
  echo "[dry-run] VITE_API_BASE=http://8.141.123.141 npm run build --prefix frontend"
  echo "[dry-run] rm -rf desktop/app && cp -R frontend/dist desktop/app"
  echo "[dry-run] (cd desktop && npx electron-builder --win nsis --x64 --publish never)"
  echo "[dry-run] ${SSH[*]} \"mkdir -p '$REMOTE_DIR'\""
  echo "[dry-run] ${SCP[*]} desktop/dist/*.exe desktop/dist/latest.yml desktop/dist/*.blockmap desktop/version.json '$TARGET:$REMOTE_DIR/'"
  exit 0
fi

# ---- 1. 版本号 ----
cd "$DESKTOP_DIR"
if [[ -n "$SET_VERSION" ]]; then
  # 与当前版本相同则跳过（npm version 同号会报 Version not changed 直接退出）
  [[ "$SET_VERSION" != "$(node -p "require('./package.json').version")" ]] \
    && npm version "$SET_VERSION" --no-git-tag-version
else
  npm version patch --no-git-tag-version
fi
VERSION="$(node -p "require('./package.json').version")"
echo "── 本次发布版本：$VERSION ──"

# ---- 2. 可选：改强制最低版本 ----
if [[ -n "$MIN_VERSION" ]]; then
  node -e "
    const fs = require('fs');
    const p = '$DESKTOP_DIR/version.json';
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    j.min_version = '$MIN_VERSION';
    fs.writeFileSync(p, JSON.stringify(j, null, 2) + '\n');
  "
  echo "✓ version.json 最低版本已改为 ${MIN_VERSION}（低于它的客户端将被强制更新）"
fi

# ---- 3. 打包前端（桌面端走绝对地址 API，与前端约定 VITE_API_BASE）----
echo "[1/4] 打包前端（VITE_API_BASE=http://8.141.123.141）..."
VITE_API_BASE=http://8.141.123.141 npm run build --prefix "$PROJECT_DIR/frontend"

# ---- 4. 拷贝前端产物到 desktop/app/（打进安装包的内置页面）----
echo "[2/4] 拷贝 frontend/dist → desktop/app/ ..."
rm -rf "$DESKTOP_DIR/app"
cp -R "$PROJECT_DIR/frontend/dist" "$DESKTOP_DIR/app"

# ---- 5. electron-builder 打 Windows NSIS 安装包 ----
# 注意：--x64 必须显式给——在 Apple Silicon 上 electron-builder 默认打 arm64 包（Windows ARM，跑不了普通 PC）；
# 两个镜像变量是防 GitHub 直连超时（Electron 运行时/打包工具二进制下载），网络好时可去掉。
echo "[3/4] electron-builder 打包 Windows 安装包（x64）..."
if [[ ! -f "$DESKTOP_DIR/build/icon.png" ]]; then
  echo "  ⚠ 未找到 desktop/build/icon.png，先用 Electron 默认图标（待用户提供 logo 后放入即可）"
fi
cd "$DESKTOP_DIR"
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
npx electron-builder --win nsis --x64 --publish never

# ---- 6. 上传到服务器（nginx /desktop/ 指向 $DEPLOY_PATH/desktop-releases/）----
echo "[4/4] 上传到服务器 ${TARGET}:${REMOTE_DIR}/ ..."
"${SSH[@]}" "mkdir -p '$REMOTE_DIR'"
"${SCP[@]}" "$DESKTOP_DIR"/dist/*.exe "$DESKTOP_DIR"/dist/latest.yml "$DESKTOP_DIR"/dist/*.blockmap "$DESKTOP_DIR/version.json" "$TARGET:$REMOTE_DIR/"

echo ""
echo "✓ 已发布 ${VERSION}，客户端下一轮检查将收到更新。"
