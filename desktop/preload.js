// ============================================================
// preload：contextBridge 向页面注入桌面端标识 + 强制更新页的最小 IPC
//   - window.pmsDesktop.isDesktop / version / deviceId：前端 axios 读取加统计请求头
//   - onProgress / onDownloaded / onUpdateError / triggerUpdate / quitAndInstall：
//     仅供强制更新页（renderer/force-update.html）使用
// contextIsolation: true、nodeIntegration: false（在 main.js webPreferences 里设置）
// ============================================================
const { contextBridge, ipcRenderer } = require('electron');

// 同步拿主进程的版本号/设备ID（preload 阶段同步注入，前端 axios 初始化时就能读到）
const info = ipcRenderer.sendSync('pms-desktop:info') || {};

contextBridge.exposeInMainWorld('pmsDesktop', {
  isDesktop: true,
  version: info.version || '',
  deviceId: info.deviceId || '',

  // 前端 Vue 挂载完成后调用：主进程收到后关启动页、亮主窗口
  notifyReady: () => ipcRenderer.send('pms-desktop:app-ready'),

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
