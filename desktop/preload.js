// ============================================================
// preload：contextBridge 向页面注入桌面端标识 + 强制更新页的最小 IPC
//   - window.pmsDesktop.isDesktop / version / deviceId：前端 axios 读取加统计请求头
//   - onProgress / onDownloaded / onUpdateError / triggerUpdate / quitAndInstall：
//     仅供强制更新页（renderer/force-update.html）使用
// contextIsolation: true、nodeIntegration: false（在 main.js webPreferences 里设置）
// ============================================================
const { contextBridge, ipcRenderer, clipboard } = require('electron');

// 同步拿主进程的版本号/设备ID（preload 阶段同步注入，前端 axios 初始化时就能读到）
const info = ipcRenderer.sendSync('pms-desktop:info') || {};

contextBridge.exposeInMainWorld('pmsDesktop', {
  isDesktop: true,
  version: info.version || '',
  deviceId: info.deviceId || '',

  // 前端 Vue 挂载完成后调用：主进程收到后关启动页、亮主窗口
  notifyReady: () => ipcRenderer.send('pms-desktop:app-ready'),

  // 🆕 反馈#422（2026-09-02）复制到剪贴板 —— **客户端必须走这条，别指望浏览器 API**。
  //   客户端页面是 loadFile 出来的 file:// 文档：navigator.clipboard 在这里能不能用
  //   取决于 Electron 版本和权限处理器，赌不起；document.execCommand('copy') 又要求
  //   真实用户手势、还被标记为废弃。财务点了没反应、而网页版测试时一切正常，
  //   是最难查的那种 bug。Electron 原生 clipboard 无条件可用，一步到位。
  copyText: (t) => { clipboard.writeText(String(t == null ? '' : t)); return true; },

  // ---- 主动检查更新（布局底部「检查更新」按钮）----
  // 触发后主进程走 electron-updater 检查，状态经 onUpdateStatus 回推：
  // checking / available(带 version) / not-available / downloaded(带 version) / error(带 message)
  checkUpdate: () => ipcRenderer.send('pms-desktop:check-update'),
  onUpdateStatus: (cb) => ipcRenderer.on('pms-desktop:update-status', (_e, s) => cb(s)),

  // ---- 登录成功后静默检查更新（LoginView 调用；30 分钟节流，不推状态、不打扰，
  //   有新版本则静默下载，下完由主进程弹「立即重启更新」原生框）----
  checkUpdateSilent: () => ipcRenderer.send('pms-desktop:check-update-silent'),

  // 🆕 登录前强制版本检查：返回 true 表示版本过低、主进程已切到强制更新页，登录别再往下走。
  //    客户端可能连开好几天不重启（还有 30 天免登录），只靠启动时那一次查不到新版。
  enforceVersion: () => ipcRenderer.invoke('pms-desktop:enforce-version'),

  // ---- 🆕 反馈#360 下载：菜单栏是隐藏的，「下载位置…」用户找不到，把它搬进页面 ----
  //   onDownloadDone：下完推给页面，前端弹应用内提示（不依赖系统通知——
  //   Windows 通知被静音时那条系统通知等于不存在，人就以为没下成）
  onDownloadDone: (cb) => ipcRenderer.on('pms-desktop:download-done', (_e, d) => cb(d)),
  showInFolder: (p) => ipcRenderer.send('pms-desktop:show-in-folder', p),
  getDownloadDir: () => ipcRenderer.invoke('pms-desktop:download-dir'),
  pickDownloadDir: () => ipcRenderer.invoke('pms-desktop:pick-download-dir'),

  // ---- 强制更新页专用最小 IPC ----
  forceUpdateNotes: info.forceNotes || '',
  onProgress: (cb) => ipcRenderer.on('force-update:progress', (_e, p) => cb(p)),
  onDownloaded: (cb) => ipcRenderer.on('force-update:downloaded', () => cb()),
  onUpdateError: (cb) => ipcRenderer.on('force-update:error', (_e, msg) => cb(msg)),
  triggerUpdate: () => ipcRenderer.send('force-update:trigger'),
  quitAndInstall: () => ipcRenderer.send('force-update:quit'),
});

// ---- 画面心跳 ----
// 只有合成器还活着 rAF 才会被回调。GPU 进程掉了的话：渲染进程照常跑、照常响应、
// setTimeout 照常走，但 rAF 停 —— 主进程那两个崩溃事件都不触发，窗口就一直是
// backgroundColor 的深蓝。所以用 rAF 而不是 setInterval，换成定时器就测不出来了。
(function paintBeat() {
  let last = 0;
  function tick(ts) {
    if (ts - last > 2000) { last = ts; ipcRenderer.send('pms-desktop:paint-beat'); }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
