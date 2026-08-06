#!/bin/bash
# ============================================================
# 并行会话用 worktree 隔离 —— 建分支干活 → 合回 main → 发版
#
# 为什么要有：2026-08-06 两个 Claude 会话同时在**同一个工作区**改代码，
# 结果互相把对方改到一半的文件卷进自己的 commit（f4c98bb 和 682b24c 各卷了一次）。
# 后果不只是 commit 记乱了：`git status` 里永远混着别人的文件，
# 于是不敢再 `git add -A`，只能一个个手打路径挑自己的——
# 一次手打 10 个路径，其中 AfterSalesView.vue 打成了小写 s，
# macOS 文件系统不区分大小写，**不报错，只是静默漏掉**，
# 209 行改动就这么没进版本库、没上线，本地却一路绿灯。
#
# worktree 各自一个目录 + 各自一个分支，从根上不会互相卷。
# 真撞同一个文件时会在合并那一步显式冲突——看得见，比静默丢文件好得多。
#
# 用法:
#   bash ops/worktree.sh new  agent      建 ../pms-wt-agent（分支 claude/agent）
#   bash ops/worktree.sh list            看都有哪些，各自领先/落后 main 多少
#   bash ops/worktree.sh merge agent     把 claude/agent 合回 main 并推送
#   bash ops/worktree.sh rm    agent     收工，删目录（分支保留在远端）
#
# ⚠️ 发版只在**主检出**（本目录）做，合并完再 `bash ops/release.sh`。
#    worktree 里不要跑 release.sh——服务器拉的是 origin/main，
#    在分支上发版等于发了个跟线上无关的东西。
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

BOLD='\033[1m'; OK='\033[32m'; BAD='\033[31m'; WARN='\033[33m'; DIM='\033[2m'; OFF='\033[0m'

