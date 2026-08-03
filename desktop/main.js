// ============================================================
// 同辉项目管理系统 —— Windows 桌面客户端主进程（Electron 壳）
//
// 设计要点：
//  - 只加载内置打包的 frontend/dist（desktop/app/index.html），不加载线上 URL
//  - webSecurity:false 是必须项：页面从 file:// 加载、API 在 http://8.141.123.141，
//    放开才能让 axios 直连内网/公网 HTTP 接口绕开 CORS。
//    这是内部专用壳，窗口只加载我们自己打进来的 dist，风险可控；
//    作为补偿防护：setWindowOpenHandler + will-navigate 把一切外部链接
//    一律交给系统浏览器，禁止窗口内跳外站。
//  - 自动更新走 electron-updater（generic provider，服务器 nginx 静态目录）。
//  - 强制最低版本：启动时拉 version.json，低于 min_version 只给「立即更新」一条路。
// ============================================================
const { app, BrowserWindow, Menu, dialog, session, shell, ipcMain, Notification } = require('electron');
const { autoUpdater } = require('electron-updater');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const UPDATE_BASE_URL = 'http://8.141.123.141/desktop/';
const VERSION_JSON_URL = UPDATE_BASE_URL + 'version.json';
const UPDATE_INTERVAL_MS = 4 * 60 * 60 * 1000; // 🆕 每 4 小时例行检查一次更新

let mainWindow = null;
let forceMode = false;   // 强制更新模式：窗口加载 renderer/force-update.html
let forceNotes = '';     // version.json 里的更新说明，透传给强制更新页
let splashWindow = null; // 启动页小窗口（品牌页，主窗口就绪后关闭）
let mainRevealed = false;

// ---- 🆕 崩溃日志：黑屏/闪退这类问题不落地就永远只能靠猜 ----
// 写到 userData/crash.log（Windows: %APPDATA%/同辉项目管理/crash.log），超过 200KB 自动截断。
// 排查时让用户把这个文件发过来，就能看到是渲染进程 OOM、GPU 掉了还是主线程卡死。
const CRASH_LOG = () => path.join(app.getPath('userData'), 'crash.log');
const GPU_FLAG = () => path.join(app.getPath('userData'), '.gpu-crashed');
function logCrash(tag, detail) {
  try {
    const f = CRASH_LOG();
    if (fs.existsSync(f) && fs.statSync(f).size > 200 * 1024) {
      fs.writeFileSync(f, fs.readFileSync(f, 'utf8').slice(-100 * 1024));
    }
    fs.appendFileSync(f, `[${new Date().toISOString()}] ${tag} ${detail || ''}\n`);
  } catch { /* 日志失败不能反过来把程序搞挂 */ }
}

// GPU 进程崩过一次 → 下次启动直接关掉硬件加速。
// "闲置一会儿就黑屏"在 Windows 上最常见的成因就是集显驱动把 GPU 进程搞崩，
// 关掉硬件加速渲染会略慢，但换来不再黑屏；只对真崩过的机器生效，不拖累正常机器。
try {
  if (fs.existsSync(GPU_FLAG())) {
    app.disableHardwareAcceleration();
    logCrash('startup', '检测到上次 GPU 崩溃标记，本次启动已禁用硬件加速');
  }
} catch { /* ignore */ }

// GPU / 工具进程崩溃：记录并打标记（下次启动降级），当前这次由页面自恢复兜着
app.on('child-process-gone', (_e, details) => {
  logCrash('child-process-gone', `${details.type} ${details.reason} exitCode=${details.exitCode}`);
  if (details.type === 'GPU') {
    try { fs.writeFileSync(GPU_FLAG(), new Date().toISOString()); } catch { /* ignore */ }
  }
});

