<script setup lang="ts">
// 🆕 v3 M10 售后部：登记(物料清单必传)→主管审批→同步财务
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Check, Delete } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/utils/format'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import FilePicker from '@/components/FilePicker.vue'

interface Att { id: number; name: string }
interface Row {
  id: number; project_id: number; code: string; name: string
  problem: string; cost: number; status: string
  mat_file_id?: number | null; mat_file_name?: string | null
  created_by_name?: string | null; created_at: string
}
interface Stats { total: number; pending: number; approved_cost: number; total_cost: number }

const auth = useAuthStore()
const canReg = computed(() => auth.hasRole('as_worker', 'admin', 'manager'))
const canApprove = computed(() => auth.hasRole('as_lead', 'admin', 'manager'))
const isManager = computed(() => auth.hasRole('admin', 'manager'))

const loading = ref(false)
const rows = ref<Row[]>([])
const stats = ref<Stats>({ total: 0, pending: 0, approved_cost: 0, total_cost: 0 })

async function load() {
  loading.value = true
  try {
    const j = (await http.get<{ rows: Row[]; stats: Stats }>('/aftersales')).data
    rows.value = j.rows; stats.value = j.stats
  } finally { loading.value = false }
}
onMounted(load)

const STATUS_TXT: Record<string, string> = { pending: '待审批', approved: '已审批', rejected: '已驳回' }
const STATUS_TAG: Record<string, any> = { pending: 'warning', approved: 'success', rejected: 'danger' }
const STATUS_VARIANT: Record<string, 'warn' | 'success' | 'danger' | 'muted'> = { pending: 'warn', approved: 'success', rejected: 'danger' }

// 登记
const regVisible = ref(false)
const regForm = reactive({ project_id: undefined as number | undefined, problem: '', cost: 0, file: null as File | null })
const projOptions = ref<{ id: number; code: string; name: string }[]>([])
const submitting = ref(false)
async function openReg() {
  projOptions.value = (await http.get<{ id: number; code: string; name: string }[]>('/aftersales/projects')).data
  regForm.project_id = undefined; regForm.problem = ''; regForm.cost = 0; regForm.file = null
  regVisible.value = true
}
function pickFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) regForm.file = f
}
async function submitReg() {
  if (!regForm.project_id) { ElMessage.warning('请选择项目'); return }
  if (!regForm.problem.trim()) { ElMessage.warning('请填写售后问题'); return }
  if (!regForm.cost) { ElMessage.warning('请填写售后费用'); return }
  if (!regForm.file) { ElMessage.warning('请上传售后物料清单'); return }
  submitting.value = true
  try {
    const fd = new FormData()
    fd.append('project_id', String(regForm.project_id))
    fd.append('problem', regForm.problem)
    fd.append('cost', String(regForm.cost))
    fd.append('file', regForm.file)
    await http.post('/aftersales', fd)
    ElMessage.success('已登记，等待售后主管审批')
    regVisible.value = false
    await load()
  } finally { submitting.value = false }
}

const actingId = ref<number | null>(null)