# 依赖和本地配置：软链而不是各装一份。
#   node_modules ×2 + venv 一共 ~700MB，每个 worktree 装一遍又慢又占地。
#   .env.local / .deploy.local 是 gitignored 的密钥与部署配置，
#   软链过去比复制安全——只有一份，改一处生效，也不会被误提交。
LINKS=(frontend/node_modules desktop/node_modules backend/.venv
       .env.local .deploy.local .deploy.local.ota.pem)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[[ $# -ge 1 ]] || usage
CMD="$1"; NAME="${2:-}"

wt_dir() { echo "$(dirname "$ROOT")/pms-wt-$1"; }
wt_branch() { echo "claude/$1"; }

case "$CMD" in

new)
  [[ -n "$NAME" ]] || { echo "用法: bash ops/worktree.sh new <名字>"; exit 1; }
  DIR="$(wt_dir "$NAME")"; BR="$(wt_branch "$NAME")"
  [[ -e "$DIR" ]] && { echo -e "${BAD}$DIR 已存在${OFF}"; exit 1; }

  echo -e "${BOLD}── 从最新的 origin/main 开分支 ──${OFF}"
  # 基于远端 main 而不是本地 main：本地可能落后，或者带着还没推的东西
  git fetch origin main --quiet 2>/dev/null || echo -e "  ${WARN}fetch 失败，用本地 main${OFF}"
  BASE=$(git rev-parse --verify --quiet origin/main || git rev-parse main)
  if git show-ref --verify --quiet "refs/heads/$BR"; then
    git worktree add "$DIR" "$BR" || exit 1
    echo -e "  分支 $BR 已存在，直接挂上"
  else
    git worktree add -b "$BR" "$DIR" "$BASE" || exit 1
  fi

  echo -e "${BOLD}── 软链依赖与本地配置（不各装一份）──${OFF}"
  for rel in "${LINKS[@]}"; do
    src="$ROOT/$rel"
    [[ -e "$src" ]] || { echo -e "  ${DIM}跳过 $rel（主检出里也没有）${OFF}"; continue; }
    mkdir -p "$(dirname "$DIR/$rel")"
    ln -s "$src" "$DIR/$rel" && echo "  → $rel"
  done

  # ⚠️ .gitignore 里是 `node_modules/`、`.venv/`——**带斜杠只匹配目录**，
  #    而软链在 git 眼里是文件不是目录，匹配不上，会以未跟踪文件的身份
  #    污染 git status（合并前的"有没有没提交的东西"检查会被它们刷屏）。
  #    写进 $GIT_COMMON_DIR/info/exclude：只在本机生效、不进仓库、对主检出无影响。
  EXCL="$(git rev-parse --git-common-dir)/info/exclude"
  mkdir -p "$(dirname "$EXCL")"
  for rel in "${LINKS[@]}"; do
    grep -qxF "$rel" "$EXCL" 2>/dev/null || echo "$rel" >> "$EXCL"
  done
  echo -e "  ${DIM}软链已加进本地 exclude（不会冒充未跟踪文件）${OFF}"

  echo -e "${BOLD}── 依赖是否还对得上 ──${OFF}"
  # 软链共用一份 node_modules，前提是依赖清单没变。变了就必须真装一次，
  # 否则会用着旧依赖跑新代码，出的问题极难查。
  for f in frontend/package-lock.json desktop/package-lock.json backend/requirements.txt; do
    [[ -f "$ROOT/$f" ]] || continue
    if ! git -C "$ROOT" diff --quiet "$BASE" -- "$f" 2>/dev/null; then
      echo -e "  ${WARN}$f 与主检出不同 —— 在 worktree 里真装一次再干活${OFF}"
    fi
  done
  echo -e "  ${OK}依赖清单一致，软链可用${OFF}"

  echo
  echo -e "${OK}✓ 好了${OFF}  ${BOLD}cd \"$DIR\"${OFF}"
  echo -e "  ${DIM}干完回主检出跑： bash ops/worktree.sh merge $NAME${OFF}"
  ;;

list)
  echo -e "${BOLD}── worktree ──${OFF}"
  git fetch origin main --quiet 2>/dev/null
  # ⚠️ 不能 `read dir sha br` 拆 `git worktree list` 的输出——
  #    仓库路径里有空格（.../excel share/...），按空白拆会把一个目录劈成两半。
  #    用 --porcelain：一行一个字段，路径原样，不受空格影响。
  git worktree list --porcelain | awk '
    /^worktree /{ d=substr($0,10) }
    /^HEAD /    { h=substr($0,6,7) }
    /^branch /  { b=substr($0,8); sub("refs/heads/","",b); print d "\t" h "\t" b; d=h=b="" }
    /^detached/ { print d "\t" h "\t(detached)"; d=h="" }
  ' | while IFS=$'\t' read -r dir sha br; do
    name=$(basename "$dir")
    if [[ "$dir" == "$ROOT" ]]; then
      printf "  %-24s %s  %-26s %s\n" "$name" "$sha" "$br" "← 主检出，发版在这儿做"
      continue
    fi
    cnt=$(git rev-list --left-right --count "origin/main...$br" 2>/dev/null || echo "0 0")
    behind=$(echo "$cnt" | awk '{print $1}'); ahead=$(echo "$cnt" | awk '{print $2}')
    tracked=$(git -C "$dir" status --porcelain 2>/dev/null | grep -vc '^??' || true)
    note=""
    [[ "${tracked:-0}" != "0" ]] && note="${BAD}有 $tracked 个改了没提交${OFF}"
    [[ "$ahead" != "0" ]] && note="$note ${WARN}$ahead 个提交待合并${OFF}"
    printf "  %-24s %s  %-26s 领先 %-3s 落后 %-3s %b\n" "$name" "$sha" "$br" "$ahead" "$behind" "$note"
  done
  ;;