// ---- 启动页：无框小窗，先声夺人；主窗口 Vue 挂载完成（app-ready）后切换 ----
function createSplash() {
  splashWindow = new BrowserWindow({
    width: 400,
    height: 470,
    frame: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    center: true,
    show: true,
    backgroundColor: '#0f1d30',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  splashWindow.loadFile(path.join(__dirname, 'renderer', 'splash.html'));
  splashWindow.on('closed', () => { splashWindow = null; });
}

// 主窗口就绪 → 关启动页、亮主窗口（只执行一次）
function revealMainWindow() {
  if (mainRevealed) return;
  mainRevealed = true;
  if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
  }
}

// 前端挂载信号到首帧绘制完成差一口气，延迟 300ms 再亮窗，避免闪一下白屏
ipcMain.on('pms-desktop:app-ready', () => setTimeout(() => revealMainWindow(), 300));

// ---- 设备 ID：userData 下存 JSON，首次启动生成 uuid（前端统计请求头用）----
// 🆕 服务端「客户端设备限制」按这个 ID 放行，所以它**必须能持久化**：
//   写失败时每次启动都会生成新 ID，那台机器就永远不在名单里，批了也没用，
//   而且现象是「登录老要验证码」，从服务端完全看不出原因。故写失败要上报。
let deviceIdPersisted = true;
function loadDeviceId() {
  const file = path.join(app.getPath('userData'), 'device.json');
  try {
    const j = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (j && j.deviceId) return j.deviceId;
  } catch (_) { /* 首次启动或文件损坏，走生成 */ }
  const deviceId = crypto.randomUUID();
  try {
    fs.writeFileSync(file, JSON.stringify({ deviceId }, null, 2));
  } catch (err) {
    // 不致命：本次仍能用，但下次启动会换 ID
    deviceIdPersisted = false;
    try { console.error('[device-id] 写入失败，设备 ID 无法固定:', err && err.message); } catch (_) {}
  }
  return deviceId;
}
const deviceId = loadDeviceId();

// preload 同步取客户端信息（版本号/设备ID/强制更新说明），同步注入 window.pmsDesktop
ipcMain.on('pms-desktop:info', (e) => {
  e.returnValue = { version: app.getVersion(), deviceId, forceNotes };
});

// ---- 简易 semver 比较：a<b 返回 -1，相等 0，a>b 返回 1（只比 x.y.z 数字段）----
function compareVersions(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) < (pb[i] || 0)) return -1;
    if ((pa[i] || 0) > (pb[i] || 0)) return 1;
  }
  return 0;
}

function log(...args) {
  console.log('[pms-desktop]', ...args);
}

// ============================================================
// 🆕 故障自动上报：把 crash.log 送到服务器，不再靠「让用户把文件发过来」
//
// 起因：old-uninstaller 崩溃导致升级失败，排查时手里什么都没有——没有崩溃转储、
// 没有日志、无法复现，只能去读 electron-builder 的 NSIS 模板反推。而更新器的日志
// 本来就写在 console.log 里，打包后没有控制台，等于全部丢弃。
//
// 关键设计：安装器是在 app 退出之后才跑的，崩溃时本进程已经不在了，没法当场上报。
// 所以改成「下次启动回溯」——下载完成时把目标版本记到 pending-update.json，
// 下次启动发现版本没变，就说明上次安装失败了。
// ============================================================
const REPORT_URL = 'http://8.141.123.141/api/desktop/report';
const PENDING_FILE = () => path.join(app.getPath('userData'), 'pending-update.json');
const REPORT_MAX_BYTES = 60 * 1024;   // 与服务端 64KB 上限留出余量

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (_) { return null; }
}
function writeJson(file, obj) {
  try { fs.writeFileSync(file, JSON.stringify(obj, null, 2)); } catch (_) { /* 写失败不致命 */ }
}

/** 上报一条。kind 用于分类（update_failed / crash / error），detail 是正文。
 *  失败一律吞掉——上报本身绝不能影响客户端可用性。 */
async function sendReport(kind, detail, extra) {
  try {
    const body = {
      device_id: deviceId,
      version: app.getVersion(),
      kind,
      detail: String(detail || '').slice(-REPORT_MAX_BYTES),
      extra: extra || null,
    };
    const res = await fetch(REPORT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(8000),
    });
    log('故障上报', kind, res.ok ? '成功' : `失败 ${res.status}`);
    return res.ok;
  } catch (e) {
    log('故障上报异常（已忽略）：', e && e.message);
    return false;
  }
}

function crashLogTail(bytes = REPORT_MAX_BYTES) {
  try { return fs.readFileSync(CRASH_LOG(), 'utf8').slice(-bytes); } catch (_) { return ''; }
}

