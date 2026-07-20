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

  // ---- 强制更新页专用最小 IPC ----
  forceUpdateNotes: info.forceNotes || '',
  onProgress: (cb) => ipcRenderer.on('force-update:progress', (_e, p) => cb(p)),
  onDownloaded: (cb) => ipcRenderer.on('force-update:downloaded', () => cb()),
  onUpdateError: (cb) => ipcRenderer.on('force-update:error', (_e, msg) => cb(msg)),
  triggerUpdate: () => ipcRenderer.send('force-update:trigger'),
  quitAndInstall: () => ipcRenderer.send('force-update:quit'),
});
