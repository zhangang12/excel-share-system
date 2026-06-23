<script setup lang="ts">
// 🆕 v3 M12 全流程工作流图：销售下单 → 并行(设计/电工/生产) → 下游(钣金/采购) → 物流发货
import { computed } from 'vue'
import type { Workflow } from '@/api/collab'

const props = defineProps<{ wf: Workflow }>()

const STATUS_CLS: Record<string, string> = {
  done: 'done', in_progress: 'doing', assigned: 'doing',
  pending_assign: 'wait', none: 'idle', voided: 'idle',
}
const STATUS_TXT: Record<string, string> = {
  done: '已完成', in_progress: '进行中', assigned: '待接单',
  pending_assign: '待分派', none: '未下单', voided: '已作废',
}

const shipCls = computed(() =>
  props.wf.ship_status === 'shipped' ? 'done' : (props.wf.can_ship ? 'doing' : 'wait'))
const shipTxt = computed(() =>
  props.wf.ship_status === 'shipped' ? '已发货' : (props.wf.can_ship ? '可发货' : '待齐'))
</script>

<template>
  <div class="wf">
    <!-- 销售下单 -->
    <div class="wf-node done">
      <div class="wf-h">💼 销售下单</div>
      <div class="wf-kv">销售：{{ wf.sales_name || '—' }}</div>
      <div class="wf-kv">签订：{{ wf.sign_date || '—' }}</div>
      <div class="wf-kv">交货：{{ wf.deliver_date || '—' }}</div>
    </div>
    <span class="wf-arr">→</span>

    <!-- 并行执行：三部门 -->
    <div class="wf-parallel">
      <div class="wf-parallel-label">⫶ 并行执行（销售派单 → 各部门负责人分派）</div>
      <div class="wf-parallel-nodes">
        <div v-for="d in wf.depts" :key="d.dept" class="wf-node" :class="STATUS_CLS[d.status] || 'idle'">
          <div class="wf-h">{{ d.name }} <span class="wf-pill">{{ STATUS_TXT[d.status] || d.status }}</span></div>
          <div class="wf-kv">负责人：{{ d.worker_name || '—' }}</div>
          <div class="wf-kv">预计完成：{{ d.due_date || '—' }}</div>
          <div class="wf-kv">实际完成：{{ d.done_date || '—' }}</div>
          <div class="wf-kv">效率：<span v-if="d.eff_pct != null" :class="d.eff_pct >= 100 ? 'good' : 'bad'">{{ d.eff_pct }}%</span><span v-else>—</span></div>
        </div>
      </div>
    </div>
    <span class="wf-arr">→</span>

    <!-- 下游 -->
    <div class="wf-node" :class="wf.sheetpkg_count ? 'done' : 'idle'">
      <div class="wf-h">🔧 钣金组</div>
      <div class="wf-kv">图纸包：{{ wf.sheetpkg_count }} 个</div>
    </div>
    <div class="wf-node" :class="wf.purchase_list_count ? 'done' : 'idle'">
      <div class="wf-h">🛒 采购部</div>
      <div class="wf-kv">采购清单：{{ wf.purchase_list_count }} 个</div>
    </div>
    <span class="wf-arr">→</span>

    <!-- 物流 -->
    <div class="wf-node" :class="shipCls">
      <div class="wf-h">🚚 物流发货 <span class="wf-pill">{{ shipTxt }}</span></div>
      <div class="wf-kv">仓库清单：{{ wf.ship_list_count }} 个</div>
      <div v-if="wf.gate_missing.length" class="wf-kv bad">待齐：{{ wf.gate_missing.join('、') }}</div>
    </div>

    <div class="wf-legend">
      <span><i class="lg done"></i>已完成</span>
      <span><i class="lg doing"></i>进行中</span>
      <span><i class="lg wait"></i>待处理</span>
      <span><i class="lg idle"></i>未开始/未下单</span>
      <span>⫶ 虚线框=并行节点 · →=串行依赖</span>
    </div>
  </div>
</template>

<style scoped>
.wf { display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap; padding: 8px 2px; }
.wf-node {
  border: 1px solid var(--el-border-color); border-radius: 10px;
  padding: 10px 12px; min-width: 150px; background: #fff;
}
.wf-node.done { background: #f0fdf4; border-color: #86efac; }
.wf-node.doing { background: #eff6ff; border-color: #93c5fd; }
.wf-node.wait { background: #fffbeb; border-color: #fcd34d; }
.wf-node.idle { background: #fff; opacity: .65; }
.wf-h { font-weight: 600; font-size: 13px; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.wf-pill { font-size: 11px; padding: 1px 7px; border-radius: 20px; background: rgba(0,0,0,.06); font-weight: 500; }
.wf-kv { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.wf-kv .good { color: #16a34a; font-weight: 600; }
.wf-kv .bad, .wf-kv.bad { color: #dc2626; }
.wf-arr { align-self: center; color: var(--el-text-color-secondary); font-size: 18px; }
.wf-parallel { border: 1px dashed var(--el-border-color); border-radius: 10px; padding: 8px; background: #fafafa; }
.wf-parallel-label { font-size: 11px; color: var(--el-text-color-secondary); margin-bottom: 6px; }
.wf-parallel-nodes { display: flex; gap: 8px; flex-wrap: wrap; }
.wf-legend { width: 100%; display: flex; gap: 16px; flex-wrap: wrap; margin-top: 6px; font-size: 11.5px; color: var(--el-text-color-secondary); align-items: center; }
.wf-legend .lg { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 4px; vertical-align: -1px; border: 1px solid; }
.wf-legend .lg.done { background: #f0fdf4; border-color: #86efac; }
.wf-legend .lg.doing { background: #eff6ff; border-color: #93c5fd; }
.wf-legend .lg.wait { background: #fffbeb; border-color: #fcd34d; }
.wf-legend .lg.idle { background: #fff; border-color: #ddd; }
</style>
