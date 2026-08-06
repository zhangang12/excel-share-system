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
interface CostItem {
  id?: number; name: string; amount: number
  invoice_file_id?: number | null; invoice_file_name?: string | null; note?: string | null
}
interface Row {
  id: number; project_id: number; kind: string; code: string; name: string
  problem: string; cost: number; status: string
  // 🆕 报销支腿：checking 待财务核对 / invoice_fix 发票退回待重传 / reimbursed 已安排报销
  pay_status?: string | null; pay_note?: string | null
  pay_by_name?: string | null; pay_at?: string | null
  items?: CostItem[]; missing_invoice?: number
  mat_file_id?: number | null; mat_file_name?: string | null
  created_by_name?: string | null; created_at: string
}
const PAY_TXT: Record<string, string> = {
  checking: '待财务核对', invoice_fix: '发票退回，待重传', reimbursed: '已安排报销',
}
interface Stats { total: number; pending: number; approved_cost: number; total_cost: number }
const KIND_TXT: Record<string, string> = { aftersales: '售后', install: '安装' }

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

// 登记（售后 / 安装 复用同一弹窗，kind 区分）
const regVisible = ref(false)
// #158：projectVal 可能是 number(系统项目 id) 或 string(以往项目名，allow-create 手输)
const regForm = reactive({
  projectVal: undefined as number | string | undefined,
  problem: '',
  // 🆕 费用改成清单：每行 费用项+金额+发票。总额由明细自动合计，不再手填——
  //    手填的总额跟发票必然对不上，财务核对时全是扯皮。
  items: [] as CostItem[],
  file: null as File | null,
  kind: 'aftersales',
})
const regTotal = computed(() =>
  regForm.items.reduce((s, it) => s + (Number(it.amount) || 0), 0))
function addCostRow() { regForm.items.push({ name: '', amount: 0, invoice_file_id: null }) }
function delCostRow(i: number) { regForm.items.splice(i, 1) }

// 发票单独先上传，拿到附件 id 再随清单提交（一行一张，跟金额对得上）
const invUploading = ref<number | null>(null)
async function pickInvoice(i: number, f: File | null) {
  if (!f) { regForm.items[i].invoice_file_id = null; regForm.items[i].invoice_file_name = null; return }
  invUploading.value = i
  try {
    const fd = new FormData()
    fd.append('file', f)
    fd.append('biz_type', 'aftersales_invoice')
    const { data } = await http.post<Att>('/attachments', fd)
    regForm.items[i].invoice_file_id = data.id
    regForm.items[i].invoice_file_name = data.name
  } catch { /* 全局拦截器已提示 */ } finally { invUploading.value = null }
}
const isInstall = computed(() => regForm.kind === 'install')
const projOptions = ref<{ id: number; code: string; name: string }[]>([])
const submitting = ref(false)
async function openReg(kind: 'aftersales' | 'install' = 'aftersales') {
  projOptions.value = (await http.get<{ id: number; code: string; name: string }[]>('/aftersales/projects')).data
  regForm.projectVal = undefined; regForm.problem = ''; regForm.file = null; regForm.kind = kind
  regForm.items = [{ name: '', amount: 0, invoice_file_id: null }]
  regVisible.value = true
}
async function submitReg() {
  const label = isInstall.value ? '安装' : '售后'
  if (regForm.projectVal === undefined || regForm.projectVal === '') { ElMessage.warning('请选择项目或填写以往项目名称'); return }
  if (!isInstall.value && !regForm.problem.trim()) { ElMessage.warning('请填写售后问题'); return }
  const rows_ = regForm.items.filter(it => it.name.trim() || Number(it.amount))
  if (!rows_.length) { ElMessage.warning(`请至少填一行${label}费用`); return }
  const bad = rows_.find(it => !it.name.trim())
  if (bad) { ElMessage.warning('有金额没填费用项名称'); return }
  if (regTotal.value <= 0) { ElMessage.warning('费用合计必须大于 0'); return }
  if (!regForm.file) { ElMessage.warning(isInstall.value ? '请上传安装清单' : '请上传售后物料清单'); return }
  submitting.value = true
  try {
    const fd = new FormData()
    // 数字=系统里的项目 id；字符串=以往项目名(系统里没有)
    if (typeof regForm.projectVal === 'number') fd.append('project_id', String(regForm.projectVal))
    else fd.append('project_name', String(regForm.projectVal).trim())
    fd.append('problem', regForm.problem)
    fd.append('items', JSON.stringify(rows_.map(it => ({
      name: it.name.trim(), amount: Number(it.amount) || 0,
      invoice_file_id: it.invoice_file_id || null, note: it.note || null,
    }))))
    fd.append('kind', regForm.kind)
    fd.append('file', regForm.file)
    await http.post('/aftersales', fd)
    ElMessage.success('已登记，等待售后主管审批')
    regVisible.value = false
    await load()
  } finally { submitting.value = false }
}

