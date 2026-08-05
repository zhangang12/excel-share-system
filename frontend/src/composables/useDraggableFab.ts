/**
 * 悬浮球拖动 —— 反馈 #347（卢照坤）：「待办、反馈按键无法移动，会有遮挡」。
 *
 * 两个球写死在 `right:22px` 的右下角，正好压住表格最右边的「编辑/删除」操作列。
 * 表格是横向滚动的，用户没法把内容挪开，只能干瞪着。
 *
 * 做法：按住拖到任意位置，**位置记在 localStorage**，下次打开还在那儿 ——
 * 挪一次就一劳永逸，不是每次都要躲。
 *
 * ⚠️ 拖动与点击要分清：按下到松开位移小于 THRESHOLD 才算点击。
 *    不区分的话，用户想拖的时候会误触发弹窗。
 * ⚠️ 记的是**距右下角的距离**而不是 left/top：窗口大小一变，
 *    存 left/top 会让球跑到屏幕外面去。
 */
import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

const THRESHOLD = 4          // 位移小于这个像素算点击，不算拖
const MARGIN = 8             // 离屏幕边缘至少留这么多，别贴死

export interface DraggableFab {
  el: Ref<HTMLElement | null>
  /** 绑到按钮的 style 上 */
  style: Ref<Record<string, string>>
  /** 绑 @pointerdown */
  onPointerDown: (e: PointerEvent) => void
  /** 本次交互算不算点击（拖动后不触发） */
  wasDrag: Ref<boolean>
}

export function useDraggableFab(key: string, def: { right: number; bottom: number }): DraggableFab {
  const el = ref<HTMLElement | null>(null)
  const pos = ref({ ...def })
  const style = ref<Record<string, string>>({})
  const wasDrag = ref(false)

  /** 夹进可视范围。**只用于显示，绝不回写 pos** —— 见 apply 里的说明。 */
  const clamped = () => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    // ⚠️ 窗口尺寸可能是 0：Electron 最小化、渲染进程刚起来、页面在后台标签页。
    //    这时候夹出来的结果是「贴左上角 8px」，一夹就是全废。
    //    量不出尺寸就**不夹**，原样显示，等真有尺寸了 resize 会再来一次。
    if (vw <= 0 || vh <= 0) return { ...pos.value }
    const w = el.value?.offsetWidth || 120
    const h = el.value?.offsetHeight || 44
    return {
      right: Math.min(Math.max(pos.value.right, MARGIN), Math.max(MARGIN, vw - w - MARGIN)),
      bottom: Math.min(Math.max(pos.value.bottom, MARGIN), Math.max(MARGIN, vh - h - MARGIN)),
    }
  }

  // ⚠️ 记住的位置(pos)和显示的位置(style)必须分开。
  //    早先是直接把夹完的值写回 pos，结果：挂载那一瞬窗口还很窄
  //    (面板刚展开 / 客户端没最大化)，22 被夹成 8；窗口变大后再夹，
  //    读到的已经是 8 —— **永久回不去右下角**，用户自己摆好的位置
  //    也会被一次临时窄窗口抹掉。夹只作用于显示，pos 保持原样。
  const apply = () => {
    const c = clamped()
    style.value = { right: `${c.right}px`, bottom: `${c.bottom}px` }
  }

  const load = () => {
    try {
      const raw = localStorage.getItem(key)
      if (raw) {
        const p = JSON.parse(raw)
        if (typeof p?.right === 'number' && typeof p?.bottom === 'number') pos.value = p
      }
    } catch { /* 存坏了就用默认位置，不影响使用 */ }
    apply()
  }

  const save = () => {
    try { localStorage.setItem(key, JSON.stringify(pos.value)) } catch { /* 隐私模式写不了，无所谓 */ }
  }

  let startX = 0, startY = 0, startRight = 0, startBottom = 0, moved = 0, dragging = false

  const onMove = (e: PointerEvent) => {
    if (!dragging) return
    const dx = e.clientX - startX
    const dy = e.clientY - startY
    moved = Math.max(moved, Math.abs(dx) + Math.abs(dy))
    // 往左拖 → right 变大；往上拖 → bottom 变大
    pos.value.right = startRight - dx
    pos.value.bottom = startBottom - dy
    apply()
  }

  const onUp = () => {
    if (!dragging) return
    dragging = false
    wasDrag.value = moved > THRESHOLD
    if (wasDrag.value) {
      // 拖动是用户在**当前真实窗口尺寸**下做的动作，这时候把 pos 收进
      // 可视范围是安全的（拖出屏幕外的部分不该被记住）。
      pos.value = clamped()
      apply()
      save()
    }
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  }

  const onPointerDown = (e: PointerEvent) => {
    if (e.button !== 0) return           // 只认左键，右键留给上下文菜单
    dragging = true; moved = 0; wasDrag.value = false
    startX = e.clientX; startY = e.clientY
    startRight = pos.value.right; startBottom = pos.value.bottom
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  const onResize = () => { apply() }

  onMounted(() => { load(); window.addEventListener('resize', onResize) })
  onBeforeUnmount(() => {
    window.removeEventListener('resize', onResize)
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
  })

  return { el, style, onPointerDown, wasDrag }
}
