import { ref, onMounted, onUnmounted, nextTick, type Ref } from 'vue'

/**
 * 让 el-table 固定高度自适应视口：表格内部滚动，
 * 横向 / 纵向滚动条始终钉在视口内、随时可拖动。
 *
 * 用固定 height（非 max-height）—— 这样无论数据多少，
 * 表格底部的横向滚动条位置恒定、始终在窗口内。
 *
 * @param tableRef  el-table 组件实例的 ref（取 .$el 拿根 DOM）
 * @param reserve   表格下方需要预留的高度（分页器 + 底部留白），默认 80
 */
export function useTableHeight(tableRef: Ref<any>, reserve = 80) {
  const height = ref(400)

  function recompute() {
    const inst = tableRef.value
    const el: HTMLElement | undefined = inst?.$el ?? inst
    if (!el || typeof el.getBoundingClientRect !== 'function') return
    const top = el.getBoundingClientRect().top
    height.value = Math.max(200, Math.floor(window.innerHeight - top - reserve))
  }

  onMounted(async () => {
    await nextTick()
    recompute()
    window.addEventListener('resize', recompute)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', recompute)
  })

  return { height, recompute }
}
