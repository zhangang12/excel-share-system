<script setup lang="ts">
// 🆕 OA 审批：部门字典 + 可配置多级审批链 + 业务/报销/采购三类共8种申请单。
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Check, Close, Download, Upload, RefreshLeft } from '@element-plus/icons-vue'
import { http } from '@/api'
import { oaApi, type Department, type OaDocType, type OaApprovalStep, type OaRequest, type OaSummaryRow, type OaChainOverviewRow } from '@/api/oa'
import { adminApi } from '@/api/admin'
import { downloadAttachment } from '@/api/orders'
import { useAuthStore } from '@/stores/auth'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtDateTime } from '@/utils/format'

const auth = useAuthStore()
const canConfig = computed(() => auth.isAdmin)               // 部门/审批流程设置：仅 admin/manager
const canViewAll = computed(() => auth.isAdmin)               // 全部申请：仅 admin/manager
const canViewSummary = computed(() => auth.isAdmin || auth.hasRole('finance'))

// ===== 基础字典：部门 / 单据类型 / 角色 =====
const departments = ref<Department[]>([])
const docTypes = ref<OaDocType[]>([])
const rolesList = ref<{ id: number; code: string; name: string }[]>([])
async function loadDepartments() { departments.value = await oaApi.departments() }
async function loadDocTypes() { docTypes.value = await oaApi.docTypes() }
async function loadRoles() { try { rolesList.value = await adminApi.listRoles() } catch { rolesList.value = [] } }
const roleName = (code?: string | null) => rolesList.value.find(r => r.code === code)?.name || code || '—'
const CATEGORY_OPTIONS = [
  { value: 'business', label: '业务申请' },
  { value: 'reimbursement', label: '报销申请' },
  { value: 'purchase', label: '采购申请' },
]
const categoryLabel = (c: string) => CATEGORY_OPTIONS.find(x => x.value === c)?.label || c
// 提交表单/审批流程配置的下拉只给启用的单据类型；停用的仍保留在历史记录里正常显示（走 docLabel）
const docTypesByCategory = computed(() => {
  const m: Record<string, OaDocType[]> = {}
  for (const d of docTypes.value.filter(x => x.enabled)) { (m[d.category] ||= []).push(d) }
  return m
})
const docLabel = (key: string) => docTypes.value.find(d => d.key === key)?.label || key
const isDeptLead = computed(() => departments.value.some(d => d.lead_role && auth.hasRole(d.lead_role)))

// ===== 列表 tab =====
const mainTab = ref('mine')
type Scope = 'mine' | 'pending_me' | 'cc_me' | 'dept' | 'all'
const activeTab = ref<Scope>('mine')
const rows = ref<OaRequest[]>([])
const listLoading = ref(false)
async function loadList() {
  listLoading.value = true
  try { rows.value = await oaApi.listRequests({ scope: activeTab.value }) }
  finally { listLoading.value = false }
}
function onTabChange(name: string | number) {
  if (['mine', 'pending_me', 'cc_me', 'dept', 'all'].includes(String(name))) {
    activeTab.value = name as Scope
    loadList()
  }
}

