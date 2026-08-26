<script setup lang="ts">
// 🆕 v3 M09 财务部：待开票 / 已开票 / 售后费用 / 请款审批 四 tab
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { http } from '@/api'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/api/sales'
import { specOf } from '@/utils/format'   // 反馈#387：名称里已含规格时别再拼一遍
import EmptyHint from '@/components/EmptyHint.vue'
import { useAuthStore } from '@/stores/auth'
import PageRefresh from '@/components/PageRefresh.vue'   // 反馈#359：每个页面都有刷新

const auth = useAuthStore()
const isManager = computed(() => auth.hasRole('admin', 'manager'))
const tv = (name: string) => auth.tabVisible('finance', name)   // 🆕 #7 按账号二级菜单授权

interface PaymentRequestOut {
  id: number; supplier_id: number; supplier_name: string
  requested_amount: number; requester_id: number; requester_name: string
  status: string; notes?: string | null
  finance_approver_id?: number | null; approver_name?: string | null; approved_at?: string | null
  paid_amount?: number | null; paid_date?: string | null; payment_method?: string | null
  pay_voucher_file_id?: number | null; pay_voucher_name?: string | null
  reject_reason?: string | null; created_at: string
  reject_stage?: string | null; rejecter_name?: string | null; rejected_at?: string | null   // 🆕 驳回环节/退回人
  // 🆕 需求十六：付款时可见收款账户信息 + 关联采购单
  supplier_bank_name?: string | null; supplier_bank_account?: string | null; supplier_tax_no?: string | null
  po_nos?: string[]
  project_codes?: string[]   // 🆕 反馈#298 请款审批列表「项目编号」列（关联采购明细的项目编号，去重）
  // 🆕 盈利改善2·应付账期：最早到期日(到货+账期) 与 距到期天数(负=已逾期)
  earliest_due?: string | null; due_in_days?: number | null
  items: { item_id: number; allocated_amount: number; item_name?: string; po_no?: string | null; spec?: string | null; project_code?: string | null; received_amount?: number }[]
}

interface InvoiceRow {
  ledger_id: number; code: string; name: string; customer?: string | null
  sales_name?: string | null; amount: number; tax_rate?: string | null
  invoice_batch_id?: number | null   // 🆕 合并开票批次号；同批多行共享，一次开一张合并发票
  apply_file_id?: number | null; apply_file_name?: string | null
  invoice_file_id?: number | null; invoice_file_name?: string | null
}
// 视图行：在 InvoiceRow 基础上叠加合并组的展示字段
type ViewRow = InvoiceRow & { _isBatch: boolean; _count: number; _codes: string }
interface AsItem {
  id: number; name: string; amount: number
  invoice_file_id?: number | null; invoice_file_name?: string | null
}
interface AsRow {
  id: number; kind: string; code: string; name: string; problem: string; cost: number
  // 🆕 报销支腿 + 逐行发票
  pay_status?: string | null; pay_note?: string | null
  items?: AsItem[]; missing_invoice?: number
  mat_file_id?: number | null; mat_file_name?: string | null
  created_by_name?: string | null; created_at?: string   // 🆕 #361 谁报的
}
const KIND_TXT: Record<string, string> = { aftersales: '售后', install: '安装' }
const AS_PAY_TXT: Record<string, string> = {
  checking: '待核对', invoice_fix: '已退回', reimbursed: '已安排报销',
}

const tab = ref('pending')
const loading = ref(false)
const pending = ref<InvoiceRow[]>([])
const invoiced = ref<InvoiceRow[]>([])
const aftersales = ref<AsRow[]>([])
const asTotal = ref(0)

// 🆕 采购应付（读采购供应商账目）+ 库存金额 / 项目成本
interface PayableRow { supplier_id: number; supplier_name: string; category?: string | null
  received_total: number; invoice_total: number; paid_total: number; outstanding: number; item_count: number }
const payables = ref<PayableRow[]>([])
const payablesLoading = ref(false)
async function loadPayables() {
  payablesLoading.value = true
  try { payables.value = (await http.get<{ rows: PayableRow[] }>('/purchase-mgmt/statements')).data.rows || [] }
  finally { payablesLoading.value = false }
}
const payablesTotal = computed(() => payables.value.reduce((s, r) => s + (r.outstanding || 0), 0))
// 🆕 反馈#373/#388：物料按有没有项目编号一刀切开——
//   有编号的料在**收货那一刻**就算那个项目的成本（不再等领料出库），并退出库存金额；
//   没编号的料才是公司库存，被领用出库时才转成领用项目的成本。
//   改之前生产上：收货 ¥421,444 只认出 ¥163,697 的项目成本，六成钱在系统里蒸发；
//   库存金额 ¥148,099 里 ¥116,718(79%) 其实早已名花有主。
const invValue = ref<{ total_value: number; rows: any[]; excluded_value?: number; excluded_count?: number }>({ total_value: 0, rows: [] })
const projCost = ref<{ project_id: number; code: string; name: string; cost: number }[]>([])
const projCostExtra = ref<{ unassigned?: number; note?: string }>({})
const invLoading = ref(false)
async function loadInventory() {
  invLoading.value = true
  try {
    const [iv, pc] = await Promise.all([
      http.get<{ total_value: number; rows: any[]; excluded_value?: number; excluded_count?: number }>('/wh/inventory-value').then(r => r.data),
      http.get<{ rows: any[]; unassigned?: number; note?: string }>('/wh/project-cost').then(r => r.data),
    ])
    invValue.value = iv; projCost.value = pc.rows || []
    projCostExtra.value = { unassigned: pc.unassigned, note: pc.note }
    costDetail.value = {}   // 口径数据重来一遍，展开过的明细缓存作废
  } finally { invLoading.value = false }
}
const projCostTotal = computed(() => projCost.value.reduce((s, r) => s + (r.cost || 0), 0))

// 🆕 #389 项目材料成本展开明细：**点开哪个项目才查哪个**。
//   全公司 500+ 项目、上万行流水，一次全查出来页面直接卡死——用户在反馈里专门点了性能。
//   查过的留在 costDetail 里，重复展开不再打接口。
interface CostDetailRow { material_id: number; name: string; spec?: string | null; unit?: string; qty: number; avg_price?: number | null; amount?: number | null; leg: string }
const costDetail = ref<Record<number, { rows: CostDetailRow[]; total: number; noprice_count: number } | 'loading'>>({})
async function onCostExpand(row: { project_id: number }, expanded: any[]) {
  if (!expanded.some((r: any) => r.project_id === row.project_id)) return   // 收起不查
  if (costDetail.value[row.project_id]) return                              // 查过就不再查
  costDetail.value[row.project_id] = 'loading'
  try {
    costDetail.value[row.project_id] = (await http.get<{ rows: CostDetailRow[]; total: number; noprice_count: number }>(
      `/wh/project-cost/${row.project_id}/detail`)).data
  } catch { delete costDetail.value[row.project_id] }   // 失败要能重试，别把 loading 卡死
}
function onFinTab(name: string) {
  if (name === 'payables') loadPayables()
  if (name === 'inventory') loadInventory()
  if (name === 'expense') loadExpense()
  if (name === 'pnl') loadPnl()
  if (name === 'audit') { loadAudit(); loadAuditProjects() }
  if (name === 'fund') loadFund()
}

// ===== 🆕 盈利改善第二档：资金面板（应收/应付/呆滞/13周现金） =====
interface FundData {
  as_of: string
  receivables: {
    total: number; buckets: { bucket: string; amount: number }[]
    balance_rows: any[]; ship_rows: any[]
    by_customer: { key: string; amount: number }[]; by_sales: { key: string; amount: number }[]
  }
  prepay: { total: number; rows: { supplier: string; amount: number; items: number; oldest_paid?: string | null; days?: number | null }[] }
  payables: {
    overdue: { supplier: string; amount: number; items: number; worst_days: number }[]
    overdue_total: number
    due_soon: { supplier: string; amount: number; items: number; nearest_due: string }[]
    due_soon_total: number
    early_paid: { total: number; avg_wasted_days: number; rows: any[] }
    missing_credit: { supplier: string; outstanding: number }[]
  }
  dead_stock: { total_value: number; buckets: { bucket: string; value: number }[]; rows: any[]; safety: any[] }
  cashgap: { weeks: { idx: number; label: string; inflow: number; outflow: number; net: number; cum: number }[]; undated_inflow: number; inflow_later: number; outflow_later: number; note: string }
}
const fundData = ref<FundData | null>(null)
const fundLoading = ref(false)
const fundTab = ref('recv')
async function loadFund() {
  fundLoading.value = true
  try { fundData.value = (await http.get<FundData>('/reports/fund-panel')).data }
  catch { fundData.value = null } finally { fundLoading.value = false }
}
const recvRows = computed(() =>
  [...(fundData.value?.receivables.balance_rows || []), ...(fundData.value?.receivables.ship_rows || [])]
    .sort((a, b) => b.over_days - a.over_days))
// 请款单「距到期」标签：负=已逾期(红)、0-7天(橙)、>7天(绿)、无账期(灰)
function dueTagType(d?: number | null) {
  if (d == null) return 'info'
  if (d < 0) return 'danger'
  if (d <= 7) return 'warning'
  return 'success'
}
function dueTagText(r: PaymentRequestOut) {
  if (r.due_in_days == null) return '未维护账期'
  if (r.due_in_days < 0) return `逾期${-r.due_in_days}天`
  if (r.due_in_days === 0) return '今天到期'
  return `距到期${r.due_in_days}天`
}

// ===== 🆕 盈利改善第一档 1a：项目毛利红黑榜 =====
interface PnlRow {
  project_id: number; code: string; name: string; status: string
  customer: string; cust_type: string; sales_name: string; order_type: string; year: string
  amount: number; mat_cost: number; direct_cost: number; as_cost: number; freight_cost: number
  total_cost: number; profit: number; margin: number | null; flags: string[]
}
interface PnlData {
  note: string; rows: PnlRow[]
  summary: { projects: number; amount: number; cost: number; profit: number; loss_count: number; incomplete_count: number }
}
const pnlData = ref<PnlData | null>(null)
const pnlLoading = ref(false)
const pnlGroup = ref('')      // ''=项目明细 / customer / cust_type / sales_name / order_type / name(机型) / year
const pnlYear = ref('')
const GROUP_LABELS: Record<string, string> = {
  customer: '客户', cust_type: '客户类型', sales_name: '销售员', order_type: '订单类型', name: '机型', year: '年份',
}
async function loadPnl() {
  pnlLoading.value = true
  try { pnlData.value = (await http.get<PnlData>('/reports/project-pnl')).data; pnlDetail.value = {} }
  catch { pnlData.value = null } finally { pnlLoading.value = false }
}

// 🆕 #390 项目毛利展开明细：把「材料/直发外协/安装售后/运费」四个大类展开成原始单据。
//   同 #389，**按项目单独查**——总榜里预先全查会把上万行流水一次拉进浏览器。
interface PnlDetailRow { leg: string; sub?: string | null; title: string; qty?: number | null; unit?: string | null; date?: string | null; party?: string | null; amount: number | null }
const pnlDetail = ref<Record<number, { rows: PnlDetailRow[]; total: number; by_leg: Record<string, number> } | 'loading'>>({})
async function onPnlExpand(row: PnlRow, expanded: any[]) {
  if (!expanded.some((r: any) => r.project_id === row.project_id)) return
  if (pnlDetail.value[row.project_id]) return
  pnlDetail.value[row.project_id] = 'loading'
  try {
    pnlDetail.value[row.project_id] = (await http.get<{ rows: PnlDetailRow[]; total: number; by_leg: Record<string, number> }>(
      `/reports/project-pnl/${row.project_id}/detail`)).data
  } catch { delete pnlDetail.value[row.project_id] }
}
const pnlYears = computed(() =>
  Array.from(new Set((pnlData.value?.rows || []).map(r => r.year))).sort().reverse())
