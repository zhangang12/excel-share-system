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

// ---- 设备 ID：userData 下存 JSON，首次启动生成 uuid（前端统计请求头用）----
function loadDeviceId() {
  const file = path.join(app.getPath('userData'), 'device.json');
  try {
    const j = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (j && j.deviceId) return j.deviceId;
  } catch (_) { /* 首次启动或文件损坏，走生成 */ }
  const deviceId = crypto.randomUUID();
  try { fs.writeFileSync(file, JSON.stringify({ deviceId }, null, 2)); } catch (_) { /* 写失败不致命 */ }
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

// ---- 强制最低版本检查：拉服务器 version.json，失败（还没这文件）视为不强制 ----
async function checkForceUpdate() {
  try {
    const res = await fetch(VERSION_JSON_URL, { cache: 'no-store' });
    if (!res.ok) { log('version.json 不可用（', res.status, '），跳过强制检查'); return null; }
    const j = await res.json();
    if (j && j.min_version && compareVersions(app.getVersion(), j.min_version) < 0) {
      log(`当前版本 ${app.getVersion()} 低于最低要求 ${j.min_version}，进入强制更新`);
      return j;
    }
  } catch (err) {
    log('version.json 拉取失败，视为不强制：', err.message);
  }
  return null;
}

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
      { role: 'quit', label: '退出' },
    ],
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---- 下载处理：落系统「下载」目录，完成后通知 + 打开所在文件夹 ----
function setupDownloadHandler() {
  session.defaultSession.on('will-download', (_event, item) => {
    const dir = app.getPath('downloads');
    let savePath = path.join(dir, item.getFilename());
    // 同名文件自动加 (1)(2)… 防覆盖
    let i = 1;
    while (fs.existsSync(savePath)) {
      const ext = path.extname(savePath);
      savePath = path.join(dir, `${path.basename(savePath, ext)}(${i})${ext}`);
      i++;
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
    backgroundColor: '#1e3a5f',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 必须：页面从 file:// 加载、API 在 http://8.141.123.141，放开绕开 CORS。
      // 本壳为内部专用，窗口只加载打进包的 frontend/dist，不加载线上 URL。
      webSecurity: false,
    },
  });

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
    mainWindow.loadFile(path.join(__dirname, 'renderer', 'force-update.html'));
    return;
  }
  const indexHtml = path.join(__dirname, 'app', 'index.html');
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
function setupAutoUpdate() {
  autoUpdater.autoDownload = true;
  autoUpdater.logger = { info: log, warn: log, error: log };

  autoUpdater.on('update-downloaded', (info) => {
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
  // 检查失败（断网/服务器没传清单等）静默记日志，不打扰用户
  autoUpdater.on('error', (err) => log('自动更新检查失败（已忽略）：', err && err.message));

  const check = () => autoUpdater.checkForUpdates().catch((err) => log('检查更新失败：', err && err.message));
  check();
  setInterval(check, UPDATE_INTERVAL_MS);
}

// ---- 强制更新模式：进度推给 force-update.html，下完自动重启安装 ----
function setupForceUpdateIpc() {
  autoUpdater.autoDownload = true;
  autoUpdater.logger = { info: log, warn: log, error: log };

  const send = (channel, payload) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
  };
  autoUpdater.on('download-progress', (p) => send('force-update:progress', p));
  autoUpdater.on('update-downloaded', () => {
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
    buildMenu();
    setupDownloadHandler();

    if (app.isPackaged) {
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
