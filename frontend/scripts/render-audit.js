/* ============================================================================
 * 渲染层巡检脚本（操作列按钮挤行 / 页面横向溢出 / 控件越界）
 * ----------------------------------------------------------------------------
 * 用途：API/集成测试与 `npm run build` 测不出"渲染层"布局问题（如操作列多按钮挤行、
 *       窄屏横向溢出、按钮/标签文字被裁切）。本脚本逐页量化这些问题。
 *
 * 用法（最简，零依赖）：
 *   1) 浏览器登录系统（管理员，菜单最全）。
 *   2) F12 打开控制台，整段粘贴本文件内容，回车。
 *   3) 脚本自动巡检所有有权限的页面，console.table 打印结果，并返回 issues 数组。
 *      （它用 Vue Router 逐页导航，约十几秒跑完；只读测量，不改任何数据。）
 *
 * 判定：
 *   wrapCols   = 该页表格"操作列"里按钮被挤成多行的组合（offsetTop 不一致 → 换行）
 *   overflow   = 页面出现横向滚动（documentElement.scrollWidth > 视口宽）
 *   clipped    = 按钮/标签文字被裁切（scrollWidth > clientWidth）
 *   全为 0/空 → 该页渲染层无挤行/溢出问题。
 *
 * CI 自动版见同目录 render-audit.pw.mjs（需 `npm i -D playwright`）。
 * ==========================================================================*/
(async function renderAudit(opts = {}) {
  const WAIT = opts.wait || 500;            // 每页路由+首屏渲染等待(ms)
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const app = document.querySelector('#app');
  const router = app && app.__vue_app__ && app.__vue_app__.config.globalProperties.$router;
  if (!router) { console.error('[render-audit] 取不到 Vue Router，请确认在本系统页面运行'); return; }

  // 待巡检路由（可见才测；带 :参数 / 登录页 自动跳过）
  const NAMES = {
    '/sales': '销售部', '/dept/design': '设计部', '/dept/electric': '电工部',
    '/dept/produce': '生产部', '/sheet': '钣金组', '/purchase': '采购部',
    '/warehouse': '仓库组', '/logistics': '物流发货部', '/finance': '财务部',
    '/aftersales': '售后部', '/report': '月度报表', '/messages': '消息中心',
    '/projects': '项目目录', '/overview': '总览',
    '/admin/users': '用户', '/admin/permissions': '权限管理',
    '/admin/audit': '操作审计', '/admin/approve': '导出审批', '/admin/wxbind': '企微绑定',
  };
  const known = new Set(router.getRoutes().map((r) => r.path));
  const paths = Object.keys(NAMES).filter((p) => known.has(p));

  function measure() {
    // ① 操作列(各表末列)按钮挤行
    const wrapCols = []; const seen = new Set();
    document.querySelectorAll('.el-table__row').forEach((tr) => {
      const tds = tr.querySelectorAll('td'); const last = tds[tds.length - 1];
      if (!last) return;
      const btns = [...last.querySelectorAll('.el-button')];
      if (btns.length < 2) return;
      const key = btns.map((b) => b.textContent.trim()).join('|');
      if (seen.has(key)) return; seen.add(key);
      if (new Set(btns.map((b) => b.offsetTop)).size > 1) wrapCols.push(key);
    });
    // ② 页面横向溢出
    const overflow = document.documentElement.scrollWidth > window.innerWidth + 2;
    // ③ 控件文字越界
    let clipped = 0;
    document.querySelectorAll('.el-button, .el-tag').forEach((el) => {
      if (el.scrollWidth > el.clientWidth + 4) clipped++;
    });
    return { wrapCols, overflow, clipped, rows: document.querySelectorAll('.el-table__row').length };
  }

  const report = [];
  for (const p of paths) {
    try {
      await router.push(p); await sleep(WAIT);
      const m = measure();
      report.push({ 页面: NAMES[p], 路由: p, 数据行: m.rows, 按钮挤行: m.wrapCols.length, 横向溢出: m.overflow, 文字裁切: m.clipped, 挤行明细: m.wrapCols.join('; ') });
    } catch (e) {
      report.push({ 页面: NAMES[p], 路由: p, 错误: String(e).slice(0, 80) });
    }
  }

  const issues = report.filter((r) => r.按钮挤行 || r.横向溢出 || r.文字裁切 || r.错误);
  console.table(report);
  if (issues.length) { console.warn(`[render-audit] ⚠ ${issues.length}/${report.length} 页有渲染层问题：`); console.table(issues); }
  else console.log(`[render-audit] ✅ ${report.length} 页全部干净：无按钮挤行 / 无横向溢出 / 无文字裁切`);
  return issues;
})();