// electron-updater 的日志出口：原来接的是 console.log，打包后没有控制台等于全丢。
// 现在落到 crash.log，升级失败时随上报一起送回服务器。
const updaterLogger = {
  info: (...a) => { log(...a); logCrash('updater', a.join(' ')); },
  warn: (...a) => { log(...a); logCrash('updater-warn', a.join(' ')); },
  error: (...a) => { log(...a); logCrash('updater-error', a.join(' ')); },
};

/** 启动时回溯：上次下载了新版本但现在版本没变 → 安装失败了。
 *  这是抓 old-uninstaller 那类问题的唯一信号（安装器崩溃时客户端已退出）。 */
async function reportPendingUpdateFailure() {
  const p = readJson(PENDING_FILE());
  if (!p || !p.version) return;
  const cur = app.getVersion();
  if (compareVersions(cur, p.version) >= 0) {
    // 装上了，正常路径，清掉标记
    fs.rmSync(PENDING_FILE(), { force: true });
    return;
  }
  // 版本没变 = 上次安装没成功
  logCrash('update-failed', `目标 ${p.version}，重启后仍是 ${cur}（下载于 ${p.at}）`);
  const ok = await sendReport('update_failed', crashLogTail(), {
    target_version: p.version,
    current_version: cur,
    downloaded_at: p.at,
    attempts: (p.attempts || 0) + 1,
  });
  // 上报成功才清标记；没成功就累加次数，下次启动再报（别把唯一的线索弄丢）
  if (ok) fs.rmSync(PENDING_FILE(), { force: true });
  else writeJson(PENDING_FILE(), { ...p, attempts: (p.attempts || 0) + 1 });
}

// 主进程未捕获异常/Promise：原来只有渲染进程和子进程有钩子，主进程炸了什么都不留
process.on('uncaughtException', (err) => {
  logCrash('uncaughtException', (err && err.stack) || String(err));
  sendReport('crash', crashLogTail(), { where: 'main:uncaughtException' });
});
process.on('unhandledRejection', (reason) => {
  logCrash('unhandledRejection', (reason && reason.stack) || String(reason));
});

