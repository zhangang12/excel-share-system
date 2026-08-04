// 客户端健康判定里**不依赖 Electron 的那部分**，单独放这，好脱离 GUI 确定性地测。
// （直接测 main.js 不行：无头环境拿不到真 OS 焦点，判定会一路 return，什么都验不到；
//   而 require('./main.js') 会把整个应用跑起来。）

/** 多久没有画面心跳算「合成器死了」 */
const PAINT_STALL_MS = 45000;

/**
 * 判不判「画面卡死、需要重载」。
 *
 * 背景：GPU/合成器死掉时，渲染进程还活着、还响应，window 还在，
 * 就是一个像素都不画了 —— 用户看到的是 BrowserWindow.backgroundColor 那片深蓝。
 * 主进程的 render-process-gone / unresponsive **都不会触发**，
 * 所以只能靠 requestAnimationFrame（由合成器驱动）的心跳来察觉。
 *
 * ⚠️ 必须用 focused，不能用 isVisible()：
 *    被别的窗口整个盖住时 isVisible() 依然返回 true，而实测**不在前台时 rAF 就被节流到不跑**。
 *    拿 isVisible 判，用户切去 Excel 看一分钟回来就会被强制重载，正在填的表单直接没。
 *    有焦点 ⇒ 一定没被遮挡、一定在人眼前，这时 rAF 还不动就只能是合成器死了。
 * ⚠️ 最小化同理：那时 rAF 合法地停，不能算故障。
 */
function shouldRecoverPaint({ focused, minimized, stalledMs }) {
  if (!focused || minimized) return false;
  return stalledMs >= PAINT_STALL_MS;
}

/** 简易 semver 比较：a<b → -1，相等 0，a>b → 1（只比 x.y.z 数字段） */
function compareVersions(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) < (pb[i] || 0)) return -1;
    if ((pa[i] || 0) > (pb[i] || 0)) return 1;
  }
  return 0;
}

/**
 * 强制更新门槛：min_version 与（force_latest 时的）通道最新版取更高者。
 * @param appVersion 当前客户端版本
 * @param cfg        version.json
 * @param latest     latest.yml 里的版本；拿不到传 ''
 * @param knowsForceLatest 该客户端是否认识 force_latest 字段（1.0.29 起才认）
 */
function requiredVersion(cfg, latest, knowsForceLatest) {
  let need = (cfg && cfg.min_version) || '';
  if (knowsForceLatest && cfg && cfg.force_latest && latest
      && (!need || compareVersions(latest, need) > 0)) need = latest;
  return need;
}

/** 刚起来这么久内不自动重启：万一判定有误，别把客户端锁进重启循环 */
const RELAUNCH_GRACE_MS = 120000;

/**
 * 画面卡死该怎么救。
 *
 * ⚠️ 不能用 webContents.reload()：合成器活在 **GPU 进程**里，不在渲染进程。
 *    重载换的是渲染进程的文档，用的还是那个坏掉的 GPU 进程，重载完大概率还是一片深蓝。
 *    用户手工的解法就是「任务管理器杀进程重开」，所以这里也只能重启整个应用
 *    （并打上 GPU 标记，下次启动禁用硬件加速，从根上不再犯）。
 * 渲染进程崩溃是另一回事——那种情况下 reload 是对的，走 recover() 那条路。
 */
function paintAction({ uptimeMs, alreadyRelaunching }) {
  if (alreadyRelaunching) return 'noop';
  if (uptimeMs < RELAUNCH_GRACE_MS) return 'log-only';
  return 'relaunch';
}

/** 同一轮故障里最多重载几次；再不行就别在原地打转了 */
const RELOAD_MAX_ATTEMPTS = 3;
/** 多久没再出故障，就认为上一轮过去了，重载计数清零 */
const RECOVER_WINDOW_MS = 5 * 60 * 1000;

/**
 * 渲染进程卡死/崩溃时该怎么救。
 *
 * ⚠️ 生产实测（2026-08-04，李新新那台）：
 *     01:10:16 无响应 → 01:10:31 重载 → **01:10:32 又无响应**
 *     01:10:47 重载 → 01:10:58 无响应 → 01:11:16 重载 → 01:11:17 又无响应
 *     …一直循环到 01:14:31
 *   **重载完 0.5 秒又卡** —— 说明对这类卡死，reload 根本不解决问题，
 *   自恢复反而制造了一个循环，那 4 分钟里他一条业务都没做成。
 *
 * 所以：连着重载 RELOAD_MAX_ATTEMPTS 次仍不恢复 → 升级成**重启整个应用**
 * （就是用户手工「杀进程重开」那一下，token 在 localStorage，重启不用重登）。
 *
 * @param attempts  本轮窗口内已经重载过几次
 * @param uptimeMs  应用已经跑了多久 —— 刚起来就重启会变成开机循环
 */
function reloadAction({ attempts, uptimeMs, alreadyRelaunching }) {
  if (alreadyRelaunching) return 'noop';
  if (attempts < RELOAD_MAX_ATTEMPTS) return 'reload';
  // 刚启动就判到上限，多半是启动本身有问题；重启也救不了，反而循环
  if (uptimeMs < RELAUNCH_GRACE_MS) return 'reload';
  return 'relaunch';
}

module.exports = { PAINT_STALL_MS, RELAUNCH_GRACE_MS,
                   RELOAD_MAX_ATTEMPTS, RECOVER_WINDOW_MS,
                   shouldRecoverPaint, paintAction, reloadAction,
                   compareVersions, requiredVersion };
