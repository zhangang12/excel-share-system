import { reactive, onScopeDispose } from 'vue'

/**
 * Excel 式「填充柄」向下拖拽复制。
 *
 * 交互：在可编辑单元格右下角放一个小方块，按下后向下拖，经过的单元格高亮，
 * 松手把源单元格的值复制到这些单元格（只复制同值、只向下）。
 *
 * 组件侧职责：
 *  1) 给每个可填充单元格加 `:data-fill-row="$index"`（当前页内行号）和
 *     `:data-fill-col="<列标识>"`（DatasheetGrid 用 field.id，OverviewView 用列 label）。
 *  2) 手柄上绑 `@mousedown="beginFill(colId, $index, $event)"`。
 *  3) 单元格 class 上用 `isInRange(colId, $index)` 做拖拽高亮。
 *  4) 实现 onCommit(colId, startIdx, endIdx)：取 startIdx 行的源值，写入
 *     (startIdx, endIdx] 各行的同列。
 */
export interface DragFillState {
  active: boolean
  colId: string
  startIdx: number
  curIdx: number
}

export function useDragFill(
  onCommit: (colId: string, startIdx: number, endIdx: number) => void,
) {
  const state = reactive<DragFillState>({
    active: false, colId: '', startIdx: -1, curIdx: -1,
  })

  // 按鼠标纵坐标找当前所在行号（忽略横坐标，鼠标在格内任意位置都算）
  function rowIndexFromY(y: number, colId: string): number {
    let sel: string
    try { sel = `[data-fill-col="${CSS.escape(colId)}"][data-fill-row]` }
    catch { sel = '[data-fill-row]' }
    const cells = Array.from(document.querySelectorAll<HTMLElement>(sel))
    for (const c of cells) {
      const r = c.getBoundingClientRect()
      if (y >= r.top && y <= r.bottom) {
        const v = c.dataset.fillRow
        if (v != null) return parseInt(v)
      }
    }
    // 拖到表格上/下方空白：取 y 之上最靠下的一行（便于一路拖到底）
    let best = -1
    for (const c of cells) {
      const r = c.getBoundingClientRect()
      if (r.top <= y) {
        const idx = parseInt(c.dataset.fillRow || '-1')
        if (idx > best) best = idx
      }
    }
    return best
  }

  function onMove(e: MouseEvent) {
    if (!state.active) return
    const idx = rowIndexFromY(e.clientY, state.colId)
    state.curIdx = idx >= state.startIdx ? idx : state.startIdx  // 只向下
  }

  function cleanup() {
    state.active = false
    state.colId = ''
    state.startIdx = -1
    state.curIdx = -1
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
  }

  function onUp() {
    if (!state.active) return
    const { colId, startIdx, curIdx } = state
    cleanup()
    if (curIdx > startIdx) onCommit(colId, startIdx, curIdx)
  }

  function beginFill(colId: string, startIdx: number, e: MouseEvent) {
    e.preventDefault()
    e.stopPropagation()
    state.active = true
    state.colId = colId
    state.startIdx = startIdx
    state.curIdx = startIdx
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  function isInRange(colId: string, idx: number): boolean {
    return state.active && colId === state.colId
      && idx > state.startIdx && idx <= state.curIdx
  }

  onScopeDispose(cleanup)

  return { fillState: state, beginFill, isInRange }
}
