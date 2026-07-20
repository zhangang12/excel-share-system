<script setup lang="ts">
// 🆕 OA 审批：部门字典 + 可配置多级审批链 + 业务/报销/采购三类共8种申请单。
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Check, Close, Download, Upload, RefreshLeft, Delete } from '@element-plus/icons-vue'
import { http } from '@/api'
import { oaApi, type Department, type OaDocType, type OaApprovalStep, type OaRequest, type OaSummaryRow, type OaSummaryDetailRow, type OaChainOverviewRow } from '@/api/oa'
import { adminApi } from '@/api/admin'
import { downloadAttachment } from '@/api/orders'
import { useAuthStore } from '@/stores/auth'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtDateTime } from '@/utils/format'

const auth = useAuthStore()
const canConfig = computed(() => auth.isAdmin)               // 部门/审批流程设置：仅 admin/manager
const canViewAll = computed(() => auth.isAdmin || auth.hasRole('finance'))  // 🆕 #256 全部申请：admin/manager + 财务
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
// 🆕 #238 待付款队列：与后端 scope=pending_pay 的门槛一致
const canPay = computed(() => auth.hasRole('finance', 'admin', 'manager'))

// ===== 列表 tab =====
const mainTab = ref('mine')
type Scope = 'mine' | 'pending_me' | 'pending_pay' | 'cc_me' | 'dept' | 'all'
const activeTab = ref<Scope>('mine')
const rows = ref<OaRequest[]>([])
const listLoading = ref(false)
async function loadList() {
  listLoading.value = true
  try { rows.value = await oaApi.listRequests({ scope: activeTab.value }) }
  finally { listLoading.value = false }
}
function onTabChange(name: string | number) {
  if (['mine', 'pending_me', 'pending_pay', 'cc_me', 'dept', 'all'].includes(String(name))) {
    activeTab.value = name as Scope
    filterAllDoc.value = ''; filterAllRequester.value = ''   // 换 tab 清筛选
    loadList()
  }
}

// 🆕 反馈#218：全部申请 加「单据类型 / 申请人」筛选(客户端过滤当前已加载列表)
const filterAllDoc = ref('')
const filterAllRequester = ref('')
const allRequesters = computed(() =>
  Array.from(new Set(rows.value.map(r => r.requester_name).filter((n): n is string => !!n))))
const allDocOptions = computed(() =>
  Array.from(new Set(rows.value.map(r => r.doc_type))).map(k => ({ key: k, label: docLabel(k) })))
