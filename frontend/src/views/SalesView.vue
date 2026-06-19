<script setup lang="ts">
// 🆕 v3 销售部：销售项目统计台账（§十三 19 列）+ 销售下单 + 上传合同 + 开票申请/审批
import { ref, computed, onMounted, reactive, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Stamp, Download, Check, ChatLineSquare, Clock, Paperclip, Document, Close, MoreFilled, Operation, UploadFilled, DocumentCopy, CircleClose, Select } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { salesApi, fmtMoney, type SalesLedgerRow, type SalesLedgerTotals } from '@/api/sales'
import { downloadAttachment, ordersApi } from '@/api/orders'
import { reportsApi, type SalesReport } from '@/api/reports'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import FilePicker from '@/components/FilePicker.vue'
import WorkflowGraph from '@/components/WorkflowGraph.vue'
import { collabApi, type Workflow } from '@/api/collab'
import { fmtDate } from '@/utils/format'

const auth = useAuthStore()
const allView = computed(() => auth.hasRole('admin', 'manager', 'sales_lead'))

// 🆕 收款金额格式化：0 显示 ¥0（销售已填 0 也要显示），仅 null/undefined 显示 —
// （区别于 fmtMoney 把 0 当空值显示 —）
function fmtPay(n?: number | null): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return '¥' + Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

// 🆕 不开票判定：税票为 "0"(新)或 "/"(历史)；用于隐藏开票申请/合并开票入口
function isNoInvoice(tax?: string | null): boolean {
  return tax === '0' || tax === '/'
}

const loading = ref(false)
const rows = ref<SalesLedgerRow[]>([])
const totals = ref<SalesLedgerTotals | null>(null)
const filters = reactive({ kw: '', cust_type: '', contract: '', sales_uid: undefined as number | undefined, balance_month: '' })

// 销售员筛选下拉：用真实销售员名单（salesStaff，分页后不能再从当前页 rows 聚合）
const salesOptions = computed(() => salesStaff.value)

// 🆕 编辑弹窗「选择销售员」用真实用户名单（拥有 sales/sales_lead 角色的在职用户），
// 修复新建销售员未挂台账前选不到；管理层/主管才需要，登录后拉一次
const salesStaff = ref<{ id: number; name: string }[]>([])
async function loadSalesStaff() {
  if (!allView.value) return
  try { salesStaff.value = await salesApi.salespeople() } catch { /* 静默 */ }
}
// 编辑下拉选项：真实名单 + 兜底当前已分配的人（防止其被停用后名字消失）
const editSalesOptions = computed(() => {
  const list = salesStaff.value.slice()
  const r = editRow.value
  if (r?.sales_uid && r.sales_name && !list.some((s) => s.id === r.sales_uid)) {
    list.unshift({ id: r.sales_uid, name: `${r.sales_name}（已停用）` })
  }
  return list
})

// 🆕 操作列折叠：宽操作列占版面，支持收起为「⋯」下拉，偏好持久化（按浏览器记忆）
const opCompact = ref(localStorage.getItem('sales_op_compact') === '1')
function toggleOpCompact() {
  opCompact.value = !opCompact.value
  localStorage.setItem('sales_op_compact', opCompact.value ? '1' : '0')
}