merge)
  [[ -n "$NAME" ]] || { echo "用法: bash ops/worktree.sh merge <名字>"; exit 1; }
  BR="$(wt_branch "$NAME")"; DIR="$(wt_dir "$NAME")"
  [[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || {
    echo -e "${BAD}合并要在主检出的 main 上做，当前在 $(git rev-parse --abbrev-ref HEAD)${OFF}"; exit 1; }

  # 先拦住「worktree 里还有没提交的东西」——今天栽的就是这个：
  # 工作区是对的、测试全过，但东西没进版本库，合并自然也带不过来。
  # 已跟踪文件被改了却没提交 → **拦住**。今天漏的 AfterSalesView.vue 正是这种（M，不是 ??）：
  # 工作区是对的、测试全过，但东西没进版本库，合并自然带不过来。
  # 未跟踪文件（??）只提醒不拦：那多半是分析稿、临时产物，拦了徒增摩擦。
  check_dirty() {  # $1=目录 $2=名字
    local d="$1" tag="$2" tracked untracked
    tracked=$(git -C "$d" status --porcelain 2>/dev/null | grep -v '^??')
    untracked=$(git -C "$d" status --porcelain 2>/dev/null | grep '^??' | wc -l | tr -d ' ')
    if [[ -n "$tracked" ]]; then
      echo -e "${BAD}✗ $tag 有改了没提交的文件，先提交，否则合过来是缺的：${OFF}"
      echo "$tracked" | sed 's/^/    /'
      return 1
    fi
    [[ "$untracked" != "0" ]] && echo -e "  ${DIM}$tag 有 $untracked 个未跟踪文件（不拦，但确认下没有该提交的）${OFF}"
    return 0
  }
  [[ -d "$DIR" ]] && { check_dirty "$DIR" "$NAME" || exit 1; }
  check_dirty "$ROOT" "主检出" || exit 1

  echo -e "${BOLD}── 拉最新 main ──${OFF}"
  set -a; [[ -f .env.local ]] && source .env.local; set +a
  REMOTE="https://x-access-token:${GITHUB_PAT:-}@github.com/zhangang12/excel-share-system.git"
  GIT_TERMINAL_PROMPT=0 git fetch "$REMOTE" main:refs/remotes/origin/main --force --quiet \
    2>&1 | sed -E 's#x-access-token:[^@]*@#x-access-token:***@#g'
  git merge --ff-only origin/main --quiet 2>/dev/null || {
    echo -e "${WARN}本地 main 与远端分叉，先自己 rebase/merge${OFF}"; exit 1; }

  echo -e "${BOLD}── 合并 $BR ──${OFF}"
  # --no-ff：保留"这是一个分支的工作"这个信息，出事好整段回退
  if ! git merge --no-ff "$BR" -m "合并 $BR"; then
    echo -e "${BAD}✗ 有冲突，解完再 git commit，然后重跑本命令${OFF}"
    git diff --name-only --diff-filter=U | sed 's/^/    /'
    exit 1
  fi
  echo -e "  ${OK}合上了${OFF}"

  echo -e "${BOLD}── 推送 ──${OFF}"
  GIT_TERMINAL_PROMPT=0 git push "$REMOTE" "HEAD:main" "HEAD:refs/heads/$BR" 2>&1 \
    | sed -E 's#x-access-token:[^@]*@#x-access-token:***@#g'

  echo
  echo -e "${OK}✓ 已合进 main 并推送${OFF}"
  echo -e "  ${BOLD}接着发版：${OFF}"
  echo -e "    bash ops/release.sh                          网页版"
  echo -e "    bash desktop/ship.sh                         ${DIM}动了前端就必须发客户端——${OFF}"
  echo -e "                                                 ${DIM}客户端加载的是打进安装包的前端${OFF}"
  echo -e "    bash ops/verify-shipped.sh <功能关键词>       ${DIM}查线上实物，别只信「发版成功」${OFF}"
  ;;

rm)
  [[ -n "$NAME" ]] || { echo "用法: bash ops/worktree.sh rm <名字>"; exit 1; }
  DIR="$(wt_dir "$NAME")"; BR="$(wt_branch "$NAME")"
  DIRTY=$(git -C "$DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  [[ "$DIRTY" != "0" ]] && { echo -e "${BAD}✗ 里面还有 $DIRTY 个未提交的文件，不删${OFF}"; exit 1; }
  UNMERGED=$(git rev-list --count "origin/main..$BR" 2>/dev/null || echo 0)
  [[ "$UNMERGED" != "0" ]] && echo -e "${WARN}提醒：$BR 还有 $UNMERGED 个提交没合进 main（分支保留，目录删掉）${OFF}"
  git worktree remove "$DIR" --force && echo -e "${OK}✓ 已删 $DIR${OFF}（分支 $BR 保留）"
  ;;

*) usage ;;
esac
