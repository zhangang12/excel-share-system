/* ============================================================================
 * 渲染层巡检 · CI 自动版（Playwright headless）
 * ----------------------------------------------------------------------------
 * 无人值守地逐页量化"操作列按钮挤行 / 页面横向溢出 / 控件文字裁切"，有问题退出码=1。
 *
 * 准备：
 *   cd frontend
 *   npm i -D playwright && npx playwright install chromium
 * 运行（先确保系统已在 BASE 跑起来）：
 *   BASE=http://127.0.0.1:8000 node scripts/render-audit.pw.mjs
 *   # 可选：USER=admin PASS=admin123 VIEWPORT=1366x768 node scripts/render-audit.pw.mjs
 *
 * 退出码：0=全干净；1=有渲染层问题（可接入 CI 卡门禁）。
 * ==========================================================================*/
import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://127.0.0.1:8000';
const USER = process.env.USER || 'admin';
const PASS = process.env.PASS || 'admin123';
const [VW, VH] = (process.env.VIEWPORT || '1440x900').split('x').map(Number);

const NAMES = {
  '/sales': '销售部', '/dept/design': '设计部', '/dept/electric': '电工部',
  '/dept/produce': '生产部', '/sheet': '钣金组', '/purchase': '采购部',
  '/warehouse': '仓库组', '/logistics': '物流发货部', '/finance': '财务部',
  '/aftersales': '售后部', '/report': '月度报表', '/messages': '消息中心',
  '/projects': '项目目录', '/admin/users': '用户', '/admin/permissions': '权限管理',
  '/admin/audit': '操作审计', '/admin/approve': '导出审批', '/admin/wxbind': '企微绑定',
};

const MEASURE = () => {
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
  const overflow = document.documentElement.scrollWidth > window.innerWidth + 2;
  let clipped = 0;
  document.querySelectorAll('.el-button, .el-tag').forEach((el) => {
    if (el.scrollWidth > el.clientWidth + 4) clipped++;
  });
  return { wrapCols, overflow, clipped, rows: document.querySelectorAll('.el-table__row').length };
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: VW || 1440, height: VH || 900 } });

// 登录
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
await page.fill('input[placeholder*="用户名"], input[type="text"]', USER);
await page.fill('input[type="password"]', PASS);
await page.click('button:has-text("登 录"), button:has-text("登录")');
await page.waitForTimeout(1500);
// 关掉首登强制改密弹窗（若有）
await page.keyboard.press('Escape').catch(() => {});

const report = [];
for (const [path, name] of Object.entries(NAMES)) {
  try {
    await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle', timeout: 15000 });
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(500);
    const m = await page.evaluate(MEASURE);
    report.push({ 页面: name, 路由: path, 数据行: m.rows, 按钮挤行: m.wrapCols.length, 横向溢出: m.overflow, 文字裁切: m.clipped, 挤行明细: m.wrapCols.join('; ') });
  } catch (e) {
    report.push({ 页面: name, 路由: path, 错误: String(e).slice(0, 80) });
  }
}
await browser.close();

const issues = report.filter((r) => r.按钮挤行 || r.横向溢出 || r.文字裁切 || r.错误);
console.table(report);
if (issues.length) {
  console.error(`\n❌ 渲染层巡检：${issues.length}/${report.length} 页有问题`);
  console.table(issues);
  process.exit(1);
}
console.log(`\n✅ 渲染层巡检：${report.length} 页全部干净（无挤行/无横向溢出/无文字裁切）`);
process.exit(0);