// 🆕 分页（性能优化：后端按编号自然序分页，前端不再整表渲染/排序）
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)
async function load() {
  loading.value = true
  try {
    const j = await salesApi.ledger({
      kw: filters.kw || undefined,
      cust_type: filters.cust_type || undefined,
      contract: filters.contract || undefined,
      sales_uid: filters.sales_uid,
      balance_month: filters.balance_month || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = j.rows   // 后端已按编号自然序返回，无需再客户端排序
    totals.value = j.totals || null
    total.value = j.total ?? j.rows.length
  } finally {
    loading.value = false
  }
}
// 改筛选 → 回到第 1 页再查；翻页 → 直接查
function reload() { page.value = 1; load() }
function onPage(p: number) { page.value = p; load() }
function onSize(s: number) { pageSize.value = s; page.value = 1; load() }   // 改每页条数 → 回第 1 页
onMounted(() => { load(); loadSalesStaff() })

// ===== 销售下单 =====
const orderVisible = ref(false)
const ordering = ref(false)
const openingOrder = ref(false)
const orderForm = reactive({
  code: '', name: '', qty: 1, unit: '台', customer: '', cust_type: '经销商', contract: '有',
  amount: 0, tax_rate: '13%', prepay: 0, prepay_note: '', before_ship: 0, before_ship_note: '',
  ship_receivable: 0,
  balance: 0, balance_date: '', depts: ['design', 'electric', 'produce'], req_text: '',
  receiver: { name: '', phone: '', addr: '' },
})
// 🆕 年度选项：选年份后按该年生成下一个编号（如 2027 → 2027-001），可跨年提前建档
const orderYear = ref(String(new Date().getFullYear()))
const yearOptions = computed(() => {
  const y = new Date().getFullYear()
  return [String(y), String(y + 1), String(y + 2)]
})
async function onYearChange() {
  try { orderForm.code = await salesApi.nextCode(orderYear.value) } catch { /* 留空人工填 */ }
}
// draftEditLid: null=新建下单；有值=修改被退回的草稿(走 draft-resubmit)
const draftEditLid = ref<number | null>(null)
async function openOrder() {
  openingOrder.value = true
  try {
    // 🆕 编号人工输入, 但自动带出最新建议编号作默认值(可改);年度默认当年
    orderYear.value = String(new Date().getFullYear())
    let suggested = ''
    try { suggested = await salesApi.nextCode(orderYear.value) } catch { /* 拿不到就留空人工填 */ }
    draftEditLid.value = null
    Object.assign(orderForm, {
      code: suggested, name: '', qty: 1, unit: '台', customer: '', cust_type: '经销商', contract: '有',
      amount: 0, tax_rate: '13%', prepay: 0, prepay_note: '', before_ship: 0, before_ship_note: '',
      ship_receivable: 0,
      balance: 0, balance_date: '', depts: ['design', 'electric', 'produce'], req_text: '',
      receiver: { name: '', phone: '', addr: '' },
    })
    orderFiles.value = []
    orderVisible.value = true
  } finally {
    openingOrder.value = false
  }
}
// 🆕 修改被退回的下单草稿：预填表单(含暂存的派单信息)后重新提交审批
function openDraftEdit(r: SalesLedgerRow) {
  draftEditLid.value = r.id
  const po: any = r.pending_order || {}
  Object.assign(orderForm, {
    code: r.code, name: r.name, qty: r.qty || 1, unit: r.unit || '台',
    customer: r.customer || '', cust_type: r.cust_type || '经销商',
    contract: r.contract || '有', amount: r.amount || 0,
    tax_rate: r.tax_rate === '/' ? '0' : (r.tax_rate || '13%'),
    prepay: r.prepay || 0, prepay_note: '', before_ship: r.before_ship || 0, before_ship_note: '',
    ship_receivable: r.ship_receivable || 0, balance: r.balance || 0, balance_date: r.balance_date || '',
    depts: (po.depts && po.depts.length) ? po.depts : ['design'],
    req_text: po.req_text || '',
    receiver: { name: po.receiver?.name || '', phone: po.receiver?.phone || '', addr: po.receiver?.addr || '' },
  })
  orderFiles.value = []
  orderVisible.value = true
}
// 🆕 下单资料（多选，PDF/Word/Excel）——下单后随附给各派往部门任务单
const orderFileInput = ref<HTMLInputElement | null>(null)
const orderFiles = ref<File[]>([])
function onPickOrderFiles(e: Event) {
  const fs = Array.from((e.target as HTMLInputElement).files || [])
  if (fs.length) orderFiles.value.push(...fs)
  ;(e.target as HTMLInputElement).value = ''  // 允许再次选同名文件/追加
}
function fmtFileSize(n: number): string {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}

async function submitOrder() {
  if (!orderForm.code.trim()) { ElMessage.warning('请填写项目编号'); return }
  if (!orderForm.name.trim()) { ElMessage.warning('请填写设备名称'); return }
  ordering.value = true
  try {
    // 🆕 修改被退回的草稿 → 重新提交审批
    if (draftEditLid.value) {
      const r = await salesApi.draftResubmit(draftEditLid.value, { ...orderForm })
      // 追加的下单资料继续暂存，审批通过后随附各部门任务
      if (orderFiles.value.length && r.ledger_id) {
        try { await salesApi.pendingFiles(r.ledger_id, orderFiles.value.slice()) } catch { /* 静默 */ }
      }
      ElMessage.success(`已重新提交审批（${r.code}），等待销售主管审批`)
      orderVisible.value = false
      draftEditLid.value = null
      await load()
      return
    }
    const r = await salesApi.createOrder({ ...orderForm })
    // order_ids 为空 = 命中下单审批开关，进入"待主管审批"（尚未创建各部门任务）
    if (!r.order_ids || r.order_ids.length === 0) {
      if (orderFiles.value.length && r.ledger_id) {
        // 🆕 下单资料暂存，审批通过后自动转挂各部门任务单
        try {
          await salesApi.pendingFiles(r.ledger_id, orderFiles.value.slice())
          ElMessage.success(`下单已提交待审批（${r.code}），资料(${orderFiles.value.length})已暂存，审批通过后自动随附各部门任务`)
        } catch {
          ElMessage.warning(`下单已提交待审批（${r.code}），但资料暂存失败，可在审批通过后到部门任务单补传`)
        }
      } else {
        ElMessage.success(`下单已提交（${r.code}），等待销售主管审批`)
      }
    } else if (orderFiles.value.length) {
      // 🆕 下单资料随附：传到每个生成的部门任务单(order_input)，接单负责人即可见
      const files = orderFiles.value.slice()
      try {
        await Promise.all(r.order_ids.map(oid => ordersApi.inputUpload(oid, files)))
        ElMessage.success(`已下单 ${r.code}，资料(${files.length})已随附给 ${r.order_ids.length} 个部门任务`)
      } catch {
        ElMessage.warning(`已下单 ${r.code}，但部分资料上传失败，可到对应部门任务单补传`)
      }
    } else {
      ElMessage.success(`已下单 ${r.code}，已推送各部门负责人分派`)
    }
    orderVisible.value = false
    await load()
  } finally {
    ordering.value = false
  }
}

// ===== 编辑（主管/管理层） =====
const editVisible = ref(false)
const editRow = ref<SalesLedgerRow | null>(null)
const editForm = reactive<any>({})
function openEdit(r: SalesLedgerRow) {
  editRow.value = r
  Object.assign(editForm, {
    name: r.name, customer: r.customer || '', cust_type: r.cust_type || '经销商',
    contract: r.contract, amount: r.amount, tax_rate: r.tax_rate === '/' ? '0' : (r.tax_rate || '13%'),
    prepay: r.prepay, before_ship: r.before_ship, ship_receivable: r.ship_receivable,
    balance: r.balance, balance_date: r.balance_date || '',
    sign_date: r.sign_date || '', deliver_date: r.deliver_date || '',
    sales_uid: r.sales_uid ?? undefined,
  })
  editVisible.value = true
}
async function submitEdit() {
  if (!editRow.value) return
  await salesApi.updateLedger(editRow.value.id, { ...editForm })
  ElMessage.success('已保存')
  editVisible.value = false
  await load()
}

// ===== 🆕 收款批注（预付 / 发货前付，支持插入时间戳） =====
const noteVisible = ref(false)
const noteRow = ref<SalesLedgerRow | null>(null)
const noteField = ref<'prepay' | 'before_ship'>('prepay')
const noteText = ref('')
const noteSaving = ref(false)
const noteInputRef = ref<any>(null)
const noteFieldLabel = computed(() => (noteField.value === 'prepay' ? '预付' : '发货前付'))

function openNote(r: SalesLedgerRow, field: 'prepay' | 'before_ship') {
  noteRow.value = r
  noteField.value = field
  noteText.value = (field === 'prepay' ? r.prepay_note : r.before_ship_note) || ''
  noteVisible.value = true
}
function nowStamp(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function insertNow() {
  const stamp = `【${nowStamp()}】`
  const ta: HTMLTextAreaElement | undefined = noteInputRef.value?.textarea
  if (ta && typeof ta.selectionStart === 'number') {
    const s = ta.selectionStart, e = ta.selectionEnd
    noteText.value = noteText.value.slice(0, s) + stamp + noteText.value.slice(e)
    nextTick(() => {
      ta.focus()
      const pos = s + stamp.length
      ta.setSelectionRange(pos, pos)
    })
  } else {
    // 退化：追加到末尾（带换行分隔）
    noteText.value = (noteText.value ? noteText.value.replace(/\s*$/, '') + '\n' : '') + stamp
  }
}
async function saveNote() {
  if (!noteRow.value) return
  noteSaving.value = true
  try {
    await salesApi.paymentNote(noteRow.value.id, noteField.value, noteText.value)
    // 本地同步，避免整表重载
    const v = noteText.value.trim() || null
    if (noteField.value === 'prepay') {
      noteRow.value.prepay_note = v
    } else {
      noteRow.value.before_ship_note = v
      // 与后端一致：发货前付有批注=货款收讫→发货款应收清零；删批注=未收→应收恢复为发货前付金额
      noteRow.value.ship_receivable = v ? 0 : (noteRow.value.before_ship || 0)
    }
    ElMessage.success('批注已保存')
    noteVisible.value = false
  } finally {
    noteSaving.value = false
  }
}

// ===== 🆕 全流程工作流（销售可查自己项目的全览，后端放行） =====
const wfVisible = ref(false)
const wfLoading = ref(false)
const wfData = ref<Workflow | null>(null)
const wfCode = ref('')
async function openWorkflow(r: SalesLedgerRow) {
  wfCode.value = r.code
  wfData.value = null
  wfVisible.value = true
  wfLoading.value = true
  try {
    wfData.value = await collabApi.workflow(r.project_id)
  } finally {
    wfLoading.value = false
  }
}

// ===== 上传合同 =====
const contractVisible = ref(false)
const contractRow = ref<SalesLedgerRow | null>(null)
const contractForm = reactive({ sign_date: '', deliver_date: '', file: null as File | null })
function openContract(r: SalesLedgerRow) {
  contractRow.value = r
  contractForm.sign_date = r.sign_date || ''
  contractForm.deliver_date = r.deliver_date || ''
  contractForm.file = null
  contractVisible.value = true
}
function pickContractFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) contractForm.file = f
}
async function submitContract() {
  const r = contractRow.value
  if (!r) return
  if (!contractForm.sign_date || !contractForm.deliver_date) { ElMessage.warning('请填写合同签订日期与交货日期'); return }
  if (!contractForm.file) { ElMessage.warning('请选择合同文件'); return }
  await salesApi.uploadContract(r.id, contractForm.file, contractForm.sign_date, contractForm.deliver_date)
  ElMessage.success('合同已上传，下单/交货日期已回写台账')
  contractVisible.value = false
  await load()
}

// ===== 开票申请 =====
async function applyInvoice(r: SalesLedgerRow) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.pdf,.xls,.xlsx,.doc,.docx'
  input.onchange = async () => {
    const f = input.files?.[0]
    if (!f) return
    await salesApi.invoiceApply(r.id, f)
    ElMessage.success('开票申请已提交，等待销售主管审批')
    await load()
  }
  input.click()
}

