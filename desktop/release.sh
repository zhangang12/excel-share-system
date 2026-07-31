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
#   bash desktop/release.sh --upload-only <目录>   🆕 只上传，不打包（配合 Windows 原生打包）
#
# 🆕 推荐路径（2026-08-01 起）：安装包改在 GitHub Actions 的 windows-latest 上打——
#   本机（macOS）交叉编译出来的卸载程序会在自动更新时崩，用户每次更新都看到
#   「old-uninstaller.exe 遇到问题已经停止工作」。流程：
#     1) GitHub → Actions → 「桌面客户端打包（Windows 原生）」→ Run workflow
#     2) 下载 artifact 并解压
#     3) bash desktop/release.sh --upload-only ~/Downloads/desktop-1.0.22
#   本脚本不带 --upload-only 时仍是原来的本机打包路径（可用，但会带回那个弹框）。
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
SET_VERSION=""; MIN_VERSION=""; DRY=0; UPLOAD_ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --set-version) SET_VERSION="$2"; shift 2 ;;
    --min-version) MIN_VERSION="$2"; shift 2 ;;
    --upload-only) UPLOAD_ONLY="$2"; shift 2 ;;
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

# ---- 🆕 --upload-only：只上传，不打包 ----
# 配合 .github/workflows/desktop-build.yml：安装包改在 windows-latest 上原生打（macOS 交叉
# 编译出来的卸载程序会在自动更新时崩，弹「old-uninstaller.exe 已停止工作」）。
# 从 Actions 下载 artifact 解压后：bash desktop/release.sh --upload-only <解压目录>
# 上传仍走本机 .deploy.local 的密钥——本仓库是公开仓库，生产私钥不进 Actions Secrets。
if [[ -n "$UPLOAD_ONLY" ]]; then
  [[ -d "$UPLOAD_ONLY" ]] || { echo "✗ 目录不存在：$UPLOAD_ONLY"; exit 1; }
  EXE_PATH="$(ls "$UPLOAD_ONLY"/*.exe 2>/dev/null | head -1)"
  [[ -n "$EXE_PATH" ]] || { echo "✗ $UPLOAD_ONLY 下没找到 .exe"; exit 1; }
  # 版本号从安装包文件名反解（「同辉项目管理 Setup 1.0.22.exe」），不依赖本地 package.json——
  # 本地版本可能已经被后续改动 bump 过，跟这个包对不上
  VERSION="$(basename "$EXE_PATH" .exe | sed 's/.* //')"
  for f in "$EXE_PATH" "$EXE_PATH.blockmap" "$UPLOAD_ONLY/latest.yml" "$UPLOAD_ONLY/version.json"; do
    [[ -f "$f" ]] || { echo "✗ 缺文件：$f（artifact 解压是否完整？）"; exit 1; }
  done
  echo "── 上传 ${VERSION}（Windows 原生打包产物）→ ${TARGET}:${REMOTE_DIR}/ ──"
  "${SSH[@]}" "mkdir -p '$REMOTE_DIR'"
  "${SCP[@]}" "$EXE_PATH" "$EXE_PATH.blockmap" "$UPLOAD_ONLY/latest.yml" "$UPLOAD_ONLY/version.json" \
              "$TARGET:$REMOTE_DIR/"
  echo ""
  echo "✓ 已发布 ${VERSION}，客户端下一轮检查将收到更新。"
  exit 0
fi

# ---- dry-run：只打印将执行的命令 ----
if [[ "$DRY" == "1" ]]; then
  echo "[dry-run] (cd desktop && npm version ${SET_VERSION:-patch} --no-git-tag-version)"
  [[ -n "$MIN_VERSION" ]] && echo "[dry-run] 更新 desktop/version.json 的 min_version → $MIN_VERSION"
  echo "[dry-run] VITE_API_BASE=http://8.141.123.141 npm run build --prefix frontend"
  echo "[dry-run] rm -rf desktop/app && cp -R frontend/dist desktop/app"
  echo "[dry-run] (cd desktop && npx electron-builder --win nsis --x64 --publish never)"
  echo "[dry-run] ${SSH[*]} \"mkdir -p '$REMOTE_DIR'\""
  echo "[dry-run] ${SCP[*]} \"desktop/dist/同辉项目管理 Setup <VER>.exe{,.blockmap}\" desktop/dist/latest.yml desktop/version.json '$TARGET:$REMOTE_DIR/'  # 只传当版，不整目录 glob"
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
# 只传当版本的文件（dist/ 里累积着历史包，整目录 glob 会把几百 MB 旧包重传一遍，
# 传输窗口长还容易像 1.0.4 那次一样中途断线留下截断包）
echo "[4/4] 上传到服务器 ${TARGET}:${REMOTE_DIR}/ ..."
EXE="同辉项目管理 Setup ${VERSION}.exe"
"${SSH[@]}" "mkdir -p '$REMOTE_DIR'"
"${SCP[@]}" "$DESKTOP_DIR/dist/$EXE" "$DESKTOP_DIR/dist/$EXE.blockmap" "$DESKTOP_DIR/dist/latest.yml" "$DESKTOP_DIR/version.json" "$TARGET:$REMOTE_DIR/"

echo ""
echo "✓ 已发布 ${VERSION}，客户端下一轮检查将收到更新。"
