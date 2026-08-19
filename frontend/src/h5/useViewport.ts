/**
 * 🆕 让页面高度跟着**可视视口**走，而不是 `100dvh`。
 *
 * 为什么需要：APP 里打拼音时，**输入框被候选框盖住看不见**（用户实测反馈）。
 *
 * Android 的 `windowSoftInputMode=adjustResize` 只按**键盘**高度缩 WebView。
 * 而中文输入法的候选条（拼音候选、联想词）是 IME 在键盘之上又加的一层——
 * 它弹出时窗口尺寸往往**不再变**，于是 CSS 的 `100dvh` 还是老高度，
 * 底部的输入框正好落在候选条底下。
 *
 * `window.visualViewport` 报的是**真正看得见**的那块区域，IME 长高、缩回、
 * 页面被上推，它都会触发事件。拿它驱动高度，候选框弹出时输入框自然被顶上来。
 *
 * ⚠️ 取不到 `visualViewport` 就什么都不做——保持 CSS 的 `100dvh` 兜底，
 *    绝不能因为这个增强把布局搞崩（老 WebView 上没有这个 API）。
 */
import { onBeforeUnmount, onMounted, type Ref } from 'vue'

export function useViewportHeight(el: Ref<HTMLElement | undefined>,
                                  onResize?: () => void) {
  const vv = typeof window !== 'undefined' ? window.visualViewport : undefined
  let raf = 0

  function sync() {
    const node = el.value
    if (!node || !vv) return
    // ⚠️ 高度和偏移都要跟：有些机型 IME 弹出时不缩窗口而是把页面整体上推，
    //    只改高度的话顶部会被推出屏幕外。
    node.style.height = `${Math.round(vv.height)}px`
    const top = Math.round(vv.offsetTop)
    node.style.transform = top ? `translateY(${top}px)` : ''
    onResize?.()
  }

  function schedule() {
    // IME 长高的过程里事件会连发好几次，合并到一帧里做，别每次都改样式
    if (raf) cancelAnimationFrame(raf)
    raf = requestAnimationFrame(sync)
  }

  onMounted(() => {
    if (!vv) return
    vv.addEventListener('resize', schedule)
    vv.addEventListener('scroll', schedule)
    sync()
  })

  onBeforeUnmount(() => {
    if (raf) cancelAnimationFrame(raf)
    if (!vv) return
    vv.removeEventListener('resize', schedule)
    vv.removeEventListener('scroll', schedule)
    // 还原：不还原的话路由切走之后，内联高度会留在下一个页面上
    const node = el.value
    if (node) {
      node.style.height = ''
      node.style.transform = ''
    }
  })
}