// ===== 🆕 合并开票申请（同客户多项目，一份申请 + 一张合并发票） =====
const mergeVisible = ref(false)
const mergeSelected = ref<SalesLedgerRow[]>([])
const mergeFile = ref<File | null>(null)
const mergeSubmitting = ref(false)
const mergeTableRef = ref()
// 可合并的项目：未进入开票流且非「不开票」
const mergeEligible = computed(() => rows.value.filter((r) => !r.invoice_state && !isNoInvoice(r.tax_rate)))
const mergeAmount = computed(() => mergeSelected.value.reduce((s, r) => s + (r.amount || 0), 0))
// 同客户约束：选中首个后，其它客户的行不可勾选（实现「002A/002B 同客户」口径）
function mergeSelectable(row: SalesLedgerRow) {
  if (!mergeSelected.value.length) return true
  return (row.customer || '') === (mergeSelected.value[0].customer || '')
}
// selectable 在选中变化时不一定即时重算，这里在 selection-change 兜底剔除跨客户勾选
let mergeSelGuard = false
function onMergeSel(sel: SalesLedgerRow[]) {
  if (mergeSelGuard) return
  if (sel.length <= 1) { mergeSelected.value = [...sel]; return }
  const cust = sel[0].customer || ''
  const bad = sel.filter((r) => (r.customer || '') !== cust)
  if (bad.length) {
    mergeSelGuard = true
    bad.forEach((r) => mergeTableRef.value?.toggleRowSelection(r, false))
    nextTick(() => { mergeSelGuard = false })
    ElMessage.warning('合并开票只能勾选同一客户的项目')
    mergeSelected.value = sel.filter((r) => (r.customer || '') === cust)
    return
  }
  mergeSelected.value = [...sel]
}
function openMerge() {
  mergeSelected.value = []
  mergeFile.value = null
  mergeVisible.value = true
  nextTick(() => mergeTableRef.value?.clearSelection?.())
}
function pickMergeFile() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.pdf,.xls,.xlsx,.doc,.docx'
  input.onchange = () => { mergeFile.value = input.files?.[0] || null }
  input.click()
}
async function submitMerge() {
  if (mergeSelected.value.length < 2) { ElMessage.warning('请至少勾选 2 个同客户项目'); return }
  if (!mergeFile.value) { ElMessage.warning('请上传合并开票申请表'); return }
  mergeSubmitting.value = true
  try {
    await salesApi.invoiceApplyMerge(mergeSelected.value.map((r) => r.id), mergeFile.value)
    ElMessage.success(`合并开票申请已提交（${mergeSelected.value.length} 个项目），等待销售主管审批`)
    mergeVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  } finally {
    mergeSubmitting.value = false
  }
}

// ===== 开票审批（主管）：单项目 + 🆕 合并批次混合展示 =====
const approvalVisible = ref(false)
const approvals = ref<SalesLedgerRow[]>([])
// 把审批列表按 invoice_batch_id 归组：合并批次显示为一组（一次通过/驳回），单项目各自一组
const approvalGroups = computed(() => {
  const singles: any[] = []
  const batches = new Map<number, SalesLedgerRow[]>()
  for (const r of approvals.value) {
    if (r.invoice_batch_id) {
      if (!batches.has(r.invoice_batch_id)) batches.set(r.invoice_batch_id, [])
      batches.get(r.invoice_batch_id)!.push(r)
    } else singles.push(r)
  }
  const groups = singles.map((r) => ({
    key: `s${r.id}`, isBatch: false, batchId: null as number | null, count: 1,
    codes: r.code, customer: r.customer, sales_name: r.sales_name, amount: r.amount || 0,
    apply_file_id: r.invoice_apply_file_id, apply_file_name: r.invoice_apply_file_name,
    ledgerId: r.id,
  }))
  for (const [bid, rs] of batches) {
    groups.push({
      key: `b${bid}`, isBatch: true, batchId: bid, count: rs.length,
      codes: rs.map((x) => x.code).join('、'), customer: rs[0].customer, sales_name: rs[0].sales_name,
      amount: rs.reduce((s, x) => s + (x.amount || 0), 0),
      apply_file_id: rs[0].invoice_apply_file_id, apply_file_name: rs[0].invoice_apply_file_name,
      ledgerId: rs[0].id,
    })
  }
  return groups
})
async function openApprovals() {
  approvals.value = (await salesApi.invoiceApprovals()).rows
  approvalVisible.value = true
}
async function approveGroup(g: any, ok: boolean) {
  if (!ok) {
    // 驳回会删除已上传的开票申请表（不可逆），加二次确认
    try {
      await ElMessageBox.confirm(
        g.isBatch
          ? `驳回后将删除该合并开票申请表（${g.count} 个项目一并退回），销售需重新申请。确认驳回？`
          : '驳回后将删除该开票申请表，销售需重新申请。确认驳回？',
        '开票驳回', { type: 'warning' })
    } catch { return }
  }
  if (g.isBatch) {
    if (ok) await salesApi.invoiceBatchApprove(g.batchId)
    else await salesApi.invoiceBatchReject(g.batchId)
  } else {
    if (ok) await salesApi.invoiceApprove(g.ledgerId)
    else await salesApi.invoiceReject(g.ledgerId)
  }
  ElMessage.success(ok ? '已通过，已推送财务部开票' : '已驳回')
  approvals.value = (await salesApi.invoiceApprovals()).rows
  await load()
}