const pnlRows = computed(() => {
  const rows = pnlData.value?.rows || []
  return pnlYear.value ? rows.filter(r => r.year === pnlYear.value) : rows
})
interface PnlGroupRow { key: string; count: number; amount: number; total_cost: number; profit: number; margin: number | null; as_cost: number; as_ratio: number | null }
const pnlGrouped = computed<PnlGroupRow[]>(() => {
  if (!pnlGroup.value) return []
  const by = new Map<string, PnlGroupRow>()
  for (const r of pnlRows.value) {
    const key = String((r as any)[pnlGroup.value] || '未填')
    let g = by.get(key)
    if (!g) { g = { key, count: 0, amount: 0, total_cost: 0, profit: 0, margin: null, as_cost: 0, as_ratio: null }; by.set(key, g) }
    g.count++; g.amount += r.amount; g.total_cost += r.total_cost; g.profit += r.profit; g.as_cost += r.as_cost
  }
  const out = Array.from(by.values())
  for (const g of out) {
    g.margin = g.amount ? Math.round((g.profit / g.amount) * 1000) / 10 : null
    g.as_ratio = g.amount ? Math.round((g.as_cost / g.amount) * 1000) / 10 : null
  }
  return out.sort((a, b) => a.profit - b.profit)
})
// 售后侵蚀 Top5：Σ售后费 ÷ 合同额，按机型/客户切换，定位返修高发
const asTopDim = ref<'name' | 'customer'>('name')
const asTop = computed(() => {
  const by = new Map<string, { key: string; amount: number; as_cost: number }>()
  for (const r of pnlRows.value) {
    if (!r.as_cost) continue
    const key = String((r as any)[asTopDim.value] || '未填')
    let g = by.get(key)
    if (!g) { g = { key, amount: 0, as_cost: 0 }; by.set(key, g) }
    g.amount += r.amount; g.as_cost += r.as_cost
  }
  return Array.from(by.values())
    .map(g => ({ ...g, ratio: g.amount ? Math.round((g.as_cost / g.amount) * 1000) / 10 : null }))
    .sort((a, b) => (b.ratio ?? 999) - (a.ratio ?? 999)).slice(0, 5)
})

// ===== 🆕 盈利改善第一档 1b：成本黑洞审计 =====
interface AuditData {
  month: string; month_unallocated: number; total_unallocated: number; fillable_count: number
  orphan_out: { id: number; ref_no: string; biz_date: string; name: string; spec?: string | null; qty: number; source?: string | null; party?: string | null; value: number | null }[]
  unpriced_in: { id: number; ref_no: string; biz_date: string; direction: string; name: string; spec?: string | null; qty: number; po_no?: string | null; supplier?: string | null; item_price?: number | null; fillable: boolean }[]
  orphan_purchase: { id: number; po_no?: string | null; supplier?: string | null; item_name: string; spec?: string | null; project_code?: string | null; received_amount: number; arrival_date?: string | null; buyer?: string | null }[]
  recon: { project_id: number; code: string; name: string; purchase: number; warehouse: number; diff: number }[]
}
const auditData = ref<AuditData | null>(null)
const auditLoading = ref(false)
const auditTab = ref('orphan_out')
async function loadAudit() {
  auditLoading.value = true
  try { auditData.value = (await http.get<AuditData>('/reports/cost-audit')).data }
  catch { auditData.value = null } finally { auditLoading.value = false }
}
const auditProjects = ref<{ id: number; code: string; name: string }[]>([])
const assignPid = ref<Record<number, number | undefined>>({})
async function loadAuditProjects() {
  if (auditProjects.value.length) return
  try { auditProjects.value = (await http.get<any[]>('/projects')).data } catch { /* 无目录权限时下拉为空 */ }
}
async function assignProject(txnId: number) {
  const pid = assignPid.value[txnId]
  if (!pid) { ElMessage.warning('请先选择要归集到的项目'); return }
  try {
    const r: any = (await http.patch(`/reports/cost-audit/txns/${txnId}/project`, { project_id: pid })).data
    ElMessage.success(r.message || '已归集')
    await loadAudit()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '归集失败') }
}
async function backfillPrices() {
  try {
    await ElMessageBox.confirm(
      `把「采购侧已补价」的无价收货流水按采购单价一键回填？当前可回填 ${auditData.value?.fillable_count ?? 0} 条。`,
      '一键回填价格', { type: 'info' })
  } catch { return }
  const r: any = (await http.post('/reports/cost-audit/backfill-prices')).data
  ElMessage.success(r.message || '已回填')
  await loadAudit()
}

// ===== 🆕 支出总览：全公司花销按月一张表（采购付款+安装售后+OA费用）=====
interface ExpenseRow { month: string; purchase: number; aftersales: number; oa: number; freight: number; total: number }
interface ExpenseData { year: number; rows: ExpenseRow[]; undated: { purchase: number; aftersales: number; oa: number; freight: number; total: number }; totals: { purchase: number; aftersales: number; oa: number; freight: number; grand: number } }
const expYear = ref(new Date().getFullYear())
const expYears = Array.from({ length: new Date().getFullYear() - 2024 + 1 }, (_, i) => new Date().getFullYear() - i)
const expData = ref<ExpenseData | null>(null)
const expLoading = ref(false)
async function loadExpense() {
  expLoading.value = true
  try { expData.value = (await http.get<ExpenseData>('/finance/expense-overview', { params: { year: expYear.value } })).data }
  catch { expData.value = null } finally { expLoading.value = false }
}

// 请款审批（🆕 #119：默认显示全部，避免只看待审批时列表空）
const prStatus = ref('all')
const payReqs = ref<PaymentRequestOut[]>([])
const prLoading = ref(false)
const rejectDialogVisible = ref(false)
const rejectReason = ref('')
const rejectTargetId = ref<number | null>(null)
const payDialogVisible = ref(false)
const payForm = ref({ paid_amount: 0, paid_date: '', payment_method: '银行转账' })
const payTargetId = ref<number | null>(null)

const prStatusLabel: Record<string, string> = {
  pending: '待审批', approved: '已审批', paid: '已付款', rejected: '已拒绝',
}

async function loadPayReqs() {
  prLoading.value = true
  try {
    // 🆕 一次性拉全部，状态改成横向状态栏，纯前端筛选+计数，切状态不用再等网络
    const r = await http.get<PaymentRequestOut[]>('/finance/payment-requests', { params: { status: 'all' } })
    payReqs.value = r.data
  } finally { prLoading.value = false }
}
// 🆕 需求一：请款审批与付款拆成两个 tab。
//   请款审批 tab 只管审批环节（待审批/已审批/已拒绝，不含已付款）；付款 tab 管已审批待付+已付款。
const approvalReqs = computed(() => payReqs.value.filter(r => r.status !== 'paid'))
const prCounts = computed(() => {
  const c: Record<string, number> = { all: approvalReqs.value.length, pending: 0, approved: 0, rejected: 0 }
  for (const r of approvalReqs.value) c[r.status] = (c[r.status] || 0) + 1
  return c
})
const filteredPayReqs = computed(() =>
  prStatus.value === 'all' ? approvalReqs.value : approvalReqs.value.filter(r => r.status === prStatus.value))

// 付款 tab：只关心已审批(待付款)/已付款
const paymentTab = ref('approved')
const paymentReqs = computed(() => payReqs.value.filter(r => r.status === 'approved' || r.status === 'paid'))
const paymentCounts = computed(() => ({
  all: paymentReqs.value.length,
  approved: paymentReqs.value.filter(r => r.status === 'approved').length,
  paid: paymentReqs.value.filter(r => r.status === 'paid').length,
}))
const paySearch = ref('')
const filteredPaymentReqs = computed(() => {
  const base = paymentTab.value === 'all' ? paymentReqs.value : paymentReqs.value.filter(r => r.status === paymentTab.value)
  const kw = paySearch.value.trim().toLowerCase()
  if (!kw) return base
  // 🆕 #300：付款情况搜索（编号/供应商/申请人/项目编号/金额/备注，大小写不敏感）
  return base.filter(r =>
    String(r.id).includes(kw)
    || (r.supplier_name || '').toLowerCase().includes(kw)
    || (r.requester_name || '').toLowerCase().includes(kw)
    || (r.project_codes || []).join(' ').toLowerCase().includes(kw)
    || String(r.requested_amount ?? '').includes(kw)
    || String(r.paid_amount ?? '').includes(kw)
    || (r.notes || '').toLowerCase().includes(kw))
})

// 🆕 #237 内控：不能审批自己提交的请款单（兼任采购+财务的账号最容易踩）
function isMyPayReq(row: PaymentRequestOut) { return !!row.requester_id && row.requester_id === auth.user?.id }

async function approvePayReq(id: number) {
  try {
    await ElMessageBox.confirm('确认审批通过此请款申请？', '审批确认', { type: 'info' })
  } catch { return }
  await http.put(`/purchase-mgmt/payment-requests/${id}/approve`)
  ElMessage.success('已审批通过')
  await loadPayReqs()
}

// 🆕 驳回三个环节共用一个弹窗：审批拒绝(reject) / 撤回审批(withdraw-approval) / 付款驳回(pay-reject)
type RejectMode = 'reject' | 'withdraw' | 'pay'
const rejectMode = ref<RejectMode>('reject')
const REJECT_META: Record<RejectMode, { title: string; ep: string; ok: string; ph: string; must: boolean }> = {
  reject: {
    title: '拒绝请款申请', ep: 'reject', ok: '已拒绝请款申请',
    ph: '请填写拒绝原因（可选）', must: false,
  },
  withdraw: {
    title: '撤回审批（退回发起人）', ep: 'withdraw-approval', ok: '已撤回审批，单子已退回发起人',
    ph: '请说明撤回原因，发起人会收到通知', must: true,
  },
  pay: {
    title: '付款驳回（退回发起人）', ep: 'pay-reject', ok: '已驳回，单子已退回发起人',
    ph: '如：收款账号与开户名不符 / 账户信息有误，请核实后重新提交', must: true,
  },
}
const rejectMeta = computed(() => REJECT_META[rejectMode.value])

function openReject(id: number, mode: RejectMode = 'reject') {
  rejectTargetId.value = id
  rejectMode.value = mode
  rejectReason.value = ''
  rejectDialogVisible.value = true
}

async function submitReject() {
  if (!rejectTargetId.value) return
  const m = rejectMeta.value
  if (m.must && !rejectReason.value.trim()) {
    ElMessage.warning('请填写原因，发起人要靠它知道该改什么')
    return
  }
  await http.put(`/purchase-mgmt/payment-requests/${rejectTargetId.value}/${m.ep}`,
                 { reason: rejectReason.value })
  ElMessage.success(m.ok)
  rejectDialogVisible.value = false
  await loadPayReqs()
}

const payVoucherFile = ref<File | null>(null)
const payingPr = ref<PaymentRequestOut | null>(null)   // 🆕 需求十六：付款弹窗展示的请款单
function openPay(pr: PaymentRequestOut) {
  payTargetId.value = pr.id
  payingPr.value = pr
  payForm.value = { paid_amount: pr.requested_amount, paid_date: new Date().toISOString().slice(0, 10), payment_method: '银行转账' }
  payVoucherFile.value = null
  payDialogVisible.value = true
}
function pickVoucher() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.pdf,.jpg,.jpeg,.png,.xlsx,.xls'
  input.onchange = () => { payVoucherFile.value = input.files?.[0] || null }
  input.click()
}

async function submitPay() {
  if (!payTargetId.value) return
  const fd = new FormData()
  fd.append('paid_amount', String(payForm.value.paid_amount))
  fd.append('paid_date', payForm.value.paid_date)
  fd.append('payment_method', payForm.value.payment_method || '')
  if (payVoucherFile.value) fd.append('file', payVoucherFile.value)
  await http.put(`/purchase-mgmt/payment-requests/${payTargetId.value}/pay`, fd)
  ElMessage.success('付款已记录')
  payDialogVisible.value = false
  await loadPayReqs()
}

// #164：复制收款账户信息
async function copyText(t?: string | null) {
  if (!t) return
  try { await navigator.clipboard.writeText(String(t)); ElMessage.success('已复制') }
  catch { ElMessage.warning('复制失败，请手动选择复制') }
}
// #161/#168/#171：财务预览关联采购单 PDF（新标签内联打开，可预览也可从中下载；finance 有权限）
async function downloadPoPdf(poNo?: string | null) {
  if (!poNo) return
  try {
    const res = await http.get(`/purchase-mgmt/orders/${encodeURIComponent(poNo)}/pdf`, { responseType: 'blob' })
    const url = URL.createObjectURL(new Blob([res.data as BlobPart], { type: 'application/pdf' }))
    const w = window.open(url, '_blank')   // #171 预览：新标签打开 PDF
    if (!w) {   // 弹窗被拦截则退回下载
      const a = document.createElement('a')
      a.href = url; a.download = `采购单_${poNo}.pdf`; a.click()
    }
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  } catch { ElMessage.error('打开采购单失败') }
}