const allRowsView = computed(() => rows.value.filter(r =>
  (!filterAllDoc.value || r.doc_type === filterAllDoc.value) &&
  (!filterAllRequester.value || r.requester_name === filterAllRequester.value)))

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
  // 🆕 反馈#217 销售提成申请字段
  project_code: '项目编号', customer_name: '客户名称', equipment_name: '设备名称',
  payback_amount: '回款金额', payback_type: '回款类型', commission_rate: '提成点(%)', commission_amount: '提成金额',
  // 🆕 反馈#236 按月 + 多项目明细
  period: '提成月份', payback_total: '回款合计', commission_total: '提成总计',
}
function detailFieldLabel(k: string): string { return DETAIL_FIELD_LABELS[k] || k }
// 🆕 #236 详情里提成明细的「总计」行：只对回款金额/提成两列求和
function commissionSummary({ columns, data }: { columns: any[]; data: any[] }) {
  return columns.map((_c, i) => {
    if (i === 0) return '总计'
    if (i === 4) return fmtMoney(data.reduce((s, r) => s + (Number(r.payback_amount) || 0), 0))
    if (i === 7) return fmtMoney(data.reduce((s, r) => s + (Number(r.commission_amount) || 0), 0))
    return ''
  })
}
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
// 🆕 反馈#236 销售提成明细行：一次申请可含多个项目（提成按月提交）
interface CommissionItem {
  project_code: string; deal_date: string; customer: string
  payback_amount: number | null; payback_type: string; rate: number | null
}
const subForm = reactive({
  doc_type: '', department_id: '' as number | '', title: '', amount: null as number | null,
  related_request_id: null as number | '' | null,
  d_destination: '', d_start_date: '', d_end_date: '', d_notes: '', d_items: '', d_purpose: '', d_transport: '',
  // 🆕 反馈#236 销售提成申请：改为「按月 + 多项目明细」，原单项目平铺字段(#217)已废弃
  c_period: '' as string,
  commission_items: [] as CommissionItem[],
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
// 🆕 反馈#264：提交申请时可直接添加文档附件（提交成功后逐个上传到本申请）
const subFiles = ref<File[]>([])
function pickSubFiles() {
  const input = document.createElement('input')
  input.type = 'file'; input.multiple = true
  input.accept = '.pdf,.doc,.docx,.xls,.xlsx,.csv,.jpg,.jpeg,.png,.gif,.bmp,.webp,.dwg,.dxf,.zip,.rar,.7z,.ofd,.txt'
  input.onchange = () => {
    for (const f of Array.from(input.files || [])) {
      if (!subFiles.value.some(x => x.name === f.name && x.size === f.size)) subFiles.value.push(f)
    }
  }
  input.click()
}
function removeSubFile(i: number) { subFiles.value.splice(i, 1) }

const subDocType = computed(() => docTypes.value.find(d => d.key === subForm.doc_type))
const subCategory = computed(() => subDocType.value?.category || '')
const showBusinessFields = computed(() => subCategory.value === 'business')
const showReimburseFields = computed(() => subCategory.value === 'reimbursement')
const showPurchaseFields = computed(() => subCategory.value === 'purchase')
const showRelatedTrip = computed(() => subForm.doc_type === 'travel_expense')
const showTripFields = computed(() => subForm.doc_type === 'trip')   // 🆕 出差申请专属：交通方式
const TRANSPORT_OPTIONS = ['高铁', '飞机', '火车', '私车公用', '公车']
// 🆕 反馈#217/#236 销售提成申请：按月提交 + 多项目明细，每行提成=回款金额×提成点%，底部总计
const showCommissionFields = computed(() => subForm.doc_type === 'sales_commission')
const PAYBACK_TYPES = ['预付款', '进度款', '到货款', '尾款', '质保金', '全款']
// 单行提成（分转整，避免浮点误差）
function rowCommission(r: CommissionItem) {
  return Math.round((Number(r.payback_amount) || 0) * (Number(r.rate) || 0)) / 100
}
// 总计：本次申请的提成合计 = 各行之和（= 提交时的申请金额）
const commissionTotal = computed(() =>
  Math.round(subForm.commission_items.reduce((s, r) => s + rowCommission(r) * 100, 0)) / 100)
const commissionPaybackTotal = computed(() =>
  Math.round(subForm.commission_items.reduce((s, r) => s + (Number(r.payback_amount) || 0) * 100, 0)) / 100)
function addCommissionRow() {
  subForm.commission_items.push({ project_code: '', deal_date: '', customer: '',
    payback_amount: null, payback_type: '', rate: null })
}
function delCommissionRow(i: number) { subForm.commission_items.splice(i, 1) }
// 🆕 #236 提成明细 8 列合计约 930px，680px 弹窗装不下会横向滚动（回款类型/提成点被挤没）——
//   提成表单时把弹窗放宽到 1060px；窄屏仍由 96vw 兜底，表格自身横向滚动。
const subDialogWidth = computed(() =>
  showCommissionFields.value ? 'min(1060px, 96vw)' : 'min(680px, 96vw)')
// 🆕 #236 部门固定销售部、不给选
const SALES_DEPT_NAME = '销售部'
const salesDeptId = computed(() => departments.value.find(d => d.name === SALES_DEPT_NAME)?.id ?? '')
// 切到销售提成申请：部门锁成销售部并起一行明细；切走时把锁上的部门清掉，避免残留
watch(showCommissionFields, (on) => {
  if (on) {
    subForm.department_id = salesDeptId.value
    if (!subForm.c_period) subForm.c_period = new Date().toISOString().slice(0, 7)
    if (!subForm.commission_items.length) addCommissionRow()
  } else if (subForm.department_id === salesDeptId.value) {
    subForm.department_id = ''
  }
})
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
    c_period: new Date().toISOString().slice(0, 7),   // 🆕 #236 提成按月提交，默认本月
    commission_items: [],
    expense_items: [],
    cc_user_ids: [],
  })
  subFiles.value = []   // 🆕 反馈#264：附件选择一并重置
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
  if (showCommissionFields.value) {
    // 🆕 反馈#236 销售提成申请：按月 + 多项目明细 + 总计
    if (!subForm.c_period) { ElMessage.warning('请选择提成月份'); return }
    const items = subForm.commission_items.filter(
      r => r.project_code.trim() || r.customer.trim() || (Number(r.payback_amount) || 0) > 0)
    if (!items.length) { ElMessage.warning('请至少添加一条项目明细'); return }
    const bad = items.findIndex(r => !r.project_code.trim() && !r.customer.trim())
    if (bad >= 0) { ElMessage.warning(`第 ${bad + 1} 行请至少填写项目编号或客户名称`); return }
    detail = {
      period: subForm.c_period,
      commission_items: items.map(r => ({
        project_code: r.project_code.trim(), deal_date: r.deal_date || '', customer_name: r.customer.trim(),
        payback_amount: Number(r.payback_amount) || 0, payback_type: r.payback_type || '',
        commission_rate: Number(r.rate) || 0, commission_amount: rowCommission(r),
      })),
      payback_total: commissionPaybackTotal.value,
      commission_total: commissionTotal.value,
      notes: subForm.d_notes || '',
    }
  } else if (showBusinessFields.value) {
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
      amount: showReimburseFields.value ? expenseTotal.value : (showCommissionFields.value ? commissionTotal.value : subForm.amount),
      detail, related_request_id: showRelatedTrip.value && subForm.related_request_id ? (subForm.related_request_id as number) : null,
      cc_user_ids: subForm.cc_user_ids,
    })
    ElMessage.success(`已提交 ${r.request_no}`)
    // 🆕 反馈#264：随申请一并上传附件（失败不阻塞，可在详情抽屉重新上传）
    if (subFiles.value.length) {
      let fail = 0
      for (const f of subFiles.value) {
        const fd = new FormData()
        fd.append('file', f); fd.append('biz_type', 'oa_request'); fd.append('biz_id', String(r.id))
        try { await http.post('/attachments', fd) } catch { fail++ }
      }
      if (fail) ElMessage.warning(`${fail} 个附件上传失败，可在申请详情里重新上传`)
      subFiles.value = []
    }
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
// 🆕 #200 流程级固定抄送(角色):随部门+单据类型联动
const flowCcRoles = ref<string[]>([])
const flowCcSaving = ref(false)
async function loadFlowCc() {
  if (!chainDeptId.value || !chainDocType.value) { flowCcRoles.value = []; return }
  try { flowCcRoles.value = (await oaApi.flowCc(chainDeptId.value as number, chainDocType.value)).roles }
  catch { flowCcRoles.value = [] }
}
async function saveFlowCc() {
  if (!chainDeptId.value || !chainDocType.value) { ElMessage.warning('请先选择部门和单据类型'); return }
  flowCcSaving.value = true
  try {
    const r: any = await oaApi.saveFlowCc(chainDeptId.value as number, chainDocType.value, flowCcRoles.value)
    ElMessage.success(r.message || '已保存')
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
  finally { flowCcSaving.value = false }
}
// 🆕 #199 管理层删除申请单
async function deleteRequest(row: any) {
  try {
    await ElMessageBox.confirm(
      `删除申请 ${row.request_no}（${row.requester_name} · ${STATUS_TEXT[row.status] || row.status}）？附件一并删除，不可恢复。`,
      '删除申请单', { type: 'warning', confirmButtonText: '删除' })
  } catch { return }
  try {
    const r: any = await oaApi.deleteRequest(row.id)
    ElMessage.success(r.message || '已删除')
    await loadList()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}

watch([chainDeptId, chainDocType], () => { loadChainSteps(); loadFlowCc() })
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
// 🆕 #257 汇总报表加「单据类型」筛选（客户端过滤已加载汇总行）
const summaryDocType = ref('')
const summaryDocOptions = computed(() =>
  Array.from(new Set(summaryRows.value.map(r => r.doc_type))).map(k => ({ key: k, label: docLabel(k) })))
const summaryView = computed(() => summaryRows.value.filter(r =>
  !summaryDocType.value || r.doc_type === summaryDocType.value))
const summaryTotal = computed(() => summaryView.value.reduce((s, r) => s + r.amount, 0))

// 🆕 #247 汇总报表下钻明细
const sumDetailVisible = ref(false)
const sumDetailLoading = ref(false)
const sumDetailRows = ref<OaSummaryDetailRow[]>([])
const sumDetailTitle = ref('')
async function openSumDetail(row: OaSummaryRow) {
  sumDetailTitle.value = `${row.department_name} · ${docLabel(row.doc_type)}`
  sumDetailVisible.value = true
  sumDetailLoading.value = true
  try { sumDetailRows.value = await oaApi.summaryDetail(row.department_id, row.doc_type) }
  finally { sumDetailLoading.value = false }
}
const sumDetailTotal = computed(() => sumDetailRows.value.reduce((s, r) => s + r.amount, 0))

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
        <el-table show-overflow-tooltip :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="还没有提交过申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="待我审批" name="pending_me">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table show-overflow-tooltip :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">处理</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="没有待你审批的申请" /></template>
        </el-table>
      </el-tab-pane>

      <!-- 🆕 #238 待付款：末环节财务审批通过后单据转「待付款」且不再属于任何审批环节，
           以前哪个队列都不显示=单据"消失"。财务在这里付款收口。 -->
      <el-tab-pane v-if="canPay" label="待付款" name="pending_pay">
        <div class="toolbar">
          <el-button :icon="RefreshLeft" @click="loadList">刷新</el-button>
          <span class="muted" style="font-size:12.5px;margin-left:10px">审批已走完、等财务实际付钱的单据。付完点「处理」里的标记已付款。</span>
        </div>
        <el-table show-overflow-tooltip :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right">
            <template #default="{ row }">{{ fmtMoney(row.settle_amount ?? row.amount) }}</template>
          </el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">处理</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="没有待付款的单据" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="抄送我的" name="cc_me">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table show-overflow-tooltip :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="提交时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="没有抄送给你的申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="isDeptLead" label="部门审批" name="dept">
        <div class="toolbar"><el-button :icon="RefreshLeft" @click="loadList">刷新</el-button></div>
        <el-table show-overflow-tooltip :data="rows" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button></template>
          </el-table-column>
          <template #empty><EmptyHint text="本部门暂无申请" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canViewAll" label="全部申请" name="all">
        <div class="toolbar" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <el-button :icon="RefreshLeft" @click="loadList">刷新</el-button>
          <!-- 🆕 反馈#218 单据类型 / 申请人 筛选 -->
          <el-select v-model="filterAllDoc" clearable filterable placeholder="单据类型(全部)" size="default" style="width:150px">
            <el-option v-for="d in allDocOptions" :key="d.key" :label="d.label" :value="d.key" />
          </el-select>
          <el-select v-model="filterAllRequester" clearable filterable placeholder="申请人(全部)" size="default" style="width:140px">
            <el-option v-for="n in allRequesters" :key="n" :label="n" :value="n" />
          </el-select>
          <span class="muted" style="font-size:13px">共 {{ allRowsView.length }} 条</span>
        </div>
        <el-table show-overflow-tooltip :data="allRowsView" v-loading="listLoading" stripe max-height="calc(100vh - 320px)">
          <el-table-column prop="request_no" label="单号" width="150" />
          <el-table-column label="单据类型" width="120"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="department_name" label="部门" width="100" />
          <el-table-column prop="requester_name" label="申请人" width="100" />
          <el-table-column prop="title" label="标题" min-width="150" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><StatusPill :text="STATUS_TEXT[row.status]" :variant="STATUS_VARIANT[row.status]" /></template></el-table-column>
          <el-table-column label="当前环节" width="110"><template #default="{ row }">{{ curStepLabel(row) }}</template></el-table-column>
          <el-table-column label="操作" width="130" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="openDetail(row.id)">查看</el-button>
              <!-- 🆕 #199 管理层删除(误提/测试单清理) -->
              <el-button size="small" link type="danger" @click="deleteRequest(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty><EmptyHint text="暂无申请记录" /></template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canViewSummary" label="汇总报表" name="summary">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
          <el-select v-model="summaryDocType" placeholder="单据类型(全部)" clearable style="width:180px" size="small">
            <el-option v-for="d in summaryDocOptions" :key="d.key" :label="d.label" :value="d.key" />
          </el-select>
          <span class="muted" style="font-size:12.5px">点某一行的「查看明细」下钻，看这个部门+单据类型下的每一条已批准申请。</span>
        </div>
        <el-table show-overflow-tooltip :data="summaryView" v-loading="summaryLoading" stripe size="small"
                  @row-click="openSumDetail" class="clickable-rows">
          <el-table-column prop="department_name" label="部门" width="140" />
          <el-table-column label="单据类型" width="140"><template #default="{ row }">{{ docLabel(row.doc_type) }}</template></el-table-column>
          <el-table-column prop="count" label="已批件数" width="100" align="right" />
          <el-table-column label="金额合计" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="操作" width="110" align="center" fixed="right" :show-overflow-tooltip="false">
            <template #default="{ row }"><el-button size="small" type="primary" link @click.stop="openSumDetail(row)">查看明细</el-button></template>
          </el-table-column>
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
          <el-table show-overflow-tooltip :data="departments" size="small" border stripe max-height="34vh">
            <el-table-column type="index" label="#" width="46" align="center" />
            <el-table-column prop="name" label="部门名称" min-width="120" />
            <el-table-column label="部门负责人角色" min-width="150"><template #default="{ row }">{{ roleName(row.lead_role) }}</template></el-table-column>
            <el-table-column label="排序" width="70" prop="sort_order" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110" fixed="right" :show-overflow-tooltip="false">
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
          <el-table show-overflow-tooltip :data="docTypes" size="small" border stripe max-height="34vh">
            <el-table-column type="index" label="#" width="46" align="center" />
            <el-table-column label="所属大类" width="110"><template #default="{ row }">{{ categoryLabel(row.category) }}</template></el-table-column>
            <el-table-column prop="label" label="展示名称" min-width="120" />
            <el-table-column prop="key" label="标识（key）" min-width="120" />
            <el-table-column label="排序" width="70" prop="sort_order" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110" fixed="right" :show-overflow-tooltip="false">
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
          <el-table show-overflow-tooltip :data="chainOverview" size="small" border stripe max-height="32vh" style="margin-bottom:18px">
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
            <el-table-column label="操作" width="80" fixed="right" :show-overflow-tooltip="false">
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
          <el-table show-overflow-tooltip :data="chainSteps" v-loading="chainLoading" size="small" border stripe>
            <el-table-column prop="step_order" label="顺序" width="60" align="center" />
            <el-table-column label="审批角色" min-width="120"><template #default="{ row }">{{ roleName(row.approver_role) }}</template></el-table-column>
            <el-table-column prop="step_label" label="展示名" min-width="120" />
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="110" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="stepEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="stepDelete(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><EmptyHint text="该部门/单据类型尚未配置审批流程" size="sm" /></template>
          </el-table>
          <!-- 🆕 #200 流程级固定抄送:该部门+单据类型的申请提交时自动抄送这些角色(与手选抄送合并) -->
          <div class="form-section-title" style="margin-top:16px">自动抄送（选填）</div>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <el-select v-model="flowCcRoles" multiple filterable clearable collapse-tags collapse-tags-tooltip
                       :disabled="!chainDeptId || !chainDocType"
                       placeholder="选择自动抄送的角色（提交时通知其在职用户,不参与审批）" style="flex:1;max-width:520px">
              <el-option v-for="r in rolesList" :key="r.code" :label="r.name" :value="r.code" />
            </el-select>
            <el-button type="primary" plain :loading="flowCcSaving" :disabled="!chainDeptId || !chainDocType"
                       @click="saveFlowCc">保存抄送</el-button>
            <span class="muted small">改配置只影响之后提交的申请</span>
          </div>

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
    <el-dialog v-model="subVisible" title="新建OA申请" :width="subDialogWidth" class="v3-scroll-dialog">
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
            <!-- 🆕 #236 销售提成申请：部门固定销售部，不给选 -->
            <el-form-item label="部门 *">
              <el-input v-if="showCommissionFields" :model-value="SALES_DEPT_NAME" disabled>
                <template #suffix><span class="muted small">提成固定销售部</span></template>
              </el-input>
              <el-select v-else v-model="subForm.department_id" filterable style="width:100%" placeholder="选择部门">
                <el-option v-for="d in departments.filter(x => x.enabled)" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <!-- 🆕 #236 提成按月提交 -->
          <el-col :xs="24" :sm="12" v-if="showCommissionFields">
            <el-form-item label="提成月份 *">
              <el-date-picker v-model="subForm.c_period" type="month" value-format="YYYY-MM"
                              placeholder="选择月份" :clearable="false" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12"><el-form-item label="标题"><el-input v-model="subForm.title" placeholder="留空则用单据类型名" /></el-form-item></el-col>
          <el-col :xs="24" :sm="12" v-if="!showReimburseFields && !showCommissionFields">
            <el-form-item :label="showPurchaseFields ? '预估采购金额' : '预估金额（选填）'">
              <el-input-number v-model="subForm.amount" :min="0" :precision="2" :controls="false" style="width:100%" />
            </el-form-item>
          </el-col>

          <!-- 🆕 反馈#217/#236 销售提成申请：按月 + 多项目明细 + 总计 -->
          <template v-if="showCommissionFields">
            <el-col :span="24">
              <el-form-item label="项目明细 *">
                <div style="width:100%">
                  <el-table :data="subForm.commission_items" size="small" border class="cm-tbl">
                    <el-table-column type="index" label="序号" width="56" align="center" />
                    <el-table-column label="项目编号" min-width="120">
                      <template #default="{ row }"><el-input v-model="row.project_code" size="small" placeholder="如 2026-061M" /></template>
                    </el-table-column>
                    <el-table-column label="成单日期" width="140">
                      <template #default="{ row }">
                        <el-date-picker v-model="row.deal_date" type="date" value-format="YYYY-MM-DD"
                                        size="small" placeholder="选择" style="width:100%" />
                      </template>
                    </el-table-column>
                    <el-table-column label="客户名称" min-width="130">
                      <template #default="{ row }"><el-input v-model="row.customer" size="small" /></template>
                    </el-table-column>
                    <el-table-column label="回款金额" width="120" align="right">
                      <template #default="{ row }">
                        <el-input-number v-model="row.payback_amount" :min="0" :precision="2" :controls="false" size="small" style="width:100%" />
                      </template>
                    </el-table-column>
                    <el-table-column label="回款类型" width="118">
                      <template #default="{ row }">
                        <el-select v-model="row.payback_type" filterable allow-create default-first-option clearable
                                   size="small" placeholder="选/填" style="width:100%">
                          <el-option v-for="t in PAYBACK_TYPES" :key="t" :label="t" :value="t" />
                        </el-select>
                      </template>
                    </el-table-column>
                    <el-table-column label="提成点(%)" width="96">
                      <template #default="{ row }">
                        <el-input-number v-model="row.rate" :min="0" :precision="3" :controls="false" size="small" style="width:100%" />
                      </template>
                    </el-table-column>
                    <el-table-column label="提成" width="104" align="right">
                      <template #default="{ row }"><b class="amt">{{ fmtMoney(rowCommission(row)) }}</b></template>
                    </el-table-column>
                    <el-table-column label="" width="46" align="center" :show-overflow-tooltip="false">
                      <template #default="{ $index }">
                        <el-button size="small" link type="danger" :icon="Delete" @click="delCommissionRow($index)" />
                      </template>
                    </el-table-column>
                    <template #empty><span class="muted small">还没有明细，点下面「添加项目」</span></template>
                  </el-table>
                  <!-- 总计 -->
                  <div class="cm-foot">
                    <el-button size="small" :icon="Plus" @click="addCommissionRow">添加项目</el-button>
                    <span style="flex:1" />
                    <span class="muted">回款合计 <b>{{ fmtMoney(commissionPaybackTotal) }}</b></span>
                    <span style="margin-left:16px">总计提成 <b class="amt" style="font-size:15px">{{ fmtMoney(commissionTotal) }}</b></span>
                  </div>
                </div>
              </el-form-item>
            </el-col>
            <el-col :span="24"><el-form-item label="备注（选填）"><el-input v-model="subForm.d_notes" type="textarea" :rows="2" placeholder="选填" /></el-form-item></el-col>
          </template>

          <template v-if="showBusinessFields && !showCommissionFields">
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
                  <el-table show-overflow-tooltip :data="subForm.expense_items" size="small" border>
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
                    <el-table-column label="操作" width="56" align="center" fixed="right" :show-overflow-tooltip="false">
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

          <!-- 🆕 反馈#264：提交申请时可直接添加文档附件 -->
          <el-col :span="24">
            <el-form-item label="附件（选填）">
              <div style="width:100%">
                <div v-for="(f, i) in subFiles" :key="i" class="att-row">
                  <span :title="f.name">{{ f.name }}</span>
                  <el-button size="small" link type="danger" @click="removeSubFile(i)">移除</el-button>
                </div>
                <el-button size="small" :icon="Upload" @click="pickSubFiles">添加文档</el-button>
              </div>
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
          <!-- 明细数组单独用表格渲染(见下)，平铺这里跳过 -->
          <div v-for="(v, k) in detailReq.detail" :key="k"
               v-show="v && k !== 'expense_items' && k !== 'commission_items'">
            <span class="muted">{{ detailFieldLabel(k) }}</span>：{{ v }}
          </div>
        </div>
        <!-- 🆕 #236：销售提成明细（按月多项目）+ 总计 -->
        <div v-if="detailReq.detail && detailReq.detail.commission_items && detailReq.detail.commission_items.length"
             style="margin:6px 0 12px">
          <div class="form-section-title" style="margin-top:0">
            项目提成明细<span v-if="detailReq.detail.period" class="muted small">（{{ detailReq.detail.period }}）</span>
          </div>
          <el-table show-overflow-tooltip :data="detailReq.detail.commission_items" size="small" border
                    show-summary :summary-method="commissionSummary">
            <el-table-column type="index" label="序号" width="56" align="center" />
            <el-table-column label="项目编号" min-width="110"><template #default="{ row }">{{ row.project_code || '—' }}</template></el-table-column>
            <el-table-column label="成单日期" width="106"><template #default="{ row }">{{ row.deal_date || '—' }}</template></el-table-column>
            <el-table-column label="客户名称" min-width="120"><template #default="{ row }">{{ row.customer_name || '—' }}</template></el-table-column>
            <el-table-column label="回款金额" width="112" align="right"><template #default="{ row }">{{ fmtMoney(row.payback_amount) }}</template></el-table-column>
            <el-table-column label="回款类型" width="92"><template #default="{ row }">{{ row.payback_type || '—' }}</template></el-table-column>
            <el-table-column label="提成点(%)" width="90" align="right"><template #default="{ row }">{{ row.commission_rate ?? '—' }}</template></el-table-column>
            <el-table-column label="提成" width="112" align="right"><template #default="{ row }"><b class="amt">{{ fmtMoney(row.commission_amount) }}</b></template></el-table-column>
          </el-table>
        </div>
        <!-- 🆕 #149：报销费用明细 + 逐行发票下载 -->
        <div v-if="detailReq.detail && detailReq.detail.expense_items && detailReq.detail.expense_items.length" style="margin:6px 0 12px">
          <div class="form-section-title" style="margin-top:0">费用明细</div>
          <el-table show-overflow-tooltip :data="detailReq.detail.expense_items" size="small" border>
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

    <!-- 🆕 #247 汇总报表下钻明细 -->
    <el-dialog v-model="sumDetailVisible" :title="`报表明细 · ${sumDetailTitle}`" width="720px" class="v3-scroll-dialog">
      <el-table show-overflow-tooltip :data="sumDetailRows" v-loading="sumDetailLoading" stripe size="small" max-height="56vh">
        <el-table-column prop="request_no" label="单号" width="150"><template #default="{ row }"><span class="code">{{ row.request_no }}</span></template></el-table-column>
        <el-table-column prop="requester_name" label="申请人" width="100"><template #default="{ row }">{{ row.requester_name || '—' }}</template></el-table-column>
        <el-table-column prop="title" label="事由/标题" min-width="160"><template #default="{ row }">{{ row.title || '—' }}</template></el-table-column>
        <el-table-column label="金额" width="128" align="right">
          <template #default="{ row }">
            <b class="amt">{{ fmtMoney(row.amount) }}</b>
            <el-tag v-if="row.settled" size="small" type="success" effect="plain" style="margin-left:4px">核定</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="批准时间" width="150"><template #default="{ row }">{{ fmtDateTime(row.updated_at) }}</template></el-table-column>
        <template #empty><EmptyHint text="暂无明细" size="sm" /></template>
      </el-table>
      <div class="summary-bar" v-if="sumDetailRows.length">共 {{ sumDetailRows.length }} 条 · 合计 <b>{{ fmtMoney(sumDetailTotal) }}</b></div>
    </el-dialog>
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
.clickable-rows :deep(.el-table__row) { cursor: pointer; }
.detail-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 16px; margin-bottom: 14px; }
.detail-json { background: var(--el-fill-color-light); border-radius: 6px; padding: 10px 14px; font-size: 13px; line-height: 1.8; margin-bottom: 14px; }
.cc-line { margin-bottom: 14px; font-size: 13px; }
/* 🆕 #236 销售提成明细行 + 总计 */
.cm-tbl :deep(.el-table__cell) { padding: 4px 0; }
.cm-foot { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 13px; }
.chain-step { display: inline-flex; align-items: center; }
.chain-arrow { margin: 0 5px; color: var(--el-text-color-secondary); }
.reject-box { background: var(--el-color-danger-light-9); color: var(--el-color-danger); border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; font-size: 13px; }
.att-list { display: flex; flex-direction: column; gap: 4px; }
.att-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; }
.drawer-actions { margin-top: 8px; }
</style>