// ===== 🆕 订单作废（销售员申请 → 负责人审批；负责人点即一键直接作废） =====
async function applyVoid(r: SalesLedgerRow) {
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(
      allView.value
        ? `确认作废订单「${r.code} ${r.name}」？通过后将同步移除项目目录、详单及各部门流程（软删可追溯）。请填写作废原因：`
        : `申请作废订单「${r.code} ${r.name}」，提交后需销售负责人审批。请填写作废原因：`,
      '订单作废',
      { inputPlaceholder: '作废原因（必填）', inputPattern: /\S+/, inputErrorMessage: '请填写作废原因',
        confirmButtonText: allView.value ? '确认作废' : '提交申请', type: 'warning' },
    )
    reason = value
  } catch { return }
  try {
    const res = await salesApi.voidApply(r.id, reason.trim())
    ElMessage.success(res.message || '已提交')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

// ===== 🆕 作废审批（负责人） =====
const voidApprovalVisible = ref(false)
const voidApprovals = ref<SalesLedgerRow[]>([])
async function openVoidApprovals() {
  voidApprovals.value = (await salesApi.voidApprovals()).rows
  voidApprovalVisible.value = true
}
async function approveVoid(r: SalesLedgerRow, ok: boolean) {
  if (!ok) {
    try {
      await ElMessageBox.confirm('驳回后订单恢复有效，销售可重新申请。确认驳回？', '作废驳回', { type: 'warning' })
    } catch { return }
    await salesApi.voidReject(r.id)
  } else {
    try {
      await ElMessageBox.confirm(
        `通过后将作废订单「${r.code}」并同步移除项目目录、详单及各部门流程（软删可追溯）。确认通过？`,
        '确认作废', { type: 'warning' })
    } catch { return }
    await salesApi.voidApprove(r.id)
  }
  ElMessage.success(ok ? '已通过，订单已作废' : '已驳回')
  voidApprovals.value = (await salesApi.voidApprovals()).rows
  await load()
}

// ===== 🆕 下单审批（销售主管）+ 放弃草稿 =====
const orderApprovalVisible = ref(false)
const orderApprovalRows = ref<SalesLedgerRow[]>([])
async function openOrderApprovals() {
  orderApprovalRows.value = (await salesApi.orderApprovals()).rows
  orderApprovalVisible.value = true
}
async function approveOrder(r: SalesLedgerRow, ok: boolean) {
  if (ok) {
    try {
      await ElMessageBox.confirm(`通过后将正式创建并推送「${r.code}」各部门任务与发货待办。确认通过？`,
        '确认下单', { type: 'warning' })
    } catch { return }
    await salesApi.orderApprove(r.id)
  } else {
    let reason = ''
    try {
      const { value } = await ElMessageBox.prompt('请填写退回原因（可留空），将通知销售修改后重新提交：',
        '退回下单', { inputType: 'textarea', confirmButtonText: '退回', inputValue: '' })
      reason = value || ''
    } catch { return }
    await salesApi.orderReject(r.id, reason.trim())
  }
  ElMessage.success(ok ? '已通过，各部门任务已派发' : '已退回销售修改')
  orderApprovalRows.value = (await salesApi.orderApprovals()).rows
  await load()
}
async function discardOrder(r: SalesLedgerRow) {
  try {
    await ElMessageBox.confirm(`放弃下单「${r.code} ${r.name}」？该下单尚未生效，放弃后将删除。`,
      '放弃下单', { type: 'warning' })
  } catch { return }
  await salesApi.orderDiscard(r.id)
  ElMessage.success('已放弃该下单')
  await load()
}

// 🆕 M14 销售报表
const reportVisible = ref(false)
const report = ref<SalesReport | null>(null)
async function openReport() {
  report.value = await reportsApi.sales()
  reportVisible.value = true
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>销售部</h1>
        <div class="desc">销售项目统计台账 · {{ allView ? '全部' : '我的' }} · 一项目一行；编号自动生成；开票申请→主管审批→财务开票回传</div>
      </div>
      <div class="spacer"></div>
      <el-button v-if="allView" type="primary" plain @click="openReport">📊 销售报表</el-button>
      <el-button v-if="allView" :icon="Stamp" @click="openApprovals">开票审批</el-button>
      <el-button v-if="allView" :icon="Select" @click="openOrderApprovals">下单审批</el-button>
      <el-button v-if="allView" :icon="CircleClose" @click="openVoidApprovals">作废审批</el-button>
      <el-button :icon="DocumentCopy" @click="openMerge">合并开票</el-button>
      <el-button type="primary" :icon="Plus" :loading="openingOrder" @click="openOrder">销售下单</el-button>
    </div>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="filter-bar">
        <el-input v-model="filters.kw" placeholder="搜索 编号/设备/客户" clearable style="width: 220px" @change="reload" />
        <el-select v-if="allView" v-model="filters.sales_uid" placeholder="销售员(全部)" clearable style="width: 150px" @change="reload">
          <el-option v-for="s in salesOptions" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select v-model="filters.cust_type" placeholder="客户分类(全部)" clearable style="width: 150px" @change="reload">
          <el-option label="经销商" value="经销商" />
          <el-option label="终端客户" value="终端客户" />
        </el-select>
        <el-select v-model="filters.contract" placeholder="合同(全部)" clearable style="width: 120px" @change="reload">
          <el-option label="有" value="有" />
          <el-option label="无" value="无" />
        </el-select>
        <el-date-picker v-model="filters.balance_month" type="month" value-format="YYYY-MM"
                        placeholder="尾款月份" clearable style="width: 150px" @change="reload" />
        <span class="muted">共 {{ total }} 个项目</span>
        <span class="spacer" style="flex:1"></span>
        <el-tooltip :content="opCompact ? '展开操作列（显示全部按钮）' : '收起操作列（省版面，操作收进 ⋯ 菜单）'" placement="top">
          <el-button :icon="Operation" @click="toggleOpCompact">{{ opCompact ? '展开操作列' : '收起操作列' }}</el-button>
        </el-tooltip>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="rows" stripe v-loading="loading" :show-summary="false"
                max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column type="index" label="#" width="48" fixed />
        <el-table-column label="项目编号" width="120" fixed>
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="设备名称" min-width="150" show-overflow-tooltip />
        <el-table-column prop="customer" label="客户单位" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.customer || '—' }}</template>
        </el-table-column>
        <el-table-column label="客户分类" width="92">
          <template #default="{ row }">
            <el-tag v-if="row.cust_type" size="small" :type="row.cust_type === '经销商' ? 'primary' : 'success'" effect="plain">{{ row.cust_type }}</el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="订单类别" width="150">
          <template #default="{ row }">
            <el-tag size="small"
                    :type="row.order_type === '调货订单' ? 'warning' : 'info'"
                    effect="plain">
              {{ row.order_type || '工厂制作订单' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="销售" width="80">
          <template #default="{ row }">{{ row.sales_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="下单日期" width="108">
          <template #default="{ row }">{{ fmtDate(row.sign_date) }}</template>
        </el-table-column>
        <el-table-column label="交货日期" width="108">
          <template #default="{ row }">{{ fmtDate(row.deliver_date) }}</template>
        </el-table-column>
        <el-table-column label="合同" width="86">
          <template #default="{ row }">
            <span v-if="row.contract_file_id" class="ct-cell">
              <StatusPill text="已签" variant="success" />
              <el-tooltip content="下载合同" placement="top">
                <el-icon class="ct-dl" @click="downloadAttachment({ id: row.contract_file_id!, name: row.contract_file_name || '合同' })">
                  <Download />
                </el-icon>
              </el-tooltip>
            </span>
            <StatusPill v-else-if="row.contract === '有'" text="有" variant="success" />
            <StatusPill v-else text="无" variant="muted" />
          </template>
        </el-table-column>
        <el-table-column label="金额" width="100" align="right">
          <template #default="{ row }">{{ fmtPay(row.amount) }}</template>
        </el-table-column>
        <el-table-column prop="tax_rate" label="税票" width="70">
          <template #default="{ row }">{{ row.tax_rate || '—' }}</template>
        </el-table-column>
        <el-table-column label="发票情况" width="124" align="center">
          <template #default="{ row }">
            <div class="inv-cell">
              <template v-if="row.invoice_state === 'invoiced'">
                <StatusPill text="已开票" variant="success" />
                <el-button v-if="row.invoice_file_id" size="small" link type="primary" class="inv-dl"
                           @click="downloadAttachment({ id: row.invoice_file_id!, name: row.invoice_file_name || '发票' })">
                  <el-icon><Download /></el-icon><span>下载发票</span>
                </el-button>
              </template>
              <StatusPill v-else-if="row.invoice_state === 'applying'" text="待主管审批" variant="warn" />
              <StatusPill v-else-if="row.invoice_state === 'pending_invoice'" text="待财务开票" variant="primary" />
              <StatusPill v-else-if="row.amount" text="未开票" variant="muted" />
              <span v-else class="muted">—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="预付" width="106" align="right">
          <template #default="{ row }">
            <div class="pay-cell">
              <span>{{ fmtPay(row.prepay) }}</span>
              <el-tooltip placement="top" :show-after="150">
                <template #content>
                  <div class="note-pop">{{ row.prepay_note || '点击添加收款批注（可插入时间）' }}</div>
                </template>
                <el-icon class="note-ico" :class="{ 'has-note': row.prepay_note }" @click="openNote(row, 'prepay')">
                  <ChatLineSquare />
                </el-icon>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="发货前付" width="106" align="right">
          <template #default="{ row }">
            <div class="pay-cell">
              <span>{{ fmtPay(row.before_ship) }}</span>
              <el-tooltip placement="top" :show-after="150">
                <template #content>
                  <div class="note-pop">{{ row.before_ship_note || '点击添加收款批注（可插入时间）' }}</div>
                </template>
                <el-icon class="note-ico" :class="{ 'has-note': row.before_ship_note }" @click="openNote(row, 'before_ship')">
                  <ChatLineSquare />
                </el-icon>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="发货款应收" width="100" align="right">
          <template #default="{ row }">{{ fmtPay(row.ship_receivable) }}</template>
        </el-table-column>
        <el-table-column label="尾款" width="92" align="right">
          <template #default="{ row }">{{ fmtPay(row.balance) }}</template>
        </el-table-column>
        <el-table-column label="发货日期 📦" width="105">
          <template #default="{ row }">
            <el-tooltip content="物流发货部确认发货时自动回传，销售不可填" placement="top">
              <span>{{ fmtDate(row.ship_date) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="尾款日期" width="118">
          <template #default="{ row }">{{ fmtDate(row.balance_date) }}</template>
        </el-table-column>
        <el-table-column label="操作" :width="opCompact ? 72 : 300" fixed="right" :align="opCompact ? 'center' : 'left'">
          <template #default="{ row }">
            <!-- 展开：行内按钮（原样） -->
            <div v-if="!opCompact" class="op-cell">
              <!-- 🆕 下单审批流：待审批/已退回时只显示对应动作（项目尚未生效，无详单/流程） -->
              <template v-if="row.order_state === 'pending'">
                <el-tag size="small" type="info" effect="plain">待审批</el-tag>
                <span class="op-sep">·</span>
                <el-button size="small" link class="op-del" @click="discardOrder(row)">放弃</el-button>
              </template>
              <template v-else-if="row.order_state === 'draft'">
                <el-tag size="small" type="warning" effect="plain">已退回</el-tag>
                <span class="op-sep">·</span>
                <el-button size="small" link class="op-main" @click="openDraftEdit(row)">修改重提</el-button>
                <span class="op-sep">·</span>
                <el-button size="small" link class="op-del" @click="discardOrder(row)">放弃</el-button>
              </template>
              <template v-else>
                <el-button v-if="allView" size="small" link type="primary" class="op-main" @click="openEdit(row)">编辑</el-button>
                <span v-if="allView" class="op-sep">·</span>
                <el-button size="small" link class="op-sub" @click="openWorkflow(row)">流程</el-button>
                <span class="op-sep">·</span>
                <el-button size="small" link class="op-sub" @click="openContract(row)">
                  {{ row.contract_file_id ? '换合同' : '上传合同' }}
                </el-button>
                <template v-if="!row.invoice_state && !isNoInvoice(row.tax_rate)">
                  <span class="op-sep">·</span>
                  <el-button size="small" link class="op-sub" @click="applyInvoice(row)">开票申请</el-button>
                </template>
                <span class="op-sep">·</span>
                <el-tag v-if="row.void_state === 'applying'" size="small" type="warning" effect="plain">作废待审批</el-tag>
                <el-button v-else size="small" link class="op-del" @click="applyVoid(row)">{{ allView ? '作废' : '申请作废' }}</el-button>
              </template>
            </div>
            <!-- 收起：操作收进「⋯」下拉，省版面 -->
            <el-dropdown v-else trigger="click" placement="bottom-end">
              <el-button size="small" text :icon="MoreFilled" class="op-more" />
              <template #dropdown>
                <el-dropdown-menu>
                  <template v-if="row.order_state === 'pending'">
                    <el-dropdown-item disabled>待审批</el-dropdown-item>
                    <el-dropdown-item divided @click="discardOrder(row)">放弃下单</el-dropdown-item>
                  </template>
                  <template v-else-if="row.order_state === 'draft'">
                    <el-dropdown-item @click="openDraftEdit(row)">修改重提</el-dropdown-item>
                    <el-dropdown-item divided @click="discardOrder(row)">放弃下单</el-dropdown-item>
                  </template>
                  <template v-else>
                    <el-dropdown-item v-if="allView" @click="openEdit(row)">编辑</el-dropdown-item>
                    <el-dropdown-item @click="openWorkflow(row)">流程</el-dropdown-item>
                    <el-dropdown-item @click="openContract(row)">{{ row.contract_file_id ? '换合同' : '上传合同' }}</el-dropdown-item>
                    <el-dropdown-item v-if="!row.invoice_state && !isNoInvoice(row.tax_rate)" @click="applyInvoice(row)">开票申请</el-dropdown-item>
                    <el-dropdown-item v-if="row.void_state === 'applying'" disabled divided>作废待审批</el-dropdown-item>
                    <el-dropdown-item v-else divided @click="applyVoid(row)">{{ allView ? '作废订单' : '申请作废' }}</el-dropdown-item>
                  </template>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length"
                 :text="filters.kw || filters.cust_type || filters.contract || filters.sales_uid ? '没有符合筛选条件的项目' : (allView ? '暂无销售项目' : '你还没有销售项目，点右上「销售下单」开始')" />

      <!-- 合计行（仅主管/管理层） -->
      <div v-if="totals" class="totals-bar">
        <span>合计（{{ totals.count }} 个项目）</span>
        <span>金额 <b>{{ fmtMoney(totals.amount) }}</b></span>
        <span>未开票 <b class="warn">{{ fmtMoney(totals.uninvoiced) }}</b></span>
        <span>预付 <b>{{ fmtMoney(totals.prepay) }}</b></span>
        <span>发货前付 <b>{{ fmtMoney(totals.before_ship) }}</b></span>
        <span>发货款应收 <b>{{ fmtMoney(totals.ship_receivable) }}</b></span>
        <span>尾款 <b>{{ fmtMoney(totals.balance) }}</b></span>
      </div>

      <!-- 🆕 分页（后端分页）+ 每页条数选择（最大 200） -->
      <div v-if="total > 20" class="pager">
        <el-pagination background layout="prev, pager, next, sizes, jumper, ->, total"
                       :total="total" :page-size="pageSize" :page-sizes="[20, 50, 100, 200]"
                       :current-page="page" @current-change="onPage" @size-change="onSize" />
      </div>
    </el-card>

    <!-- ===== 销售下单 / 修改重提 ===== -->
    <el-dialog v-model="orderVisible" :title="draftEditLid ? '✏️ 修改重提下单' : '💼 销售下单'" width="640px" :close-on-click-modal="false" class="v3-scroll-dialog">
      <el-alert :type="draftEditLid ? 'warning' : 'info'" :closable="false" style="margin-bottom: 14px"
                :title="draftEditLid ? '修改被退回的下单，提交后重新进入销售主管审批' : '下单日期/交货日期在「上传合同」时填写；发货日期由物流部确认发货时自动回传'" />
      <el-form label-position="top">
        <div class="fsec">📋 项目信息</div>
        <div class="frow">
          <el-form-item label="年度" style="flex: 0 0 110px">
            <el-select v-model="orderYear" @change="onYearChange" :disabled="!!draftEditLid">
              <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
            </el-select>
          </el-form-item>
          <el-form-item label="项目编号（可改）" required style="flex: 1">
            <el-input v-model="orderForm.code" placeholder="如 2026-057 / 2026-050C" maxlength="64" clearable :disabled="!!draftEditLid" />
          </el-form-item>
          <el-form-item label="设备名称" required style="flex: 1">
            <el-input v-model="orderForm.name" placeholder="如 300L真空乳化机" />
          </el-form-item>
          <el-form-item label="数量" style="flex: 0 0 150px">
            <el-input-number v-model="orderForm.qty" :min="1" :controls="false" style="width: 80px" />
            <el-select v-model="orderForm.unit" style="width: 64px; margin-left: 6px">
              <el-option label="台" value="台" /><el-option label="套" value="套" />
            </el-select>
          </el-form-item>
        </div>
        <div class="fsec">🤝 客户与合同</div>
        <div class="frow">
          <el-form-item label="客户单位" style="flex: 1"><el-input v-model="orderForm.customer" /></el-form-item>
          <el-form-item label="客户分类" style="flex: 1">
            <el-select v-model="orderForm.cust_type" style="width: 100%">
              <el-option label="经销商" value="经销商" /><el-option label="终端客户" value="终端客户" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="合同" style="flex: 1">
            <el-select v-model="orderForm.contract" style="width: 100%">
              <el-option label="有" value="有" /><el-option label="无" value="无" />
            </el-select>
          </el-form-item>
          <el-form-item label="金额(元)" style="flex: 1">
            <el-input-number v-model="orderForm.amount" :min="0" :controls="false" style="width: 100%" />
          </el-form-item>
          <el-form-item label="税票" style="flex: 1">
            <el-select v-model="orderForm.tax_rate" style="width: 100%">
              <el-option label="13%" value="13%" /><el-option label="0（不开票）" value="0" />
            </el-select>
          </el-form-item>
        </div>
        <div class="fsec">💰 收款（选填）</div>
        <div class="frow">
          <el-form-item label="预付" style="flex: 1"><el-input-number v-model="orderForm.prepay" :min="0" :controls="false" style="width: 100%" /></el-form-item>
          <el-form-item label="发货前付" style="flex: 1"><el-input-number v-model="orderForm.before_ship" :min="0" :controls="false" style="width: 100%" /></el-form-item>
          <el-form-item label="发货款应收" style="flex: 1"><el-input-number v-model="orderForm.ship_receivable" :min="0" :controls="false" style="width: 100%" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="尾款" style="flex: 1"><el-input-number v-model="orderForm.balance" :min="0" :controls="false" style="width: 100%" @change="() => { if (!orderForm.balance) orderForm.balance_date = '' }" /></el-form-item>
          <el-form-item label="尾款日期" style="flex: 1">
            <el-date-picker v-model="orderForm.balance_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" :disabled="!orderForm.balance" placeholder="尾款为0时无需填" />
          </el-form-item>
        </div>
        <div class="fsec">🛠 派单</div>
        <el-form-item label="派往部门（可多选，可不选）">
          <div style="width: 100%">
            <el-checkbox-group v-model="orderForm.depts">
              <el-checkbox value="design">📐 设计部</el-checkbox>
              <el-checkbox value="electric">⚡ 电工部</el-checkbox>
              <el-checkbox value="produce">🏭 生产部</el-checkbox>
            </el-checkbox-group>
            <div v-if="!orderForm.depts.length" class="muted" style="font-size: 12px; margin-top: 4px">
              ⓘ 未选任何部门 = 调货订单：不流转生产，仅创建发货待办同步发货部
            </div>
          </div>
        </el-form-item>
        <el-form-item label="下单要求">
          <el-input v-model="orderForm.req_text" type="textarea" :rows="2" placeholder="技术要求/交底说明" />
        </el-form-item>
        <el-form-item label="下单资料（PDF / Word / Excel，可多选）">
          <div style="width: 100%">
            <div class="up-box" @click="orderFileInput?.click()">
              <el-icon><Paperclip /></el-icon>
              <span class="up-box-t">选择文件上传</span>
              <small class="up-box-s">下单后随附给所选部门任务单，负责人接单即可见</small>
            </div>
            <input ref="orderFileInput" type="file" multiple
                   accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png" hidden @change="onPickOrderFiles" />
            <div v-if="orderFiles.length" class="up-list">
              <div v-for="(f, i) in orderFiles" :key="i" class="up-item">
                <el-icon class="up-item-ico"><Document /></el-icon>
                <span class="up-name" :title="f.name">{{ f.name }}</span>
                <span class="up-size">{{ fmtFileSize(f.size) }}</span>
                <el-icon class="up-rm" @click="orderFiles.splice(i, 1)"><Close /></el-icon>
              </div>
            </div>
          </div>
        </el-form-item>
        <div class="fsec">📍 收货信息</div>
        <div class="frow">
          <el-form-item label="收货人 / 单位" style="flex: 1"><el-input v-model="orderForm.receiver.name" /></el-form-item>
          <el-form-item label="联系电话" style="flex: 1"><el-input v-model="orderForm.receiver.phone" /></el-form-item>
        </div>
        <el-form-item label="收货地址"><el-input v-model="orderForm.receiver.addr" placeholder="省市区 + 详细地址" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="orderVisible = false">取消</el-button>
        <el-button type="primary" :loading="ordering" @click="submitOrder">下单</el-button>
      </template>
    </el-dialog>

    <!-- ===== 编辑台账 ===== -->
    <el-dialog v-model="editVisible" :title="`✏️ 编辑台账 · ${editRow?.code || ''}`" width="560px" class="v3-scroll-dialog">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="设备名称" style="flex: 1"><el-input v-model="editForm.name" /></el-form-item>
          <el-form-item label="客户单位" style="flex: 1"><el-input v-model="editForm.customer" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="客户分类" style="flex: 1">
            <el-select v-model="editForm.cust_type" style="width: 100%">
              <el-option label="经销商" value="经销商" /><el-option label="终端客户" value="终端客户" />
            </el-select>
          </el-form-item>
          <el-form-item label="合同" style="flex: 1">
            <el-select v-model="editForm.contract" style="width: 100%">
              <el-option label="有" value="有" /><el-option label="无" value="无" />
            </el-select>
          </el-form-item>
          <el-form-item label="金额(元)" style="flex: 1"><el-input-number v-model="editForm.amount" :min="0" :controls="false" style="width: 100%" /></el-form-item>
          <el-form-item label="税票" style="flex: 1">
            <el-select v-model="editForm.tax_rate" style="width: 100%">
              <el-option label="13%" value="13%" /><el-option label="0（不开票）" value="0" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="销售员" style="flex: 1">
            <el-select v-model="editForm.sales_uid" filterable clearable placeholder="选择销售员" style="width: 100%">
              <el-option v-for="s in editSalesOptions" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="下单日期（合同签订）" style="flex: 1">
            <el-date-picker v-model="editForm.sign_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
          </el-form-item>
          <el-form-item label="交货日期" style="flex: 1">
            <el-date-picker v-model="editForm.deliver_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="预付" style="flex: 1"><el-input-number v-model="editForm.prepay" :min="0" :controls="false" style="width: 100%" /></el-form-item>
          <el-form-item label="发货前付" style="flex: 1"><el-input-number v-model="editForm.before_ship" :min="0" :controls="false" style="width: 100%" /></el-form-item>
          <el-form-item label="发货款应收" style="flex: 1"><el-input-number v-model="editForm.ship_receivable" :min="0" :controls="false" style="width: 100%" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="尾款" style="flex: 1"><el-input-number v-model="editForm.balance" :min="0" :controls="false" style="width: 100%" @change="() => { if (!editForm.balance) editForm.balance_date = '' }" /></el-form-item>
          <el-form-item label="尾款日期" style="flex: 1">
            <el-date-picker v-model="editForm.balance_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" :disabled="!editForm.balance" placeholder="尾款为0时无需填" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ===== 🆕 收款批注（预付 / 发货前付） ===== -->
    <el-dialog v-model="noteVisible" :title="`📝 ${noteFieldLabel}收款批注 · ${noteRow?.code || ''}`" width="480px" append-to-body>
      <el-alert type="info" :closable="false" style="margin-bottom: 12px"
                title="记录该笔款项的收款时间、金额、方式等；点「插入当前时间」可在光标处快速插入时间戳" />
      <el-input ref="noteInputRef" v-model="noteText" type="textarea" :rows="6" resize="none"
                placeholder="如：【2026-06-16 14:30】收到预付款 3 万元（微信转账，已对账）" />
      <div style="margin-top: 10px">
        <el-button size="small" :icon="Clock" @click="insertNow">插入当前时间</el-button>
        <span class="muted" style="margin-left: 8px; font-size: 12px">时间格式：YYYY-MM-DD HH:mm</span>
      </div>
      <template #footer>
        <el-button @click="noteVisible = false">取消</el-button>
        <el-button type="primary" :loading="noteSaving" @click="saveNote">保存</el-button>
      </template>
    </el-dialog>

    <!-- ===== 🆕 全流程工作流 ===== -->
    <el-dialog v-model="wfVisible" :title="`🔗 全流程进度 · ${wfCode}`" width="92%" top="5vh" append-to-body>
      <div v-loading="wfLoading" style="min-height: 200px">
        <WorkflowGraph v-if="wfData" :wf="wfData" />
      </div>
    </el-dialog>

    <!-- ===== 上传合同 ===== -->
    <el-dialog v-model="contractVisible" :title="`📄 上传合同 · ${contractRow?.code || ''}`" width="500px">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px"
                title="合同签订日期 = 下单日期；提交后两日期自动回写台账与项目目录" />
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="合同签订日期（=下单日期）" required style="flex: 1">
            <el-date-picker v-model="contractForm.sign_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
          </el-form-item>
          <el-form-item label="交货日期" required style="flex: 1">
            <el-date-picker v-model="contractForm.deliver_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
          </el-form-item>
        </div>
        <el-form-item label="合同文件" required>
          <FilePicker v-model="contractForm.file" accept=".pdf,.doc,.docx,.jpg,.png" placeholder="选择合同文件（PDF/Word/图片）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="contractVisible = false">取消</el-button>
        <el-button type="primary" @click="submitContract">提交</el-button>
      </template>
    </el-dialog>

    <!-- ===== 销售报表 ===== -->
    <el-dialog v-model="reportVisible" title="📊 销售部报表" width="880px" class="v3-scroll-dialog">
      <div v-if="report">
        <!-- 经营概览：合同总额为核心，已/待开票语义配色 -->
        <div class="sec-title">经营概览</div>
        <div class="kpi-grid">
          <div class="kpi is-primary"><div class="kpi-v">{{ fmtMoney(report.total_amount) }}</div><div class="kpi-l">合同总额 · {{ report.project_count }} 个项目</div></div>
          <div class="kpi is-good"><div class="kpi-v">{{ fmtMoney(report.invoiced_amount) }}</div><div class="kpi-l">已开票额</div></div>
          <div class="kpi is-warn"><div class="kpi-v">{{ fmtMoney(report.uninvoiced_amount) }}</div><div class="kpi-l">待开票额</div></div>
          <div class="kpi"><div class="kpi-v">{{ report.shipped_count }} / {{ report.project_count }}</div><div class="kpi-l">已发货项目</div></div>
        </div>
        <!-- 关键率用进度条而非干巴巴百分比 -->
        <div style="margin-top:14px">
          <div class="rate-bar"><span class="rb-label">开票完成率</span><div class="rb-track"><div class="rb-fill good" :style="{ width: (report.invoice_rate || 0) + '%' }"></div></div><span class="rb-val">{{ report.invoice_rate ?? 0 }}%</span></div>
          <div class="rate-bar"><span class="rb-label">合同覆盖率</span><div class="rb-track"><div class="rb-fill" :style="{ width: (report.contract_rate || 0) + '%' }"></div></div><span class="rb-val">{{ report.contract_rate ?? 0 }}%</span></div>
        </div>
        <div class="sec-title">销售员业绩</div>
        <el-table :data="report.by_salesperson" size="small" stripe>
          <el-table-column prop="name" label="销售员" min-width="100" />
          <el-table-column prop="count" label="项目数" width="80" />
          <el-table-column label="合同额" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
          <el-table-column label="已开票额" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.invoiced) }}</template></el-table-column>
          <el-table-column label="已发货" width="80"><template #default="{ row }">{{ row.shipped }}</template></el-table-column>
          <el-table-column label="占比" width="70"><template #default="{ row }">{{ row.pct }}%</template></el-table-column>
        </el-table>
        <el-row :gutter="14" style="margin-top:8px">
          <el-col :span="12">
            <div class="sec-title">客户分类</div>
            <el-table :data="report.by_cust_type" size="small">
              <el-table-column prop="type" label="分类" /><el-table-column prop="count" label="项目" width="70" />
              <el-table-column label="金额" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            </el-table>
          </el-col>
          <el-col :span="12">
            <div class="sec-title">开票状态</div>
            <el-table :data="report.by_invoice_state" size="small">
              <el-table-column prop="label" label="状态" /><el-table-column prop="count" label="项目" width="70" />
              <el-table-column label="金额" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            </el-table>
          </el-col>
        </el-row>
        <div class="sec-title">收款计划汇总</div>
        <div class="rcv-row">
          <span>预付 <b>{{ fmtMoney(report.receivables.prepay) }}</b></span>
          <span>发货前付 <b>{{ fmtMoney(report.receivables.before_ship) }}</b></span>
          <span>发货款应收 <b>{{ fmtMoney(report.receivables.ship_receivable) }}</b></span>
          <span>尾款 <b>{{ fmtMoney(report.receivables.balance) }}</b></span>
        </div>
      </div>
    </el-dialog>

    <!-- ===== 开票审批（主管）：单项目 + 🆕 合并批次 ===== -->
    <el-dialog v-model="approvalVisible" title="🧾 开票审批" width="760px" class="v3-scroll-dialog">
      <EmptyHint v-if="!approvalGroups.length" text="暂无待审批的开票申请" size="sm" />
      <el-table v-else :data="approvalGroups" row-key="key" stripe>
        <el-table-column label="项目编号" min-width="150">
          <template #default="{ row }">
            <el-tag v-if="row.isBatch" size="small" type="warning" effect="plain" style="margin-right:4px">合并{{ row.count }}</el-tag>
            <span>{{ row.codes }}</span>
          </template>
        </el-table-column>
        <el-table-column label="客户" min-width="110">
          <template #default="{ row }">{{ row.customer || '—' }}</template>
        </el-table-column>
        <el-table-column label="销售" width="84">
          <template #default="{ row }">{{ row.sales_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="金额" width="110" align="right">
          <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="申请表" min-width="140">
          <template #default="{ row }">
            <el-button v-if="row.apply_file_id" size="small" link type="primary"
                       @click="downloadAttachment({ id: row.apply_file_id, name: row.apply_file_name || '申请表' })">
              {{ row.apply_file_name }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="156">
          <template #default="{ row }">
            <el-button size="small" type="success" :icon="Check" @click="approveGroup(row, true)">通过</el-button>
            <el-button size="small" @click="approveGroup(row, false)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- ===== 🆕 合并开票申请（同客户多项目，一份申请 + 一张合并发票） ===== -->
    <el-dialog v-model="mergeVisible" title="🧾 合并开票申请" width="780px" class="v3-scroll-dialog">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px"
                title="勾选同一客户的多个项目合并开票：选中首个项目后，其它客户的项目将不可勾选。上传一份合并开票申请表，主管审批通过后由财务开具一张合并发票。" />
      <el-table ref="mergeTableRef" :data="mergeEligible" stripe max-height="380" @selection-change="onMergeSel">
        <el-table-column type="selection" width="46" :selectable="mergeSelectable" />
        <el-table-column prop="code" label="项目编号" width="120" />
        <el-table-column prop="name" label="设备名称" min-width="150" />
        <el-table-column label="客户单位" min-width="120">
          <template #default="{ row }">{{ row.customer || '—' }}</template>
        </el-table-column>
        <el-table-column label="金额" width="110" align="right">
          <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!mergeEligible.length" text="暂无可合并开票的项目（需未开票且税票非「不开票」）" size="sm" />
      <div class="merge-foot">
        <div class="merge-sum">
          <template v-if="mergeSelected.length">
            已选 <b>{{ mergeSelected.length }}</b> 个 · 客户「{{ mergeSelected[0].customer || '—' }}」 · 合计 <b>{{ fmtMoney(mergeAmount) }}</b>
          </template>
          <span v-else class="muted">请勾选 2 个及以上同客户项目</span>
        </div>
        <div class="merge-up" @click="pickMergeFile">
          <el-icon><UploadFilled /></el-icon>
          <span>{{ mergeFile ? mergeFile.name : '点击选择合并开票申请表' }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="mergeVisible = false">取消</el-button>
        <el-button type="primary" :loading="mergeSubmitting"
                   :disabled="mergeSelected.length < 2 || !mergeFile" @click="submitMerge">
          提交合并申请
        </el-button>
      </template>
    </el-dialog>

    <!-- ===== 🆕 作废审批（销售负责人） ===== -->
    <el-dialog v-model="voidApprovalVisible" title="🗑️ 订单作废审批" width="720px" class="v3-scroll-dialog">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 10px"
                title="通过后将作废订单并同步移除项目目录、项目详单及各部门流程（软删可追溯，必要时管理层可恢复）。" />
      <EmptyHint v-if="!voidApprovals.length" text="暂无待审批的作废申请" size="sm" />
      <el-table v-else :data="voidApprovals" stripe>
        <el-table-column prop="code" label="项目编号" width="110" />
        <el-table-column prop="name" label="设备名称" min-width="130" />
        <el-table-column label="销售" width="84">
          <template #default="{ row }">{{ row.sales_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="金额" width="100" align="right">
          <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="作废原因" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.void_reason || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="156">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="approveVoid(row, true)">通过作废</el-button>
            <el-button size="small" @click="approveVoid(row, false)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- ===== 🆕 下单审批（销售主管） ===== -->
    <el-dialog v-model="orderApprovalVisible" title="📝 销售下单审批" width="760px" class="v3-scroll-dialog">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px"
                title="通过后才正式创建并推送各部门任务与物流发货待办；退回则销售可修改后重新提交。" />
      <EmptyHint v-if="!orderApprovalRows.length" text="暂无待审批的下单" size="sm" />
      <el-table v-else :data="orderApprovalRows" stripe>
        <el-table-column prop="code" label="项目编号" width="110" />
        <el-table-column prop="name" label="设备名称" min-width="130" />
        <el-table-column label="客户" min-width="100">
          <template #default="{ row }">{{ row.customer || '—' }}</template>
        </el-table-column>
        <el-table-column label="销售" width="84">
          <template #default="{ row }">{{ row.sales_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="金额" width="100" align="right">
          <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="派往" min-width="120">
          <template #default="{ row }">{{ (row.pending_order?.depts || []).map((d:string)=>({design:'设计',electric:'电工',produce:'生产'}[d]||d)).join('、') || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button size="small" type="success" :icon="Check" @click="approveOrder(row, true)">通过</el-button>
            <el-button size="small" @click="approveOrder(row, false)">退回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.code { color: var(--primary, #2563eb); white-space: nowrap; }
.dl-icon { cursor: pointer; color: var(--primary, #2563eb); margin-left: 4px; vertical-align: -2px; }
/* 🆕 v4 操作列: 主操作蓝色, 次操作灰色 hover 才蓝, 中点 · 分隔 */
.op-cell { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.op-cell .el-button.is-link { padding: 3px 4px !important; height: auto !important; font-size: 13px; }
.op-cell .op-main { color: var(--primary, #2563eb); font-weight: 500; }
.op-cell .op-sub :deep(span) { color: var(--text-3, #9ca3af) !important; transition: color .12s; }
.op-cell .op-sub:hover :deep(span) { color: var(--primary, #2563eb) !important; }
/* 🆕 作废操作：默认灰、hover 转危险红 */
.op-cell .op-del :deep(span) { color: var(--text-3, #9ca3af) !important; transition: color .12s; }
.op-cell .op-del:hover :deep(span) { color: var(--el-color-danger, #f56c6c) !important; }
.op-del { color: var(--text-3, #9ca3af); }
.op-del:hover { color: var(--el-color-danger, #f56c6c); }
.op-cell .op-sep { color: var(--text-4, #d1d5db); font-size: 11px; user-select: none; }
/* 🆕 操作列收起态：⋯ 触发按钮 */
.op-more { color: var(--text-3, #9ca3af); font-size: 18px; padding: 4px 6px; }
.op-more:hover { color: var(--primary, #2563eb); }
/* 🆕 销售下单·下单资料多选上传 */
.up-box {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; padding: 14px; width: 100%;
  border: 1px dashed var(--border, #d1d5db); border-radius: 8px;
  color: var(--text-3, #9ca3af); cursor: pointer; transition: all .15s;
}
.up-box:hover { border-color: var(--primary, #2563eb); color: var(--primary, #2563eb); background: rgba(37,99,235,.03); }
.up-box-t { font-size: 13.5px; font-weight: 500; }
.up-box-s { font-size: 12px; color: var(--text-4, #cbd5e1); }
/* 🆕 合并开票弹窗底部：已选汇总 + 申请表上传 */
.merge-foot { display: flex; align-items: center; gap: 14px; margin-top: 12px; }
.merge-sum { font-size: 13px; color: var(--el-text-color-regular); }
.merge-sum b { color: var(--primary, #2563eb); }
.merge-up {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px; border: 1px dashed var(--border, #d1d5db); border-radius: 8px;
  color: var(--text-3, #9ca3af); cursor: pointer; transition: all .15s; font-size: 13px;
}
.merge-up:hover { border-color: var(--primary, #2563eb); color: var(--primary, #2563eb); background: rgba(37,99,235,.03); }
.up-list { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.up-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border: 1px solid var(--border-light, #eef0f3); border-radius: 6px;
  background: var(--el-fill-color-light); font-size: 13px;
}
.up-item-ico { color: var(--primary, #2563eb); flex-shrink: 0; }
.up-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-1); }
.up-size { font-size: 12px; color: var(--text-3, #9ca3af); flex-shrink: 0; font-variant-numeric: tabular-nums; }
.up-rm { color: var(--text-3, #9ca3af); cursor: pointer; flex-shrink: 0; padding: 2px; border-radius: 4px; transition: all .12s; }
.up-rm:hover { background: rgba(239,68,68,.1); color: var(--danger, #ef4444); }
/* 🆕 预付/发货前付列: 金额 + 收款批注图标(有批注高亮蓝) */
.pay-cell { display: inline-flex; align-items: center; gap: 6px; justify-content: flex-end; width: 100%; }
.pay-cell .note-ico {
  font-size: 14px; color: var(--text-4, #cbd5e1); cursor: pointer;
  flex-shrink: 0; transition: color .15s;
}
.pay-cell .note-ico:hover { color: var(--primary, #2563eb); }
.pay-cell .note-ico.has-note { color: var(--primary, #2563eb); }
.note-pop { max-width: 280px; white-space: pre-line; line-height: 1.6; font-size: 12.5px; }
/* 🆕 v4 合同列: pill + 旁挂下载图标 */
.ct-cell { display: inline-flex; align-items: center; gap: 6px; }
.ct-cell .ct-dl { cursor: pointer; color: var(--text-3, #9ca3af); font-size: 14px; transition: color .12s; }
.ct-cell .ct-dl:hover { color: var(--primary, #2563eb); }
.totals-bar {
  display: flex; gap: 22px; flex-wrap: wrap;
  padding: 12px 14px; margin-top: 8px;
  background: var(--el-fill-color-light); border-radius: 8px;
  font-size: 13px; color: var(--el-text-color-secondary);
}
.totals-bar b { color: var(--el-text-color-primary); }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.totals-bar .warn { color: var(--warning); }
.fsec {
  font-size: 13px; font-weight: 600; color: var(--el-text-color-primary);
  border-left: 3px solid var(--primary, #2563eb);
  padding-left: 8px; margin: 6px 0 12px;
}
.frow { display: flex; gap: 12px; flex-wrap: wrap; }
.frow > * { flex: 1; min-width: 140px; }
.rcv-row { display: flex; gap: 24px; flex-wrap: wrap; padding: 12px 14px; background: var(--el-fill-color-light); border-radius: 8px; font-size: 13px; }
.rcv-row b { color: var(--el-text-color-primary); }
/* 发票情况单元格：标签 + 下载链接纵向居中、紧凑 */
.inv-cell { display: flex; flex-direction: column; align-items: center; gap: 3px; }
.inv-dl { height: auto; padding: 0; font-size: 12px; }
.inv-dl :deep(.el-icon) { margin-right: 2px; }
</style>