// ---- 通道上的最新版本号：从 latest.yml 抠 version（不为一行引个 yaml 依赖）----
async function latestChannelVersion() {
  try {
    const res = await fetch(UPDATE_BASE_URL + 'latest.yml',
      { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    if (!res.ok) return '';
    const m = /^version:\s*([\w.\-+]+)\s*$/m.exec(await res.text());
    return m ? m[1] : '';
  } catch (err) {
    log('latest.yml 拉取失败：', err.message);
    return '';
  }
}

// ---- 强制版本检查：拉服务器 version.json，失败（网络不通/还没这文件）一律视为不强制 ----
// 两种口径：
//   min_version   —— 手工设的地板，老客户端只认这个
//   force_latest  —— 「必须是通道上的最新版」，省得每次发版都要记得改 min_version
// 判定用两者里更高的那个。网络不通时**放行**：宁可漏拦，也不能因为服务器抖一下就把人锁在门外。
async function checkForceUpdate() {
  try {
    const res = await fetch(VERSION_JSON_URL, { cache: 'no-store', signal: AbortSignal.timeout(5000) });
    if (!res.ok) { log('version.json 不可用（', res.status, '），跳过强制检查'); return null; }
    const j = (await res.json()) || {};
    let need = j.min_version || '';
    if (j.force_latest) {
      const latest = await latestChannelVersion();
      if (latest && (!need || compareVersions(latest, need) > 0)) need = latest;
    }
    if (need && compareVersions(app.getVersion(), need) < 0) {
      log(`当前版本 ${app.getVersion()} 低于要求 ${need}，进入强制更新`);
      return { ...j, need };
    }
  } catch (err) {
    log('version.json 拉取失败，视为不强制：', err.message);
  }
  return null;
}

// ---- 登录前再拦一次 ----
// 启动时查过一遍，但客户端可能连着开好几天不重启（现在还有 30 天免登录），
// 期间发了新版就一直查不到。前端登录按钮按下前调这里，过期的直接切到强制更新页。
let enforcing = false;
async function enforceVersionBeforeLogin() {
  if (forceMode || enforcing) return forceMode;
  enforcing = true;
  try {
    const forced = await checkForceUpdate();
    if (!forced) return false;
    forceMode = true;
    forceNotes = forced.notes || '';
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadFile(path.join(__dirname, 'renderer', 'force-update.html'));
      setupForceUpdateIpc();
    }
    return true;
  } finally { enforcing = false; }
}
ipcMain.handle('pms-desktop:enforce-version', () => enforceVersionBeforeLogin());

// ---- 精简中文菜单 ----
function buildMenu() {
  const template = [];
  if (process.platform === 'darwin') {
    template.push({ role: 'appMenu' }); // macOS 保留应用菜单（关于/退出等）
  }
  template.push({
    label: '操作',
    submenu: [
      { role: 'reload', label: '重新加载' },
      { role: 'forceReload', label: '强制刷新' },
      { role: 'toggleDevTools', label: '开发者工具' },
      { type: 'separator' },
      { role: 'togglefullscreen', label: '全屏' },
      { type: 'separator' },
      {
        label: `下载位置…（当前：${dlBaseDir()}）`,
        click: () => { if (pickDownloadDir()) buildMenu(); },   // 标题带着路径，改完要重建菜单
      },
      {
        label: '每次下载都询问位置',
        type: 'checkbox',
        checked: dlPrefs.alwaysAsk,
        click: (mi) => { dlPrefs.alwaysAsk = mi.checked; saveDlPrefs(); },
      },
      { type: 'separator' },
      { role: 'quit', label: '退出' },
    ],
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---- 下载偏好：目录 + 是否每次询问，存 userData/download-prefs.json ----
// 默认保持原样（系统「下载」目录、不询问），只有用户主动改过才不一样，装了新版的人不会突然被弹窗。
const DL_PREFS_FILE = () => path.join(app.getPath('userData'), 'download-prefs.json');
let dlPrefs = { dir: '', alwaysAsk: false };
function loadDlPrefs() {
  try {
    const j = JSON.parse(fs.readFileSync(DL_PREFS_FILE(), 'utf8'));
    if (j && typeof j === 'object') {
      dlPrefs = { dir: String(j.dir || ''), alwaysAsk: !!j.alwaysAsk };
    }
  } catch { /* 没有就用默认 */ }
  // 目录被删/换盘/U盘拔了 → 退回系统下载目录，不能让下载直接失败
  if (dlPrefs.dir && !fs.existsSync(dlPrefs.dir)) {
    log('下载目录不存在，退回系统下载目录：', dlPrefs.dir);
    dlPrefs.dir = '';
  }
}
function saveDlPrefs() {
  try { fs.writeFileSync(DL_PREFS_FILE(), JSON.stringify(dlPrefs, null, 2)); }
  catch (err) { log('下载偏好写入失败：', err.message); }
}
function dlBaseDir() { return dlPrefs.dir || app.getPath('downloads'); }

/** 同名自动加 (1)(2)… 防覆盖 */
function uniquePath(dir, filename) {
  let out = path.join(dir, filename);
  let i = 1;
  while (fs.existsSync(out)) {
    const ext = path.extname(out);
    out = path.join(dir, `${path.basename(out, ext)}(${i})${ext}`);
    i++;
  }
  return out;
}

/** 选下载目录（菜单「下载位置…」）。返回是否改了。 */
function pickDownloadDir() {
  const r = dialog.showOpenDialogSync(mainWindow, {
    title: '选择下载保存位置',
    defaultPath: dlBaseDir(),
    properties: ['openDirectory', 'createDirectory'],
    buttonLabel: '就存这里',
  });
  if (!r || !r.length) return false;
  dlPrefs.dir = r[0];
  saveDlPrefs();
  log('下载目录已设为：', dlPrefs.dir);
  return true;
}

// ---- 下载处理：落指定目录（默认系统「下载」），完成后通知 + 打开所在文件夹 ----
function setupDownloadHandler() {
  session.defaultSession.on('will-download', (_event, item) => {
    // setSavePath 必须在本回调里同步调用，异步版对话框会被 Electron 抢先接管，所以用 Sync 版
    let savePath;
    if (dlPrefs.alwaysAsk) {
      savePath = dialog.showSaveDialogSync(mainWindow, {
        title: '保存到',
        defaultPath: path.join(dlBaseDir(), item.getFilename()),
        buttonLabel: '保存',
      });
      if (!savePath) { item.cancel(); return; }   // 用户点了取消就别偷偷存一份
      dlPrefs.dir = path.dirname(savePath);       // 记住这次选的，下次从这里开始
      saveDlPrefs();
    } else {
      savePath = uniquePath(dlBaseDir(), item.getFilename());
    }
    item.setSavePath(savePath);
    item.once('done', (_e, state) => {
      if (state === 'completed') {
        const n = new Notification({
          title: '下载完成',
          body: `${path.basename(savePath)}\n点击打开所在文件夹`,
        });
        n.on('click', () => shell.showItemInFolder(savePath));
        n.show();
      } else {
        new Notification({ title: '下载失败', body: path.basename(savePath) }).show();
      }
    });
  });
}

// ---- 主窗口 ----
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 1100,
    minHeight: 700,
    autoHideMenuBar: true,
    show: false,                 // 先隐藏，启动页挡在前面，就绪后再亮窗
    backgroundColor: '#0f1d30',  // 与登录页底色一致，切窗不闪白
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 必须：页面从 file:// 加载、API 在 http://8.141.123.141，放开绕开 CORS。
      // 本壳为内部专用，窗口只加载打进包的 frontend/dist，不加载线上 URL。
      webSecurity: false,
      // 🆕 窗口切后台时别把定时器降频——心跳/自动刷新被掐，回来会看到一屏死数据
      backgroundThrottling: false,
    },
  });

  const indexHtml = path.join(__dirname, 'app', 'index.html');
  // 兜底：前端因故没发 app-ready（如老版本内置页），10s 后也要亮窗
  setTimeout(() => revealMainWindow(), 10000);
  mainWindow.webContents.on('did-fail-load', () => revealMainWindow());

  // ---- 🆕 崩溃自恢复：闲置一段时间后"黑屏"的真身 ----
  // 渲染进程被系统回收/崩溃后，窗口还在，但已经没有任何内容在画，只剩上面那句
  // backgroundColor('#0f1d30') 的底色——用户看到的就是一整片深蓝，且怎么点都没反应，
  // 只能去任务管理器结束进程重开。此前主进程对此毫无感知（只监听了 did-fail-load，
  // 那个管的是"页面没加载成功"，管不了"加载成功之后进程死掉"）。
  // 这里把死亡事件接住并自动重载页面；登录 token 存在 localStorage，重载后不用重新登录。
  let reloadingAfterCrash = false;
  const recover = (tag, detail) => {
    logCrash(tag, detail);
    if (reloadingAfterCrash) return;          // 防止崩溃风暴里反复重载
    reloadingAfterCrash = true;
    setTimeout(() => { reloadingAfterCrash = false; }, 5000);
    if (!mainWindow || mainWindow.isDestroyed()) return;
    try {
      mainWindow.webContents.reload();
    } catch {
      // webContents 已经不可用（进程彻底没了）→ 重建窗口
      try { mainWindow.destroy(); } catch { /* ignore */ }
      mainWindow = null;
      createWindow();
    }
    revealMainWindow();
  };
  // render-process-gone：渲染进程崩溃/被杀/OOM（Electron 22+ 取代已废弃的 'crashed'）
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    recover('render-process-gone', `${details.reason} exitCode=${details.exitCode}`);
  });
  // unresponsive：主线程卡死（长任务/死循环），先记一笔，卡满 15s 仍不恢复就重载
  let unresponsiveTimer = null;
  mainWindow.on('unresponsive', () => {
    logCrash('unresponsive', '主线程无响应');
    clearTimeout(unresponsiveTimer);
    unresponsiveTimer = setTimeout(() => recover('unresponsive-timeout', '卡死超过15s，自动重载'), 15000);
  });
  mainWindow.on('responsive', () => { clearTimeout(unresponsiveTimer); unresponsiveTimer = null; });

  // 补偿防护：外部链接一律交给系统浏览器，窗口内不允许跳外站
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file://')) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  if (forceMode) {
    // 强制更新页不需要启动页，渲染好直接亮
    mainWindow.loadFile(path.join(__dirname, 'renderer', 'force-update.html'));
    mainWindow.once('ready-to-show', () => revealMainWindow());
    return;
  }

  if (fs.existsSync(indexHtml)) {
    mainWindow.loadFile(indexHtml);
  } else {
    // 开发态没拷贝 dist 时给句人话提示
    mainWindow.loadURL('data:text/html;charset=utf-8,'
      + encodeURIComponent('<h2 style="font-family:sans-serif">未找到内置前端（app/index.html）</h2>'
        + '<p style="font-family:sans-serif">请先执行 bash desktop/release.sh --dry-run 之外的真实打包流程，'
        + '或手动把 frontend/dist 拷到 desktop/app/。</p>'));
  }
}

