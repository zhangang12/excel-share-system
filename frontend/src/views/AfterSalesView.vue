<script setup lang="ts">
// 🆕 v3 M10 售后部：登记(物料清单必传)→主管审批→同步财务
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/api/sales'

interface Att { id: number; name: string }
interface Row {
  id: number; project_id: number; code: string; name: string
  problem: string; cost: number; status: string
  mat_file_id?: number | null; mat_file_name?: string | null
  created_by_name?: string | null; created_at: string
}
interface Stats { total: number; pending: number; approved_cost: number; total_cost: number }

const auth = useAuthStore()
const canReg = computed(() => ['as_worker', 'admin', 'manager'].includes(auth.user?.role_code || ''))
const canApprove = computed(() => ['as_lead', 'admin', 'manager'].includes(auth.user?.role_code || ''))

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

async function approve(r: Row, ok: boolean) {
  if (ok) {
    await http.post(`/aftersales/${r.id}/approve`)
    ElMessage.success('已通过，售后费用已同步财务部')
  } else {
    // #97/#98 驳回收集原因并通知登记人
    let reason = ''
    try {
      const res = await ElMessageBox.prompt('请填写驳回原因（将通知登记人）：', '驳回售后', {
        inputType: 'textarea', confirmButtonText: '确认驳回', type: 'warning',
      })
      reason = res.value || ''
    } catch { return }
    const fd = new FormData()
    fd.append('reason', reason)
    await http.post(`/aftersales/${r.id}/reject`, fd)
    ElMessage.success('已驳回')
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

    <div class="stat-row">
      <div class="stat-card"><div class="v">{{ stats.total }}</div><div class="l">售后记录</div></div>
      <div class="stat-card"><div class="v warn">{{ stats.pending }}</div><div class="l">待审批</div></div>
      <div class="stat-card"><div class="v">{{ fmtMoney(stats.approved_cost) }}</div><div class="l">已审批费用</div></div>
      <div class="stat-card"><div class="v">{{ fmtMoney(stats.total_cost) }}</div><div class="l">累计售后费用</div></div>
    </div>

    <el-card shadow="never">
      <template #header>📋 售后登记台账</template>
      <el-table :data="rows" stripe v-loading="loading">
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
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="STATUS_TAG[row.status]">{{ STATUS_TXT[row.status] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <template v-if="row.status === 'pending' && canApprove">
              <el-button size="small" type="success" @click="approve(row, true)">✓ 通过</el-button>
              <el-button size="small" @click="approve(row, false)">驳回</el-button>
            </template>
            <span v-else-if="row.status === 'approved'" class="muted small">已同步财务</span>
            <span v-else class="muted small">—</span>
          </template>
        </el-table-column>
      </el-table>
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
          <input type="file" accept=".xlsx,.xls,.pdf,.doc,.docx" @change="pickFile" />
          <span v-if="regForm.file" class="muted small" style="margin-left: 8px">{{ regForm.file.name }}</span>
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
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 14px; }
.stat-card { background: var(--el-fill-color-light); border-radius: 10px; padding: 16px; }
.stat-card .v { font-size: 24px; font-weight: 600; }
.stat-card .v.warn { color: #d97706; }
.stat-card .l { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 4px; }
</style>
