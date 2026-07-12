#!/bin/bash
# ============================================================
# 本地一键发版 —— 从你的电脑通过 SSH 触发服务器安全升级
#
#   本机跑一条命令 → SSH 进服务器 → 执行 ops/upgrade.sh
#   服务器端 upgrade.sh 会：备份 → git pull → 重建 → 健康检查 → 失败自动回滚
#   （所以发版失败线上仍是旧版，安全）
#
# 用法:
#   bash ops/release.sh              部署 GitHub 上最新的 main 到服务器
#   bash ops/release.sh --push       先把本地提交推到 main，再部署
#   bash ops/release.sh --logs       部署成功后跟随后端日志
#   bash ops/release.sh --no-rebuild 只重启不 rebuild（仅改了 .env 之类时）
#   bash ops/release.sh --health     只在服务器跑一次健康检查，不部署
#   bash ops/release.sh --dry-run    只打印将执行的 SSH 命令，不真跑
#
# 首次使用:
#   cp .deploy.local.example .deploy.local   # 再填服务器信息（该文件已 gitignore，绝不入库）
#   确保已能免密 SSH 登录服务器（ssh 密钥）：ssh-copy-id user@host
# ============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

CONF=".deploy.local"
DO_PUSH=0; DO_LOGS=0; DRY=0; HEALTH_ONLY=0; UPGRADE_ARGS=""
for a in "$@"; do
  case "$a" in
    --push)       DO_PUSH=1 ;;
    --logs)       DO_LOGS=1 ;;
    --dry-run)    DRY=1 ;;
    --health)     HEALTH_ONLY=1 ;;
    --no-rebuild) UPGRADE_ARGS="--no-rebuild" ;;
    -h|--help)    grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "未知参数: ${a}（用 --help 看用法）"; exit 1 ;;
  esac
done

# ---- 读配置（gitignore 的 .deploy.local）----
if [[ ! -f "$CONF" ]]; then
  echo "✗ 没找到 $CONF"
  echo "  首次使用请执行： cp .deploy.local.example .deploy.local  然后填服务器信息"
  exit 1
fi
set -a; source "$CONF"; set +a
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
: "${DEPLOY_PATH:?请在 $CONF 填 DEPLOY_PATH（服务器上项目目录）}"

# ---- 组装 SSH 命令：优先用 ~/.ssh/config 里的 Host 别名 ----
if [[ -n "${DEPLOY_SSH_ALIAS:-}" ]]; then
  SSH=(ssh -o ConnectTimeout=10 "$DEPLOY_SSH_ALIAS")
  TARGET="$DEPLOY_SSH_ALIAS"
else
  : "${DEPLOY_HOST:?请在 $CONF 填 DEPLOY_HOST 或 DEPLOY_SSH_ALIAS}"
  : "${DEPLOY_USER:?请在 $CONF 填 DEPLOY_USER}"
  SSH=(ssh -p "$DEPLOY_PORT" -o ConnectTimeout=10)
  [[ -n "${DEPLOY_KEY:-}" ]] && SSH+=(-i "${DEPLOY_KEY/#\~/$HOME}")
  SSH+=("${DEPLOY_USER}@${DEPLOY_HOST}")
  TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
fi

if [[ "$HEALTH_ONLY" == "1" ]]; then
  REMOTE="cd '$DEPLOY_PATH' && bash ops/health-check.sh"
else
  REMOTE="cd '$DEPLOY_PATH' && bash ops/upgrade.sh $UPGRADE_ARGS"
fi

echo "── 发版目标：${TARGET} : ${DEPLOY_PATH} （分支 ${DEPLOY_BRANCH}）──"

# ---- 可选：先把本地提交推到 GitHub（服务器只部署已推送的代码）----
if [[ "$HEALTH_ONLY" == "0" ]]; then
  if [[ "$DO_PUSH" == "1" ]]; then
    echo "[本地] git push → origin/$DEPLOY_BRANCH ..."
    git push origin "HEAD:$DEPLOY_BRANCH"
  elif git rev-parse "origin/$DEPLOY_BRANCH" >/dev/null 2>&1; then
    AHEAD=$(git rev-list --count "origin/$DEPLOY_BRANCH..HEAD" 2>/dev/null || echo 0)
    if [[ "$AHEAD" != "0" ]]; then
      echo "⚠ 本地比 origin/$DEPLOY_BRANCH 领先 $AHEAD 个提交——服务器只会部署已推送到 GitHub 的代码。"
      echo "  想连本地提交一起发： bash ops/release.sh --push"
    fi
  fi
fi

if [[ "$DRY" == "1" ]]; then
  echo "[dry-run] ${SSH[*]} \"$REMOTE\""
  exit 0
fi

# ---- SSH 执行升级 ----
echo "[SSH] 连接服务器执行升级（备份→拉码→重建→健康检查→失败自动回滚）..."
echo ""
if "${SSH[@]}" "$REMOTE"; then
  echo ""
  echo "✓ 发版成功。"
  if [[ "$DO_LOGS" == "1" ]]; then
    echo "── 跟随后端日志（Ctrl+C 退出）──"
    "${SSH[@]}" "cd '$DEPLOY_PATH' && docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f --tail=60 backend"
  fi
else
  code=$?
  echo ""
  echo "✗ 发版失败（exit ${code}）"
  if [[ "$code" == "1" ]]; then
    echo "  服务器 upgrade.sh 已自动回滚到升级前版本，线上仍是旧版、可正常访问。"
    echo "  排查： ${SSH[*]} \"cd '$DEPLOY_PATH' && bash ops/logs.sh\""
  elif [[ "$code" == "2" ]]; then
    echo "  ⚠⚠ 升级失败且自动回滚也失败！请立即人工登录处理："
    echo "     ${SSH[*]}"
  fi
  exit "$code"
fi
