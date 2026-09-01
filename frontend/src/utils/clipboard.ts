/** 复制到剪贴板 —— 两级兜底。
 *
 * ⚠️ 桌面客户端(Electron)的页面是从 **file://** 加载的（见 desktop/main.js 顶部注释），
 *    属于「非安全上下文」，`navigator.clipboard` 在那里可能整个是 undefined 或
 *    调用直接抛异常。只写 navigator.clipboard 的话，财务在客户端里点复制毫无反应，
 *    而网页版测试时又一切正常——最难查的那种。
 *    所以必须保留 `document.execCommand('copy')` 这条老路：它在 file:// 和 http:// 都能用。
 *    参考 LoginView.vue 复制设备 ID 的写法（那里已经踩过这个坑）。
 *
 * @returns 复制成功返回 true；两条路都失败返回 false，由调用方提示「请手动选中复制」。
 */
export async function copyText(text?: string | null): Promise<boolean> {
  const s = text == null ? '' : String(text)
  if (!s) return false
  // ① 桌面客户端：走 Electron 原生剪贴板（preload 暴露），无条件可用。
  //    客户端页面是 file://，下面两条路都可能失灵，所以它必须排第一。
  const dk = (window as any).pmsDesktop
  if (dk?.copyText) {
    try { if (dk.copyText(s)) return true } catch { /* 落到下面的通用路径 */ }
  }
  try {
    // ② 网页版安全上下文（https / localhost）走标准 API
    await navigator.clipboard.writeText(s)
    return true
  } catch {
    // ③ 兜底：老 API。要求真实用户手势（我们的调用都在 @click 里，满足）
    try {
      const ta = document.createElement('textarea')
      ta.value = s
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      ta.style.top = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch {
      return false
    }
  }
}