// ---- 常规自动更新（后台静默下载，下完弹原生对话框问是否重启）----
// 手动检查标记：布局底部「检查更新」按钮触发时置 true；
// 只有手动触发的这一轮状态才推给前端，后台 4h 轮询保持静默不打扰用户
let manualChecking = false;

// 主动检查更新（前端 pmsDesktop.checkUpdate → 此 IPC）
ipcMain.on('pms-desktop:check-update', () => {
  const reply = (payload) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('pms-desktop:update-status', payload);
  };
  if (!app.isPackaged || forceMode) {
    reply({ status: 'error', message: '当前环境不支持检查更新' });
    return;
  }
  manualChecking = true;
  autoUpdater.checkForUpdates().catch((err) => {
    manualChecking = false;
    reply({ status: 'error', message: (err && err.message) || '检查更新失败' });
  });
});

// 🆕 登录成功后静默检查更新（LoginView 每次登录成功调用）：
// 30 分钟节流防频繁登录重复检查；不动 manualChecking（不往布局按钮推状态），
// 有新版走 autoDownload 静默下载，下完 update-downloaded 里统一弹「立即重启更新」。
let lastSilentCheck = 0;
ipcMain.on('pms-desktop:check-update-silent', () => {
  if (!app.isPackaged || forceMode) return;
  const now = Date.now();
  if (now - lastSilentCheck < 30 * 60 * 1000) { log('登录触发检查更新：30 分钟内已查过，跳过'); return; }
  lastSilentCheck = now;
  log('登录触发检查更新');
  autoUpdater.checkForUpdates().catch((err) => log('登录触发检查失败（已忽略）：', err && err.message));
});

