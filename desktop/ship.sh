#!/bin/bash
# ============================================================
# 桌面客户端全自动发版 —— 一条命令从改完代码到用户能收到更新
#
#   bump 版本 → 提交 → 推 main → 触发 GitHub Actions（windows-latest 原生打包）
#   → 轮询等构建完 → 下载 artifact → 上传到更新通道 → 校验 → 按需开强制更新
#
# 为什么要有这个：原来这几步全靠手工——去 Actions 点 Run、等、下载 zip、解压、
# 再跑 desktop/release.sh --upload-only。中间任何一步忘了，通道上就是旧包，
# 而 force_latest 开着的时候「通道上没有新包」等于把所有人锁在门外。
#
# 用法:
#   bash desktop/ship.sh                          patch +1（1.0.31→1.0.32），发布但不改强制门槛
#   bash desktop/ship.sh --set-version 1.1.0      指定版本号
#   bash desktop/ship.sh --force-latest           发布后把 min_version 提到本次版本（旧客户端强制更新）
#   bash desktop/ship.sh --no-bump                不改版本号，用仓库里当前的版本重新发一次
#   bash desktop/ship.sh --dry-run                只打印将执行的步骤
#
# 前置：
#   .env.local 里有 GITHUB_PAT（已 gitignore，密钥永不入库）
#   能免密 ssh 到服务器（desktop/release.sh --upload-only 走的同一套）
#
# ⚠️ 顺序不能反：**必须先把包传上通道，再抬 min_version**。
#    反过来会出现「要求升到 X 但通道上没有 X」——所有客户端卡在强制更新页，谁都进不去。
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

REPO="zhangang12/excel-share-system"
WORKFLOW="desktop-build.yml"
BUMP="patch"
SET_VERSION=""
FORCE_LATEST=0
DRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --set-version) SET_VERSION="$2"; shift 2 ;;
    --no-bump)     BUMP=""; shift ;;
    --force-latest) FORCE_LATEST=1; shift ;;
    --dry-run)     DRY=1; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

say() { echo -e "\n\033[1;36m── $* ──\033[0m"; }
run() { if [[ $DRY == 1 ]]; then echo "[dry-run] $*"; else eval "$@"; fi; }

[[ -f .env.local ]] || { echo "❌ 缺 .env.local（里面要有 GITHUB_PAT）"; exit 1; }
set -a; . ./.env.local; set +a
[[ -n "${GITHUB_PAT:-}" ]] || { echo "❌ .env.local 里没有 GITHUB_PAT"; exit 1; }
GH=(-H "Authorization: Bearer $GITHUB_PAT" -H "Accept: application/vnd.github+json")

CUR=$(node -p "require('./desktop/package.json').version")

# ---------- 1. 版本号 ----------
if [[ -n "$SET_VERSION" ]]; then
  NEW="$SET_VERSION"
elif [[ -n "$BUMP" ]]; then
  NEW=$(node -p "const v='$CUR'.split('.').map(Number); v[2]++; v.join('.')")
else
  NEW="$CUR"
fi
say "版本 $CUR → $NEW"

if [[ "$NEW" != "$CUR" ]]; then
  # ⚠️ package.json 与 package-lock.json 的版本必须同时改且保持一致，否则 CI 的 npm ci 直接 EUSAGE。
  #    lock 里有两处（根 version 和 packages[""].version），一并替换。
  #    也别用 `open(p,'w').write(open(p).read())` 那种一行写法——先截断再读，文件会被清零（踩过）。
  run "node -e \"
    const fs=require('fs');
    for (const p of ['desktop/package.json','desktop/package-lock.json']) {
      const s=fs.readFileSync(p,'utf8');
      fs.writeFileSync(p, s.split('\\\"version\\\": \\\"$CUR\\\"').join('\\\"version\\\": \\\"$NEW\\\"'));
    }
    for (const p of ['desktop/package.json','desktop/package-lock.json']) JSON.parse(fs.readFileSync(p,'utf8'));
    console.log('  版本号已改并通过 JSON 校验');
  \""
  # 本地先验一次 npm ci，别把问题留给 CI 去发现
  say "本地预检 npm ci（CI 挂在这一步的话本地也会挂）"
  run "rm -rf /tmp/ship-ci && mkdir -p /tmp/ship-ci && cp desktop/package.json desktop/package-lock.json /tmp/ship-ci/ && (cd /tmp/ship-ci && npm ci >/dev/null 2>&1) && echo '  ✅ npm ci 通过' && rm -rf /tmp/ship-ci"
  run "git add desktop/package.json desktop/package-lock.json"
  run "git commit -q -m '客户端 $NEW' || true"
  run "git push -q \"https://\${GITHUB_PAT}@github.com/$REPO.git\" main"
fi

# ---------- 2. 打包完整性 + 逻辑测试（无条件跑，--no-bump 也要跑）----------
# 曾经把 npm test 放在「版本号变了才跑」的分支里，等于重发同一版时不检查。
# 而 lib/ 漏打那次恰恰是新建目录、不改逻辑的改动。
say "本地预检：打包完整性 + 判定逻辑"
run "(cd desktop && npm test)"

