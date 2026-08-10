#!/bin/bash
# ============================================================
# 「发版了吗」一条命令回答 —— 查**线上实物**，不看发布脚本的输出。
#
# 为什么要有：2026-08-06 出过一次事——git add 时把路径手打成
# AftersalesView.vue（小写 s），而仓库里实际是 AfterSalesView.vue（大写 S）。
# macOS 文件系统不区分大小写，于是本地一切正常：类型检查过、构建过、
# 浏览器里整条流程都测通了，`ops/release.sh` 也报「发版成功」——
# 但那 209 行改动**从没进过版本库**，线上自然没有。
#
# 教训：「发版成功」只证明部署流程跑完了，不证明你的代码在里面。
#       要证明，就得去线上把功能的特征字符串搜出来。
#
# 用法:
#   bash ops/verify-shipped.sh                     只对 commit 和客户端版本
#   bash ops/verify-shipped.sh 售后费用清单 指定审批人   再确认这些功能在不在线上包里
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

CONF=".deploy.local"
[[ -f "$CONF" ]] || { echo "缺 $CONF（照 .deploy.local.example 填）" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"
# 变量名跟 ops/release.sh 保持一致（DEPLOY_*），别自己另造一套
SSH="ssh -p ${DEPLOY_PORT:-22} -o ConnectTimeout=10 ${DEPLOY_USER}@${DEPLOY_HOST}"
REMOTE_DIR="${DEPLOY_PATH:-/opt/pms/excel-share-system-main}"

BOLD='\033[1m'; OK='\033[32m'; BAD='\033[31m'; DIM='\033[2m'; OFF='\033[0m'
fail=0

echo -e "${BOLD}── 代码 ──${OFF}"
LOCAL=$(git rev-parse --short HEAD)
DIRTY=$(git status --porcelain -- backend frontend desktop | head -5)
PROD=$($SSH "cd '$REMOTE_DIR' && git rev-parse --short HEAD" 2>/dev/null)
printf "  本地 %s   生产 %s  " "$LOCAL" "$PROD"
if [[ "$LOCAL" == "$PROD" ]]; then echo -e "${OK}一致${OFF}"; else echo -e "${BAD}不一致${OFF}"; fail=1; fi
if [[ -n "$DIRTY" ]]; then
  echo -e "  ${BAD}有未提交的改动——它们不可能在线上：${OFF}"
  echo "$DIRTY" | sed 's/^/    /'
  fail=1
fi

echo -e "${BOLD}── 客户端 ──${OFF}"
REPO_VER=$(grep -m1 '"version"' desktop/package.json | sed -E 's/.*"([0-9.]+)".*/\1/')
CH_VER=$($SSH "grep -m1 '^version:' '$REMOTE_DIR/desktop-releases/latest.yml' 2>/dev/null | awk '{print \$2}'" 2>/dev/null)
printf "  仓库 %s   更新通道 %s  " "$REPO_VER" "${CH_VER:-取不到}"
if [[ "$REPO_VER" == "$CH_VER" ]]; then echo -e "${OK}一致${OFF}"; else echo -e "${BAD}通道上不是最新的，要跑 desktop/ship.sh${OFF}"; fail=1; fi
# ⚠️ 客户端加载的是**打进安装包的前端**，不是服务器上的。
#    所以任何前端改动都要重新发客户端，光发网页版客户端用户看不到。
NEWER=$(git log --oneline "$(git log --oneline -1 --format=%H --grep="客户端 $REPO_VER" || echo HEAD)"..HEAD -- frontend/ 2>/dev/null | wc -l | tr -d ' ')
[[ "$NEWER" -gt 0 ]] && { echo -e "  ${BAD}客户端打包之后还有 $NEWER 个提交动了前端——客户端用户看不到这些改动${OFF}"; fail=1; }

if [[ $# -gt 0 ]]; then
  echo -e "${BOLD}── 功能是否真在线上包里 ──${OFF}"
  # ⚠️ **三个地方都要搜**（2026-08-10 踩到）：原来只搜网页版的 assets/，
  #    于是查 H5 的改动一律报「不在生产构建产物里」——连早就上线的 sumcard
  #    也查不到。差点当成发版失败去重发。三份产物各在各的位置：
  #      · 网页版  /usr/share/nginx/html/assets   （npm run build）
  #      · H5 助手 /usr/share/nginx/html/h5       （npm run build:h5，base=/h5/）
  #      · 后端    pms2_backend:/app              （Python 源码，没有构建产物）
  #    后端符号（函数名）永远不可能出现在前端产物里，反之亦然。
  for s in "$@"; do
    hit=""
    n=$($SSH "docker exec pms2_frontend sh -c \"grep -rl -- '$s' /usr/share/nginx/html/assets/ 2>/dev/null | wc -l\"" 2>/dev/null | tr -d ' \r')
    [[ "${n:-0}" -gt 0 ]] && hit="网页版"
    n=$($SSH "docker exec pms2_frontend sh -c \"grep -rl -- '$s' /usr/share/nginx/html/h5/ 2>/dev/null | wc -l\"" 2>/dev/null | tr -d ' \r')
    [[ "${n:-0}" -gt 0 ]] && hit="${hit:+$hit+}H5"
    n=$($SSH "docker exec pms2_backend sh -c \"grep -rl --include='*.py' -- '$s' /app 2>/dev/null | wc -l\"" 2>/dev/null | tr -d ' \r')
    [[ "${n:-0}" -gt 0 ]] && hit="${hit:+$hit+}后端"
    if [[ -n "$hit" ]]; then echo -e "  ${OK}✅${OFF} $s  ${DIM}($hit)${OFF}"
    else echo -e "  ${BAD}❌ $s —— 网页版/H5/后端 三处都没有${OFF}"; fail=1; fi
  done
  echo -e "  ${DIM}（这是查线上实物，比「发版成功」可靠）${OFF}"
fi

echo
[[ $fail -eq 0 ]] && echo -e "${OK}✓ 全部对得上${OFF}" || echo -e "${BAD}✗ 有对不上的，见上面${OFF}"
exit $fail