const actingId = ref<number | null>(null)
const canFinance = computed(() => auth.hasRole('finance', 'admin', 'manager'))

async function doReimburse(r: Row) {
  if (r.missing_invoice) {
    ElMessage.warning(`还有 ${r.missing_invoice} 行没传发票，请先退回让登记人补齐`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认「${r.code}」的报销明细与发票核对无误，安排报销 ${fmtMoney(r.cost)}？`,
      '安排报销', { confirmButtonText: '核对无误，安排报销' })
  } catch { return }
  actingId.value = r.id
  try {
    await http.post(`/aftersales/${r.id}/reimburse`, new FormData())
    ElMessage.success('已安排报销')
    await load()
  } finally { actingId.value = null }
}

async function doPayReject(r: Row) {
  let reason = ''
  try {
    const v = await ElMessageBox.prompt('发票哪里对不上？说清楚登记人才知道要改什么。', '发票退回', {
      confirmButtonText: '退回登记人', inputPlaceholder: '如：差旅那行发票抬头不对',
      inputValidator: (t: string) => (t || '').trim() ? true : '请填写退回原因',
    })
    reason = (v.value || '').trim()
  } catch { return }
  actingId.value = r.id
  try {
    const fd = new FormData(); fd.append('reason', reason)
    await http.post(`/aftersales/${r.id}/pay-reject`, fd)
    ElMessage.success('已退回登记人重传发票')
    await load()
  } finally { actingId.value = null }
}

// 重传发票：把原明细拉出来改（整份覆盖回去）
const fixVisible = ref(false)
const fixRow = ref<Row | null>(null)
const fixItems = ref<CostItem[]>([])
const fixTotal = computed(() => fixItems.value.reduce((s, it) => s + (Number(it.amount) || 0), 0))
function openFixInvoice(r: Row) {
  fixRow.value = r
  fixItems.value = (r.items || []).map(it => ({ ...it }))
  if (!fixItems.value.length) fixItems.value = [{ name: '', amount: 0, invoice_file_id: null }]
  fixVisible.value = true
}
async function pickFixInvoice(i: number, f: File | null) {
  if (!f) { fixItems.value[i].invoice_file_id = null; fixItems.value[i].invoice_file_name = null; return }
  const fd = new FormData()
  fd.append('file', f); fd.append('biz_type', 'aftersales_invoice')
  const { data } = await http.post<Att>('/attachments', fd)
  fixItems.value[i].invoice_file_id = data.id
  fixItems.value[i].invoice_file_name = data.name
}
async function submitFix() {
  if (!fixRow.value) return
  const rows_ = fixItems.value.filter(it => it.name.trim() || Number(it.amount))
  if (!rows_.length) { ElMessage.warning('费用清单不能为空'); return }
  if (rows_.some(it => !it.name.trim())) { ElMessage.warning('有金额没填费用项名称'); return }
  const miss = rows_.filter(it => !it.invoice_file_id).length
  if (miss) { ElMessage.warning(`还有 ${miss} 行没传发票，财务核对时还会被退回来`); return }
  submitting.value = true
  try {
    const fd = new FormData()
    fd.append('items', JSON.stringify(rows_.map(it => ({
      name: it.name.trim(), amount: Number(it.amount) || 0,
      invoice_file_id: it.invoice_file_id || null, note: it.note || null,
    }))))
    await http.post(`/aftersales/${fixRow.value.id}/resubmit-invoice`, fd)
    ElMessage.success('已重新提交财务核对')
    fixVisible.value = false
    await load()
  } finally { submitting.value = false }
}

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
        <div class="desc">按项目登记 安装/售后 费用 + 清单；售后主管审批后自动同步财务部</div>
      </div>
      <div class="spacer"></div>
      <el-button v-if="canReg" type="primary" :icon="Plus" @click="openReg('aftersales')">登记售后</el-button>
      <el-button v-if="canReg" type="success" :icon="Plus" @click="openReg('install')">登记安装</el-button>
    </div>

    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-v">{{ stats.total }}</div><div class="kpi-l">登记记录</div></div>
      <div class="kpi" :class="stats.pending ? 'is-warn' : ''"><div class="kpi-v">{{ stats.pending }}</div><div class="kpi-l">待审批</div></div>
      <div class="kpi is-good"><div class="kpi-v">{{ fmtMoney(stats.approved_cost) }}</div><div class="kpi-l">已审批费用</div></div>
      <div class="kpi"><div class="kpi-v">{{ fmtMoney(stats.total_cost) }}</div><div class="kpi-l">累计安装/售后费用</div></div>
    </div>

    <el-card shadow="never">
      <template #header>📋 安装/售后登记台账</template>
      <el-table show-overflow-tooltip :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="50" />
        <el-table-column label="类型" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.kind === 'install' ? 'success' : 'warning'" size="small" effect="light">{{ KIND_TXT[row.kind] || '售后' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="项目编号" width="110">
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="项目名称" min-width="140" show-overflow-tooltip />
        <el-table-column prop="problem" label="问题/说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="费用" width="110" align="right">
          <template #default="{ row }">{{ fmtMoney(row.cost) }}</template>
        </el-table-column>
        <el-table-column label="清单" min-width="150">
          <template #default="{ row }">
            <el-button v-if="row.mat_file_id" size="small" link type="primary"
                       @click="downloadAttachment({ id: row.mat_file_id, name: row.mat_file_name || '物料清单' })">
              {{ row.mat_file_name }}
            </el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130" align="center">
          <template #default="{ row }">
            <StatusPill :text="STATUS_TXT[row.status]" :variant="STATUS_VARIANT[row.status] || 'muted'" />
            <!-- 🆕 审批通过之后钱走到哪一步了。旧流程登记的没有这一段，不显示。 -->
            <div v-if="row.pay_status" class="small" style="margin-top:3px">
              <el-tag size="small" effect="plain"
                      :type="row.pay_status === 'reimbursed' ? 'success' : row.pay_status === 'invoice_fix' ? 'danger' : 'warning'">
                {{ PAY_TXT[row.pay_status] }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <div class="op-cell">
              <template v-if="row.status === 'pending' && canApprove">
                <el-button size="small" type="success" :icon="Check" :loading="actingId === row.id" @click="approve(row, true)">通过</el-button>
                <el-button size="small" :loading="actingId === row.id" @click="approve(row, false)">驳回</el-button>
              </template>
              <!-- 🆕 报销支腿的操作 -->
              <template v-else-if="row.status === 'approved' && row.pay_status === 'checking' && canFinance">
                <el-button size="small" type="success" :loading="actingId === row.id" @click="doReimburse(row)">核对无误，安排报销</el-button>
                <el-button size="small" type="danger" plain :loading="actingId === row.id" @click="doPayReject(row)">发票退回</el-button>
              </template>
              <template v-else-if="row.status === 'approved' && row.pay_status === 'invoice_fix' && canReg">
                <el-button size="small" type="primary" :loading="actingId === row.id" @click="openFixInvoice(row)">重传发票</el-button>
              </template>
              <span v-else-if="row.status === 'approved' && row.pay_status === 'checking'" class="muted small">待财务核对</span>
              <span v-else-if="row.status === 'approved' && row.pay_status === 'invoice_fix'" class="muted small">等登记人重传发票</span>
              <span v-else-if="row.status === 'approved' && row.pay_status === 'reimbursed'" class="muted small">已安排报销</span>
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

    <el-dialog v-model="regVisible" :title="isInstall ? '🔧 登记安装' : '🛎️ 登记售后'" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px"
                :title="isInstall
                  ? '选项目 + 逐行填安装费用并附发票 + 上传安装清单 → 售后主管审批 → 财务核对发票后安排报销'
                  : '选项目 + 填问题与解决办法 + 逐行填费用并附发票 + 上传物料清单 → 售后主管审批 → 财务核对发票后安排报销'" />
      <el-form label-position="top">
        <el-form-item label="项目" required>
          <el-select v-model="regForm.projectVal" filterable allow-create default-first-option
                     placeholder="选择项目；以往项目(系统里没有)可直接输入名称" style="width: 100%">
            <el-option v-for="p in projOptions" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="!isInstall" label="售后问题" required>
          <el-input v-model="regForm.problem" type="textarea" :rows="3" placeholder="描述客户反馈的售后问题与处理情况" />
        </el-form-item>
        <el-form-item v-else label="安装说明（选填）">
          <el-input v-model="regForm.problem" type="textarea" :rows="2" placeholder="安装内容 / 备注（选填）" />
        </el-form-item>
        <el-form-item :label="isInstall ? '安装费用清单' : '售后费用清单'" required>
          <div class="cost-list">
            <div v-for="(it, i) in regForm.items" :key="i" class="cost-row">
              <el-input v-model="it.name" placeholder="费用项，如 配件费/差旅/住宿" class="c-name" />
              <el-input-number v-model="it.amount" :controls="false" :precision="2"
                               placeholder="金额" class="c-amt" />
              <FilePicker :model-value="null" accept=".pdf,.jpg,.jpeg,.png,.ofd"
                          :placeholder="it.invoice_file_name || '发票'"
                          class="c-inv"
                          @update:model-value="(f: File | null) => pickInvoice(i, f)" />
              <el-button link type="danger" :icon="Delete" class="c-del"
                         :disabled="regForm.items.length <= 1" @click="delCostRow(i)" />
            </div>
            <div class="cost-foot">
              <el-button link type="primary" :icon="Plus" @click="addCostRow">添加一行</el-button>
              <span class="cost-total">合计 <b>{{ fmtMoney(regTotal) }}</b></span>
            </div>
            <div class="muted small">
              发票选填，但财务核对时必须齐——缺发票的行会被退回来重传。
              差旅、住宿这类也在这里加一行，不用再去 OA 报销。
            </div>
          </div>
        </el-form-item>
        <el-form-item :label="isInstall ? '安装清单（回传财务部）' : '售后物料清单（回传财务部）'" required>
          <FilePicker v-model="regForm.file" accept=".xlsx,.xls,.pdf,.doc,.docx" placeholder="选择清单（Excel/PDF/Word）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="regVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitReg">提交（待审批）</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 发票被财务退回后，登记人在这里改完再交回去（整份清单覆盖式替换） -->
    <el-dialog v-model="fixVisible" title="🧾 重传发票" width="560px">
      <el-alert v-if="fixRow?.pay_note" type="warning" :closable="false" style="margin-bottom:12px"
                :title="`财务退回原因：${fixRow.pay_note}`" />
      <div class="cost-list">
        <div v-for="(it, i) in fixItems" :key="i" class="cost-row">
          <el-input v-model="it.name" placeholder="费用项" class="c-name" />
          <el-input-number v-model="it.amount" :controls="false" :precision="2" class="c-amt" />
          <FilePicker :model-value="null" accept=".pdf,.jpg,.jpeg,.png,.ofd"
                      :placeholder="it.invoice_file_name || '发票'" class="c-inv"
                      @update:model-value="(f: File | null) => pickFixInvoice(i, f)" />
          <el-button link type="danger" :icon="Delete" class="c-del"
                     :disabled="fixItems.length <= 1" @click="fixItems.splice(i, 1)" />
        </div>
        <div class="cost-foot">
          <el-button link type="primary" :icon="Plus"
                     @click="fixItems.push({ name: '', amount: 0, invoice_file_id: null })">添加一行</el-button>
          <span class="cost-total">合计 <b>{{ fmtMoney(fixTotal) }}</b></span>
        </div>
        <div class="muted small">改完直接回到财务核对，不用售后主管再批一遍。</div>
      </div>
      <template #footer>
        <el-button @click="fixVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitFix">提交财务核对</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cost-list { width: 100%; }
.cost-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.cost-row .c-name { flex: 2 1 0; min-width: 110px; }
.cost-row .c-amt { flex: 1 1 0; min-width: 90px; }
.cost-row .c-inv { flex: 1.4 1 0; min-width: 100px; }
.cost-row .c-del { flex: none; }
.cost-foot { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.cost-total b { font-size: 15px; }
@media (max-width: 640px) {
  .cost-row { flex-wrap: wrap; }
  .cost-row .c-name { flex-basis: 100%; }
}
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.kpi-grid { margin-bottom: 14px; }
.op-cell { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
</style>