// 🆕 请款单全流程删除：任意状态可删；已付款的会额外提示（后端会冲销采购明细付款）
async function deletePayReq(row: PaymentRequestOut) {
  const extra = row.status === 'paid'
    ? '\n⚠ 该请款单已付款，删除会把这笔付款从相关采购明细里冲销掉（付款金额回退）。'
    : ''
  try {
    await ElMessageBox.confirm(
      `确认删除请款单 #${row.id}（${prStatusLabel[row.status] || row.status}）？删除后不可恢复。${extra}`,
      '删除请款单', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch { return }
  try {
    await http.delete(`/purchase-mgmt/payment-requests/${row.id}`)
    ElMessage.success('请款单已删除')
    await loadPayReqs()
  } catch { /* handled */ }
}

function prTagType(status: string) {
  return status === 'paid' ? 'success' : status === 'approved' ? 'primary' : status === 'rejected' ? 'danger' : 'warning'
}

async function load() {
  loading.value = true
  try {
    const [p, i, a] = await Promise.all([
      http.get<InvoiceRow[]>('/finance/pending-invoices').then(r => r.data),
      http.get<InvoiceRow[]>('/finance/invoiced').then(r => r.data),
      http.get<{ rows: AsRow[]; stats: { approved_cost: number } }>('/finance/aftersales').then(r => r.data),
    ])
    pending.value = p; invoiced.value = i
    aftersales.value = a.rows; asTotal.value = a.stats.approved_cost
  } finally { loading.value = false }
}
onMounted(async () => { await load(); await loadPayReqs() })

// 🆕 把同 invoice_batch_id 的多行归为一行展示（合并组），单项目保持原样
function groupRows(list: InvoiceRow[]): ViewRow[] {
  const out: ViewRow[] = []
  const batches = new Map<number, InvoiceRow[]>()
  for (const r of list) {
    if (r.invoice_batch_id) {
      if (!batches.has(r.invoice_batch_id)) batches.set(r.invoice_batch_id, [])
      batches.get(r.invoice_batch_id)!.push(r)
    } else {
      out.push({ ...r, _isBatch: false, _count: 1, _codes: r.code })
    }
  }
  for (const [, rs] of batches) {
    out.push({
      ...rs[0], _isBatch: true, _count: rs.length,
      _codes: rs.map((x) => x.code).join('、'),
      name: rs.length > 1 ? `${rs[0].name} 等 ${rs.length} 项` : rs[0].name,
      amount: rs.reduce((s, x) => s + (x.amount || 0), 0),
    })
  }
  return out
}
const pendingView = computed(() => groupRows(pending.value))
const invoicedView = computed(() => groupRows(invoiced.value))

async function uploadInvoice(row: ViewRow) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.pdf,.jpg,.jpeg,.png,.ofd'
  input.onchange = async () => {
    const f = input.files?.[0]
    if (!f) return
    const fd = new FormData(); fd.append('file', f)
    // 🆕 合并批次走批次端点，一张发票回传整组；单项目走原端点
    if (row._isBatch && row.invoice_batch_id) {
      await http.post(`/sales/invoice-batch/${row.invoice_batch_id}/invoice-upload`, fd)
      ElMessage.success(`合并发票已上传，${row._count} 个项目已开票`)
    } else {
      await http.post(`/sales/ledger/${row.ledger_id}/invoice-upload`, fd)
      ElMessage.success('发票已上传，已回传销售订单')
    }
    await load()
    tab.value = 'invoiced'
  }
  input.click()
}

// 管理员/主管作废待开票申请，退回未申请状态
async function voidPendingInvoice(row: ViewRow) {
  try {
    await ElMessageBox.confirm(
      `确认作废「${row._codes}」的开票申请？将退回未申请状态，申请表文件删除，销售需重新提交。`,
      '作废开票申请', { type: 'warning', confirmButtonText: '确认作废' })
  } catch { return }
  try {
    await http.post(`/sales/ledger/${row.ledger_id}/invoice-void`)
    ElMessage.success('已作废，退回未申请状态')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

// 🆕 2026-08-13：安装/售后费用加 类型 / 项目编号 / 提交人 筛选。
//   与售后部那张台账同一套做法：整表本来就是一次 /finance/aftersales 取回全量，前端过滤即可。
//   下拉选项从数据现算，新来的人/项目自动进选项。
const fAsKind = ref('')
const fAsCode = ref('')
const fAsUser = ref('')
const asCodeOptions = computed(() =>
  [...new Set(aftersales.value.map(r => r.code).filter(Boolean))].sort())
const asUserOptions = computed(() =>
  [...new Set(aftersales.value.map(r => r.created_by_name || '').filter(Boolean))].sort())
const filteredAftersales = computed(() => aftersales.value.filter(r =>
  (!fAsKind.value || r.kind === fAsKind.value)
  && (!fAsCode.value || r.code === fAsCode.value)
  && (!fAsUser.value || (r.created_by_name || '') === fAsUser.value)))
const hasAsFilter = computed(() => !!(fAsKind.value || fAsCode.value || fAsUser.value))
function clearAsFilters() { fAsKind.value = ''; fAsCode.value = ''; fAsUser.value = '' }
// ⚠️ 表底「合计」必须跟着筛选走，否则筛完只剩 1 行、合计还是全量，那就是个错数。
//   后端 stats.approved_cost 本来就是 sum(rows.cost)（finance_router:99），
//   所以不筛的时候按可见行加也和原来一致。
const asShownTotal = computed(() =>
  filteredAftersales.value.reduce((s, r) => s + (r.cost || 0), 0))

// 🆕 售后报销：核对发票 → 安排报销 / 发票退回
const asActing = ref<number | null>(null)

async function asReimburse(row: AsRow) {
  // 🆕 2026-08-13：缺发票**不再拦**（有的报销本来就没票，比如差旅零星支出、个人垫付小件）。
  //   原来这里 return 掉，财务只能一直退回、单子永远走不完——拦不住乱报，只拦住了正常报销。
  //   改成二次确认时把缺几张写在弹窗里，确认后照走；后端也会把"缺 N 张仍安排报销"记进审计。
  const warn = row.missing_invoice
    ? `\n\n⚠️ 其中 ${row.missing_invoice} 行没有发票，确认后仍会安排报销（此操作会记入审计）。`
    : ''
  try {
    await ElMessageBox.confirm(
      `确认「${row.code}」的报销明细核对无误，安排报销 ${fmtMoney(row.cost)}？${warn}`,
      '安排报销',
      { confirmButtonText: '核对无误，安排报销',
        type: row.missing_invoice ? 'warning' : undefined })
  } catch { return }
  asActing.value = row.id
  try {
    const r = await http.post<{ message: string }>(`/aftersales/${row.id}/reimburse`, new FormData())
    ElMessage.success(r.data?.message || '已安排报销')
    await load()
  } finally { asActing.value = null }
}

// 🆕 反馈#417：财务直接在表上填/改备注。退回中的行备注是退回原因，按钮不出、后端也拦。
async function asEditNote(row: AsRow) {
  let v = ''
  try {
    const r = await ElMessageBox.prompt('要记的信息（打款批次、核对情况等）；清空则删除备注。', '备注', {
      inputValue: row.pay_note || '', inputType: 'textarea',
      confirmButtonText: '保存', cancelButtonText: '取消' })
    v = (r.value ?? '').trim()
  } catch { return }
  const fd = new FormData(); fd.append('note', v)
  const r2 = await http.post<{ message: string }>(`/aftersales/${row.id}/pay-note`, fd)
  row.pay_note = v || null
  ElMessage.success(r2.data?.message || '已保存')
}

async function asPayReject(row: AsRow) {
  let reason = ''
  try {
    const v = await ElMessageBox.prompt('发票哪里对不上？说清楚登记人才知道要改什么。', '发票退回', {
      confirmButtonText: '退回登记人', inputPlaceholder: '如：差旅那行发票抬头不对',
      inputValidator: (t: string) => (t || '').trim() ? true : '请填写退回原因',
    })
    reason = (v.value || '').trim()
  } catch { return }
  asActing.value = row.id
  try {
    const fd = new FormData(); fd.append('reason', reason)
    await http.post(`/aftersales/${row.id}/pay-reject`, fd)
    ElMessage.success('已退回登记人重传发票')
    await load()
  } finally { asActing.value = null }
}

// 财务管理层作废售后费用，退回售后部重审
async function voidAfterSales(row: AsRow) {
  try {
    await ElMessageBox.confirm(
      `确认作废「${row.code}」的售后费用（¥${row.cost}）？将退回售后部重新审批，财务列表中移除。`,
      '作废售后费用', { type: 'warning', confirmButtonText: '确认作废' })
  } catch { return }
  try {
    await http.post(`/aftersales/${row.id}/finance-void`)
    ElMessage.success('已作废，退回售后部重新审批')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

// #2 财务开票纠错：作废原发票退回待开票，可重新上传正确发票（合并发票暂不支持单项目作废）
async function revokeInvoice(row: ViewRow) {
  try {
    await ElMessageBox.confirm(
      '作废原发票并退回「待开票」以便重新开具？原发票文件将删除。', '作废重开', { type: 'warning' })
  } catch { return }
  await http.post(`/sales/ledger/${row.ledger_id}/invoice-revoke`)
  ElMessage.success('已作废原发票，退回待开票')
  await load()
  tab.value = 'pending'
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>财务部</h1>
        <div class="desc">销售主管审批通过的开票申请汇到这里；开票后上传发票自动回传销售订单；售后费用经售后部审批自动同步</div>
      </div>
      <div class="spacer"></div>
      <PageRefresh :load="async () => { await load(); await loadPayReqs(); onFinTab(tab) }" />
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-tabs v-model="tab" @tab-change="onFinTab">
        <el-tab-pane v-if="tv('pending')" :label="`待开票 (${pendingView.length})`" name="pending">
          <el-table show-overflow-tooltip :data="pendingView" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" min-width="140">
              <template #default="{ row }">
                <el-tag v-if="row._isBatch" size="small" type="warning" effect="plain" style="margin-right:4px">合并{{ row._count }}</el-tag>
                <b class="code">{{ row._codes }}</b>
              </template>
            </el-table-column>
            <el-table-column prop="name" label="设备名称" min-width="150" />
            <el-table-column prop="customer" label="客户单位" min-width="120"><template #default="{ row }">{{ row.customer || '—' }}</template></el-table-column>
            <el-table-column prop="sales_name" label="销售" width="90"><template #default="{ row }">{{ row.sales_name || '—' }}</template></el-table-column>
            <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column prop="tax_rate" label="税票" width="70"><template #default="{ row }">{{ row.tax_rate || '—' }}</template></el-table-column>
            <el-table-column label="开票申请表" min-width="130">
              <template #default="{ row }">
                <el-tooltip v-if="row.apply_file_id" :content="row.apply_file_name" placement="top">
                  <el-button size="small" link type="primary"
                             @click="downloadAttachment({ id: row.apply_file_id, name: row.apply_file_name || '申请表' })">
                    📎 申请表
                  </el-button>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" type="primary" :icon="UploadFilled" @click="uploadInvoice(row)">
                  {{ row._isBatch ? '上传合并发票' : '上传发票' }}
                </el-button>
                <el-button v-if="isManager && !row._isBatch" size="small" link type="danger"
                           style="margin-left:6px" @click="voidPendingInvoice(row)">
                  作废
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!pendingView.length" text="暂无待开票" />
        </el-tab-pane>

        <el-tab-pane v-if="tv('invoiced')" :label="`已开票 (${invoicedView.length})`" name="invoiced">
          <el-table show-overflow-tooltip :data="invoicedView" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" min-width="140">
              <template #default="{ row }">
                <el-tag v-if="row._isBatch" size="small" type="warning" effect="plain" style="margin-right:4px">合并{{ row._count }}</el-tag>
                <b class="code">{{ row._codes }}</b>
              </template>
            </el-table-column>
            <el-table-column prop="name" label="设备名称" min-width="150" />
            <el-table-column prop="sales_name" label="销售" width="90"><template #default="{ row }">{{ row.sales_name || '—' }}</template></el-table-column>
            <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column label="发票" min-width="150">
              <template #default="{ row }">
                <el-button v-if="row.invoice_file_id" size="small" link type="success"
                           @click="downloadAttachment({ id: row.invoice_file_id, name: row.invoice_file_name || '发票' })">
                  📎 {{ row.invoice_file_name }}{{ row._isBatch ? '（合并）' : '' }}
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button v-if="!row._isBatch" size="small" link type="warning" @click="revokeInvoice(row)">作废重开</el-button>
                <el-tooltip v-else content="合并发票暂不支持单项目作废" placement="top">
                  <span class="muted">—</span>
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!invoicedView.length" text="暂无已开票" />
        </el-tab-pane>

        <el-tab-pane v-if="tv('aftersales')" :label="`安装/售后费用 (${aftersales.length})`" name="aftersales">
          <!-- 🆕 2026-08-13：类型 / 项目编号 / 提交人 筛选 -->
          <div class="as-filters">
            <el-select v-model="fAsKind" clearable placeholder="全部类型" size="small" style="width:110px">
              <el-option label="售后" value="aftersales" />
              <el-option label="安装" value="install" />
            </el-select>
            <el-select v-model="fAsCode" clearable filterable placeholder="全部项目编号" size="small" style="width:180px">
              <el-option v-for="c in asCodeOptions" :key="c" :label="c" :value="c" />
            </el-select>
            <el-select v-model="fAsUser" clearable filterable placeholder="全部提交人" size="small" style="width:140px">
              <el-option v-for="u in asUserOptions" :key="u" :label="u" :value="u" />
            </el-select>
            <el-button v-if="hasAsFilter" link size="small" @click="clearAsFilters">清空筛选</el-button>
            <span class="muted small">
              <template v-if="hasAsFilter">命中 {{ filteredAftersales.length }} / {{ aftersales.length }} 条</template>
              <template v-else>共 {{ aftersales.length }} 条</template>
            </span>
          </div>
          <!-- ⚠️ summary-method 是**按数组下标**对列的：这张表 12 列，合计落在下标 7（费用）。
               加/删列必须同步改这个数组，否则「合计」会落到隔壁列上（#361 踩过）。 -->
          <el-table show-overflow-tooltip :data="filteredAftersales" stripe show-summary :summary-method="() => ['', '合计', '', '', '', '', '', fmtMoney(asShownTotal), '', '', '', '']" max-height="calc(100vh - 290px)" :scrollbar-always-on="true">
            <!-- 明细放展开行：一条售后动辄三五行费用，摊成列会把表挤到要横向滚动 -->
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="as-detail">
                  <div v-for="it in (row.items || [])" :key="it.id" class="as-item">
                    <span class="as-nm">{{ it.name }}</span>
                    <span class="as-amt">{{ fmtMoney(it.amount) }}</span>
                    <el-button v-if="it.invoice_file_id" size="small" link type="primary"
                               @click="downloadAttachment({ id: it.invoice_file_id, name: it.invoice_file_name || '发票' })">查看发票</el-button>
                    <span v-else class="miss">缺发票</span>
                  </div>
                  <div v-if="!(row.items || []).length" class="muted small">旧流程登记的记录没有费用清单</div>
                  <div v-if="row.pay_note" class="muted small">备注：{{ row.pay_note }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column type="index" label="#" width="50" />
            <el-table-column label="类型" width="70" align="center">
              <template #default="{ row }"><el-tag :type="row.kind === 'install' ? 'success' : 'warning'" size="small" effect="light">{{ KIND_TXT[row.kind] || '售后' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="项目编号" width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="140" />
            <el-table-column prop="problem" label="问题/说明" min-width="200" show-overflow-tooltip />
            <!-- 🆕 #361：财务这张表同样看不到是谁报的。要打钱的人更需要知道找谁核实。 -->
            <el-table-column label="提交人" width="110">
              <template #default="{ row }">
                <div>{{ row.created_by_name || '—' }}</div>
                <div class="muted small">{{ (row.created_at || '').slice(5, 16).replace('T', ' ') }}</div>
              </template>
            </el-table-column>
            <el-table-column label="费用" width="130" align="right">
              <template #default="{ row }">
                <div>{{ fmtMoney(row.cost) }}</div>
                <!-- 缺发票是财务最先要看的，直接标在金额下面，不用横向滚动去找 -->
                <div v-if="row.missing_invoice" class="miss small">缺 {{ row.missing_invoice }} 张发票</div>
              </template>
            </el-table-column>
            <el-table-column label="清单" min-width="140">
              <template #default="{ row }">
                <el-button v-if="row.mat_file_id" size="small" link type="primary"
                           @click="downloadAttachment({ id: row.mat_file_id, name: row.mat_file_name || '物料清单' })">
                  {{ row.mat_file_name }}
                </el-button>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <!-- 🆕 报销核对：售后费用现在带逐行发票，财务在这里核对后安排报销。
                 ⚠️ 必须放在**财务部**这个 tab——财务的菜单里根本没有「售后部」，
                    按钮做在售后页他们走不到。 -->
            <el-table-column label="报销状态" width="110" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.pay_status" size="small" effect="plain"
                        :type="row.pay_status === 'reimbursed' ? 'success' : row.pay_status === 'invoice_fix' ? 'danger' : 'warning'">
                  {{ AS_PAY_TXT[row.pay_status] }}
                </el-tag>
                <span v-else class="muted small">旧流程</span>
              </template>
            </el-table-column>
            <!-- 🆕 反馈#417（王芹）：财务安排完报销要补记信息（打款批次/核对情况），直接在表上填。
                 退回中(invoice_fix)的备注是给登记人看的退回原因，不给改（后端同样拦）。 -->
            <el-table-column label="备注" min-width="150">
              <template #default="{ row }">
                <span v-if="row.pay_note">{{ row.pay_note }}</span><span v-else class="muted">—</span>
                <el-button v-if="row.pay_status !== 'invoice_fix'" size="small" link type="primary"
                           @click="asEditNote(row)">{{ row.pay_note ? '改' : '填' }}</el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="210" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <template v-if="row.pay_status === 'checking'">
                  <el-button size="small" type="success" :loading="asActing === row.id" @click="asReimburse(row)">核对无误，安排报销</el-button>
                  <el-button size="small" type="danger" plain :loading="asActing === row.id" @click="asPayReject(row)">发票退回</el-button>
                </template>
                <el-button v-if="isManager" size="small" link type="danger" @click="voidAfterSales(row)">作废</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!filteredAftersales.length" :text="hasAsFilter ? '当前筛选没有匹配的记录' : '暂无已审批售后费用（售后部审批后自动同步）'" />
        </el-tab-pane>

        <el-tab-pane v-if="tv('pay_requests')" :label="`请款审批 (${prCounts.pending})`" name="pay_requests">
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
            <el-radio-group v-model="prStatus">
              <el-radio-button value="all">全部 ({{ prCounts.all }})</el-radio-button>
              <el-radio-button value="pending">待审批 ({{ prCounts.pending }})</el-radio-button>
              <el-radio-button value="approved">已审批 ({{ prCounts.approved }})</el-radio-button>
              <el-radio-button value="rejected">已拒绝 ({{ prCounts.rejected }})</el-radio-button>
            </el-radio-group>
            <el-button @click="loadPayReqs" :loading="prLoading">刷新</el-button>
            <span class="muted small">💡 内控职责分离：不能审批自己提交的请款单；审批通过后到「付款」tab 付款，审批人不能给自己审过的单付款。</span>
          </div>
          <el-table show-overflow-tooltip :data="filteredPayReqs" stripe v-loading="prLoading" max-height="calc(100vh - 280px)" :scrollbar-always-on="true">
            <el-table-column prop="id" label="申请编号" width="80" />
            <el-table-column prop="supplier_name" label="供应商" min-width="130" />
            <!-- 🆕 反馈#298：项目编号列——取请款单关联采购明细的项目编号，多个不同编号逗号拼接 -->
            <el-table-column label="项目编号" min-width="110" show-overflow-tooltip>
              <template #default="{ row }">{{ row.project_codes?.length ? row.project_codes.join('，') : '—' }}</template>
            </el-table-column>
            <el-table-column prop="requester_name" label="申请人" width="90" />
            <el-table-column label="申请金额" width="120" align="right">
              <template #default="{ row }">{{ fmtMoney(row.requested_amount) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="prTagType(row.status)" size="small">{{ prStatusLabel[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <!-- 🆕 盈利改善2·应付账期利用：按到期日排程付款,有账期别提前付、逾期未付防断供 -->
            <el-table-column label="账期到期" width="110">
              <template #default="{ row }">
                <span v-if="row.status === 'paid'" class="muted small">已付</span>
                <el-tag v-else :type="dueTagType(row.due_in_days)" size="small" effect="plain"
                        :title="row.earliest_due ? '到期日 ' + row.earliest_due : ''">{{ dueTagText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.notes || '—' }}</template>
            </el-table-column>
            <el-table-column label="付款信息" min-width="170">
              <template #default="{ row }">
                <template v-if="row.status === 'paid'">
                  <div>{{ fmtMoney(row.paid_amount) }} · {{ row.paid_date }} · {{ row.payment_method }}</div>
                  <div v-if="row.approver_name" class="muted small">审批：{{ row.approver_name }}</div>
                  <el-button v-if="row.pay_voucher_file_id" size="small" link type="primary"
                             @click="downloadAttachment({ id: row.pay_voucher_file_id!, name: row.pay_voucher_name || '付款凭证' })">
                    📎 付款凭证
                  </el-button>
                </template>
                <span v-else-if="row.status === 'approved' && row.approver_name" class="muted small">已审批（{{ row.approver_name }}），待付款</span>
                <span v-else-if="row.reject_reason" class="muted">拒绝：{{ row.reject_reason }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="申请时间" width="110">
              <template #default="{ row }">{{ row.created_at?.slice(0, 10) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="248" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <template v-if="row.status === 'pending'">
                  <!-- 🆕 #237 内控：自己提的单自己不能审(后端同样硬校验),按钮直接禁掉并说明原因 -->
                  <el-tooltip v-if="isMyPayReq(row)" content="职责分离：这是你自己提交的请款单，需由另一位财务审批" placement="top">
                    <span><el-button size="small" type="primary" disabled>审批通过</el-button></span>
                  </el-tooltip>
                  <el-button v-else size="small" type="primary" @click="approvePayReq(row.id)">审批通过</el-button>
                  <!-- 🆕 原来是 link 细字，容易被当成说明文字；与「审批通过」同等分量 -->
                  <el-button size="small" type="danger" plain @click="openReject(row.id, 'reject')">驳回</el-button>
                </template>
                <!-- 🆕 批完才发现不对：撤回审批，退回发起人（重提后需重新审批） -->
                <el-button v-else-if="row.status === 'approved'" size="small" type="warning" plain
                           @click="openReject(row.id, 'withdraw')">撤回审批</el-button>
                <el-button size="small" type="danger" link @click="deletePayReq(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!approvalReqs.length" text="暂无请款申请" />
        </el-tab-pane>

        <!-- 🆕 需求一：付款 tab（已审批待付 / 已付款）-->
        <el-tab-pane v-if="tv('pay_payment')" :label="`付款 (${paymentCounts.approved})`" name="pay_payment">
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
            <el-radio-group v-model="paymentTab">
              <el-radio-button value="all">全部 ({{ paymentCounts.all }})</el-radio-button>
              <el-radio-button value="approved">待付款 ({{ paymentCounts.approved }})</el-radio-button>
              <el-radio-button value="paid">已付款 ({{ paymentCounts.paid }})</el-radio-button>
            </el-radio-group>
            <el-button @click="loadPayReqs" :loading="prLoading">刷新</el-button>
            <!-- 🆕 #300：搜索所有付款情况（已付款 tab 后面加搜索栏） -->
            <el-input v-model="paySearch" placeholder="搜索编号/供应商/申请人/项目编号/金额/备注" clearable style="width:300px" />
            <span class="muted small">💡 仅对已审批通过的请款单付款；审批人不能给自己审过的单付款（后端校验）。</span>
          </div>
          <el-table show-overflow-tooltip :data="filteredPaymentReqs" stripe v-loading="prLoading" max-height="calc(100vh - 280px)" :scrollbar-always-on="true">
            <el-table-column prop="id" label="申请编号" width="80" />
            <el-table-column prop="supplier_name" label="供应商" min-width="130" />
            <el-table-column prop="requester_name" label="申请人" width="90" />
            <el-table-column label="申请金额" width="120" align="right">
              <template #default="{ row }">{{ fmtMoney(row.requested_amount) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="prTagType(row.status)" size="small">{{ prStatusLabel[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <!-- 🆕 盈利改善2·应付账期利用：按到期日排程付款,有账期别提前付、逾期未付防断供 -->
            <el-table-column label="账期到期" width="110">
              <template #default="{ row }">
                <span v-if="row.status === 'paid'" class="muted small">已付</span>
                <el-tag v-else :type="dueTagType(row.due_in_days)" size="small" effect="plain"
                        :title="row.earliest_due ? '到期日 ' + row.earliest_due : ''">{{ dueTagText(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="采购单" min-width="130">
              <template #default="{ row }">
                <template v-if="row.po_nos?.length">
                  <el-button v-for="po in row.po_nos" :key="po" size="small" link type="primary" @click="downloadPoPdf(po)">📄 {{ po }}</el-button>
                </template>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <!-- 🆕 反馈 2026-08-07（王芹）：「付款人看不到备注，付款人需要看到备注」。
                 请款时填的「付款说明/账期」只在「请款审批」tab 有，付款这一步反而没有，
                 付款的人得回头去翻审批 tab 才知道这笔钱有什么讲究。 -->
            <el-table-column prop="notes" label="备注" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.notes">{{ row.notes }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="付款信息" min-width="170">
              <template #default="{ row }">
                <template v-if="row.status === 'paid'">
                  <div>{{ fmtMoney(row.paid_amount) }} · {{ row.paid_date }} · {{ row.payment_method }}</div>
                  <div v-if="row.approver_name" class="muted small">审批：{{ row.approver_name }}</div>
                  <el-button v-if="row.pay_voucher_file_id" size="small" link type="primary"
                             @click="downloadAttachment({ id: row.pay_voucher_file_id!, name: row.pay_voucher_name || '付款凭证' })">
                    📎 付款凭证
                  </el-button>
                </template>
                <span v-else-if="row.approver_name" class="muted small">已审批（{{ row.approver_name }}），待付款</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="申请时间" width="110">
              <template #default="{ row }">{{ row.created_at?.slice(0, 10) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="248" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button v-if="row.status === 'approved'" size="small" type="success" @click="openPay(row)">记录付款</el-button>
                <!-- 🆕 出纳付款时发现收款账户名称/账号不对：驳回退回发起人，别硬付出去打错账户 -->
                <el-button v-if="row.status === 'approved'" size="small" type="danger" plain
                           @click="openReject(row.id, 'pay')">驳回</el-button>
                <el-button size="small" type="danger" link @click="deletePayReq(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!paymentReqs.length" text="暂无待付款 / 已付款记录" />
        </el-tab-pane>

        <!-- 🆕 支出总览：全公司的钱花在哪，一张表看全（盈利改善第一档的第一块） -->
        <el-tab-pane v-if="tv('expense')" label="支出总览" name="expense">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
            <el-select v-model="expYear" style="width:110px" @change="loadExpense">
              <el-option v-for="y in expYears" :key="y" :label="y + ' 年'" :value="y" />
            </el-select>
            <span class="muted small">口径：采购付款(按付款日期) + 安装/售后费用(已审批) + OA业务/报销费用(已审批，核定金额优先) + 物料运输费(物流录入·我方承担)。材料是项目成本口径(钱已含在采购付款里,不重复计)，项目级毛利见「📈 项目毛利」tab。</span>
          </div>
          <div v-if="expData" class="kpi-grid" style="margin-bottom:12px">
            <div class="kpi is-primary"><div class="kpi-v">{{ fmtMoney(expData.totals.grand) }}</div><div class="kpi-l">{{ expData.year }} 年总支出</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(expData.totals.purchase) }}</div><div class="kpi-l">采购付款</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(expData.totals.aftersales) }}</div><div class="kpi-l">安装/售后费用</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(expData.totals.oa) }}</div><div class="kpi-l">OA 业务/报销</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(expData.totals.freight) }}</div><div class="kpi-l">物料运输费</div></div>
          </div>
          <el-table show-overflow-tooltip v-loading="expLoading" :data="expData?.rows || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
            <el-table-column prop="month" label="月份" width="110"><template #default="{ row }"><b>{{ row.month }}</b></template></el-table-column>
            <el-table-column label="采购付款" min-width="130" align="right"><template #default="{ row }">{{ row.purchase ? fmtMoney(row.purchase) : '—' }}</template></el-table-column>
            <el-table-column label="安装/售后" min-width="130" align="right"><template #default="{ row }">{{ row.aftersales ? fmtMoney(row.aftersales) : '—' }}</template></el-table-column>
            <el-table-column label="OA 业务/报销" min-width="130" align="right"><template #default="{ row }">{{ row.oa ? fmtMoney(row.oa) : '—' }}</template></el-table-column>
            <el-table-column label="物料运输费" min-width="120" align="right"><template #default="{ row }">{{ row.freight ? fmtMoney(row.freight) : '—' }}</template></el-table-column>
            <el-table-column label="合计" min-width="140" align="right"><template #default="{ row }"><b class="amt">{{ row.total ? fmtMoney(row.total) : '—' }}</b></template></el-table-column>
          </el-table>
          <el-alert v-if="expData && expData.undated.total > 0" type="warning" :closable="false" style="margin-top:10px"
            :title="`另有 ${fmtMoney(expData.undated.total)} 已付款但未记付款日期（采购 ${fmtMoney(expData.undated.purchase)}），未计入上表月份——请在采购明细补付款日期。`" />
        </el-tab-pane>

        <!-- 🆕 盈利改善1a：项目毛利红黑榜——哪个项目赚钱、哪个项目亏钱 -->
        <el-tab-pane v-if="tv('pnl')" label="项目毛利" name="pnl">
          <el-alert type="warning" :closable="false" style="margin-bottom:10px"
            :title="pnlData?.note || '口径：材料边际贡献 = 合同额 − 材料领料 − 直发/外协采购 − 安装/售后费用；不含人工/运费'" />
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
            <el-select v-model="pnlGroup" style="width:150px">
              <el-option label="按项目明细" value="" />
              <el-option v-for="(l, k) in GROUP_LABELS" :key="k" :label="'按' + l + '汇总'" :value="k" />
            </el-select>
            <el-select v-model="pnlYear" clearable style="width:120px" placeholder="全部年份">
              <el-option v-for="y in pnlYears" :key="y" :label="y" :value="y" />
            </el-select>
            <span class="muted small">默认亏得最多的排最前（红榜在上）；点列头可重新排序。点行首箭头展开看成本明细。带「物料缺价」标签的行成本被低估，先去成本审计页清黑洞。</span>
          </div>
          <div v-if="pnlData" class="kpi-grid" style="margin-bottom:12px">
            <div class="kpi is-primary"><div class="kpi-v">{{ fmtMoney(pnlData.summary.profit) }}</div><div class="kpi-l">总毛利（材料边际贡献）</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(pnlData.summary.amount) }}</div><div class="kpi-l">合同额合计</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(pnlData.summary.cost) }}</div><div class="kpi-l">成本合计</div></div>
            <div class="kpi"><div class="kpi-v" :class="pnlData.summary.loss_count ? 'danger' : ''">{{ pnlData.summary.loss_count }} / {{ pnlData.summary.projects }}</div><div class="kpi-l">亏损项目数 / 上榜项目</div></div>
          </div>
          <el-table v-if="!pnlGroup" show-overflow-tooltip v-loading="pnlLoading" :data="pnlRows" stripe size="small"
                    class="compact-tbl" max-height="calc(100vh - 400px)" :scrollbar-always-on="true"
                    row-key="project_id" @expand-change="onPnlExpand"
                    :row-class-name="({ row }: any) => row.profit < 0 ? 'pnl-loss-row' : ''">
            <!-- 🆕 #390 展开明细：四个大类展开成原始单据。**点开哪个项目才查哪个**——
                 总榜预先全查会把上万行流水一次拉进浏览器，用户在反馈里专门点了性能。 -->
            <el-table-column type="expand" fixed="left" width="40">
              <template #default="{ row }">
                <div style="padding:6px 10px 10px 40px">
                  <div v-if="pnlDetail[row.project_id] === 'loading'" class="muted small">明细加载中…</div>
                  <template v-else-if="pnlDetail[row.project_id]">
                    <div class="muted small" style="margin-bottom:6px">
                      <span v-for="(v, k) in (pnlDetail[row.project_id] as any).by_leg" :key="k" style="margin-right:14px">
                        {{ k }} <b>{{ fmtMoney(v) }}</b>
                      </span>
                      <span>合计 <b>{{ fmtMoney((pnlDetail[row.project_id] as any).total) }}</b></span>
                    </div>
                    <el-table :data="(pnlDetail[row.project_id] as any).rows" size="small" stripe class="compact-tbl" max-height="340">
                      <el-table-column label="大类" width="90">
                        <template #default="{ row: d }"><el-tag size="small" effect="plain">{{ d.leg }}</el-tag></template>
                      </el-table-column>
                      <el-table-column prop="sub" label="来源" width="110"><template #default="{ row: d }">{{ d.sub || '—' }}</template></el-table-column>
                      <el-table-column prop="title" label="内容" min-width="200" show-overflow-tooltip />
                      <el-table-column label="数量" width="80" align="right"><template #default="{ row: d }">{{ d.qty != null ? `${d.qty}${d.unit || ''}` : '—' }}</template></el-table-column>
                      <el-table-column prop="date" label="日期" width="100"><template #default="{ row: d }">{{ d.date || '—' }}</template></el-table-column>
                      <el-table-column prop="party" label="供应商/对象" min-width="120" show-overflow-tooltip><template #default="{ row: d }">{{ d.party || '—' }}</template></el-table-column>
                      <el-table-column label="金额" width="110" align="right"><template #default="{ row: d }"><b>{{ d.amount != null ? fmtMoney(d.amount) : '缺价' }}</b></template></el-table-column>
                    </el-table>
                    <EmptyHint v-if="!(pnlDetail[row.project_id] as any).rows.length" text="这个项目没有成本明细（成本全为 0）" size="sm" />
                  </template>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="项目" min-width="170" fixed="left">
              <template #default="{ row }"><b class="code">{{ row.code }}</b> {{ row.name }}</template>
            </el-table-column>
            <el-table-column prop="customer" label="客户" min-width="110" />
            <el-table-column prop="sales_name" label="销售" width="80" />
            <el-table-column prop="amount" label="合同额" width="110" align="right" sortable>
              <template #default="{ row }">{{ row.amount ? fmtMoney(row.amount) : '—' }}</template>
            </el-table-column>
            <!-- 🆕 #373：口径已从「领料出库×均价」改成「挂项目收货金额 + 通用物料领用」，列名跟着改 -->
            <el-table-column prop="mat_cost" label="材料成本" width="105" align="right" sortable>
              <template #default="{ row }">{{ row.mat_cost ? fmtMoney(row.mat_cost) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="direct_cost" label="直发/外协" width="105" align="right" sortable>
              <template #default="{ row }">{{ row.direct_cost ? fmtMoney(row.direct_cost) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="as_cost" label="安装/售后" width="100" align="right" sortable>
              <template #default="{ row }">{{ row.as_cost ? fmtMoney(row.as_cost) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="freight_cost" label="运费" width="90" align="right" sortable>
              <template #default="{ row }">{{ row.freight_cost ? fmtMoney(row.freight_cost) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="total_cost" label="成本合计" width="110" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.total_cost) }}</template>
            </el-table-column>
            <el-table-column prop="profit" label="毛利" width="115" align="right" sortable>
              <template #default="{ row }"><b :class="row.profit < 0 ? 'danger' : 'profit-pos'">{{ fmtMoney(row.profit) }}</b></template>
            </el-table-column>
            <el-table-column prop="margin" label="毛利率" width="90" align="right" sortable>
              <template #default="{ row }"><b v-if="row.margin != null" :class="row.margin < 0 ? 'danger' : 'profit-pos'">{{ row.margin }}%</b><span v-else class="muted">—</span></template>
            </el-table-column>
            <el-table-column label="成本完整度" min-width="130">
              <template #default="{ row }">
                <el-tag v-if="!row.flags.length" size="small" type="success" effect="plain">完整</el-tag>
                <el-tag v-for="f in row.flags" :key="f" size="small" type="warning" effect="plain" style="margin-right:4px">{{ f }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <el-table v-else show-overflow-tooltip v-loading="pnlLoading" :data="pnlGrouped" stripe size="small"
                    class="compact-tbl" max-height="calc(100vh - 400px)" :fit="false">
            <el-table-column prop="key" :label="GROUP_LABELS[pnlGroup]" min-width="150" />
            <el-table-column prop="count" label="项目数" width="80" align="center" sortable />
            <el-table-column prop="amount" label="合同额" width="130" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
            </el-table-column>
            <el-table-column prop="total_cost" label="成本合计" width="130" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.total_cost) }}</template>
            </el-table-column>
            <el-table-column prop="profit" label="毛利" width="130" align="right" sortable>
              <template #default="{ row }"><b :class="row.profit < 0 ? 'danger' : 'profit-pos'">{{ fmtMoney(row.profit) }}</b></template>
            </el-table-column>
            <el-table-column prop="margin" label="毛利率" width="95" align="right" sortable>
              <template #default="{ row }"><b v-if="row.margin != null" :class="row.margin < 0 ? 'danger' : 'profit-pos'">{{ row.margin }}%</b><span v-else class="muted">—</span></template>
            </el-table-column>
            <el-table-column prop="as_ratio" label="售后侵蚀率" width="110" align="right" sortable>
              <template #default="{ row }"><span v-if="row.as_ratio != null" :class="row.as_ratio > 5 ? 'danger' : ''">{{ row.as_ratio }}%</span><span v-else class="muted">—</span></template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!pnlLoading && !pnlRows.length" text="暂无项目毛利数据（需要销售台账或项目成本数据）" />
          <div v-if="asTop.length" style="margin-top:14px">
            <div class="section-title" style="display:flex;align-items:center;gap:10px">
              🛎️ 售后侵蚀 Top5（Σ安装/售后费 ÷ 合同额，定位返修高发）
              <el-radio-group v-model="asTopDim" size="small">
                <el-radio-button value="name">按机型</el-radio-button>
                <el-radio-button value="customer">按客户</el-radio-button>
              </el-radio-group>
            </div>
            <el-table show-overflow-tooltip :data="asTop" size="small" class="compact-tbl" style="max-width:720px">
              <el-table-column prop="key" :label="asTopDim === 'name' ? '机型' : '客户'" min-width="180" />
              <el-table-column label="合同额" width="130" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
              <el-table-column label="售后费用" width="130" align="right"><template #default="{ row }">{{ fmtMoney(row.as_cost) }}</template></el-table-column>
              <el-table-column label="侵蚀率" width="100" align="right">
                <template #default="{ row }"><b :class="(row.ratio ?? 0) > 5 ? 'danger' : ''">{{ row.ratio != null ? row.ratio + '%' : '—' }}</b></template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <!-- 🆕 盈利改善1b：成本黑洞审计——清单不清零，毛利榜就系统性虚高 -->
        <el-tab-pane v-if="tv('audit')" label="成本审计" name="audit">
          <div class="summary-bar" style="margin-bottom:10px" v-if="auditData">
            <span>本月未归集到项目的成本 <b class="danger">{{ fmtMoney(auditData.month_unallocated) }}</b></span>
            <span>累计未归集 <b class="danger">{{ fmtMoney(auditData.total_unallocated) }}</b></span>
            <el-button v-if="auditData.fillable_count" size="small" type="primary" @click="backfillPrices">
              ⚡ 一键回填 {{ auditData.fillable_count }} 条已补价的无价流水
            </el-button>
            <span class="muted small">这三张清单是「项目毛利」可信度的前提：无主领料的钱在全系统蒸发、无价入库压低库存与项目成本、孤儿采购挂空</span>
          </div>
          <el-tabs v-model="auditTab" type="card" v-loading="auditLoading">
            <el-tab-pane :label="`无主领料 (${auditData?.orphan_out.length ?? 0})`" name="orphan_out">
              <el-table show-overflow-tooltip :data="auditData?.orphan_out || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
                <el-table-column prop="ref_no" label="单据号" width="130"><template #default="{ row }"><span class="code">{{ row.ref_no }}</span></template></el-table-column>
                <el-table-column prop="biz_date" label="日期" width="100" />
                <el-table-column label="物料" min-width="140"><template #default="{ row }">{{ row.name }}<span v-if="specOf(row.name, row.spec)" class="muted small"> · {{ specOf(row.name, row.spec) }}</span></template></el-table-column>
                <el-table-column prop="qty" label="数量" width="70" align="right" />
                <el-table-column label="估值(均价)" width="110" align="right"><template #default="{ row }"><b v-if="row.value != null" class="danger">{{ fmtMoney(row.value) }}</b><span v-else class="muted">无均价</span></template></el-table-column>
                <el-table-column prop="party" label="领用方" min-width="110"><template #default="{ row }">{{ row.party || '—' }}</template></el-table-column>
                <el-table-column label="补选项目（归集）" min-width="250" fixed="right">
                  <template #default="{ row }">
                    <div style="display:flex;gap:6px;align-items:center">
                      <el-select v-model="assignPid[row.id]" filterable clearable size="small" placeholder="归集到哪个项目" style="flex:1">
                        <el-option v-for="p in auditProjects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
                      </el-select>
                      <el-button size="small" type="primary" @click="assignProject(row.id)">归集</el-button>
                    </div>
                  </template>
                </el-table-column>
              </el-table>
              <EmptyHint v-if="!auditLoading && !(auditData?.orphan_out.length)" text="没有无主领料 ✅（出库登记已强制选项目/非项目领用）" />
            </el-tab-pane>
            <el-tab-pane :label="`无价入库 (${auditData?.unpriced_in.length ?? 0})`" name="unpriced">
              <el-table show-overflow-tooltip :data="auditData?.unpriced_in || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
                <el-table-column prop="ref_no" label="单据号" width="130"><template #default="{ row }"><span class="code">{{ row.ref_no }}</span></template></el-table-column>
                <el-table-column label="方向" width="70"><template #default="{ row }"><el-tag size="small" :type="row.direction === 'in' ? 'success' : 'warning'" effect="plain">{{ row.direction === 'in' ? '入库' : '出库' }}</el-tag></template></el-table-column>
                <el-table-column prop="biz_date" label="日期" width="100" />
                <el-table-column label="物料" min-width="140"><template #default="{ row }">{{ row.name }}<span v-if="specOf(row.name, row.spec)" class="muted small"> · {{ specOf(row.name, row.spec) }}</span></template></el-table-column>
                <el-table-column prop="qty" label="数量" width="70" align="right" />
                <el-table-column prop="po_no" label="采购单号" width="140"><template #default="{ row }"><span class="code">{{ row.po_no || '—' }}</span></template></el-table-column>
                <el-table-column prop="supplier" label="供应商" min-width="120"><template #default="{ row }">{{ row.supplier || '—' }}</template></el-table-column>
                <el-table-column label="采购侧单价" width="110" align="right">
                  <template #default="{ row }">
                    <el-tag v-if="row.fillable" size="small" type="success">{{ fmtMoney(row.item_price) }} 可回填</el-tag>
                    <el-tag v-else size="small" type="danger" effect="plain">采购也没价</el-tag>
                  </template>
                </el-table-column>
              </el-table>
              <div class="muted small" style="margin-top:6px">「可回填」= 采购明细后来补了价，但收货流水还没同步——点上方「一键回填」。「采购也没价」的请先到采购明细补单价（补价现在会自动同步流水）。</div>
              <EmptyHint v-if="!auditLoading && !(auditData?.unpriced_in.length)" text="没有无价入库 ✅" />
            </el-tab-pane>
            <el-tab-pane :label="`孤儿采购 (${auditData?.orphan_purchase.length ?? 0})`" name="orphan_purchase">
              <el-table show-overflow-tooltip :data="auditData?.orphan_purchase || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
                <el-table-column prop="po_no" label="采购单号" width="140"><template #default="{ row }"><span class="code">{{ row.po_no || '散件' }}</span></template></el-table-column>
                <el-table-column label="名称" min-width="140"><template #default="{ row }">{{ row.item_name }}<span v-if="specOf(row.item_name, row.spec)" class="muted small"> · {{ specOf(row.item_name, row.spec) }}</span></template></el-table-column>
                <el-table-column prop="supplier" label="供应商" min-width="120"><template #default="{ row }">{{ row.supplier || '—' }}</template></el-table-column>
                <el-table-column label="订单编号(挂空)" width="130"><template #default="{ row }"><b class="danger">{{ row.project_code || '未填' }}</b></template></el-table-column>
                <el-table-column label="收货金额" width="110" align="right"><template #default="{ row }">{{ row.received_amount ? fmtMoney(row.received_amount) : '—' }}</template></el-table-column>
                <el-table-column prop="arrival_date" label="到货日期" width="100"><template #default="{ row }">{{ row.arrival_date || '—' }}</template></el-table-column>
                <el-table-column prop="buyer" label="采购员" width="90"><template #default="{ row }">{{ row.buyer || '—' }}</template></el-table-column>
              </el-table>
              <div class="muted small" style="margin-top:6px">订单编号既不是项目编号、也不在字典「订单编号」里 → 成本挂空。请到采购明细把订单编号改成正确的项目编号（下单入口已改为只能下拉选择，新增不会再产生）。</div>
              <EmptyHint v-if="!auditLoading && !(auditData?.orphan_purchase.length)" text="没有孤儿采购 ✅" />
            </el-tab-pane>
            <el-tab-pane label="双口径对账" name="recon">
              <el-table show-overflow-tooltip :data="auditData?.recon || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
                <el-table-column label="项目" min-width="170"><template #default="{ row }"><b class="code">{{ row.code }}</b> {{ row.name }}</template></el-table-column>
                <el-table-column prop="purchase" label="采购口径(收货金额)" width="150" align="right" sortable><template #default="{ row }">{{ fmtMoney(row.purchase) }}</template></el-table-column>
                <el-table-column prop="warehouse" label="仓库口径(领料×均价)" width="160" align="right" sortable><template #default="{ row }">{{ fmtMoney(row.warehouse) }}</template></el-table-column>
                <el-table-column prop="diff" label="差异" width="130" align="right" sortable>
                  <template #default="{ row }"><b :class="Math.abs(row.diff) > 0.01 ? 'danger' : 'profit-pos'">{{ fmtMoney(row.diff) }}</b></template>
                </el-table-column>
              </el-table>
              <div class="muted small" style="margin-top:6px">差异大 = 漏归集重灾区：采购按订单编号进了项目、但材料没领(还压在库存)，或领料没挂项目。默认按差异绝对值倒序。</div>
              <EmptyHint v-if="!auditLoading && !(auditData?.recon.length)" text="暂无对账数据" />
            </el-tab-pane>
          </el-tabs>
        </el-tab-pane>

        <!-- 🆕 盈利改善2：资金面板——现金断裂比利润难看死得更快 -->
        <el-tab-pane v-if="tv('fund')" label="资金面板" name="fund">
          <div v-if="fundData" class="kpi-grid" style="margin-bottom:12px">
            <div class="kpi"><div class="kpi-v danger">{{ fmtMoney(fundData.receivables.total) }}</div><div class="kpi-l">逾期应收（尾款+发货款）</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(fundData.prepay.total) }}</div><div class="kpi-l">预付敞口（已付未到货）</div></div>
            <div class="kpi"><div class="kpi-v danger">{{ fmtMoney(fundData.payables.overdue_total) }}</div><div class="kpi-l">逾期应付（断供风险）</div></div>
            <div class="kpi"><div class="kpi-v">{{ fmtMoney(fundData.dead_stock.total_value) }}</div><div class="kpi-l">呆滞库存（≥90天无动销）</div></div>
          </div>
          <el-tabs v-model="fundTab" type="card" v-loading="fundLoading">
            <el-tab-pane :label="`⏰ 逾期应收 (${recvRows.length})`" name="recv">
              <div class="summary-bar" style="margin-bottom:10px" v-if="fundData">
                <span v-for="b in fundData.receivables.buckets" :key="b.bucket">
                  {{ b.bucket }} <b :class="b.bucket === '90天以上' ? 'danger' : ''">{{ fmtMoney(b.amount) }}</b>
                </span>
                <span class="muted small">尾款按约定日、发货款按发货日计龄；逾期后系统每周自动催办（销售→第2周+主管→第3周+管理层）</span>
              </div>
              <el-row :gutter="16">
                <el-col :span="16">
                  <el-table show-overflow-tooltip :data="recvRows" stripe size="small" class="compact-tbl" max-height="calc(100vh - 460px)">
                    <el-table-column label="项目" min-width="150"><template #default="{ row }"><b class="code">{{ row.code }}</b> {{ row.name }}</template></el-table-column>
                    <el-table-column prop="kind" label="款项" width="70"><template #default="{ row }"><el-tag size="small" :type="row.kind === '尾款' ? 'warning' : 'primary'" effect="plain">{{ row.kind }}</el-tag></template></el-table-column>
                    <el-table-column prop="customer" label="客户" min-width="110" />
                    <el-table-column prop="sales_name" label="销售" width="80" />
                    <el-table-column label="金额" width="110" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.amount) }}</b></template></el-table-column>
                    <el-table-column prop="due_date" label="约定/发货日" width="105" />
                    <el-table-column prop="over_days" label="逾期" width="90" align="right" sortable>
                      <template #default="{ row }"><b :class="row.over_days > 90 ? 'danger' : ''">{{ row.over_days }}天</b></template>
                    </el-table-column>
                  </el-table>
                  <EmptyHint v-if="!fundLoading && !recvRows.length" text="没有逾期应收 ✅" />
                </el-col>
                <el-col :span="8" v-if="fundData">
                  <div class="section-title">按客户排名（Top10）</div>
                  <el-table show-overflow-tooltip :data="fundData.receivables.by_customer" size="small" class="compact-tbl">
                    <el-table-column prop="key" label="客户" min-width="120" />
                    <el-table-column label="逾期金额" width="120" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.amount) }}</b></template></el-table-column>
                  </el-table>
                  <div class="section-title" style="margin-top:10px">按销售排名（Top10）</div>
                  <el-table show-overflow-tooltip :data="fundData.receivables.by_sales" size="small" class="compact-tbl">
                    <el-table-column prop="key" label="销售" min-width="120" />
                    <el-table-column label="逾期金额" width="120" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.amount) }}</b></template></el-table-column>
                  </el-table>
                </el-col>
              </el-row>
            </el-tab-pane>
            <el-tab-pane :label="`预付敞口 (${fundData?.prepay.rows.length ?? 0})`" name="prepay">
              <div class="muted small" style="margin-bottom:8px">已付款但货未到 = 押在供应商那里的钱，按押款天数催交货。</div>
              <el-table show-overflow-tooltip :data="fundData?.prepay.rows || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 420px)" :fit="false">
                <el-table-column prop="supplier" label="供应商" min-width="220" />
                <el-table-column label="押款金额" width="130" align="right" sortable prop="amount"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.amount) }}</b></template></el-table-column>
                <el-table-column prop="items" label="明细数" width="80" align="center" />
                <el-table-column prop="oldest_paid" label="最早付款日" width="110"><template #default="{ row }">{{ row.oldest_paid || '—' }}</template></el-table-column>
                <el-table-column prop="days" label="已押天数" width="100" align="right" sortable><template #default="{ row }"><b v-if="row.days != null" :class="row.days > 30 ? 'danger' : ''">{{ row.days }}天</b><span v-else class="muted">未记付款日</span></template></el-table-column>
              </el-table>
              <EmptyHint v-if="!fundLoading && !(fundData?.prepay.rows.length)" text="没有预付敞口 ✅" />
            </el-tab-pane>
            <el-tab-pane label="应付账期" name="payterm">
              <div class="summary-bar" style="margin-bottom:10px" v-if="fundData">
                <span>逾期未付 <b class="danger">{{ fmtMoney(fundData.payables.overdue_total) }}</b></span>
                <span>14天内到期 <b>{{ fmtMoney(fundData.payables.due_soon_total) }}</b></span>
                <span>历史提前付 <b>{{ fmtMoney(fundData.payables.early_paid.total) }}</b>（平均白放弃 {{ fundData.payables.early_paid.avg_wasted_days }} 天免息）</span>
                <span class="muted small">到期日=到货日+供应商账期；请款审批/付款页每张单都有「距到期」标签，按到期排程付款</span>
              </div>
              <el-row :gutter="16">
                <el-col :span="12">
                  <div class="section-title danger">⚠️ 逾期未付（断供风险）</div>
                  <el-table show-overflow-tooltip :data="fundData?.payables.overdue || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 480px)">
                    <el-table-column prop="supplier" label="供应商" min-width="130" />
                    <el-table-column label="逾期金额" width="115" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.amount) }}</b></template></el-table-column>
                    <el-table-column prop="worst_days" label="最长逾期" width="90" align="right"><template #default="{ row }">{{ row.worst_days }}天</template></el-table-column>
                  </el-table>
                  <EmptyHint v-if="!fundLoading && !(fundData?.payables.overdue.length)" text="没有逾期应付 ✅" size="sm" />
                </el-col>
                <el-col :span="12">
                  <div class="section-title">🗓 14天内到期（排程付款）</div>
                  <el-table show-overflow-tooltip :data="fundData?.payables.due_soon || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 480px)">
                    <el-table-column prop="supplier" label="供应商" min-width="130" />
                    <el-table-column label="金额" width="115" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
                    <el-table-column prop="nearest_due" label="最近到期" width="105" />
                  </el-table>
                  <EmptyHint v-if="!fundLoading && !(fundData?.payables.due_soon.length)" text="14天内无到期应付" size="sm" />
                </el-col>
              </el-row>
              <el-alert v-if="fundData?.payables.missing_credit.length" type="warning" :closable="false" style="margin-top:10px"
                :title="`有 ${fundData.payables.missing_credit.length} 家供应商未维护账期天数（合计应付 ${fmtMoney(fundData.payables.missing_credit.reduce((s, x) => s + x.outstanding, 0))}），无法算到期日——请在供应商档案补 credit_days：${fundData.payables.missing_credit.slice(0, 5).map(x => x.supplier).join('、')}${fundData.payables.missing_credit.length > 5 ? ' 等' : ''}`" />
            </el-tab-pane>
            <el-tab-pane :label="`呆滞库存 (${fundData?.dead_stock.rows.length ?? 0})`" name="dead">
              <div class="summary-bar" style="margin-bottom:10px" v-if="fundData">
                <span>锁死现金合计 <b class="danger">{{ fmtMoney(fundData.dead_stock.total_value) }}</b></span>
                <span v-for="b in fundData.dead_stock.buckets" :key="b.bucket">{{ b.bucket }} <b>{{ fmtMoney(b.value) }}</b></span>
                <span class="muted small">≥90 天无出库动销的 现存×加权均价；可回溯谁为哪个项目买的</span>
              </div>
              <el-table show-overflow-tooltip :data="fundData?.dead_stock.rows || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 470px)">
                <el-table-column label="物料" min-width="150"><template #default="{ row }">{{ row.name }}<span v-if="specOf(row.name, row.spec)" class="muted small"> · {{ specOf(row.name, row.spec) }}</span></template></el-table-column>
                <el-table-column prop="stock" label="现存" width="80" align="right" />
                <el-table-column label="金额" width="110" align="right" sortable prop="value"><template #default="{ row }"><b v-if="row.value != null" class="danger">{{ fmtMoney(row.value) }}</b><span v-else class="muted">无均价</span></template></el-table-column>
                <el-table-column prop="idle_days" label="呆滞天数" width="100" align="right" sortable>
                  <template #default="{ row }">{{ row.idle_days }}天<el-tag v-if="row.never_out" size="small" type="danger" effect="plain" style="margin-left:4px">从未出库</el-tag></template>
                </el-table-column>
                <el-table-column prop="bucket" label="档位" width="100" />
                <el-table-column label="采购回溯" min-width="140"><template #default="{ row }">{{ row.trace_buyer || '—' }}<span v-if="row.trace_project" class="muted small"> → {{ row.trace_project }}</span></template></el-table-column>
              </el-table>
              <EmptyHint v-if="!fundLoading && !(fundData?.dead_stock.rows.length)" text="没有呆滞库存 ✅" />
              <div v-if="fundData?.dead_stock.safety.length" style="margin-top:12px">
                <div class="section-title">🩺 安全库存体检</div>
                <el-table show-overflow-tooltip :data="fundData.dead_stock.safety" size="small" class="compact-tbl" style="max-width:860px" :fit="false">
                  <el-table-column label="物料" min-width="150"><template #default="{ row }">{{ row.name }}<span v-if="specOf(row.name, row.spec)" class="muted small"> · {{ specOf(row.name, row.spec) }}</span></template></el-table-column>
                  <el-table-column prop="safety_stock" label="安全库存" width="90" align="right" />
                  <el-table-column prop="month_avg_out" label="月均出库" width="90" align="right" />
                  <el-table-column prop="stock" label="现存" width="80" align="right" />
                  <el-table-column label="体检结论" width="170"><template #default="{ row }"><el-tag size="small" :type="row.verdict === '偏高压钱' ? 'warning' : 'danger'" effect="plain">{{ row.verdict }}</el-tag></template></el-table-column>
                </el-table>
              </div>
            </el-tab-pane>
            <el-tab-pane label="13周现金排程" name="cash">
              <el-alert type="info" :closable="false" style="margin-bottom:10px" :title="fundData?.cashgap.note || ''" />
              <el-table show-overflow-tooltip :data="fundData?.cashgap.weeks || []" stripe size="small" class="compact-tbl" max-height="calc(100vh - 420px)"
                        :fit="false" :row-class-name="({ row }: any) => row.cum < 0 ? 'pnl-loss-row' : ''">
                <el-table-column prop="label" label="周" width="130"><template #default="{ row }"><b>{{ row.label }}</b></template></el-table-column>
                <el-table-column label="预计流入" width="130" align="right"><template #default="{ row }"><span :class="row.inflow ? 'profit-pos' : 'muted'">{{ row.inflow ? fmtMoney(row.inflow) : '—' }}</span></template></el-table-column>
                <el-table-column label="预计流出" width="130" align="right"><template #default="{ row }"><span :class="row.outflow ? 'danger' : 'muted'">{{ row.outflow ? fmtMoney(row.outflow) : '—' }}</span></template></el-table-column>
                <el-table-column label="当周净额" width="130" align="right"><template #default="{ row }"><b :class="row.net < 0 ? 'danger' : 'profit-pos'">{{ fmtMoney(row.net) }}</b></template></el-table-column>
                <el-table-column label="累计净额" width="140" align="right"><template #default="{ row }"><b :class="row.cum < 0 ? 'danger' : 'profit-pos'">{{ fmtMoney(row.cum) }}</b><el-tag v-if="row.cum < 0" size="small" type="danger" style="margin-left:6px">缺口</el-tag></template></el-table-column>
              </el-table>
              <div class="muted small" style="margin-top:8px" v-if="fundData">
                另有：无约定日的已发货应收 {{ fmtMoney(fundData.cashgap.undated_inflow) }}（催回即是流入）；
                13周以后的流入 {{ fmtMoney(fundData.cashgap.inflow_later) }} / 流出 {{ fmtMoney(fundData.cashgap.outflow_later) }}。
              </div>
            </el-tab-pane>
          </el-tabs>
        </el-tab-pane>

        <!-- 🆕 采购应付 -->
        <el-tab-pane v-if="tv('payables')" label="采购应付" name="payables">
          <div class="summary-bar" style="margin-bottom:10px">
            <span>应付合计 <b class="danger">{{ fmtMoney(payablesTotal) }}</b></span>
            <span class="muted small">已收货未付款 = 对供应商的应付;审批走「请款审批」，付款走「付款」页</span>
          </div>
          <el-table show-overflow-tooltip :data="payables" v-loading="payablesLoading" stripe size="small"
                    max-height="calc(100vh - 300px)" :scrollbar-always-on="true" class="compact-tbl" :fit="false">
            <el-table-column prop="supplier_name" label="供应商" min-width="220" />
            <el-table-column prop="category" label="分类" width="90"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column label="收货合计" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.received_total) }}</template></el-table-column>
            <el-table-column label="开票合计" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.invoice_total) }}</template></el-table-column>
            <el-table-column label="已付款" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.paid_total) }}</template></el-table-column>
            <el-table-column label="应付余额" width="120" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.outstanding) }}</b></template></el-table-column>
            <el-table-column prop="item_count" label="明细数" width="80" align="center" />
          </el-table>
          <EmptyHint v-if="!payablesLoading && !payables.length" text="暂无采购应付" />
        </el-tab-pane>

        <!-- 🆕 库存 / 成本（需求六：仅管理层可见） -->
        <el-tab-pane v-if="tv('inventory')" label="库存 / 成本" name="inventory">
          <div class="summary-bar" style="margin-bottom:10px">
            <span>通用库存金额 <b class="amt">{{ fmtMoney(invValue.total_value) }}</b></span>
            <span>项目材料成本 <b class="amt">{{ fmtMoney(projCostTotal) }}</b></span>
            <span v-if="projCostExtra.unassigned" class="muted">未归集 <b>{{ fmtMoney(projCostExtra.unassigned) }}</b></span>
          </div>
          <!-- 🆕 #373/#388：这一刀砍掉了大半个库存金额，不把去向说清楚，财务只会以为系统坏了 -->
          <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px"
            title="口径：物料按有没有订单编号分两边，同一笔钱只算一次">
            <template #default>
              <div style="line-height:1.7">
                <b>库存金额</b>只算<b>通用物料</b>（现存 × 入库加权均价）。<br />
                <b>项目材料成本</b> = 挂了订单编号的<b>收货金额</b>（到货即计，不用等领料出库）
                + 通用物料<b>领用出库</b> × 加权均价。<br />
                <template v-if="invValue.excluded_value">
                  本页已把 <b>{{ invValue.excluded_count }}</b> 种项目物料共
                  <b>{{ fmtMoney(invValue.excluded_value) }}</b> 从库存金额移到右侧项目材料成本——它们是买给具体项目的料，不是公司备货。
                </template>
                <template v-if="projCostExtra.unassigned">
                  另有 <b>{{ fmtMoney(projCostExtra.unassigned) }}</b> 未归集（项目物料上没填订单编号的零星收货），在采购明细里补上编号即可归位。
                </template>
              </div>
            </template>
          </el-alert>
          <el-row :gutter="16">
            <el-col :span="12">
              <div class="section-title">库存金额（按物料 · 仅通用物料）</div>
              <el-table show-overflow-tooltip :data="invValue.rows" v-loading="invLoading" stripe size="small" max-height="calc(100vh - 420px)" class="compact-tbl">
                <!-- 🆕 #408（赵仁辉）「未排序」：默认仍按金额从大到小（先看占钱多的），
                     但每列都可点表头自己排——他要按物料名或现存找东西时不用一行行翻 -->
                <el-table-column prop="name" label="物料" min-width="140" sortable />
                <el-table-column prop="spec" label="规格" min-width="110" sortable><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
                <el-table-column prop="stock" label="现存" width="86" align="right" sortable><template #default="{ row }">{{ row.stock }}</template></el-table-column>
                <el-table-column prop="avg_price" label="均价" width="106" align="right" sortable><template #default="{ row }">{{ row.avg_price != null ? fmtMoney(row.avg_price) : '—' }}</template></el-table-column>
                <el-table-column prop="value" label="金额" width="126" align="right" sortable><template #default="{ row }"><b>{{ row.value != null ? fmtMoney(row.value) : '—' }}</b></template></el-table-column>
              </el-table>
              <EmptyHint v-if="!invLoading && !invValue.rows.length" text="暂无通用库存物料（料都挂了订单编号，已归入项目材料成本）" size="sm" />
            </el-col>
            <el-col :span="12">
              <div class="section-title">项目材料成本<span class="muted small">（点左侧箭头展开明细）</span></div>
              <!-- 🆕 #389 展开明细：row-key + @expand-change 懒加载，展开哪个查哪个 -->
              <el-table show-overflow-tooltip :data="projCost" v-loading="invLoading" stripe size="small"
                        max-height="calc(100vh - 420px)" class="compact-tbl"
                        row-key="project_id" @expand-change="onCostExpand">
                <el-table-column type="expand">
                  <template #default="{ row }">
                    <!-- ⚠️ 列宽总和必须留在展开格宽度内：这一列只占半屏(~485px)，
                         固定列宽加起来超了 el-table 不会收缩，最右的「金额」直接被卡掉一截。
                         实测 150+70+80+90+110=500 > 485，金额列少 37px。 -->
                    <div style="padding:6px 8px 10px 12px">
                      <div v-if="costDetail[row.project_id] === 'loading'" class="muted small">明细加载中…</div>
                      <template v-else-if="costDetail[row.project_id]">
                        <el-table :data="(costDetail[row.project_id] as any).rows" size="small" stripe class="compact-tbl" max-height="300">
                          <el-table-column label="物料" min-width="110" show-overflow-tooltip>
                            <template #default="{ row: d }">{{ d.name }}<span v-if="specOf(d.name, d.spec)" class="muted small"> · {{ specOf(d.name, d.spec) }}</span></template>
                          </el-table-column>
                          <el-table-column label="来源" width="58" align="center">
                            <template #default="{ row: d }">
                              <el-tag size="small" :type="d.leg === '收货' ? 'success' : 'warning'" effect="plain">{{ d.leg }}</el-tag>
                            </template>
                          </el-table-column>
                          <el-table-column label="数量" width="70" align="right"><template #default="{ row: d }">{{ d.qty }} {{ d.unit || '' }}</template></el-table-column>
                          <el-table-column label="均价" width="76" align="right"><template #default="{ row: d }">{{ d.avg_price != null ? fmtMoney(d.avg_price) : '—' }}</template></el-table-column>
                          <el-table-column label="金额" width="96" align="right"><template #default="{ row: d }"><b>{{ d.amount != null ? fmtMoney(d.amount) : '缺价' }}</b></template></el-table-column>
                        </el-table>
                        <div class="muted small" style="margin-top:6px">
                          明细合计 {{ fmtMoney((costDetail[row.project_id] as any).total) }}
                          <template v-if="(costDetail[row.project_id] as any).noprice_count">
                            ；<b style="color:var(--el-color-warning)">{{ (costDetail[row.project_id] as any).noprice_count }} 项缺加权均价</b>（入库没填金额，成本偏低）
                          </template>
                          。「收货」= 挂本项目编号的采购入库；「领料」= 从通用库存领用到本项目。
                        </div>
                      </template>
                    </div>
                  </template>
                </el-table-column>
                <!-- 🆕 #408：他圈的就是这一列——默认按成本从大到小排，要按项目编号找就点「项目」表头。
                     ⚠️ 用 prop 指定排序依据（code / cost），光写 sortable 而列是模板列的话排不动。 -->
                <el-table-column prop="code" label="项目" min-width="120" sortable><template #default="{ row }"><b class="code">{{ row.code }}</b> {{ row.name }}</template></el-table-column>
                <el-table-column prop="cost" label="材料成本" width="136" align="right" sortable><template #default="{ row }"><b>{{ fmtMoney(row.cost) }}</b></template></el-table-column>
              </el-table>
              <EmptyHint v-if="!invLoading && !projCost.length" text="暂无项目材料成本" size="sm" />
            </el-col>
          </el-row>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 驳回原因弹窗（审批拒绝 / 撤回审批 / 付款驳回 共用） -->
    <el-dialog v-model="rejectDialogVisible" :title="rejectMeta.title" width="460px">
      <el-alert v-if="rejectMode !== 'reject'" type="warning" :closable="false" style="margin-bottom:12px"
        title="退回后发起人会收到站内+企微通知，修改（如去供应商档案改收款账号）后可重新提交，届时需重新审批。" />
      <el-form label-width="80px">
        <el-form-item :label="rejectMode === 'reject' ? '拒绝原因' : '退回原因'">
          <el-input v-model="rejectReason" type="textarea" :rows="3" :placeholder="rejectMeta.ph" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rejectDialogVisible = false">取消</el-button>
        <el-button type="danger" @click="submitReject">确认退回</el-button>
      </template>
    </el-dialog>

    <!-- 记录付款弹窗（🆕 需求十六：展示收款账户信息 + 关联采购单）-->
    <el-dialog v-model="payDialogVisible" title="记录付款" width="600px">
      <div v-if="payingPr" class="pay-info">
        <div class="pay-info-block">
          <div class="pay-info-title">🏦 收款账户信息（供应商：{{ payingPr.supplier_name }}<el-button v-if="payingPr.supplier_name" size="small" link type="primary" style="margin-left:8px" @click="copyText(payingPr.supplier_name)">复制</el-button>）</div>
          <div class="pay-info-row"><span class="k">开户行</span>{{ payingPr.supplier_bank_name || '—' }}<el-button v-if="payingPr.supplier_bank_name" size="small" link type="primary" style="margin-left:8px" @click="copyText(payingPr.supplier_bank_name)">复制</el-button></div>
          <div class="pay-info-row"><span class="k">银行账号</span><b>{{ payingPr.supplier_bank_account || '—' }}</b><el-button v-if="payingPr.supplier_bank_account" size="small" link type="primary" style="margin-left:8px" @click="copyText(payingPr.supplier_bank_account)">复制</el-button></div>
          <div class="pay-info-row"><span class="k">税号</span>{{ payingPr.supplier_tax_no || '—' }}<el-button v-if="payingPr.supplier_tax_no" size="small" link type="primary" style="margin-left:8px" @click="copyText(payingPr.supplier_tax_no)">复制</el-button></div>
          <div v-if="!payingPr.supplier_bank_account" class="muted small">该供应商未维护银行账号，请先在采购管理补全供应商档案。</div>
        </div>
        <div class="pay-info-block">
          <div class="pay-info-title">📄 关联采购单
            <template v-if="payingPr.po_nos?.length">
              <el-button v-for="po in payingPr.po_nos" :key="po" size="small" link type="primary" @click="downloadPoPdf(po)">📄 {{ po }}</el-button>
            </template>
          </div>
          <el-table show-overflow-tooltip :data="payingPr.items" size="small" border max-height="180">
            <el-table-column label="采购单号" width="150"><template #default="{ row }"><span class="code">{{ row.po_no || '散件' }}</span></template></el-table-column>
            <el-table-column label="名称" min-width="120"><template #default="{ row }">{{ row.item_name }}<span v-if="specOf(row.item_name, row.spec)" class="muted small"> · {{ specOf(row.item_name, row.spec) }}</span></template></el-table-column>
            <el-table-column label="项目" width="100"><template #default="{ row }">{{ row.project_code || '—' }}</template></el-table-column>
            <el-table-column label="本次付款" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.allocated_amount) }}</template></el-table-column>
          </el-table>
        </div>
      </div>
      <el-form :model="payForm" label-width="90px" style="margin-top:12px">
        <el-form-item label="付款金额">
          <el-input-number v-model="payForm.paid_amount" :min="0" :precision="2" style="width:100%" />
        </el-form-item>
        <el-form-item label="付款日期">
          <el-date-picker v-model="payForm.paid_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
        <el-form-item label="付款方式">
          <el-select v-model="payForm.payment_method" style="width:100%">
            <el-option value="银行转账" label="银行转账" />
            <el-option value="现金" label="现金" />
            <el-option value="支票" label="支票" />
            <el-option value="其他" label="其他" />
          </el-select>
        </el-form-item>
        <el-form-item label="付款单据">
          <el-button :icon="UploadFilled" @click="pickVoucher">上传付款凭证</el-button>
          <span v-if="payVoucherFile" style="margin-left:10px;font-size:13px">{{ payVoucherFile.name }}</span>
          <div class="muted small" style="margin-top:4px">选填：付款水单 / 回单（PDF / 图片 / Excel）</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="payDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitPay">确认付款</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 🆕 安装/售后费用筛选条 */