let autoUpdateTimer = null;
function setupAutoUpdate() {
  autoUpdater.autoDownload = true;
  autoUpdater.logger = updaterLogger;   // 🆕 落 crash.log，否则打包后全丢

  // 状态推给渲染进程（manualChecking 外的后台轮询一律吞掉）
  const sendStatus = (status, extra) => {
    if (!manualChecking) return;
    if (['not-available', 'downloaded', 'error'].includes(status)) manualChecking = false;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('pms-desktop:update-status', { status, ...(extra || {}) });
    }
  };

  autoUpdater.on('checking-for-update', () => sendStatus('checking'));
  autoUpdater.on('update-available', (info) => sendStatus('available', { version: info.version }));
  autoUpdater.on('update-not-available', () => sendStatus('not-available'));

  autoUpdater.on('update-downloaded', (info) => {
    // 🆕 记下目标版本：下次启动若版本没变，说明安装失败了（安装器在 app 退出后才跑，
    //    崩溃时本进程已经不在，只能这样回溯）
    writeJson(PENDING_FILE(), { version: info.version, at: new Date().toISOString() });
    sendStatus('downloaded', { version: info.version });
    dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: '更新就绪',
      message: `新版本 ${info.version} 已就绪`,
      detail: '重启客户端后自动完成更新。',
      buttons: ['立即重启更新', '稍后'],
      defaultId: 0,
      cancelId: 1,
    }).then((r) => {
      if (r.response === 0) autoUpdater.quitAndInstall();
    });
  });
  // 检查失败（断网/服务器没传清单等）静默记日志，不打扰用户（手动触发的经 sendStatus 告知）
  autoUpdater.on('error', (err) => {
    logCrash('updater-error', (err && err.stack) || String(err));
    log('自动更新检查失败（已忽略）：', err && err.message);
    sendStatus('error', { message: (err && err.message) || '' });
  });

  const check = () => autoUpdater.checkForUpdates().catch((err) => log('检查更新失败：', err && err.message));
  check();
  autoUpdateTimer = setInterval(check, UPDATE_INTERVAL_MS);
}