const STATUS_VARIANT: Record<string, 'success' | 'warn' | 'info' | 'danger' | 'muted'> = {
  pending: 'warn', pending_payment: 'info', approved: 'success', rejected: 'danger', withdrawn: 'muted',
}
const STATUS_TEXT: Record<string, string> = {
  pending: '审批中', pending_payment: '待付款', approved: '已通过', rejected: '已驳回', withdrawn: '已撤回',
}
// 🆕 详情里 detail(JSON) 各业务字段的中文标签，没收录的兜底显示原始 key
const DETAIL_FIELD_LABELS: Record<string, string> = {
  destination: '目的地/对象', start_date: '开始日期', end_date: '结束日期', notes: '事由/备注',
  transport: '交通方式', items: '物品清单', purpose: '用途',
}
function detailFieldLabel(k: string): string { return DETAIL_FIELD_LABELS[k] || k }
function curStepLabel(r: OaRequest): string {
  if (r.status !== 'pending') return '—'
  return r.steps.find(s => s.step_order === r.current_step_order)?.step_label || '—'
}
function fmtMoney(n?: number | null): string {
  return n == null ? '—' : `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

// ===== 新建申请 =====
const subVisible = ref(false)
const subSaving = ref(false)
interface ExpenseItem { category: string; note: string; amount: number | null; invoice_file_id: number | null; invoice_file_name: string }
const subForm = reactive({
  doc_type: '', department_id: '' as number | '', title: '', amount: null as number | null,
  related_request_id: null as number | '' | null,
  d_destination: '', d_start_date: '', d_end_date: '', d_notes: '', d_items: '', d_purpose: '', d_transport: '',
  expense_items: [] as ExpenseItem[],   // 🆕 #149 报销费用明细
  cc_user_ids: [] as number[],   // 🆕 抄送人
})
// 🆕 #149：报销费用明细
const EXPENSE_CATS = ['交通费', '住宿费', '餐饮费', '办公用品', '业务招待', '通讯费', '其他']
const expenseTotal = computed(() => subForm.expense_items.reduce((s, r) => s + (Number(r.amount) || 0), 0))
function addExpenseRow() { subForm.expense_items.push({ category: '', note: '', amount: null, invoice_file_id: null, invoice_file_name: '' }) }
function delExpenseRow(i: number) { subForm.expense_items.splice(i, 1) }
function uploadExpenseInvoice(row: ExpenseItem) {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.pdf,.jpg,.jpeg,.png,.webp,.ofd'
  input.onchange = async () => {
    const f = input.files?.[0]; if (!f) return
    const fd = new FormData()
    fd.append('file', f); fd.append('biz_type', 'oa_request'); fd.append('kind', 'expense_invoice')
    try {
      const r = await http.post<{ id: number; name: string }>('/attachments', fd)
      row.invoice_file_id = r.data.id; row.invoice_file_name = r.data.name
      ElMessage.success('发票已上传')
    } catch { /* 全局拦截器已提示 */ }
  }
  input.click()
}
const subDocType = computed(() => docTypes.value.find(d => d.key === subForm.doc_type))
const subCategory = computed(() => subDocType.value?.category || '')
const showBusinessFields = computed(() => subCategory.value === 'business')
const showReimburseFields = computed(() => subCategory.value === 'reimbursement')
const showPurchaseFields = computed(() => subCategory.value === 'purchase')
const showRelatedTrip = computed(() => subForm.doc_type === 'travel_expense')
const showTripFields = computed(() => subForm.doc_type === 'trip')   // 🆕 出差申请专属：交通方式
const TRANSPORT_OPTIONS = ['高铁', '飞机', '火车', '私车公用', '公车']
const myApprovedTrips = ref<OaRequest[]>([])
async function loadMyApprovedTrips() {
  try { myApprovedTrips.value = await oaApi.listRequests({ scope: 'mine', doc_type: 'trip', status: 'approved' }) }
  catch { myApprovedTrips.value = [] }
}
// 🆕 抄送人可选名单（在职用户）
const ccCandidates = ref<{ id: number; name: string }[]>([])
async function loadCcCandidates() {
  try { ccCandidates.value = await oaApi.ccCandidates() } catch { ccCandidates.value = [] }
}
function resetSubForm() {
  Object.assign(subForm, {
    doc_type: '', department_id: '', title: '', amount: null, related_request_id: null,
    d_destination: '', d_start_date: '', d_end_date: '', d_notes: '', d_items: '', d_purpose: '', d_transport: '',
    expense_items: [],
    cc_user_ids: [],
  })
}
function openSubmit() {
  resetSubForm()
  subVisible.value = true
  loadMyApprovedTrips()
  loadCcCandidates()
}
async function submitNew() {
  if (!subForm.doc_type) { ElMessage.warning('请选择单据类型'); return }
  if (!subForm.department_id) { ElMessage.warning('请选择部门'); return }
  let detail: Record<string, any> = {}
  if (showBusinessFields.value) {
    detail = { destination: subForm.d_destination, start_date: subForm.d_start_date, end_date: subForm.d_end_date, notes: subForm.d_notes }
    if (showTripFields.value) detail.transport = subForm.d_transport
  } else if (showReimburseFields.value) {
    // 🆕 #149：报销费用明细（逐行金额 + 发票），报销金额=各行合计
    const items = subForm.expense_items.filter(r => (Number(r.amount) || 0) > 0 || r.invoice_file_id)
    if (!items.length) { ElMessage.warning('请至少添加一条费用明细'); return }
    detail = {
      notes: subForm.d_notes,
      expense_items: items.map(r => ({
        category: r.category || '', note: r.note || '', amount: Number(r.amount) || 0,
        invoice_file_id: r.invoice_file_id, invoice_file_name: r.invoice_file_name || '',
      })),
    }
  } else if (showPurchaseFields.value) {
    detail = { items: subForm.d_items, purpose: subForm.d_purpose }
  }
  subSaving.value = true
  try {
    const r = await oaApi.createRequest({
      category: subCategory.value, doc_type: subForm.doc_type, department_id: subForm.department_id as number,
      title: subForm.title || undefined,
      amount: showReimburseFields.value ? expenseTotal.value : subForm.amount,
      detail, related_request_id: showRelatedTrip.value && subForm.related_request_id ? (subForm.related_request_id as number) : null,
      cc_user_ids: subForm.cc_user_ids,
    })
    ElMessage.success(`已提交 ${r.request_no}`)
    subVisible.value = false
    if (activeTab.value === 'mine') await loadList()
  } catch { /* 全局拦截器已提示 */ }
  finally { subSaving.value = false }
}

// ===== 详情抽屉（审批/驳回/撤回/附件） =====
const detailVisible = ref(false)
const detailReq = ref<OaRequest | null>(null)
const detailLoading = ref(false)
const attachments = ref<{ id: number; name: string }[]>([])
async function loadAttachments(rid: number) {
  try { attachments.value = (await http.get('/attachments', { params: { biz_type: 'oa_request', biz_id: rid } })).data }
  catch { attachments.value = [] }
}
async function openDetail(id: number) {
  detailVisible.value = true
  detailLoading.value = true
  try {
    detailReq.value = await oaApi.getRequest(id)
    await loadAttachments(id)
  } finally { detailLoading.value = false }
}
async function refreshDetail() {
  if (!detailReq.value) return
  detailReq.value = await oaApi.getRequest(detailReq.value.id)
}
function uploadAttachment() {
  if (!detailReq.value) return
  const rid = detailReq.value.id
  const input = document.createElement('input')
  input.type = 'file'
  input.onchange = async () => {
    const f = input.files?.[0]
    if (!f) return
    const fd = new FormData()
    fd.append('file', f); fd.append('biz_type', 'oa_request'); fd.append('biz_id', String(rid))
    try { await http.post('/attachments', fd); ElMessage.success('已上传'); await loadAttachments(rid) }
    catch { /* 全局拦截器已提示 */ }
  }
  input.click()
}

const approveNote = ref('')
const approveSettle = ref<number | null>(null)
const approving = ref(false)
async function doApprove() {
  if (!detailReq.value) return
  approving.value = true
  try {
    await oaApi.approve(detailReq.value.id, { note: approveNote.value || undefined, settle_amount: approveSettle.value })
    ElMessage.success('已审批通过')
    approveNote.value = ''; approveSettle.value = null
    await refreshDetail(); await loadList()
  } catch { /* 全局拦截器已提示 */ }
  finally { approving.value = false }
}
const rejecting = ref(false)
async function doReject() {
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt('请填写驳回原因', '驳回申请', {
      confirmButtonText: '驳回', cancelButtonText: '取消', inputType: 'textarea',
      inputValidator: (v: string) => (v && v.trim().length > 0) || '驳回原因不能为空',
    })
    reason = value
  } catch { return }
  rejecting.value = true
  try {
    await oaApi.reject(detailReq.value!.id, reason)
    ElMessage.success('已驳回')
    await refreshDetail(); await loadList()
  } catch { /* 全局拦截器已提示 */ }
  finally { rejecting.value = false }
}
async function doWithdraw() {
  if (!detailReq.value) return
  try { await ElMessageBox.confirm('确认撤回该申请？', '撤回申请', { type: 'warning' }) } catch { return }
  try {
    await oaApi.withdraw(detailReq.value.id)
    ElMessage.success('已撤回')
    await refreshDetail(); await loadList()
  } catch { /* 全局拦截器已提示 */ }
}
const markingPaid = ref(false)
async function doMarkPaid() {
  if (!detailReq.value) return
  try { await ElMessageBox.confirm('确认这笔申请的款项已经付出去了？', '标记已付款', { type: 'warning' }) } catch { return }
  markingPaid.value = true
  try {
    await oaApi.markPaid(detailReq.value.id)
    ElMessage.success('已标记为已付款')
    await refreshDetail(); await loadList()
  } catch { /* 全局拦截器已提示 */ }
  finally { markingPaid.value = false }
}

// ===== 设置：部门字典 =====
const settingsTab = ref<'dept' | 'doctype' | 'chain'>('dept')
const deptEditId = ref<number | null>(null)
const deptForm = reactive({ name: '', lead_role: '' as string | '', sort_order: 0, enabled: true })
function deptResetForm() { deptEditId.value = null; Object.assign(deptForm, { name: '', lead_role: '', sort_order: 0, enabled: true }) }
function deptEdit(d: Department) {
  deptEditId.value = d.id
  Object.assign(deptForm, { name: d.name, lead_role: d.lead_role || '', sort_order: d.sort_order, enabled: d.enabled })
}
const deptSaving = ref(false)
async function deptSave() {
  if (!deptForm.name.trim()) { ElMessage.warning('请填写部门名称'); return }
  const payload = { name: deptForm.name.trim(), lead_role: deptForm.lead_role || null, sort_order: deptForm.sort_order, enabled: deptForm.enabled }
  deptSaving.value = true
  try {
    if (deptEditId.value) { await oaApi.updateDepartment(deptEditId.value, payload); ElMessage.success('已更新') }
    else { await oaApi.createDepartment(payload); ElMessage.success('已新增部门') }
    deptResetForm(); await loadDepartments()
  } catch { /* 全局拦截器已提示 */ } finally { deptSaving.value = false }
}
async function deptDelete(d: Department) {
  try { await ElMessageBox.confirm(`删除部门「${d.name}」？已有申请记录会拦截删除，可改为停用。`, '删除部门', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await oaApi.deleteDepartment(d.id); ElMessage.success('已删除'); await loadDepartments() } catch { /* 全局拦截器已提示 */ }
}

// ===== 设置：单据类型字典 =====
const dtEditId = ref<number | null>(null)
const dtForm = reactive({ key: '', category: 'business', label: '', sort_order: 0, enabled: true })
function dtResetForm() { dtEditId.value = null; Object.assign(dtForm, { key: '', category: 'business', label: '', sort_order: 0, enabled: true }) }
function dtEdit(d: OaDocType) {
  dtEditId.value = d.id
  Object.assign(dtForm, { key: d.key, category: d.category, label: d.label, sort_order: d.sort_order, enabled: d.enabled })
}
const dtSaving = ref(false)
async function dtSave() {
  if (!dtEditId.value && !/^[a-zA-Z0-9_]+$/.test(dtForm.key.trim())) { ElMessage.warning('标识只能是英文字母/数字/下划线'); return }
  if (!dtForm.label.trim()) { ElMessage.warning('请填写展示名称'); return }
  const payload = { key: dtForm.key.trim(), category: dtForm.category, label: dtForm.label.trim(), sort_order: dtForm.sort_order, enabled: dtForm.enabled }
  dtSaving.value = true
  try {
    if (dtEditId.value) { await oaApi.updateDocType(dtEditId.value, payload); ElMessage.success('已更新') }
    else { await oaApi.createDocType(payload); ElMessage.success('已新增单据类型') }
    dtResetForm(); await loadDocTypes()
  } catch { /* 全局拦截器已提示 */ } finally { dtSaving.value = false }
}
async function dtDelete(d: OaDocType) {
  try { await ElMessageBox.confirm(`删除单据类型「${d.label}」？已有申请或审批流程配置会拦截删除，可改为停用。`, '删除单据类型', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await oaApi.deleteDocType(d.id); ElMessage.success('已删除'); await loadDocTypes() } catch { /* 全局拦截器已提示 */ }
}

// ===== 设置：审批流程配置 =====
const chainDeptId = ref<number | ''>('')
const chainDocType = ref('')
const chainSteps = ref<OaApprovalStep[]>([])
const chainLoading = ref(false)
async function loadChainSteps() {
  if (!chainDeptId.value || !chainDocType.value) { chainSteps.value = []; return }
  chainLoading.value = true
  try { chainSteps.value = await oaApi.chainSteps(chainDeptId.value as number, chainDocType.value) }
  finally { chainLoading.value = false }
}
watch([chainDeptId, chainDocType], loadChainSteps)
// 🆕 已配置流程一览
const chainOverview = ref<OaChainOverviewRow[]>([])
async function loadChainOverview() {
  try { chainOverview.value = await oaApi.chainsOverview() } catch { chainOverview.value = [] }
}
function loadChainForEdit(row: OaChainOverviewRow) {
  chainDeptId.value = row.department_id
  chainDocType.value = row.doc_type
}
const stepEditId = ref<number | null>(null)
const stepForm = reactive({ step_order: 1, approver_role: '', step_label: '', enabled: true })
function stepResetForm() {
  stepEditId.value = null
  Object.assign(stepForm, { step_order: (chainSteps.value[chainSteps.value.length - 1]?.step_order || 0) + 1, approver_role: '', step_label: '', enabled: true })
}
function stepEdit(s: OaApprovalStep) {
  stepEditId.value = s.id
  Object.assign(stepForm, { step_order: s.step_order, approver_role: s.approver_role, step_label: s.step_label || '', enabled: s.enabled })
}
const stepSaving = ref(false)
async function stepSave() {
  if (!chainDeptId.value || !chainDocType.value) { ElMessage.warning('请先选择部门和单据类型'); return }
  if (!stepForm.approver_role) { ElMessage.warning('请选择审批角色'); return }
  const payload = {
    department_id: chainDeptId.value as number, doc_type: chainDocType.value,
    step_order: stepForm.step_order, approver_role: stepForm.approver_role,
    step_label: stepForm.step_label || undefined, enabled: stepForm.enabled,
  }
  stepSaving.value = true
  try {
    if (stepEditId.value) { await oaApi.updateChainStep(stepEditId.value, payload); ElMessage.success('已更新') }
    else { await oaApi.createChainStep(payload); ElMessage.success('已新增步骤') }
    stepResetForm(); await loadChainSteps(); await loadChainOverview()
  } catch { /* 全局拦截器已提示 */ } finally { stepSaving.value = false }
}
async function stepDelete(s: OaApprovalStep) {
  try { await ElMessageBox.confirm(`删除步骤「${s.step_label}」？不影响已提交的历史申请。`, '删除步骤', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await oaApi.deleteChainStep(s.id); ElMessage.success('已删除'); await loadChainSteps(); await loadChainOverview() } catch { /* 全局拦截器已提示 */ }
}

// ===== 汇总报表 =====
const summaryRows = ref<OaSummaryRow[]>([])
const summaryLoading = ref(false)
async function loadSummary() {
  summaryLoading.value = true
  try { summaryRows.value = await oaApi.summary() }
  finally { summaryLoading.value = false }
}
const summaryTotal = computed(() => summaryRows.value.reduce((s, r) => s + r.amount, 0))

function onMainTabChange(name: string | number) {
  const n = String(name)
  if (n === 'settings' && canConfig.value) { loadDepartments(); loadRoles(); stepResetForm(); loadChainOverview() }
  else if (n === 'summary' && canViewSummary.value) { loadSummary() }
  else onTabChange(n)
}

onMounted(async () => {
  await Promise.all([loadDepartments(), loadDocTypes()])
  await loadList()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>OA审批</h2>
      <div class="desc">业务申请 · 报销申请 · 采购申请 —— 按部门+单据类型配置多级审批</div>
    </div>

    <el-tabs v-model="mainTab" type="border-card" @tab-change="onMainTabChange">
      <el-tab-pane label="我的申请" name="mine">
        <div class="toolbar">
          <el-button type="primary" :icon="Plus" @click="openSubmit">新建申请</el-button>
          <el-button :icon="RefreshLeft" @click="loadList">刷新</el-button>
        </div>
        <el-table :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="还没有提交过申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="待我审批" name="pending_me">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">处理</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="没有待你审批的申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="抄送我的" name="cc_me">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="没有抄送给你的申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="isDeptLead" label="部门审批" name="dept">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="本部门暂无申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canViewAll" label="全部申请" name="all">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="暂无申请记录" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canViewSummary" label="汇总报表" name="summary">
        <el-table :data="summaryRows" v-loading="summaryLoading" stripe size="small">
          <el-table-column prop="department_name" label="部门" width="140" />
          <el-table-column label="单据类型" width="140"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="count" label="已批件数" width="100" align="right" />
          <el-table-column label="金额合计" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <template #empty><EmptyHint text="暂无已批准的申请数据" /></template>
        </el-table>
        <div class="summary-bar" v-if="summaryRows.length">合计 <b>{{ fmtMoney(summaryTotal) }}</b></div>
      </el-tab-pane>

      <el-tab-pane v-if="canConfig" label="部门与流程设置" name="settings">
        <el-radio-group v-model="settingsTab" style="margin-bottom:14px">
          <el-radio-button value="dept">部门管理</el-radio-button>
          <el-radio-button value="doctype">单据类型管理</el-radio-button>
          <el-radio-button value="chain">审批流程配置</el-radio-button>
        </el-radio-group>

        <div v-if="settingsTab === 'dept'">
          <el-table :data="departments" size="small" border stripe max-height="34vh">
            <el-table-column type="index" label="#" width="46" align="center" />
            <el-table-column prop="name" label="部门名称" min-width="120" />
            <el-table-column label="部门负责人角色" min-width="150"><template #default="{ row }">{{ roleName(row.lead_role) }}</template></el-table-column>
            <el-table-column label="排序" width="70" prop="sort_order" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="deptEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="deptDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="form-section-title" style="margin-top:16px">{{ deptEditId ? '编辑部门' : '新增部门' }}</div>
          <el-form :model="deptForm" label-position="top">
            <el-row :gutter="16">
              <el-col :xs="24" :sm="8"><el-form-item label="部门名称 *"><el-input v-model="deptForm.name" /></el-form-item></el-col>
              <el-col :xs="24" :sm="8">
                <el-form-item label="部门负责人角色（可看本部门全部申请）">
                  <el-select v-model="deptForm.lead_role" clearable filterable style="width:100%" placeholder="不设置">
                    <el-option v-for="r in rolesList" :key="r.code" :label="r.name" :value="r.code" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="12" :sm="4"><el-form-item label="排序"><el-input-number v-model="deptForm.sort_order" :controls="false" style="width:100%" /></el-form-item></el-col>
              <el-col :xs="12" :sm="4"><el-form-item label="启用"><el-switch v-model="deptForm.enabled" /></el-form-item></el-col>
            </el-row>
          </el-form>
          <div style="display:flex;gap:10px">
            <el-button v-if="deptEditId" @click="deptResetForm">取消编辑</el-button>
            <el-button type="primary" :loading="deptSaving" @click="deptSave">{{ deptEditId ? '保存修改' : '新增部门' }}</el-button>
          </div>
        </div>

        <div v-else-if="settingsTab === 'doctype'">
          <el-alert type="info" :closable="false" style="margin-bottom:14px"
            title="维护业务/报销/采购三大类下具体的单据类型。标识（key）创建后不可修改；停用后不再出现在新建申请/审批流程配置的下拉里，但历史记录正常显示。" />
          <el-table :data="docTypes" size="small" border stripe max-height="34vh">
            <el-table-column type="index" label="#" width="46" align="center" />
            <el-table-column label="所属大类" width="110"><template #default="{ row }">{{ categoryLabel(row.category) }}</template></el-table-column>
            <el-table-column prop="label" label="展示名称" min-width="120" />
            <el-table-column prop="key" label="标识（key）" min-width="120" />
            <el-table-column label="排序" width="70" prop="sort_order" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="dtEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="dtDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="form-section-title" style="margin-top:16px">{{ dtEditId ? '编辑单据类型' : '新增单据类型' }}</div>
          <el-form :model="dtForm" label-position="top">
            <el-row :gutter="16">
              <el-col :xs="24" :sm="6">
                <el-form-item label="所属大类 *">
                  <el-select v-model="dtForm.category" style="width:100%">
                    <el-option v-for="c in CATEGORY_OPTIONS" :key="c.value" :label="c.label" :value="c.value" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="8"><el-form-item label="展示名称 *"><el-input v-model="dtForm.label" placeholder="如 值班补贴申请" /></el-form-item></el-col>
              <el-col :xs="16" :sm="6">
                <el-form-item label="标识 key *（创建后不可改）">
                  <el-input v-model="dtForm.key" :disabled="!!dtEditId" placeholder="英文/数字/下划线，如 night_shift" />
                </el-form-item>
              </el-col>
              <el-col :xs="8" :sm="2"><el-form-item label="排序"><el-input-number v-model="dtForm.sort_order" :controls="false" style="width:100%" /></el-form-item></el-col>
              <el-col :xs="8" :sm="2"><el-form-item label="启用"><el-switch v-model="dtForm.enabled" /></el-form-item></el-col>
            </el-row>
          </el-form>
          <div style="display:flex;gap:10px">
            <el-button v-if="dtEditId" @click="dtResetForm">取消编辑</el-button>
            <el-button type="primary" :loading="dtSaving" @click="dtSave">{{ dtEditId ? '保存修改' : '新增单据类型' }}</el-button>
          </div>
        </div>

        <div v-else-if="settingsTab === 'chain'">
          <!-- 🆕 已配置流程一览：一屏看到所有部门×单据类型的审批链 -->
          <div class="form-section-title" style="margin-top:0">已配置的审批流程一览</div>
          <el-table :data="chainOverview" size="small" border stripe max-height="32vh" style="margin-bottom:18px">
            <el-table-column label="部门" width="120"><template #default="{ row }">{{ row.department_name }}</template></el-table-column>
            <el-table-column label="单据类型" width="130"><template #default="{ row }">{{ row.doc_label }}</template></el-table-column>
            <el-table-column label="审批链（按顺序）" min-width="280">
              <template #default="{ row }">
                <span v-for="(s, i) in row.steps" :key="s.step_order" class="chain-step">
                  <el-tag :type="s.enabled ? 'primary' : 'info'" size="small" effect="plain">{{ s.step_order }}. {{ s.step_label || s.role_name }}</el-tag>
                  <span v-if="i < row.steps.length - 1" class="chain-arrow">→</span>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="{ row }"><el-button size="small" link type="primary" @click="loadChainForEdit(row)">编辑</el-button></template>
            </el-table-column>
            <template #empty><EmptyHint text="还没有配置任何审批流程，在下方选部门+单据类型开始配置" size="sm" /></template>
          </el-table>

          <el-divider content-position="left">配置某个「部门 + 单据类型」的审批链</el-divider>
          <el-alert type="info" :closable="false" style="margin-bottom:14px"
            title="选部门+单据类型 → 配置该组合下的多级审批步骤（按顺序）。改配置不影响已提交的历史申请。" />
          <el-row :gutter="16" style="margin-bottom:14px">
            <el-col :xs="12" :sm="8">
              <el-select v-model="chainDeptId" placeholder="选择部门" style="width:100%">
                <el-option v-for="d in departments" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
            </el-col>
            <el-col :xs="12" :sm="8">
              <el-select v-model="chainDocType" placeholder="选择单据类型" style="width:100%">
                <el-option-group v-for="(list, cat) in docTypesByCategory" :key="cat" :label="list[0]?.category_label">
                  <el-option v-for="t in list" :key="t.key" :label="t.label" :value="t.key" />
                </el-option-group>
              </el-select>
            </el-col>
          </el-row>
          <el-table :data="chainSteps" v-loading="chainLoading" size="small" border stripe>
            <el-table-column prop="step_order" label="顺序" width="60" align="center" />
            <el-table-column label="审批角色" min-width="120"><template #default="{ row }">{{ roleName(row.approver_role) }}</template></el-table-column>
            <el-table-column prop="step_label" label="展示名" min-width="120" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="stepEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="stepDelete(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><EmptyHint text="该部门/单据类型尚未配置审批流程" size="sm" /></template>
          </el-table>
          <div class="form-section-title" style="margin-top:16px">{{ stepEditId ? '编辑步骤' : '新增步骤' }}</div>
          <el-form :model="stepForm" label-position="top">
            <el-row :gutter="16">
              <el-col :xs="8" :sm="4"><el-form-item label="顺序"><el-input-number v-model="stepForm.step_order" :min="1" :controls="false" style="width:100%" /></el-form-item></el-col>
              <el-col :xs="16" :sm="8">
                <el-form-item label="审批角色 *">
                  <el-select v-model="stepForm.approver_role" filterable style="width:100%" placeholder="选择角色">
                    <el-option v-for="r in rolesList" :key="r.code" :label="r.name" :value="r.code" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :xs="16" :sm="8"><el-form-item label="展示名（留空用角色名）"><el-input v-model="stepForm.step_label" placeholder="如 部门主管审批" /></el-form-item></el-col>
              <el-col :xs="8" :sm="4"><el-form-item label="启用"><el-switch v-model="stepForm.enabled" /></el-form-item></el-col>
            </el-row>
          </el-form>
          <div style="display:flex;gap:10px">
            <el-button v-if="stepEditId" @click="stepResetForm">取消编辑</el-button>
            <el-button type="primary" :loading="stepSaving" @click="stepSave">{{ stepEditId ? '保存修改' : '新增步骤' }}</el-button>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ==================== 新建申请 ==================== -->
    <el-dialog v-model="subVisible" title="新建OA申请" width="min(680px, 96vw)" class="v3-scroll-dialog">
      <el-form :model="subForm" label-position="top">
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item label="单据类型 *">
              <el-select v-model="subForm.doc_type" style="width:100%" placeholder="选择单据类型">
                <el-option-group v-for="(list, cat) in docTypesByCategory" :key="cat" :label="list[0]?.category_label">
                  <el-option v-for="t in list" :key="t.key" :label="t.label" :value="t.key" />
                </el-option-group>
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item label="部门 *">
              <el-select v-model="subForm.department_id" filterable style="width:100%" placeholder="选择部门">
                <el-option v-for="d in departments.filter(x => x.enabled)" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12"><el-form-item label="标题"><el-input v-model="subForm.title" placeholder="留空则用单据类型名" /></el-form-item></el-col>
          <el-col :xs="24" :sm="12" v-if="!showReimburseFields">
            <el-form-item :label="showPurchaseFields ? '预估采购金额' : '预估金额（选填）'">
              <el-input-number v-model="subForm.amount" :min="0" :precision="2" :controls="false" style="width:100%" />
            </el-form-item>
          </el-col>

          <template v-if="showBusinessFields">
            <el-col :xs="24" :sm="12"><el-form-item label="目的地/对象"><el-input v-model="subForm.d_destination" /></el-form-item></el-col>
            <el-col :xs="12" :sm="6"><el-form-item label="开始日期"><el-date-picker v-model="subForm.d_start_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item></el-col>
            <el-col :xs="12" :sm="6"><el-form-item label="结束日期"><el-date-picker v-model="subForm.d_end_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item></el-col>
            <el-col v-if="showTripFields" :xs="24" :sm="12">
              <el-form-item label="交通方式">
                <el-select v-model="subForm.d_transport" clearable style="width:100%" placeholder="选择交通方式">
                  <el-option v-for="t in TRANSPORT_OPTIONS" :key="t" :label="t" :value="t" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="24"><el-form-item label="事由/备注"><el-input v-model="subForm.d_notes" type="textarea" :rows="2" /></el-form-item></el-col>
          </template>

          <template v-if="showReimburseFields">
            <el-col :span="24" v-if="showRelatedTrip">
              <el-form-item label="关联出差申请（选填）">
                <el-select v-model="subForm.related_request_id" clearable filterable style="width:100%" placeholder="选择已批准的出差申请">
                  <el-option v-for="t in myApprovedTrips" :key="t.id" :label="`${t.request_no} · ${t.title}`" :value="t.id" />
                </el-select>
              </el-form-item>
            </el-col>
            <!-- 🆕 #149：费用明细（逐行费用 + 发票），报销金额自动合计 -->
            <el-col :span="24">
              <el-form-item label="费用明细">
                <div style="width:100%">
                  <el-table :data="subForm.expense_items" size="small" border>
                    <el-table-column label="费用类型" width="130">
                      <template #default="{ row }">
                        <el-select v-model="row.category" filterable allow-create default-first-option placeholder="选/填类型" size="small" style="width:100%">
                          <el-option v-for="c in EXPENSE_CATS" :key="c" :label="c" :value="c" />
                        </el-select>
                      </template>
                    </el-table-column>
                    <el-table-column label="说明" min-width="140">
                      <template #default="{ row }"><el-input v-model="row.note" size="small" placeholder="用途/说明" /></template>
                    </el-table-column>
                    <el-table-column label="金额(元)" width="120">
                      <template #default="{ row }"><el-input-number v-model="row.amount" :min="0" :precision="2" :controls="false" size="small" style="width:100%" /></template>
                    </el-table-column>
                    <el-table-column label="发票" width="120" align="center">
                      <template #default="{ row }">
                        <el-button v-if="!row.invoice_file_id" size="small" link type="primary" @click="uploadExpenseInvoice(row)">上传</el-button>
                        <el-tooltip v-else :content="row.invoice_file_name" placement="top">
                          <el-button size="small" link type="success" @click="uploadExpenseInvoice(row)">📎 已传·换</el-button>
                        </el-tooltip>
                      </template>
                    </el-table-column>
                    <el-table-column label="操作" width="56" align="center">
                      <template #default="{ $index }"><el-button size="small" link type="danger" @click="delExpenseRow($index)">删</el-button></template>
                    </el-table-column>
                    <template #empty><span class="muted" style="font-size:12px">还没有费用明细，点下方「添加一行」</span></template>
                  </el-table>
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
                    <el-button size="small" :icon="Plus" @click="addExpenseRow">添加一行</el-button>
                    <span>报销合计 <b style="color:var(--el-color-primary)">{{ fmtMoney(expenseTotal) }}</b></span>
                  </div>
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="24"><el-form-item label="费用说明"><el-input v-model="subForm.d_notes" type="textarea" :rows="2" placeholder="整单说明（选填）" /></el-form-item></el-col>
          </template>

          <template v-if="showPurchaseFields">
            <el-col :span="24"><el-form-item label="物品清单"><el-input v-model="subForm.d_items" type="textarea" :rows="2" placeholder="名称/规格/数量，每行一项" /></el-form-item></el-col>
            <el-col :span="24"><el-form-item label="用途"><el-input v-model="subForm.d_purpose" /></el-form-item></el-col>
          </template>

          <el-col :span="24">
            <el-form-item label="抄送（选填）">
              <el-select v-model="subForm.cc_user_ids" multiple filterable clearable collapse-tags collapse-tags-tooltip
                         style="width:100%" placeholder="抄送给谁看——不参与审批，仅收到通知并可查看该申请">
                <el-option v-for="u in ccCandidates" :key="u.id" :label="u.name" :value="u.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="subVisible = false">取消</el-button>
        <el-button type="primary" :loading="subSaving" @click="submitNew">提交申请</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 详情抽屉 ==================== -->
    <el-drawer v-model="detailVisible" :title="detailReq ? `${detailReq.request_no} · ${docLabel(detailReq.doc_type)}` : ''" size="min(560px, 96vw)">
      <div v-loading="detailLoading" v-if="detailReq">
        <div class="detail-grid">
          <div><span class="muted">部门</span><div>{{ detailReq.department_name }}</div></div>
          <div><span class="muted">申请人</span><div>{{ detailReq.requester_name }}</div></div>
          <div><span class="muted">金额</span><div>{{ fmtMoney(detailReq.amount) }}</div></div>
          <div><span class="muted">状态</span><div><StatusPill :text="STATUS_TEXT[detailReq.status]" :variant="STATUS_VARIANT[detailReq.status]" /></div></div>
          <div v-if="detailReq.related_request_no"><span class="muted">关联申请</span><div>{{ detailReq.related_request_no }}</div></div>
          <div v-if="detailReq.settle_amount != null"><span class="muted">核定金额</span><div>{{ fmtMoney(detailReq.settle_amount) }}</div></div>
        </div>
        <div v-if="detailReq.cc_users && detailReq.cc_users.length" class="cc-line">
          <span class="muted">抄送</span>：
          <el-tag v-for="u in detailReq.cc_users" :key="u.id" size="small" effect="plain" style="margin-right:6px">{{ u.name }}</el-tag>
        </div>
        <div v-if="detailReq.detail && Object.keys(detailReq.detail).length" class="detail-json">
          <div v-for="(v, k) in detailReq.detail" :key="k" v-show="v && k !== 'expense_items'">
            <span class="muted">{{ detailFieldLabel(k) }}</span>：{{ v }}
          </div>
        </div>
        <!-- 🆕 #149：报销费用明细 + 逐行发票下载 -->
        <div v-if="detailReq.detail && detailReq.detail.expense_items && detailReq.detail.expense_items.length" style="margin:6px 0 12px">
          <div class="form-section-title" style="margin-top:0">费用明细</div>
          <el-table :data="detailReq.detail.expense_items" size="small" border>
            <el-table-column label="费用类型" width="96"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column label="说明" min-width="110"><template #default="{ row }">{{ row.note || '—' }}</template></el-table-column>
            <el-table-column label="金额" width="96" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column label="发票" width="80" align="center">
              <template #default="{ row }">
                <el-button v-if="row.invoice_file_id" size="small" link type="primary" @click="downloadAttachment({ id: row.invoice_file_id, name: row.invoice_file_name || '发票' })">下载</el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div v-if="detailReq.reject_reason" class="reject-box">驳回原因：{{ detailReq.reject_reason }}</div>

        <div class="form-section-title">审批进度</div>
        <el-steps direction="vertical" :active="99" style="margin-bottom:16px">
          <el-step v-for="s in detailReq.steps" :key="s.id" :title="s.step_label || roleName(s.approver_role)"
                   :status="s.status === 'approved' ? 'success' : s.status === 'rejected' ? 'error' : (detailReq.current_step_order === s.step_order && detailReq.status === 'pending') ? 'process' : 'wait'">
            <template #description>
              <div v-if="s.status !== 'pending'">
                {{ s.actor_name }} · {{ fmtDateTime(s.acted_at) }}
                <span v-if="s.note">：{{ s.note }}</span>
              </div>
              <div v-else class="muted">待处理</div>
            </template>
          </el-step>
        </el-steps>

        <div class="form-section-title">附件</div>
        <div class="att-list">
          <div v-for="a in attachments" :key="a.id" class="att-row">
            <span>{{ a.name }}</span>
            <el-button size="small" link type="primary" :icon="Download" @click="downloadAttachment(a)">下载</el-button>
          </div>
          <EmptyHint v-if="!attachments.length" text="暂无附件" size="sm" />
        </div>
        <el-button size="small" :icon="Upload" @click="uploadAttachment" style="margin-top:8px">上传附件</el-button>

        <div class="drawer-actions" v-if="detailReq.can_approve || detailReq.can_withdraw || detailReq.can_mark_paid">
          <el-divider />
          <template v-if="detailReq.can_approve">
            <el-input v-model="approveNote" placeholder="审批意见（选填）" style="margin-bottom:8px" />
            <el-input-number v-model="approveSettle" :min="0" :precision="2" :controls="false"
                             placeholder="核定金额（选填，与申请金额不同时填写）" style="width:100%;margin-bottom:8px" />
            <div style="display:flex;gap:10px">
              <el-button type="success" :icon="Check" :loading="approving" @click="doApprove">审批通过</el-button>
              <el-button type="danger" :icon="Close" :loading="rejecting" @click="doReject">驳回</el-button>
            </div>
          </template>
          <template v-if="detailReq.can_mark_paid">
            <div class="muted small" style="margin-bottom:8px">已审批通过，等待财务实际付款；付款后点下面按钮标记，跟"审批通过"是两件事。</div>
            <el-button type="primary" :icon="Check" :loading="markingPaid" @click="doMarkPaid">标记已付款</el-button>
          </template>
          <el-button v-if="detailReq.can_withdraw" type="warning" plain @click="doWithdraw" style="margin-top:8px">撤回申请</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.page { padding: 20px; }
.page-head { margin-bottom: 16px; }
.page-head h2 { margin: 0 0 4px; font-size: 20px; }
.desc { color: var(--el-text-color-secondary); font-size: 13px; }
.toolbar { display: flex; gap: 10px; margin-bottom: 14px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.form-section-title { font-weight: 600; margin: 18px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--el-border-color-lighter); }
.summary-bar { margin-top: 12px; padding: 10px 14px; background: var(--el-fill-color-light); border-radius: 6px; text-align: right; }
.detail-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 16px; margin-bottom: 14px; }
.detail-json { background: var(--el-fill-color-light); border-radius: 6px; padding: 10px 14px; font-size: 13px; line-height: 1.8; margin-bottom: 14px; }
.cc-line { margin-bottom: 14px; font-size: 13px; }
.chain-step { display: inline-flex; align-items: center; }
.chain-arrow { margin: 0 5px; color: var(--el-text-color-secondary); }
.reject-box { background: var(--el-color-danger-light-9); color: var(--el-color-danger); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; font-size: 13px; }
.att-list { display: flex; flex-direction: column; gap: 4px; }
.att-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; }
.drawer-actions { margin-top: 8px; }
</style>