.as-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.as-detail { padding: 6px 18px 8px; }
.as-item { display: flex; align-items: center; gap: 10px; font-size: 13px; line-height: 2; }
.as-item .as-nm { min-width: 90px; }
.as-item .as-amt { min-width: 90px; text-align: right; font-variant-numeric: tabular-nums; }
.miss { color: var(--el-color-danger); }
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.small { font-size: 12px; }
.summary-bar { display: flex; gap: 24px; align-items: center; padding: 10px 16px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 14px; }
.section-title { font-weight: 600; font-size: 14px; margin: 4px 0 8px; color: var(--el-text-color-primary); }
.danger { color: var(--el-color-danger); }
.amt { color: var(--el-color-primary); }
.profit-pos { color: var(--el-color-success); }
/* 🆕 项目毛利红黑榜：亏损行整行淡红 */
:deep(.pnl-loss-row) { --el-table-tr-bg-color: var(--el-color-danger-light-9); }
.code { color: var(--primary, #2563eb); }
/* 🆕 需求十六：付款弹窗的账户信息/采购单区块 */
.pay-info { display: flex; flex-direction: column; gap: 12px; }
.pay-info-block { background: var(--el-fill-color-light); border-radius: 8px; padding: 10px 14px; }
.pay-info-title { font-weight: 600; font-size: 13.5px; margin-bottom: 6px; color: var(--el-text-color-primary); }
.pay-info-row { font-size: 13px; line-height: 1.9; color: var(--el-text-color-regular); }
.pay-info-row .k { display: inline-block; min-width: 72px; color: var(--el-text-color-secondary); }
</style>
