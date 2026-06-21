<script setup lang="ts">
// 🆕 v3 M14 月度工作报表（仅管理层）
import { ref, computed, onMounted } from 'vue'
import { reportsApi, type MonthlyReport } from '@/api/reports'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtDate } from '@/utils/format'

const month = ref(new Date().toISOString().slice(0, 7))
const curYear = new Date().getFullYear()
const yearOptions = computed(() => [curYear - 1, curYear, curYear + 1].map(String))
const loading = ref(false)
const rep = ref<MonthlyReport | null>(null)

async function load() {
  loading.value = true
  try { rep.value = await reportsApi.monthly(month.value) }
  finally { loading.value = false }
}
onMounted(load)

function effClass(v?: number | null) { return v == null ? '' : (v <= 100 ? 'good' : 'bad') }
function barWidth(v?: number | null) { return v == null ? 8 : Math.max(Math.min(v, 200) / 2, 8) }
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>月度工作报表</h1>
        <div class="desc">全公司任务量 / 按时率 / 完成效率 / 逾期清单（仅管理层）</div>
      </div>
      <div class="spacer"></div>
      <el-select size="large" style="width:90px"
                 :model-value="month.slice(0,4)"
                 @update:model-value="(v: string) => { month = v + month.slice(4); load() }">
        <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
      </el-select>
      <el-date-picker v-model="month" type="month" value-format="YYYY-MM" @change="load" />
    </div>

    <div v-loading="loading">
      <div class="sec-title" v-if="rep">本月概览</div>
      <div class="kpi-grid" v-if="rep">
        <div class="kpi is-primary"><div class="kpi-v">{{ rep.total }}</div><div class="kpi-l">任务总数 · 当月下单 {{ rep.sales_order_count }}</div></div>
        <div class="kpi is-good"><div class="kpi-v">{{ rep.done }}</div><div class="kpi-l">已完成</div></div>
        <div class="kpi" :class="rep.overdue ? 'is-bad' : ''"><div class="kpi-v">{{ rep.overdue }}</div><div class="kpi-l">逾期任务</div></div>
        <div class="kpi"><div class="kpi-v">{{ rep.ontime_rate ?? '—' }}%</div><div class="kpi-l">按时率</div></div>
        <div class="kpi"><div class="kpi-v">{{ rep.avg_eff ?? '—' }}%</div><div class="kpi-l">平均效率（越低越好）</div></div>
      </div>

      <div class="sec-title" v-if="rep">部门概览</div>
      <el-row :gutter="14" v-if="rep">
        <el-col :span="8" v-for="d in rep.dept_cards" :key="d.dept">
          <el-card shadow="never" class="dc">
            <div class="dc-h"><span class="dc-dot"></span>{{ d.name }}</div>
            <div class="dc-row"><span>任务 / 完成</span><b>{{ d.total }} / {{ d.done }}</b></div>
            <div class="dc-row"><span>逾期</span><b :class="{ bad: d.over }">{{ d.over }}</b></div>
            <div class="dc-row"><span>按时率</span><b>{{ d.rate ?? '—' }}%</b></div>
            <div class="dc-row"><span>平均效率</span><b :class="effClass(d.avg_eff)">{{ d.avg_eff ?? '—' }}%</b></div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" style="margin-top:14px" v-if="rep">
        <template #header>📈 人均完成效率（实际÷预计 · 越低越好，绿=按时）</template>
        <div v-for="w in rep.workers.filter(x => x.avg_eff != null)" :key="w.dept + w.worker_name" class="bar-row">
          <span class="bl">{{ w.dept_name }} · {{ w.worker_name }}</span>
          <div class="bt"><div class="bf" :class="effClass(w.avg_eff)" :style="{ width: barWidth(w.avg_eff) + '%' }">{{ w.avg_eff }}%</div></div>
        </div>
        <EmptyHint v-if="!rep.workers.some(x => x.avg_eff != null)" text="本月暂无完成数据" size="sm" />
      </el-card>

      <el-card shadow="never" style="margin-top:14px" v-if="rep">
        <template #header>👤 人员明细</template>
        <el-table :data="rep.workers" size="small" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="dept_name" label="部门" width="90" />
          <el-table-column prop="worker_name" label="人员" width="100" />
          <el-table-column prop="total" label="任务数" width="80" />
          <el-table-column prop="done" label="完成" width="70" />
          <el-table-column prop="ontime" label="按时" width="70" />
          <el-table-column label="逾期" width="70"><template #default="{ row }"><span :class="{ bad: row.over }">{{ row.over }}</span></template></el-table-column>
          <el-table-column label="按时率" width="80"><template #default="{ row }">{{ row.rate ?? '—' }}%</template></el-table-column>
          <el-table-column label="平均效率" width="90"><template #default="{ row }"><span :class="effClass(row.avg_eff)">{{ row.avg_eff ?? '—' }}%</span></template></el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" style="margin-top:14px" v-if="rep">
        <template #header>⏰ 逾期任务清单（{{ rep.overdue_items.length }}）</template>
        <el-table v-if="rep.overdue_items.length" :data="rep.overdue_items" size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="dept_name" label="部门" width="90" />
          <el-table-column prop="worker_name" label="人员" width="100" />
          <el-table-column prop="code" label="项目编号" width="120" />
          <el-table-column prop="name" label="项目名称" min-width="160" show-overflow-tooltip />
          <el-table-column label="预计完成" width="110"><template #default="{ row }">{{ fmtDate(row.due_date) }}</template></el-table-column>
          <el-table-column label="实际完成" width="110"><template #default="{ row }">{{ fmtDate(row.done_date) }}</template></el-table-column>
          <el-table-column label="逾期" width="90"><template #default="{ row }"><StatusPill :text="`超 ${row.over_days} 天`" variant="danger" /></template></el-table-column>
          <el-table-column label="效率" width="80"><template #default="{ row }"><span class="bad">{{ row.eff ?? '—' }}%</span></template></el-table-column>
        </el-table>
        <EmptyHint v-else text="本月无逾期任务" size="sm" />
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.dc { margin-bottom: 14px; }
.dc-h { font-weight: 600; margin-bottom: 8px; display: flex; align-items: center; gap: 7px; }
.dc-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--primary, #2563eb); }
.dc-row { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; color: var(--el-text-color-secondary); }
.dc-row b { color: var(--el-text-color-primary); }
.bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.bl { width: 150px; font-size: 12.5px; flex-shrink: 0; }
.bt { flex: 1; background: var(--el-fill-color); border-radius: 6px; height: 20px; overflow: hidden; }
.bf { height: 100%; display: flex; align-items: center; justify-content: flex-end; padding-right: 6px; color: #fff; font-size: 11px; border-radius: 6px; }
.bf.good { background: linear-gradient(90deg, #34d399, var(--success)); }
.bf.bad { background: linear-gradient(90deg, #f87171, var(--danger)); }
.good { color: var(--success); font-weight: 600; }
.bad { color: var(--danger); font-weight: 600; }
</style>