// ---- 强制更新模式：进度推给 force-update.html，下完自动重启安装 ----
let forceIpcBound = false;
function setupForceUpdateIpc() {
  // 登录前复检可能在常规更新流程已经跑起来之后才切进强制模式：
  // 那时 autoUpdater 上挂着 setupAutoUpdate 注册的监听，不摘掉会两套逻辑同时跑
  // （用户会先看到常规的「立即重启更新」原生对话框，点了取消就卡在强制页上不动）。
  if (autoUpdateTimer) { clearInterval(autoUpdateTimer); autoUpdateTimer = null; }
  autoUpdater.removeAllListeners();
  autoUpdater.autoDownload = true;
  autoUpdater.logger = updaterLogger;   // 🆕 落 crash.log，否则打包后全丢

  const send = (channel, payload) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
  };
  autoUpdater.on('download-progress', (p) => send('force-update:progress', p));
  autoUpdater.on('update-downloaded', (info) => {
    writeJson(PENDING_FILE(), { version: info && info.version, at: new Date().toISOString() });
    send('force-update:downloaded');
    autoUpdater.quitAndInstall(); // 下完自动重启安装，不给绕过出口
  });
  autoUpdater.on('update-not-available', () => {
    // 服务器清单还没上传新版本：提示用户稍后再试（无法绕过，只能重试）
    send('force-update:error', '暂未检测到新版本安装包，请稍后再点「立即更新」重试。');
  });
  autoUpdater.on('error', (err) => {
    log('强制更新下载失败：', err && err.message);
    send('force-update:error', '更新下载失败，请检查网络后重试。');
  });

  if (forceIpcBound) return;    // ipcMain.on 重复注册会让一次点击触发两遍
  forceIpcBound = true;
  ipcMain.on('force-update:trigger', () => {
    autoUpdater.checkForUpdates().catch((err) => {
      send('force-update:error', `检查更新失败：${err && err.message}`);
    });
  });
  ipcMain.on('force-update:quit', () => autoUpdater.quitAndInstall());
}

// ---- 单实例锁：重复启动聚焦已有窗口 ----
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    // 任务栏图标分组 ID（与 appId 一致，通知/任务栏归属正确）
    app.setAppUserModelId('com.tonghui.pms');
    loadDlPrefs();          // 菜单标题要显示当前下载目录，必须在 buildMenu 之前
    buildMenu();
    setupDownloadHandler();
    // 启动页立刻亮（强制版本检查要走网络，不能让用户干等黑屏）
    createSplash();

    if (app.isPackaged) {
      // 🆕 先回溯上次的升级结果：下载了新版本但版本没变 = 安装失败，上报出去。
      //    不 await——上报走网络，不能拖慢启动；失败了下次启动还会再报。
      reportPendingUpdateFailure().catch(() => { /* 上报失败不影响启动 */ });
      // 🆕 设备 ID 没能落盘：这台机器每次启动都会换 ID，服务端设备名单永远认不出它。
      //    报出来，管理层在故障列表里能直接看到原因，不用对着"批了还是要验证码"瞎猜。
      if (!deviceIdPersisted) {
        sendReport('error', 'device.json 写入失败，设备 ID 无法固定；每次启动都会生成新 ID，'
          + '服务端「客户端设备限制」将永远认不出这台机器（常见原因：杀毒软件拦截 %APPDATA% 写入）',
          { where: 'loadDeviceId', userData: app.getPath('userData') })
          .catch(() => { /* 上报失败不影响启动 */ });
      }
      // 打包模式：先查强制最低版本，再决定进应用还是进强制更新页
      const forced = await checkForceUpdate();
      forceMode = !!forced;
      if (forceMode) {
        forceNotes = (forced && forced.notes) || '';
        createWindow();
        setupForceUpdateIpc();
        return;
      }
      createWindow();
      setupAutoUpdate();
    } else {
      // 开发态：不查更新，直接进应用
      createWindow();
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });
}
