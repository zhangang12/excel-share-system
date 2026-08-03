// 验证**刚打出来的安装包里真的有**主进程需要的每个文件。
//
// 与 packaging.test.js 的分工：
//   packaging.test.js  查「配置意图」—— build.files 白名单覆没覆盖到
//   verify-asar.js     查「实际产物」—— app.asar 里到底有没有
// 两个都要。1.0.30/1.0.31 那次事故里，构建、上传、latest.yml 校验全绿，
// 因为它们查的都是「包在不在」，没有任何一环打开包看「里面缺不缺东西」。
//
// 用法（CI 里 electron-builder 之后）：
//   node test/verify-asar.js dist/win-unpacked/resources/app.asar
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const asarPath = process.argv[2];
if (!asarPath) { console.error('用法: node test/verify-asar.js <app.asar 路径>'); process.exit(2); }
if (!fs.existsSync(asarPath)) { console.error(`❌ 找不到 ${asarPath} —— 打包产物路径变了？`); process.exit(1); }

// 复用同一套 require 扫描（先剥注释，递归跟本地依赖）
function localRequires(file, seen = new Set()) {
  const abs = path.resolve(file);
  if (seen.has(abs)) return seen;
  seen.add(abs);
  let src;
  try { src = fs.readFileSync(abs, 'utf8'); } catch { return seen; }
  src = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  for (const m of src.matchAll(/require\(\s*['"](\.[^'"]+)['"]\s*\)/g)) {
    let p = path.resolve(path.dirname(abs), m[1]);
    if (!fs.existsSync(p) && fs.existsSync(p + '.js')) p += '.js';
    if (fs.existsSync(p)) localRequires(p, seen);
  }
  return seen;
}

const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const need = new Set();
for (const e of [pkg.main || 'main.js', 'preload.js']) {
  for (const f of localRequires(path.join(ROOT, e))) {
    need.add(path.relative(ROOT, f).split(path.sep).join('/'));
  }
}
// 内置前端页面：没有它窗口会一片空白（#343 那种），也必须在包里
need.add('app/index.html');

const listed = execFileSync('npx', ['--yes', 'asar', 'list', asarPath], { encoding: 'utf8' })
  .split('\n').map((l) => l.replace(/^[\\/]/, '').trim()).filter(Boolean);
const have = new Set(listed);

console.log(`\n===== 安装包内容校验 =====`);
console.log(`  asar: ${asarPath}（共 ${listed.length} 个条目）`);
let bad = 0;
for (const f of [...need].sort()) {
  if (have.has(f)) console.log(`  ok: ${f}`);
  else { console.log(`  FAIL: ${f} **不在安装包里** —— 装上去主进程会 Cannot find module`); bad++; }
}
console.log('\n' + '='.repeat(56));
if (bad) { console.log(`❌ ${bad} 个必需文件没打进包，绝不能发布`); process.exit(1); }
console.log(`✅ ${need.size}/${need.size} 个必需文件都在包里`);
