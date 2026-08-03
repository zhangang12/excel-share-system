// 客户端健康判定的确定性测试：node desktop/test/health.test.js
//
// 起因是反馈 #343（王利利「到4点多一点就黑屏了」）。查下来两件事都跟原先以为的不一样：
//   1. 他跑的是 **1.0.28**（desktop_clients 表里查到的），render 崩溃自恢复本来就装着，
//      所以崩的不是渲染进程。
//   2. 截图是**纯色 #0f1d30**——不是渐变。页面背景 --bg-page 是浅灰 #eef1f5，
//      Vue 挂了也该是浅灰；深蓝只可能是 BrowserWindow.backgroundColor，
//      即一个像素都没画 = 合成器层面的事。
// 而 GPU/合成器死掉时渲染进程还活着、还响应，render-process-gone 和 unresponsive
// 一个都不触发 —— 老逻辑在这种情况下完全失效。所以加了 rAF 画面心跳。
const assert = require('assert');
const { shouldRecoverPaint, paintAction, compareVersions, requiredVersion,
        PAINT_STALL_MS, RELAUNCH_GRACE_MS } = require('../lib/health');

let n = 0, bad = [];
function t(name, fn) {
  n++;
  try { fn(); console.log('  ok:', name); }
  catch (e) { bad.push(name); console.log('  FAIL:', name, '—', e.message); }
}

const S = PAINT_STALL_MS;

console.log('\n===== 画面心跳判定 =====');
t('有焦点 + 心跳停够久 → 判定为卡死（这才是要救的场景）', () => {
  assert.strictEqual(shouldRecoverPaint({ focused: true, minimized: false, stalledMs: S }), true);
  assert.strictEqual(shouldRecoverPaint({ focused: true, minimized: false, stalledMs: S * 3 }), true);
});
t('有焦点但没停够久 → 不动', () => {
  assert.strictEqual(shouldRecoverPaint({ focused: true, minimized: false, stalledMs: S - 1 }), false);
  assert.strictEqual(shouldRecoverPaint({ focused: true, minimized: false, stalledMs: 0 }), false);
});
t('失焦 → 永不判卡死（被别的窗口盖住时 rAF 本来就停）', () => {
  // 这条是本次改动的核心：原来写的 isVisible()，而被整个盖住时 isVisible() 仍是 true，
  // 用户切去 Excel 看一分钟回来就会被强制重启，正在填的表单直接没。
  for (const ms of [S, S * 10, S * 100]) {
    assert.strictEqual(shouldRecoverPaint({ focused: false, minimized: false, stalledMs: ms }), false);
  }
});
t('最小化 → 永不判卡死（rAF 合法停止）', () => {
  for (const ms of [S, S * 10]) {
    assert.strictEqual(shouldRecoverPaint({ focused: true, minimized: true, stalledMs: ms }), false);
  }
});
t('阈值是 45 秒（改小会误伤、改大用户干等）', () => {
  assert.strictEqual(PAINT_STALL_MS, 45000);
});

console.log('\n===== 画面卡死怎么救 =====');
t('稳定运行后卡死 → 重启整个应用（不是重载页面）', () => {
  // 合成器在 GPU 进程里，reload 换的是渲染进程的文档，坏掉的 GPU 进程还是那个
  assert.strictEqual(paintAction({ uptimeMs: RELAUNCH_GRACE_MS, alreadyRelaunching: false }), 'relaunch');
  assert.strictEqual(paintAction({ uptimeMs: 86400000, alreadyRelaunching: false }), 'relaunch');
});
t('刚启动就判卡死 → 只记录不重启（防重启循环）', () => {
  assert.strictEqual(paintAction({ uptimeMs: 0, alreadyRelaunching: false }), 'log-only');
  assert.strictEqual(paintAction({ uptimeMs: RELAUNCH_GRACE_MS - 1, alreadyRelaunching: false }), 'log-only');
});
t('已经在重启了 → 不重复触发', () => {
  assert.strictEqual(paintAction({ uptimeMs: 86400000, alreadyRelaunching: true }), 'noop');
});

console.log('\n===== 强制更新门槛 =====');
const cfg = { min_version: '1.0.29', force_latest: true };
t('新客户端认 force_latest：门槛跟着 latest.yml 走', () => {
  assert.strictEqual(requiredVersion(cfg, '1.0.31', true), '1.0.31');
});
t('老客户端不认 force_latest：只看 min_version', () => {
  // 1.0.29 之前的客户端不认识这个字段，所以两个都得设，光设 force_latest 拦不住老版本
  assert.strictEqual(requiredVersion(cfg, '1.0.31', false), '1.0.29');
});
t('latest.yml 拉不到 → 退回 min_version，不是放弃拦截', () => {
  assert.strictEqual(requiredVersion(cfg, '', true), '1.0.29');
});
t('latest.yml 比 min_version 旧 → 取高的那个（防回滚发布把门槛拉低）', () => {
  assert.strictEqual(requiredVersion({ min_version: '1.0.29', force_latest: true }, '1.0.20', true), '1.0.29');
});
t('version.json 整个拿不到 → 门槛为空 = 放行', () => {
  // 网络不通宁可漏拦，也不能因为服务器抖一下把人锁在门外
  assert.strictEqual(requiredVersion({}, '', true), '');
  assert.strictEqual(requiredVersion({ force_latest: true }, '', true), '');
});

console.log('\n===== 版本比较 =====');
t('位数不齐也要比对', () => {
  assert.strictEqual(compareVersions('1.0.9', '1.0.10'), -1);   // 字符串比会判反
  assert.strictEqual(compareVersions('1.0.29', '1.0.29'), 0);
  assert.strictEqual(compareVersions('1.1.0', '1.0.99'), 1);
  assert.strictEqual(compareVersions('1.0', '1.0.0'), 0);
});

console.log('\n' + '='.repeat(52));
if (bad.length) { console.log(`❌ ${bad.length}/${n} 失败：` + bad.join(' / ')); process.exit(1); }
console.log(`✅ ${n}/${n} 通过`);