async function deleteRow(r: Row) {
  try {
    await ElMessageBox.confirm(
      `确认删除「${r.code}」的售后记录（${r.problem.slice(0, 20)}）？物料清单附件将一并删除，此操作不可撤回。`,
      '删除售后记录', { type: 'warning', confirmButtonText: '确认删除', confirmButtonClass: 'el-button--danger' })
  } catch { return }
  try {
    await http.delete(`/aftersales/${r.id}`)
    ElMessage.success('售后记录已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function approve(r: Row, ok: boolean) {
  if (ok) {
    try {
      await ElMessageBox.confirm('通过后将自动把售后费用同步到财务部，确认通过？', '审批通过', { type: 'warning' })
    } catch { return }
    actingId.value = r.id
    try {
      await http.post(`/aftersales/${r.id}/approve`)
      ElMessage.success('已通过，售后费用已同步财务部')
    } finally { actingId.value = null }
  } else {
    // #97/#98 驳回收集原因并通知登记人
    let reason = ''
    try {
      const res = await ElMessageBox.prompt('请填写驳回原因（将通知登记人）：', '驳回售后', {
        inputType: 'textarea', confirmButtonText: '确认驳回', type: 'warning',
      })
      reason = res.value || ''
    } catch { return }
    actingId.value = r.id
    try {
      const fd = new FormData()
      fd.append('reason', reason)
      await http.post(`/aftersales/${r.id}/reject`, fd)
      ElMessage.success('已驳回')
    } finally { actingId.value = null }
  }
  await load()
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>售后部</h1>
        <div class="desc">按项目登记售后问题与费用 + 物料清单；售后主管审批后自动同步财务部</div>
      </div>
      <div class="spacer"></div>
      <el-button v-if="canReg" type="primary" :icon="Plus" @click="openReg">登记售后</el-button>
    </div>

    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-v">{{ stats.total }}</div><div class="kpi-l">售后记录</div></div>
      <div class="kpi" :class="stats.pending ? 'is-warn' : ''"><div class="kpi-v">{{ stats.pending }}</div><div class="kpi-l">待审批</div></div>
      <div class="kpi is-good"><div class="kpi-v">{{ fmtMoney(stats.approved_cost) }}</div><div class="kpi-l">已审批费用</div></div>
      <div class="kpi"><div class="kpi-v">{{ fmtMoney(stats.total_cost) }}</div><div class="kpi-l">累计售后费用</div></div>
    </div>

    <el-card shadow="never">
      <template #header>📋 售后登记台账</template>
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="50" />
        <el-table-column label="项目编号" width="110">
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" min-width="140" show-overflow-tooltip />
        <el-table-column prop="problem" label="售后问题" min-width="200" show-overflow-tooltip />
        <el-table-column label="售后费用" width="110" align="right">
          <template #default="{ row }">{{ fmtMoney(row.cost) }}</template>
        </el-table-column>
        <el-table-column label="售后物料清单" min-width="150">
          <template #default="{ row }">
            <el-button v-if="row.mat_file_id" size="small" link type="primary"
                       @click="downloadAttachment({ id: row.mat_file_id, name: row.mat_file_name || '物料清单' })">
              {{ row.mat_file_name }}
            </el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <StatusPill :text="STATUS_TXT[row.status]" :variant="STATUS_VARIANT[row.status] || 'muted'" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190">
          <template #default="{ row }">
            <div class="op-cell">
              <template v-if="row.status === 'pending' && canApprove">
                <el-button size="small" type="success" :icon="Check" :loading="actingId === row.id" @click="approve(row, true)">通过</el-button>
                <el-button size="small" :loading="actingId === row.id" @click="approve(row, false)">驳回</el-button>
              </template>
              <span v-else-if="row.status === 'approved'" class="muted small">已同步财务</span>
              <span v-else class="muted small">—</span>
              <el-tooltip v-if="isManager" content="删除此售后记录" placement="top">
                <el-button size="small" link type="danger" :icon="Delete" @click="deleteRow(row)" style="margin-left:4px" />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无售后记录" />
    </el-card>

    <el-dialog v-model="regVisible" title="🛎️ 登记售后" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px"
                title="选项目 + 填问题 + 填费用 + 上传物料清单，提交后待审批，售后主管通过后自动同步财务部" />
      <el-form label-position="top">
        <el-form-item label="项目" required>
          <el-select v-model="regForm.project_id" filterable placeholder="选择项目" style="width: 100%">
            <el-option v-for="p in projOptions" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="售后问题" required>
          <el-input v-model="regForm.problem" type="textarea" :rows="3" placeholder="描述客户反馈的售后问题与处理情况" />
        </el-form-item>
        <el-form-item label="售后费用(元)" required>
          <el-input-number v-model="regForm.cost" :min="0" :controls="false" style="width: 100%" />
        </el-form-item>
        <el-form-item label="售后物料清单（回传财务部）" required>
          <FilePicker v-model="regForm.file" accept=".xlsx,.xls,.pdf,.doc,.docx" placeholder="选择物料清单（Excel/PDF/Word）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="regVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitReg">提交（待审批）</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.kpi-grid { margin-bottom: 14px; }
.op-cell { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
</style>