# ---------- 3. 触发构建 ----------
say "触发 GitHub Actions 打包（windows-latest 原生）"
if [[ $DRY == 1 ]]; then
  echo "[dry-run] POST .../workflows/$WORKFLOW/dispatches"
  echo "[dry-run] 轮询等构建 → 下载 artifact → bash desktop/release.sh --upload-only <解压目录>"
  [[ $FORCE_LATEST == 1 ]] && echo "[dry-run] 上传成功后把 version.json 的 min_version 抬到 $NEW"
  exit 0
fi

BEFORE=$(curl -s "${GH[@]}" "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/runs?per_page=1" \
         | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).workflow_runs[0]?.id || 0")
curl -s -X POST "${GH[@]}" \
  "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" -d '{"ref":"main"}' >/dev/null

# 等新 run 出现（dispatches 是异步的，立刻查会拿到上一次的 run）
RID=0
for _ in $(seq 1 20); do
  sleep 5
  RID=$(curl -s "${GH[@]}" "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/runs?per_page=1" \
        | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).workflow_runs[0]?.id || 0")
  [[ "$RID" != "$BEFORE" && "$RID" != "0" ]] && break
done
[[ "$RID" == "$BEFORE" || "$RID" == "0" ]] && { echo "❌ 没等到新的构建任务"; exit 1; }
echo "  run $RID  https://github.com/$REPO/actions/runs/$RID"

# ---------- 3. 等构建完 ----------
say "等构建完成（约 3~8 分钟）"
for _ in $(seq 1 60); do
  ST=$(curl -s "${GH[@]}" "https://api.github.com/repos/$REPO/actions/runs/$RID" \
       | node -p "const d=JSON.parse(require('fs').readFileSync(0,'utf8')); d.status+' '+(d.conclusion||'-')")
  echo "  [$(date +%H:%M:%S)] $ST"
  [[ "$ST" == completed* ]] && break
  sleep 20
done
[[ "$ST" == "completed success" ]] || {
  echo "❌ 构建未成功：$ST"
  echo "   失败步骤："
  curl -s "${GH[@]}" "https://api.github.com/repos/$REPO/actions/runs/$RID/jobs" \
    | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).jobs
        .flatMap(j=>j.steps.filter(s=>!['success','skipped'].includes(s.conclusion))
        .map(s=>'     ✗ '+j.name+' / '+s.name+' → '+s.conclusion)).join('\n')"
  exit 1
}

# ---------- 4. 下载 artifact ----------
say "下载安装包"
OUT="/tmp/ship-$NEW"
rm -rf "$OUT"; mkdir -p "$OUT"
AID=$(curl -s "${GH[@]}" "https://api.github.com/repos/$REPO/actions/runs/$RID/artifacts" \
      | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).artifacts[0]?.id || 0")
[[ "$AID" == "0" ]] && { echo "❌ 构建产物为空"; exit 1; }
curl -sL "${GH[@]}" "https://api.github.com/repos/$REPO/actions/artifacts/$AID/zip" -o "$OUT/a.zip"
(cd "$OUT" && unzip -q a.zip && rm -f a.zip)
ls -la "$OUT" | sed 's/^/  /'

# ---------- 5. 上传到通道 ----------
say "上传到更新通道"
bash desktop/release.sh --upload-only "$OUT"

# ---------- 6. 校验通道（这一步才是「用户真能拿到」的证据）----------
say "校验通道"
LATEST=$(curl -s "http://8.141.123.141/desktop/latest.yml" | sed -n 's/^version: *//p' | tr -d '\r')
[[ "$LATEST" == "$NEW" ]] || { echo "❌ 通道上的 latest.yml 是 $LATEST，不是 $NEW"; exit 1; }
EXE=$(curl -s "http://8.141.123.141/desktop/latest.yml" | sed -n 's/^path: *//p' | tr -d '\r')
CODE=$(curl -s -o /dev/null -w '%{http_code}' -r 0-0 \
       "http://8.141.123.141/desktop/$(node -p "encodeURIComponent(process.argv[1])" "$EXE")")
[[ "$CODE" == "206" || "$CODE" == "200" ]] || { echo "❌ 安装包下不动：HTTP $CODE"; exit 1; }
echo "  ✅ latest.yml = $NEW，安装包可下载"

# ---------- 7. 强制门槛（必须在上传成功之后）----------
if [[ $FORCE_LATEST == 1 ]]; then
  say "开启强制更新：min_version → $NEW"
  node -e "
    const fs=require('fs'), p='desktop/version.json';
    const j=JSON.parse(fs.readFileSync(p,'utf8'));
    j.min_version='$NEW'; j.force_latest=true;
    fs.writeFileSync(p, JSON.stringify(j,null,2)+'\n');
  "
  scp -q desktop/version.json "root@8.141.123.141:/opt/pms/excel-share-system-main/desktop-releases/version.json"
  curl -s "http://8.141.123.141/desktop/version.json" | sed 's/^/  /'
  git add desktop/version.json && git commit -q -m "强制更新门槛 → $NEW" || true
  git push -q "https://${GITHUB_PAT}@github.com/$REPO.git" main
fi

say "完成：$NEW 已发布，客户端下一轮检查即可收到"
echo "  安装包留在 $OUT（需要手动派发时用）"
