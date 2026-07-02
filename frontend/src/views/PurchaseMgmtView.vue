<script setup lang="ts">
// 采购管理（含采购部）：采购部 / 采购明细 / 供应商账目 / 汇总报表
import { ref, computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Refresh, View, Plus, Delete, Printer, Upload } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { datasheetsApi } from '@/api/datasheets'
import EmptyHint from '@/components/EmptyHint.vue'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasRole('buyer', 'buyer_lead', 'buyer_standard', 'buyer_outsource', 'admin', 'manager'))
const isLeadOrAbove = computed(() => auth.hasRole('buyer_lead', 'finance', 'admin', 'manager'))
const showPurchaseTab = computed(() => auth.hasRole('buyer', 'buyer_lead', 'buyer_standard', 'buyer_outsource', 'admin', 'manager'))

function fmtMoney(v: number | undefined | null) {
  if (v == null) return '¥0'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// 🆕 付款方式下拉选项（可自定义追加）
const PAY_METHODS = ['现金', '转账', '月结', '承兑', '预付']

// ===== types =====
interface Att { id: number; name: string }
interface PurchaseRow {
  project_id: number; code: string; name: string; designer?: string | null
  outsource_sheet_id?: number | null; sheetmetal_sheet_id?: number | null
  material_sheet_id?: number | null; laser_sheet_id?: number | null
  elec_po_sheet_id?: number | null; standard_sheet_id?: number | null
  cad_laser_files: Att[]; outsource_img_files: Att[]
}
interface SupplierOut {
  id: number; name: string; code?: string | null; category?: string | null
  contact?: string | null; phone?: string | null; address?: string | null
  tax_no?: string | null; bank_name?: string | null; bank_account?: string | null
  settlement_type?: string | null; credit_days?: number | null
  status: string; notes?: string | null; created_at: string
}
interface PurchaseItemOut {
  id: number; po_no?: string | null; supplier_id: number; supplier_name: string
  delivery_date?: string | null; contract_no?: string | null; arrival_date?: string | null
  project_code?: string | null; delivery_note_no?: string | null
  item_name: string; spec?: string | null; qty?: number | null; unit_price?: number | null
  received_amount: number; invoice_date?: string | null; tax_rate?: string | null
  invoice_amount: number; paid_amount: number; paid_date?: string | null
  payment_method?: string | null
  invoice_status: string; buyer_id?: number | null; buyer_name?: string | null
  notes?: string | null; created_at: string
}
interface ItemSummary { received_total: number; uninvoiced: number; paid_total: number; outstanding: number; count: number }
interface SupplierStatementRow {
  supplier_id: number; supplier_name: string; category?: string | null
  opening_balance: number; received_total: number; invoice_total: number
  paid_total: number; outstanding: number; uninvoiced: number; item_count: number
}
interface StatementList {
  rows: SupplierStatementRow[]; total_opening: number; total_received: number
  total_paid: number; total_outstanding: number
}
interface PaymentRequestOut {
  id: number; supplier_id: number; supplier_name: string; requested_amount: number
  requester_id?: number; requester_name?: string; status: string; notes?: string
  approved_at?: string; paid_amount?: number; paid_date?: string
  payment_method?: string; reject_reason?: string; created_at: string
  items: Array<{ item_id: number; item_name: string; allocated_amount: number }>
}
interface PurchaseKPI { month_amount: number; quarter_amount: number; year_amount: number; total_outstanding: number; pending_requests: number }
interface MonthlyPoint { month: string; amount: number; paid: number }
interface BuyerRow { buyer_id?: number; buyer_name: string; amount: number; count: number }
interface TopSupplier { supplier_id: number; supplier_name: string; amount: number; count: number }

// ===== tab =====
const tab = ref(auth.hasRole('buyer', 'buyer_lead', 'buyer_standard', 'buyer_outsource', 'admin', 'manager') ? 'purchase' : 'items')
const loading = ref(false)

// ===== 采购部 state =====
const purchaseLoading = ref(false)
const purchaseRows = ref<PurchaseRow[]>([])

const isFangbusen = computed(() => auth.user?.username === 'fangbusen')
const isWangqin   = computed(() => auth.user?.username === 'wangqin')
const isLixinxin  = computed(() => auth.user?.username === 'lixinxin')
const seeAll = computed(() => !isFangbusen.value && !isWangqin.value && !isLixinxin.value)
const showDesigner      = computed(() => seeAll.value || isWangqin.value || isFangbusen.value)
const showOutsource     = computed(() => seeAll.value || isFangbusen.value)
const showSheetmetal    = computed(() => seeAll.value || isFangbusen.value)
const showMaterial      = computed(() => seeAll.value || isWangqin.value)
const showLaser         = computed(() => seeAll.value || isWangqin.value)
const showCadLaser      = computed(() => seeAll.value || isWangqin.value)
const showElecPo        = computed(() => seeAll.value || isLixinxin.value)
const showStandardSheet = computed(() => seeAll.value || isLixinxin.value)
const showOutImg        = computed(() => seeAll.value || isLixinxin.value)

const curYear = String(new Date().getFullYear())
const pYearFilter = ref(curYear)
const pYearOptions = computed(() => { const y = parseInt(curYear); return [y - 1, y, y + 1].map(String) })
const pProjStatusFilter = ref('进行中')

async function loadPurchaseRows() {
  purchaseLoading.value = true
  try {
    purchaseRows.value = (await http.get<PurchaseRow[]>('/purchase/projects', {
      params: { year: pYearFilter.value, proj_status: pProjStatusFilter.value || undefined }
    })).data
  } finally { purchaseLoading.value = false }
}

// ===== 预览 =====
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewTitle = ref('')
const previewFields = ref<{ id: number; name: string }[]>([])
const previewRecords = ref<any[]>([])
const previewColWidth = computed(() => {
  const n = previewFields.value.length
  if (!n) return 120
  const usable = (typeof window !== 'undefined' ? window.innerWidth : 1280) - 50 - 32
  return Math.max(80, Math.floor(usable / n))
})

async function openPreview(did: number | null | undefined, title: string) {
  if (!did) { ElMessage.info(`该项目暂无「${title}」`); return }
  previewTitle.value = title
  previewVisible.value = true
  previewLoading.value = true
  try {
    const [fs, recs] = await Promise.all([
      datasheetsApi.listFields(did),
      datasheetsApi.listRecords(did),
    ])
    previewFields.value = fs.map((f: any) => ({ id: f.id, name: f.name }))
    previewRecords.value = recs
  } finally { previewLoading.value = false }
}

function cellVal(rec: any, fid: number) {
  return rec.values?.[String(fid)] ?? ''
}

// ===== 打包下载 =====
type SheetKey = 'sheetmetal_sheet_id' | 'standard_sheet_id' | 'outsource_sheet_id' | 'material_sheet_id' | 'laser_sheet_id' | 'elec_po_sheet_id'
const SHEET_DEFS: { key: SheetKey; label: string; vis: () => boolean }[] = [
  { key: 'sheetmetal_sheet_id', label: '钣金装配表', vis: () => showSheetmetal.value },
  { key: 'standard_sheet_id', label: '标准件清单', vis: () => showStandardSheet.value },
  { key: 'outsource_sheet_id', label: '外协加工表', vis: () => showOutsource.value },
  { key: 'material_sheet_id', label: '不锈钢原料下料单', vis: () => showMaterial.value },
  { key: 'laser_sheet_id', label: '激光件清单', vis: () => showLaser.value },
  { key: 'elec_po_sheet_id', label: '电工采购单', vis: () => showElecPo.value },
]

const dlVisible = ref(false)
const dlRow = ref<PurchaseRow | null>(null)
const dlSelSheets = ref<number[]>([])
const dlSelAtts = ref<number[]>([])
const dlPacking = ref(false)

const dlSheets = computed(() => {
  const r = dlRow.value
  if (!r) return [] as { id: number; label: string }[]
  return SHEET_DEFS
    .filter(d => d.vis())
    .map(d => ({ id: r[d.key] as number | null | undefined, label: d.label }))
    .filter((s): s is { id: number; label: string } => s.id != null)
})
const dlAtts = computed(() => {
  const r = dlRow.value
  if (!r) return [] as { id: number; name: string; kind: string }[]
  const out: { id: number; name: string; kind: string }[] = []
  if (showCadLaser.value) r.cad_laser_files.forEach(f => out.push({ id: f.id, name: f.name, kind: 'CAD激光图纸' }))
  if (showOutImg.value) r.outsource_img_files.forEach(f => out.push({ id: f.id, name: f.name, kind: '外购附图' }))
  return out
})
const dlTotal = computed(() => dlSheets.value.length + dlAtts.value.length)
const dlSelCount = computed(() => dlSelSheets.value.length + dlSelAtts.value.length)

function openDownload(row: PurchaseRow) {
  dlRow.value = row
  dlSelSheets.value = dlSheets.value.map(s => s.id)
  dlSelAtts.value = dlAtts.value.map(a => a.id)
  dlVisible.value = true
}
function toggleAllSheets(v: any) { dlSelSheets.value = v ? dlSheets.value.map(s => s.id) : [] }
function toggleAllAtts(v: any) { dlSelAtts.value = v ? dlAtts.value.map(a => a.id) : [] }

async function packDownload() {
  if (!dlRow.value) return
  if (!dlSelCount.value) { ElMessage.info('请至少勾选一项'); return }
  dlPacking.value = true
  try {
    const res = await http.post('/purchase/package', {
      project_id: dlRow.value.project_id,
      sheet_ids: dlSelSheets.value,
      attachment_ids: dlSelAtts.value,
    }, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a'); a.href = url
    a.download = `${dlRow.value.code}_采购资料.zip`; a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已打包下载')
  } catch {
    ElMessage.error('打包下载失败')
  } finally { dlPacking.value = false }
}

// ===== items tab state =====
const items = ref<PurchaseItemOut[]>([])
const itemSummary = ref<ItemSummary | null>(null)
const selectedItems = ref<PurchaseItemOut[]>([])
const filterSupplierId = ref<number | ''>('')
const filterProjectCode = ref('')
const filterMonth = ref('')
const filterInvoiceStatus = ref('')
const filterCategory = ref('')   // 🆕 按供应商分类筛选

// suppliers & statements
const suppliers = ref<SupplierOut[]>([])
const statementData = ref<StatementList | null>(null)
const drawerVisible = ref(false)
const drawerSupplier = ref<SupplierStatementRow | null>(null)
const drawerItems = ref<PurchaseItemOut[]>([])
const drawerLoading = ref(false)

// 🆕 按月「合计开票」汇总：按到货日期分月，看某月 收货合计 / 已开票 / 未开票
const drawerMonthly = computed(() => {
  const map = new Map<string, { month: string; received: number; invoiced: number; uninvoiced: number; count: number }>()
  for (const it of drawerItems.value) {
    const m = (it.arrival_date || '').slice(0, 7) || '未收货'
    let g = map.get(m)
    if (!g) { g = { month: m, received: 0, invoiced: 0, uninvoiced: 0, count: 0 }; map.set(m, g) }
    g.received += it.received_amount || 0
    if (it.invoice_status === '已开票') g.invoiced += (it.invoice_amount || it.received_amount || 0)
    else g.uninvoiced += it.received_amount || 0
    g.count++
  }
  return Array.from(map.values()).sort((a, b) => (a.month < b.month ? 1 : -1))
})

// 🆕 供应商分类：默认分类 ∪ 已有供应商里出现过的分类（自定义分类下次下拉仍在）
const DEFAULT_SUP_CATEGORIES = ['外协', '标准件', '不锈钢', '激光', '电气', '运输']
const categoryOptions = computed(() => {
  const set = new Set<string>(DEFAULT_SUP_CATEGORIES)
  for (const s of suppliers.value) { if (s.category) set.add(s.category) }
  return Array.from(set)
})
// 🆕 供应商账目筛选（名称 / 分类）
const stmtNameFilter = ref('')
const stmtCatFilter = ref('')
const filteredStatementRows = computed(() => {
  const rows = statementData.value?.rows || []
  const kw = stmtNameFilter.value.trim().toLowerCase()
  const cat = stmtCatFilter.value
  return rows.filter(r =>
    (!kw || r.supplier_name.toLowerCase().includes(kw)) &&
    (!cat || (r.category || '') === cat)
  )
})

// reports
const kpi = ref<PurchaseKPI | null>(null)
const monthlyTrend = ref<MonthlyPoint[]>([])
const byBuyer = ref<BuyerRow[]>([])
const byProject = ref<{ project_code: string; amount: number; count: number }[]>([])
const topSuppliers = ref<TopSupplier[]>([])
const projectSearch = ref('')

// dialogs
const itemDialogVisible = ref(false)
const editingItem = ref<PurchaseItemOut | null>(null)
const itemForm = reactive({
  supplier_id: '' as number | '',
  delivery_date: '', contract_no: '', project_code: '', delivery_note_no: '',
  item_name: '', spec: '', qty: null as number | null, unit_price: null as number | null,
  received_amount: 0, invoice_date: '', tax_rate: '', invoice_amount: 0,
  payment_method: '', invoice_status: '待对账', notes: '',
})

// 🆕 采购单：同一供应商 + 多个零件行（表头共享，单价选填以支持「先填/后填价格」两种流程）
interface OrderLine {
  item_name: string; spec: string; project_code: string
  qty: number | null; unit_price: number | null; received_amount: number | null
  tax_rate: string; notes: string
}
function blankLine(): OrderLine {
  return { item_name: '', spec: '', project_code: '', qty: null, unit_price: null, received_amount: null, tax_rate: '', notes: '' }
}
const orderDialogVisible = ref(false)
const orderSaving = ref(false)
const orderForm = reactive({
  supplier_id: '' as number | '',
  delivery_date: '', contract_no: '', project_code: '', payment_method: '',
  lines: [blankLine()] as OrderLine[],
})
// 采购商抬头（打印采购单用；如公司全称有出入，改这里即可）
const PO_COMPANY = '同辉智能装备有限公司'
const orderTotal = computed(() =>
  orderForm.lines.reduce((s, l) => s + (l.received_amount ?? ((l.qty || 0) * (l.unit_price || 0))), 0))

function openNewOrder() {
  Object.assign(orderForm, {
    supplier_id: '', delivery_date: new Date().toISOString().slice(0, 10),
    contract_no: '', project_code: '', payment_method: '', lines: [blankLine()],
  })
  orderDialogVisible.value = true
}
function addOrderLine() { orderForm.lines.push(blankLine()) }
function removeOrderLine(idx: number) {
  orderForm.lines.splice(idx, 1)
  if (!orderForm.lines.length) orderForm.lines.push(blankLine())
}
// 数量/单价变动 → 自动带出收货金额（仍可手改，后填价格流程只填金额也行）
function onLineCalc(l: OrderLine) {
  if (l.qty != null && l.unit_price != null) l.received_amount = Number((l.qty * l.unit_price).toFixed(2))
}

async function saveOrder() {
  if (!orderForm.supplier_id) { ElMessage.error('请选择供应商'); return }
  const lines = orderForm.lines.filter(l => l.item_name.trim())
  if (!lines.length) { ElMessage.error('请至少填写一行零件（名称必填）'); return }
  orderSaving.value = true
  try {
    await http.post('/purchase-mgmt/orders', {
      supplier_id: orderForm.supplier_id,
      delivery_date: orderForm.delivery_date || null,
      contract_no: orderForm.contract_no || null,
      project_code: orderForm.project_code || null,
      payment_method: orderForm.payment_method || null,
      lines: lines.map(l => ({
        item_name: l.item_name.trim(),
        spec: l.spec || null,
        project_code: l.project_code || null,
        qty: l.qty,
        unit_price: l.unit_price,
        received_amount: l.received_amount,
        tax_rate: l.tax_rate || null,
        notes: l.notes || null,
      })),
    })
    ElMessage.success(`采购单已保存（${lines.length} 个零件行）`)
    orderDialogVisible.value = false
    await loadItems()
  } catch { /* handled */ } finally { orderSaving.value = false }
}

// 打印采购单：把当前表单渲染成可打印的采购单（浏览器打印 → 可另存为 PDF 发供应商）
function printPurchaseOrder() {
  if (!orderForm.supplier_id) { ElMessage.warning('请先选择供应商'); return }
  const sup = suppliers.value.find(s => s.id === orderForm.supplier_id)
  const lines = orderForm.lines.filter(l => l.item_name.trim())
  if (!lines.length) { ElMessage.warning('请至少填写一行零件'); return }
  const esc = (v: unknown) => String(v ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string))
  const rows = lines.map((l, i) => `<tr>
    <td class="c">${i + 1}</td>
    <td class="c">${esc(l.project_code || orderForm.project_code)}</td>
    <td>${esc(l.item_name)}</td><td>${esc(l.spec)}</td>
    <td class="r">${l.qty ?? ''}</td><td class="r">${l.unit_price ?? ''}</td>
    <td class="r">${l.received_amount ?? (l.qty != null && l.unit_price != null ? (l.qty * l.unit_price).toFixed(2) : '')}</td>
    <td>${esc(l.notes)}</td></tr>`).join('')
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>采购单</title>
    <style>@page{size:A4;margin:16mm}body{font-family:'Microsoft YaHei',SimSun,sans-serif;color:#111;font-size:13px}
    h1{text-align:center;font-size:22px;margin:0 0 12px;letter-spacing:2px}
    table{width:100%;border-collapse:collapse}
    table.meta td{border:1px solid #333;padding:7px 10px}
    table.items{margin-top:-1px}
    table.items th,table.items td{border:1px solid #333;padding:6px 8px}
    table.items th{background:#e8f5ee}.r{text-align:right}.c{text-align:center}
    .foot{margin-top:22px;display:flex;justify-content:space-between}.sign{margin-top:36px}</style></head>
    <body>
    <h1>采购单</h1>
    <table class="meta">
      <tr><td style="width:14%"><b>需方</b></td><td style="width:40%">${esc(PO_COMPANY)}</td>
          <td style="width:14%"><b>下单日期</b></td><td>${esc(orderForm.delivery_date)}</td></tr>
      <tr><td><b>供方</b></td><td>${esc(sup?.name || '')}</td>
          <td><b>付款方式</b></td><td>${esc(orderForm.payment_method || '')}</td></tr>
    </table>
    <table class="items"><thead><tr>
      <th style="width:40px">序号</th><th style="width:100px">订单编号</th><th>名称</th><th>规格型号</th>
      <th style="width:60px">数量</th><th style="width:82px">单价</th><th style="width:104px">合计金额</th><th style="width:120px">备注</th>
    </tr></thead><tbody>${rows}
      <tr><td class="c" colspan="6"><b>合计</b></td>
      <td class="r"><b>￥${orderTotal.value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</b></td><td></td></tr>
    </tbody></table>
    <div class="foot"><div class="sign">采购（签字）：____________</div><div class="sign">供方（盖章）：____________</div></div>
    </body></html>`
  const w = window.open('', '_blank', 'width=900,height=700')
  if (!w) { ElMessage.warning('请允许弹窗以打印采购单'); return }
  w.document.write(html); w.document.close()
  w.onload = () => { w.focus(); w.print() }
}

// 🆕 采购历史数据导入：下载模板 / 上传导入
async function downloadImportTemplate() {
  try {
    const res = await http.get('/purchase-mgmt/items/import-template', { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url; a.download = '采购明细导入模板.xlsx'
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { ElMessage.error('模板下载失败') }
}
const importing = ref(false)
function importItems() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.xlsx,.xls'
  input.onchange = async () => {
    const f = input.files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    importing.value = true
    try {
      const r = await http.post<{ created: number; suppliers_created: number; failed: number; errors: string[] }>(
        '/purchase-mgmt/items/import', fd)
      const d = r.data
      const parts = [`成功导入 ${d.created} 条`]
      if (d.suppliers_created) parts.push(`新建供应商 ${d.suppliers_created} 个`)
      if (d.failed) parts.push(`跳过 ${d.failed} 条`)
      if (d.failed && d.errors?.length) {
        ElMessageBox.alert(d.errors.join('\n'), `导入完成：${parts.join('，')}`, { type: 'warning' })
      } else {
        ElMessage.success(parts.join('，'))
      }
      await Promise.all([loadItems(), loadSuppliers(), loadStatements()])
    } catch { /* handled */ } finally { importing.value = false }
  }
  input.click()
}

const supplierDialogVisible = ref(false)
const editingSupplier = ref<SupplierOut | null>(null)
const supplierForm = reactive({
  name: '', code: '', category: '', contact: '', phone: '', address: '',
  tax_no: '', bank_name: '', bank_account: '', settlement_type: '', credit_days: null as number | null,
  notes: '',
})

const payReqVisible = ref(false)
const payReqForm = reactive({
  supplier_id: '' as number | '',
  requested_amount: 0,
  notes: '',
  items: [] as Array<{ item_id: number; item_name: string; allocated_amount: number }>,
})

const openingBalanceVisible = ref(false)
const openingBalanceSupplierId = ref<number | null>(null)
const openingBalanceSupplierName = ref('')
const openingBalanceForm = reactive({ balance_date: '', outstanding_amount: 0, notes: '' })

// ===== loaders =====
async function loadSuppliers() {
  const r = await http.get<SupplierOut[]>('/purchase-mgmt/suppliers')
  suppliers.value = r.data
}

async function loadItems() {
  loading.value = true
  try {
    const params: Record<string, string> = {}
    if (filterSupplierId.value) params.supplier_id = String(filterSupplierId.value)
    if (filterProjectCode.value) params.project_code = filterProjectCode.value
    if (filterMonth.value) params.month = filterMonth.value
    if (filterInvoiceStatus.value) params.invoice_status = filterInvoiceStatus.value
    if (filterCategory.value) params.category = filterCategory.value
    const [ir, sr] = await Promise.all([
      http.get<PurchaseItemOut[]>('/purchase-mgmt/items', { params }),
      http.get<ItemSummary>('/purchase-mgmt/items/summary', { params }),
    ])
    items.value = ir.data
    itemSummary.value = sr.data
  } finally { loading.value = false }
}

async function loadStatements() {
  loading.value = true
  try {
    const r = await http.get<StatementList>('/purchase-mgmt/statements')
    statementData.value = r.data
  } finally { loading.value = false }
}

async function loadReports() {
  loading.value = true
  try {
    const [k, t, b, ts] = await Promise.all([
      http.get<PurchaseKPI>('/purchase-mgmt/reports/overview'),
      http.get<MonthlyPoint[]>('/purchase-mgmt/reports/monthly-trend'),
      http.get<BuyerRow[]>('/purchase-mgmt/reports/by-buyer'),
      http.get<TopSupplier[]>('/purchase-mgmt/reports/top-suppliers'),
    ])
    kpi.value = k.data
    monthlyTrend.value = t.data
    byBuyer.value = b.data
    topSuppliers.value = ts.data
  } finally { loading.value = false }
}

async function loadProjectReport() {
  const r = await http.get<{ project_code: string; amount: number; count: number }[]>(
    '/purchase-mgmt/reports/by-project',
    { params: projectSearch.value ? { q: projectSearch.value } : {} }
  )
  byProject.value = r.data
}

onMounted(async () => {
  await loadSuppliers()
  if (showPurchaseTab.value) {
    await loadPurchaseRows()
  } else {
    await loadItems()
  }
})

async function onTabChange(name: string) {
  if (name === 'purchase') await loadPurchaseRows()
  else if (name === 'items') await loadItems()
  else if (name === 'statements') { await loadSuppliers(); await loadStatements() }
  else if (name === 'reports' && isLeadOrAbove.value) { await loadReports(); await loadProjectReport() }
}

// ===== item CRUD =====
function openNewItem() {
  editingItem.value = null
  Object.assign(itemForm, {
    supplier_id: '', delivery_date: '', contract_no: '', project_code: '',
    delivery_note_no: '', item_name: '', spec: '', qty: null, unit_price: null,
    received_amount: 0, invoice_date: '', tax_rate: '', invoice_amount: 0,
    payment_method: '', invoice_status: '待对账', notes: '',
  })
  itemDialogVisible.value = true
}

function openEditItem(row: PurchaseItemOut) {
  editingItem.value = row
  Object.assign(itemForm, {
    supplier_id: row.supplier_id, delivery_date: row.delivery_date || '',
    contract_no: row.contract_no || '', project_code: row.project_code || '',
    delivery_note_no: row.delivery_note_no || '', item_name: row.item_name,
    spec: row.spec || '', qty: row.qty, unit_price: row.unit_price,
    received_amount: row.received_amount, invoice_date: row.invoice_date || '',
    tax_rate: row.tax_rate || '', invoice_amount: row.invoice_amount,
    payment_method: row.payment_method || '',
    invoice_status: row.invoice_status, notes: row.notes || '',
  })
  itemDialogVisible.value = true
}

async function saveItem() {
  if (!itemForm.item_name || !itemForm.supplier_id) {
    ElMessage.error('名称和供应商为必填项')
    return
  }
  try {
    const payload = {
      supplier_id: itemForm.supplier_id,
      delivery_date: itemForm.delivery_date || null,
      contract_no: itemForm.contract_no || null,
      project_code: itemForm.project_code || null,
      delivery_note_no: itemForm.delivery_note_no || null,
      item_name: itemForm.item_name,
      spec: itemForm.spec || null,
      qty: itemForm.qty,
      unit_price: itemForm.unit_price,
      received_amount: itemForm.received_amount,
      invoice_date: itemForm.invoice_date || null,
      tax_rate: itemForm.tax_rate || null,
      invoice_amount: itemForm.invoice_amount,
      payment_method: itemForm.payment_method || null,
      invoice_status: itemForm.invoice_status,
      notes: itemForm.notes || null,
    }
    if (editingItem.value) {
      await http.put(`/purchase-mgmt/items/${editingItem.value.id}`, payload)
      ElMessage.success('已更新')
    } else {
      await http.post('/purchase-mgmt/items', payload)
      ElMessage.success('已新增')
    }
    itemDialogVisible.value = false
    await loadItems()
  } catch { /* handled by axios interceptor */ }
}

async function deleteItem(row: PurchaseItemOut) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.item_name}」？`, '删除明细', { type: 'warning' })
  } catch { return }
  try {
    await http.delete(`/purchase-mgmt/items/${row.id}`)
    ElMessage.success('已删除')
    await loadItems()
  } catch { /* handled */ }
}

// ===== payment request =====
function openPaymentRequest() {
  if (!selectedItems.value.length) { ElMessage.warning('请先勾选明细行'); return }
  const firstSid = selectedItems.value[0].supplier_id
  if (!selectedItems.value.every(i => i.supplier_id === firstSid)) {
    ElMessage.error('请款单只能关联同一供应商的明细')
    return
  }
  payReqForm.supplier_id = firstSid
  payReqForm.requested_amount = selectedItems.value.reduce((s, i) => s + (i.received_amount - i.paid_amount), 0)
  payReqForm.notes = ''
  payReqForm.items = selectedItems.value.map(i => ({
    item_id: i.id,
    item_name: i.item_name,
    allocated_amount: i.received_amount - i.paid_amount,
  }))
  payReqVisible.value = true
}

async function submitPaymentRequest() {
  if (!payReqForm.requested_amount || payReqForm.requested_amount <= 0) {
    ElMessage.error('请款金额必须大于0')
    return
  }
  try {
    await http.post('/purchase-mgmt/payment-requests', {
      supplier_id: payReqForm.supplier_id,
      requested_amount: payReqForm.requested_amount,
      notes: payReqForm.notes || null,
      items: payReqForm.items.map(i => ({ item_id: i.item_id, allocated_amount: i.allocated_amount })),
    })
    ElMessage.success('请款单已提交，等待财务审批')
    payReqVisible.value = false
    selectedItems.value = []
  } catch { /* handled */ }
}

// ===== supplier CRUD =====
function openNewSupplier() {
  editingSupplier.value = null
  Object.assign(supplierForm, {
    name: '', code: '', category: '', contact: '', phone: '', address: '',
    tax_no: '', bank_name: '', bank_account: '', settlement_type: '', credit_days: null, notes: '',
  })
  supplierDialogVisible.value = true
}

function openEditSupplier(s: SupplierOut) {
  editingSupplier.value = s
  Object.assign(supplierForm, {
    name: s.name, code: s.code || '', category: s.category || '',
    contact: s.contact || '', phone: s.phone || '', address: s.address || '',
    tax_no: s.tax_no || '', bank_name: s.bank_name || '', bank_account: s.bank_account || '',
    settlement_type: s.settlement_type || '', credit_days: s.credit_days,
    notes: s.notes || '',
  })
  supplierDialogVisible.value = true
}

async function saveSupplier() {
  if (!supplierForm.name) { ElMessage.error('供应商名称为必填项'); return }
  try {
    const payload = {
      name: supplierForm.name,
      code: supplierForm.code || null,
      category: supplierForm.category || null,
      contact: supplierForm.contact || null,
      phone: supplierForm.phone || null,
      address: supplierForm.address || null,
      tax_no: supplierForm.tax_no || null,
      bank_name: supplierForm.bank_name || null,
      bank_account: supplierForm.bank_account || null,
      settlement_type: supplierForm.settlement_type || null,
      credit_days: supplierForm.credit_days,
      notes: supplierForm.notes || null,
    }
    if (editingSupplier.value) {
      await http.put(`/purchase-mgmt/suppliers/${editingSupplier.value.id}`, payload)
      ElMessage.success('供应商已更新')
    } else {
      await http.post('/purchase-mgmt/suppliers', payload)
      ElMessage.success('供应商已新增')
    }
    supplierDialogVisible.value = false
    await Promise.all([loadSuppliers(), loadStatements()])
  } catch { /* handled */ }
}

async function toggleSupplier(row: SupplierStatementRow) {
  const s = suppliers.value.find(x => x.id === row.supplier_id)
  const cur = s?.status || 'active'
  const action = cur === 'active' ? '停用' : '启用'
  try {
    await ElMessageBox.confirm(`确认${action}供应商「${row.supplier_name}」？`, action, { type: 'warning' })
  } catch { return }
  try {
    await http.put(`/purchase-mgmt/suppliers/${row.supplier_id}/toggle`)
    ElMessage.success(`已${action}`)
    await loadSuppliers()
  } catch { /* handled */ }
}

// 🆕 删除供应商（后端仅允许无采购明细/请款记录的供应商硬删除，否则提示改用停用）
async function deleteSupplier(row: { supplier_id: number; supplier_name: string }): Promise<boolean> {
  try {
    await ElMessageBox.confirm(
      `确认删除供应商「${row.supplier_name}」？删除后不可恢复。\n（有采购明细/请款记录的供应商不能删除，请改用「停用」）`,
      '删除供应商', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch { return false }
  try {
    await http.delete(`/purchase-mgmt/suppliers/${row.supplier_id}`)
    ElMessage.success('供应商已删除')
    await Promise.all([loadSuppliers(), loadStatements()])
    return true
  } catch { return false /* 拦截器已提示（含「不能删除」原因） */ }
}

// 🆕 从「编辑供应商」弹窗删除：成功后关闭弹窗
async function deleteEditingSupplier() {
  if (!editingSupplier.value) return
  const ok = await deleteSupplier({ supplier_id: editingSupplier.value.id, supplier_name: editingSupplier.value.name })
  if (ok) supplierDialogVisible.value = false
}

// ===== opening balance =====
function openOpeningBalance(row: SupplierStatementRow) {
  openingBalanceSupplierId.value = row.supplier_id
  openingBalanceSupplierName.value = row.supplier_name
  openingBalanceForm.balance_date = ''
  openingBalanceForm.outstanding_amount = row.opening_balance
  openingBalanceForm.notes = ''
  openingBalanceVisible.value = true
  http.get<{ balance_date: string; outstanding_amount: number; notes?: string }>(
    `/purchase-mgmt/suppliers/${row.supplier_id}/opening-balance`
  ).then(r => {
    if (r.data) {
      openingBalanceForm.balance_date = r.data.balance_date || ''
      openingBalanceForm.outstanding_amount = r.data.outstanding_amount
      openingBalanceForm.notes = r.data.notes || ''
    }
  }).catch(() => {})
}

async function saveOpeningBalance() {
  if (!openingBalanceForm.balance_date) { ElMessage.error('截止日期为必填项'); return }
  try {
    await http.post(`/purchase-mgmt/suppliers/${openingBalanceSupplierId.value}/opening-balance`, {
      balance_date: openingBalanceForm.balance_date,
      outstanding_amount: openingBalanceForm.outstanding_amount,
      notes: openingBalanceForm.notes || null,
    })
    ElMessage.success('期初余额已保存')
    openingBalanceVisible.value = false
    await loadStatements()
  } catch { /* handled */ }
}

// ===== statement drawer =====
async function openDrawer(row: SupplierStatementRow) {
  drawerSupplier.value = row
  drawerVisible.value = true
  drawerLoading.value = true
  try {
    const r = await http.get<PurchaseItemOut[]>(`/purchase-mgmt/statements/${row.supplier_id}/detail`)
    drawerItems.value = r.data
  } finally { drawerLoading.value = false }
}

// ===== helpers =====
function statusTag(s: string) {
  if (s === '已开票') return 'success'
  if (s === '已对账') return 'warning'
  return 'info'
}

function prStatusTag(s: string) {
  if (s === 'paid') return 'success'
  if (s === 'approved') return 'warning'
  if (s === 'rejected') return 'danger'
  return 'info'
}
const PR_STATUS_LABEL: Record<string, string> = { pending: '待审', approved: '已批', rejected: '已驳', paid: '已付' }
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购管理</h1>
        <div class="desc">采购部项目一览 · 采购明细录入 · 供应商账目 · 请款流程 · 汇总报表</div>
      </div>
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-tabs v-model="tab" @tab-change="onTabChange">

        <!-- ==================== Tab 0: 采购部 ==================== -->
        <el-tab-pane v-if="showPurchaseTab" label="🗂️ 采购部" name="purchase">
          <!-- 筛选栏 -->
          <div class="filter-bar">
            <el-select v-model="pYearFilter" style="width:100px" @change="loadPurchaseRows">
              <el-option v-for="y in pYearOptions" :key="y" :label="y + '年'" :value="y" />
            </el-select>
            <el-select v-model="pProjStatusFilter" style="width:100px" @change="loadPurchaseRows">
              <el-option label="进行中" value="进行中" />
              <el-option label="已完成" value="已完成" />
              <el-option label="全部" value="" />
            </el-select>
            <el-button :icon="Refresh" :loading="purchaseLoading" @click="loadPurchaseRows">刷新</el-button>
          </div>

          <el-table :data="purchaseRows" stripe v-loading="purchaseLoading" max-height="calc(100vh - 310px)" :scrollbar-always-on="true">
            <el-table-column type="index" label="#" width="50" fixed />
            <el-table-column label="项目编号" width="116" fixed>
              <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
            </el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="170" show-overflow-tooltip />
            <el-table-column v-if="showDesigner" label="设计师" min-width="90" align="center">
              <template #default="{ row }">{{ row.designer || '—' }}</template>
            </el-table-column>

            <el-table-column v-if="showSheetmetal" label="钣金装配表" min-width="100" align="center">
              <template #default="{ row }">
                <el-button v-if="row.sheetmetal_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.sheetmetal_sheet_id, `${row.code} · 钣金装配表`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showStandardSheet" label="标准件清单" min-width="100" align="center">
              <template #default="{ row }">
                <el-button v-if="row.standard_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.standard_sheet_id, `${row.code} · 标准件清单`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showOutsource" label="外协加工表" min-width="100" align="center">
              <template #default="{ row }">
                <el-button v-if="row.outsource_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.outsource_sheet_id, `${row.code} · 外协加工表`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showMaterial" label="不锈钢原料下料单" min-width="120" align="center">
              <template #default="{ row }">
                <el-button v-if="row.material_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.material_sheet_id, `${row.code} · 不锈钢原料下料单`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showLaser" label="激光件清单" min-width="100" align="center">
              <template #default="{ row }">
                <el-button v-if="row.laser_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.laser_sheet_id, `${row.code} · 激光件清单`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showElecPo" label="电工采购单" min-width="100" align="center">
              <template #default="{ row }">
                <el-button v-if="row.elec_po_sheet_id" size="small" link type="primary"
                           @click="openPreview(row.elec_po_sheet_id, `${row.code} · 电工采购单`)">
                  <el-icon><View /></el-icon>预览
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>

            <el-table-column v-if="showCadLaser" label="CAD激光图纸" min-width="120" align="center">
              <template #default="{ row }">
                <el-tooltip v-if="row.cad_laser_files.length" placement="top">
                  <template #content>
                    <div v-for="f in row.cad_laser_files" :key="f.id" class="tip-line">{{ f.name }}</div>
                  </template>
                  <el-tag size="small" type="success" effect="light" round>已推送 {{ row.cad_laser_files.length }}</el-tag>
                </el-tooltip>
                <span v-else class="muted">待推送</span>
              </template>
            </el-table-column>
            <el-table-column v-if="showOutImg" label="外购附图" min-width="116" align="center">
              <template #default="{ row }">
                <el-tooltip v-if="row.outsource_img_files.length" placement="top">
                  <template #content>
                    <div v-for="f in row.outsource_img_files" :key="f.id" class="tip-line">{{ f.name }}</div>
                  </template>
                  <el-tag size="small" type="success" effect="light" round>已推送 {{ row.outsource_img_files.length }}</el-tag>
                </el-tooltip>
                <span v-else class="muted">待推送</span>
              </template>
            </el-table-column>

            <el-table-column label="下载" width="84" fixed="right" align="center">
              <template #default="{ row }">
                <el-button size="small" link type="primary" :icon="Download" @click="openDownload(row)">打包</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!purchaseLoading && !purchaseRows.length" text="暂无项目" />
        </el-tab-pane>

        <!-- ==================== Tab 1: 采购明细 ==================== -->
        <el-tab-pane label="📦 采购明细" name="items">
          <div class="filter-bar">
            <el-select v-model="filterSupplierId" placeholder="全部供应商" clearable style="width:160px" @change="loadItems">
              <el-option v-for="s in suppliers" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
            <el-input v-model="filterProjectCode" placeholder="项目编号" clearable style="width:130px" @change="loadItems" />
            <el-date-picker v-model="filterMonth" type="month" placeholder="下单月份" value-format="YYYY-MM"
                            clearable style="width:140px" @change="loadItems" />
            <el-select v-model="filterInvoiceStatus" placeholder="对账状态" clearable style="width:120px" @change="loadItems">
              <el-option label="待对账" value="待对账" />
              <el-option label="已对账" value="已对账" />
              <el-option label="已开票" value="已开票" />
            </el-select>
            <el-select v-model="filterCategory" placeholder="供应商分类" clearable style="width:130px" @change="loadItems">
              <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
            </el-select>
            <el-button @click="loadItems">刷新</el-button>
            <template v-if="canWrite">
              <el-button type="primary" @click="openNewOrder">+ 新建采购单</el-button>
              <el-button @click="openNewItem">+ 单条明细</el-button>
              <el-button :disabled="!selectedItems.length" type="warning" @click="openPaymentRequest">发起请款</el-button>
              <el-button :icon="Upload" :loading="importing" @click="importItems">导入历史</el-button>
              <el-button :icon="Download" link type="primary" @click="downloadImportTemplate">下载模板</el-button>
            </template>
          </div>

          <el-table
            :data="items" stripe
            @selection-change="(v: PurchaseItemOut[]) => selectedItems = v"
            max-height="calc(100vh - 340px)"
            :scrollbar-always-on="true"
            show-overflow-tooltip
          >
            <el-table-column v-if="canWrite" type="selection" width="40" />
            <el-table-column prop="po_no" label="采购单号" width="128">
              <template #default="{ row }"><span class="po-no">{{ row.po_no || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="供应商" min-width="110">
              <template #default="{ row }"><span class="sup-name">{{ row.supplier_name }}</span></template>
            </el-table-column>
            <el-table-column prop="delivery_date" label="下单日期" width="95" />
            <el-table-column prop="project_code" label="项目编号" width="100">
              <template #default="{ row }"><b class="code">{{ row.project_code || '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="delivery_note_no" label="送货单号" width="100">
              <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
            </el-table-column>
            <el-table-column prop="arrival_date" label="到货日期" width="95">
              <template #default="{ row }">
                <span v-if="row.arrival_date">{{ row.arrival_date }}</span>
                <el-tag v-else size="small" type="info" effect="plain">待收货</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="item_name" label="名称" min-width="120" />
            <el-table-column prop="spec" label="规格" min-width="100">
              <template #default="{ row }">{{ row.spec || '—' }}</template>
            </el-table-column>
            <el-table-column label="数量" width="70" align="right">
              <template #default="{ row }">{{ row.qty ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="单价" width="90" align="right">
              <template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template>
            </el-table-column>
            <el-table-column label="收货金额" width="100" align="right">
              <template #default="{ row }"><b>{{ fmtMoney(row.received_amount) }}</b></template>
            </el-table-column>
            <el-table-column prop="invoice_date" label="开票日期" width="95">
              <template #default="{ row }">{{ row.invoice_date || '—' }}</template>
            </el-table-column>
            <el-table-column label="开票金额" width="100" align="right">
              <template #default="{ row }">{{ row.invoice_amount ? fmtMoney(row.invoice_amount) : '—' }}</template>
            </el-table-column>
            <el-table-column label="已付款" width="100" align="right">
              <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="statusTag(row.invoice_status)" size="small">{{ row.invoice_status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="isLeadOrAbove" prop="buyer_name" label="采购员" width="80">
              <template #default="{ row }">{{ row.buyer_name || '—' }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="120" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openEditItem(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="deleteItem(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div v-if="itemSummary" class="summary-bar">
            <span>共 <b>{{ itemSummary.count }}</b> 条</span>
            <span>收货合计 <b class="amt">{{ fmtMoney(itemSummary.received_total) }}</b></span>
            <span>待开票 <b class="amt warn">{{ fmtMoney(itemSummary.uninvoiced) }}</b></span>
            <span>已付款 <b class="amt">{{ fmtMoney(itemSummary.paid_total) }}</b></span>
            <span>欠款 <b class="amt danger">{{ fmtMoney(itemSummary.outstanding) }}</b></span>
          </div>
        </el-tab-pane>

        <!-- ==================== Tab 2: 供应商账目一览 ==================== -->
        <el-tab-pane label="📊 供应商账目" name="statements">
          <div class="filter-bar">
            <el-input v-model="stmtNameFilter" placeholder="搜索供应商名称" clearable style="width:200px" />
            <el-select v-model="stmtCatFilter" placeholder="全部分类" clearable style="width:140px">
              <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
            </el-select>
            <el-button v-if="canWrite" type="primary" @click="openNewSupplier">+ 新增供应商</el-button>
          </div>
          <el-table
            :data="filteredStatementRows"
            stripe show-summary
            :summary-method="() => {
              const rows = filteredStatementRows
              const sum = (k: keyof SupplierStatementRow) => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0)
              return ['合计', '', '',
                fmtMoney(sum('opening_balance')),
                fmtMoney(sum('received_total')), '','',
                fmtMoney(sum('paid_total')),
                fmtMoney(sum('outstanding')), '', '', '']
            }"
            max-height="calc(100vh - 280px)"
            :scrollbar-always-on="true"
          >
            <el-table-column prop="supplier_name" label="供应商" min-width="200" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="80" show-overflow-tooltip>
              <template #default="{ row }">{{ row.category || '—' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="76">
              <template #default="{ row }">
                <el-tag :type="suppliers.find(s=>s.id===row.supplier_id)?.status==='active'?'success':'info'" size="small">
                  {{ suppliers.find(s=>s.id===row.supplier_id)?.status==='active' ? '启用' : '停用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="期初欠款" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }">{{ fmtMoney(row.opening_balance) }}</template>
            </el-table-column>
            <el-table-column label="收货合计" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }"><b>{{ fmtMoney(row.received_total) }}</b></template>
            </el-table-column>
            <el-table-column label="开票合计" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }">{{ fmtMoney(row.invoice_total) }}</template>
            </el-table-column>
            <el-table-column label="待开票" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }"><span class="warn">{{ fmtMoney(row.uninvoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="已付款" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }">{{ fmtMoney(row.paid_total) }}</template>
            </el-table-column>
            <el-table-column label="欠款余额" width="100" align="right" show-overflow-tooltip>
              <template #default="{ row }"><b class="danger">{{ fmtMoney(row.outstanding) }}</b></template>
            </el-table-column>
            <el-table-column label="明细数" width="68" align="center">
              <template #default="{ row }">{{ row.item_count }}</template>
            </el-table-column>
            <el-table-column label="操作" width="336" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openDrawer(row)">查看明细</el-button>
                <template v-if="canWrite">
                  <el-button size="small" link @click="openEditSupplier(suppliers.find(s=>s.id===row.supplier_id)!)">编辑</el-button>
                  <el-button size="small" link @click="openOpeningBalance(row)">期初余额</el-button>
                  <el-button size="small" link type="warning" @click="toggleSupplier(row)">
                    {{ suppliers.find(s=>s.id===row.supplier_id)?.status==='active' ? '停用' : '启用' }}
                  </el-button>
                  <el-button size="small" link type="danger" @click="deleteSupplier(row)">删除</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Tab 3: 汇总报表 ==================== -->
        <el-tab-pane v-if="isLeadOrAbove" label="📈 汇总报表" name="reports">
          <div v-if="kpi" class="kpi-row">
            <div class="kpi-card">
              <div class="kpi-label">本月采购额</div>
              <div class="kpi-value">{{ fmtMoney(kpi.month_amount) }}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">本季采购额</div>
              <div class="kpi-value">{{ fmtMoney(kpi.quarter_amount) }}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">本年采购额</div>
              <div class="kpi-value">{{ fmtMoney(kpi.year_amount) }}</div>
            </div>
            <div class="kpi-card danger-card">
              <div class="kpi-label">应付总额</div>
              <div class="kpi-value danger">{{ fmtMoney(kpi.total_outstanding) }}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">待审请款</div>
              <div class="kpi-value">{{ kpi.pending_requests }} 单</div>
            </div>
          </div>

          <el-row :gutter="16">
            <el-col :span="14">
              <div class="report-section">
                <div class="section-title">月度趋势（近12个月）</div>
                <el-table :data="monthlyTrend" stripe size="small" max-height="320">
                  <el-table-column prop="month" label="月份" width="90" />
                  <el-table-column label="收货金额" align="right">
                    <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
                  </el-table-column>
                  <el-table-column label="已付款" align="right">
                    <template #default="{ row }">{{ fmtMoney(row.paid) }}</template>
                  </el-table-column>
                  <el-table-column label="欠款" align="right">
                    <template #default="{ row }"><span class="danger">{{ fmtMoney(row.amount - row.paid) }}</span></template>
                  </el-table-column>
                </el-table>
              </div>
            </el-col>
            <el-col :span="10">
              <div class="report-section">
                <div class="section-title">Top 供应商</div>
                <el-table :data="topSuppliers" stripe size="small" max-height="320">
                  <el-table-column type="index" width="40" />
                  <el-table-column prop="supplier_name" label="供应商" min-width="100" />
                  <el-table-column label="采购额" align="right">
                    <template #default="{ row }"><b>{{ fmtMoney(row.amount) }}</b></template>
                  </el-table-column>
                </el-table>
              </div>
            </el-col>
          </el-row>

          <el-row :gutter="16" style="margin-top:16px">
            <el-col :span="10">
              <div class="report-section">
                <div class="section-title">按采购员</div>
                <el-table :data="byBuyer" stripe size="small" max-height="260">
                  <el-table-column prop="buyer_name" label="采购员" min-width="90" />
                  <el-table-column label="采购额" align="right">
                    <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
                  </el-table-column>
                  <el-table-column prop="count" label="条数" width="60" align="right" />
                </el-table>
              </div>
            </el-col>
            <el-col :span="14">
              <div class="report-section">
                <div class="section-title">按项目编号</div>
                <div style="margin-bottom:8px;display:flex;gap:8px">
                  <el-input v-model="projectSearch" placeholder="搜索项目编号" clearable style="width:180px" @change="loadProjectReport" />
                  <el-button @click="loadProjectReport">查询</el-button>
                </div>
                <el-table :data="byProject" stripe size="small" max-height="220">
                  <el-table-column label="项目编号" min-width="110">
                    <template #default="{ row }"><b class="code">{{ row.project_code }}</b></template>
                  </el-table-column>
                  <el-table-column label="采购额" align="right">
                    <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
                  </el-table-column>
                  <el-table-column prop="count" label="条数" width="60" align="right" />
                </el-table>
              </div>
            </el-col>
          </el-row>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ==================== 采购单弹窗（同一供应商多个零件行）==================== -->
    <el-dialog v-model="orderDialogVisible" title="新建采购单" width="1080px" top="6vh">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="同一供应商一次录入多个零件：表头（供应商 / 下单日期 / 合同 / 默认项目）在上，零件逐行填。单价「选填」——已谈好价先填；激光板材等到货送货单才带价的，单价留空，货到仓库再补。保存后自动生成采购单号；可「打印采购单」发供应商。" />
      <el-form :model="orderForm" label-position="top" class="order-form">
        <el-row :gutter="20">
          <el-col :span="7">
            <el-form-item label="供应商 *">
              <el-select v-model="orderForm.supplier_id" filterable placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="5">
            <el-form-item label="下单日期">
              <el-date-picker v-model="orderForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="4">
            <el-form-item label="付款方式">
              <el-select v-model="orderForm.payment_method" clearable allow-create filterable default-first-option
                         placeholder="选择/输入" style="width:100%">
                <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="4">
            <el-form-item label="合同编号">
              <el-input v-model="orderForm.contract_no" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :span="4">
            <el-form-item label="默认订单编号">
              <el-input v-model="orderForm.project_code" placeholder="行内可覆盖" />
            </el-form-item>
          </el-col>
        </el-row>

        <div class="order-lines-head">
          <span class="section-title" style="margin:0">零件明细（{{ orderForm.lines.length }} 行）</span>
          <el-button size="small" :icon="Plus" @click="addOrderLine">添加一行</el-button>
        </div>
        <el-table :data="orderForm.lines" size="small" border class="order-lines">
          <el-table-column type="index" label="#" width="44" align="center" />
          <el-table-column label="名称 *" min-width="150">
            <template #default="{ row }"><el-input v-model="row.item_name" placeholder="零件名称" /></template>
          </el-table-column>
          <el-table-column label="规格型号" min-width="150">
            <template #default="{ row }"><el-input v-model="row.spec" placeholder="规格/型号" /></template>
          </el-table-column>
          <el-table-column label="订单编号" width="128">
            <template #default="{ row }"><el-input v-model="row.project_code" :placeholder="orderForm.project_code || '默认'" /></template>
          </el-table-column>
          <el-table-column label="数量" width="110">
            <template #default="{ row }">
              <el-input-number v-model="row.qty" :min="0" :precision="2" :controls="false" style="width:100%" @change="onLineCalc(row)" />
            </template>
          </el-table-column>
          <el-table-column label="单价（选填）" width="120">
            <template #default="{ row }">
              <el-input-number v-model="row.unit_price" :min="0" :precision="4" :controls="false" style="width:100%"
                               placeholder="后填留空" @change="onLineCalc(row)" />
            </template>
          </el-table-column>
          <el-table-column label="合计金额" width="128">
            <template #default="{ row }">
              <el-input-number v-model="row.received_amount" :min="0" :precision="2" :controls="false" style="width:100%" />
            </template>
          </el-table-column>
          <el-table-column label="备注" min-width="130">
            <template #default="{ row }"><el-input v-model="row.notes" placeholder="选填" /></template>
          </el-table-column>
          <el-table-column label="" width="50" align="center">
            <template #default="{ $index }">
              <el-button size="small" link type="danger" :icon="Delete" @click="removeOrderLine($index)" />
            </template>
          </el-table-column>
        </el-table>
        <div class="order-total-bar">
          <span>合计金额 <b class="amt">{{ fmtMoney(orderTotal) }}</b></span>
          <span class="muted">送货单号 / 到货日期由仓库收货时填写</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="orderDialogVisible = false">取消</el-button>
        <el-button :icon="Printer" @click="printPurchaseOrder">打印采购单</el-button>
        <el-button type="primary" :loading="orderSaving" @click="saveOrder">保存采购单</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 采购明细弹窗 ==================== -->
    <el-dialog v-model="itemDialogVisible" :title="editingItem ? '编辑采购明细' : '新增采购明细'" width="860px">
      <el-form :model="itemForm" label-position="top" class="item-form">
        <div class="form-section-title">基本信息</div>
        <el-row :gutter="24">
          <el-col :span="8">
            <el-form-item label="供应商 *">
              <el-select v-model="itemForm.supplier_id" placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="下单日期">
              <el-date-picker v-model="itemForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="项目编号">
              <el-input v-model="itemForm.project_code" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="名称 *">
              <el-input v-model="itemForm.item_name" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="规格型号">
              <el-input v-model="itemForm.spec" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="付款方式">
              <el-select v-model="itemForm.payment_method" clearable allow-create filterable default-first-option
                         placeholder="选择/输入" style="width:100%">
                <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section-title">数量与金额</div>
        <el-row :gutter="24">
          <el-col :span="8">
            <el-form-item label="数量">
              <el-input-number v-model="itemForm.qty" :precision="2" :min="0" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="单价">
              <el-input-number v-model="itemForm.unit_price" :precision="4" :min="0" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="合计金额">
              <el-input-number v-model="itemForm.received_amount" :precision="2" :min="0" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="itemForm.notes" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 开票 / 对账 / 送货单号：仅编辑时显示（新增时精简，货到仓库或需要开票时再补） -->
        <template v-if="editingItem">
          <div class="form-section-title">送货 · 开票与对账</div>
          <el-row :gutter="24">
            <el-col :span="8">
              <el-form-item label="送货单号">
                <el-input v-model="itemForm.delivery_note_no" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="合同编号">
                <el-input v-model="itemForm.contract_no" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="开票日期">
                <el-date-picker v-model="itemForm.invoice_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="税率">
                <el-select v-model="itemForm.tax_rate" style="width:100%" clearable placeholder="请选择">
                  <el-option label="13%" value="13%" />
                  <el-option label="9%" value="9%" />
                  <el-option label="6%" value="6%" />
                  <el-option label="/" value="/" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="开票金额">
                <el-input-number v-model="itemForm.invoice_amount" :precision="2" :min="0" style="width:100%" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="对账状态">
                <el-select v-model="itemForm.invoice_status" style="width:100%">
                  <el-option label="待对账" value="待对账" />
                  <el-option label="已对账" value="已对账" />
                  <el-option label="已开票" value="已开票" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="itemDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveItem">保存</el-button>
      </template>
    </el-dialog>


    <!-- ==================== 请款弹窗 ==================== -->
    <el-dialog v-model="payReqVisible" title="发起请款" width="620px">
      <el-form :model="payReqForm" label-width="100px">
        <el-form-item label="供应商">
          <span>{{ suppliers.find(s => s.id === payReqForm.supplier_id)?.name }}</span>
        </el-form-item>
        <el-form-item label="关联明细">
          <div v-for="it in payReqForm.items" :key="it.item_id" class="pr-item-row">
            <span>{{ it.item_name }}</span>
            <el-input-number v-model="it.allocated_amount" :precision="2" :min="0" size="small" style="width:120px;margin-left:8px" />
          </div>
        </el-form-item>
        <el-form-item label="请款总额">
          <el-input-number v-model="payReqForm.requested_amount" :precision="2" :min="0" style="width:100%" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="payReqForm.notes" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="payReqVisible = false">取消</el-button>
        <el-button type="primary" @click="submitPaymentRequest">提交请款</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 供应商弹窗 ==================== -->
    <el-dialog v-model="supplierDialogVisible" :title="editingSupplier ? '编辑供应商' : '新增供应商'" width="780px">
      <el-form :model="supplierForm" label-position="top" class="supplier-form">
        <div class="form-section-title">基本信息</div>
        <el-row :gutter="24">
          <el-col :span="12">
            <el-form-item label="供应商名称 *">
              <el-input v-model="supplierForm.name" placeholder="请输入供应商名称" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="编码">
              <el-input v-model="supplierForm.code" placeholder="内部编码（可选）" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="分类">
              <el-select v-model="supplierForm.category" style="width:100%" clearable allow-create filterable
                         default-first-option placeholder="选择或输入新分类">
                <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="结算方式">
              <el-select v-model="supplierForm.settlement_type" style="width:100%" clearable placeholder="请选择">
                <el-option label="现金" value="现金" />
                <el-option label="月结" value="月结" />
                <el-option label="无账期" value="无账期" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="账期天数" v-if="supplierForm.settlement_type === '月结'">
              <el-input-number v-model="supplierForm.credit_days" :min="0" :max="365" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section-title">联系方式</div>
        <el-row :gutter="24">
          <el-col :span="8">
            <el-form-item label="联系人">
              <el-input v-model="supplierForm.contact" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="电话">
              <el-input v-model="supplierForm.phone" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="地址">
              <el-input v-model="supplierForm.address" />
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section-title">财务信息</div>
        <el-row :gutter="24">
          <el-col :span="8">
            <el-form-item label="税号">
              <el-input v-model="supplierForm.tax_no" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="开户行">
              <el-input v-model="supplierForm.bank_name" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="银行账号">
              <el-input v-model="supplierForm.bank_account" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="supplierForm.notes" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button v-if="editingSupplier" type="danger" plain style="margin-right:auto" @click="deleteEditingSupplier">删除供应商</el-button>
        <el-button @click="supplierDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSupplier">保存</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 期初余额弹窗 ==================== -->
    <el-dialog v-model="openingBalanceVisible" :title="`录入期初余额 — ${openingBalanceSupplierName}`" width="500px">
      <el-form :model="openingBalanceForm" label-width="100px">
        <el-form-item label="截止日期" required>
          <el-date-picker v-model="openingBalanceForm.balance_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
        <el-form-item label="期初欠款">
          <el-input-number v-model="openingBalanceForm.outstanding_amount" :precision="2" :min="0" style="width:100%" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="openingBalanceForm.notes" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="openingBalanceVisible = false">取消</el-button>
        <el-button type="primary" @click="saveOpeningBalance">保存</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 供应商明细下钻抽屉 ==================== -->
    <el-drawer v-model="drawerVisible" :title="drawerSupplier ? `${drawerSupplier.supplier_name} — 采购明细` : ''" size="75%" direction="rtl">
      <div v-if="drawerSupplier" class="drawer-summary">
        <span>收货合计 <b class="amt">{{ fmtMoney(drawerSupplier.received_total) }}</b></span>
        <span>已付款 <b class="amt">{{ fmtMoney(drawerSupplier.paid_total) }}</b></span>
        <span>欠款 <b class="danger">{{ fmtMoney(drawerSupplier.outstanding) }}</b></span>
      </div>

      <!-- 🆕 按月合计开票：按到货日期分月，未开票/已开票一目了然 -->
      <el-collapse v-if="drawerMonthly.length" class="monthly-collapse">
        <el-collapse-item :title="`按月合计开票汇总（${drawerMonthly.length} 个月）`" name="m">
          <el-table :data="drawerMonthly" size="small" stripe>
            <el-table-column label="月份（按到货日期）" min-width="140">
              <template #default="{ row }"><b>{{ row.month }}</b></template>
            </el-table-column>
            <el-table-column label="收货合计" width="130" align="right">
              <template #default="{ row }">{{ fmtMoney(row.received) }}</template>
            </el-table-column>
            <el-table-column label="已开票" width="130" align="right">
              <template #default="{ row }"><span class="amt">{{ fmtMoney(row.invoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="未开票" width="130" align="right">
              <template #default="{ row }"><span class="warn">{{ fmtMoney(row.uninvoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="明细数" width="80" align="center">
              <template #default="{ row }">{{ row.count }}</template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <el-table v-loading="drawerLoading" :data="drawerItems" stripe size="small"
                max-height="calc(100vh - 180px)" :scrollbar-always-on="true" show-overflow-tooltip>
        <el-table-column prop="delivery_date" label="下单日期" width="100" />
        <el-table-column prop="project_code" label="项目编号" width="100">
          <template #default="{ row }"><b class="code">{{ row.project_code || '—' }}</b></template>
        </el-table-column>
        <el-table-column prop="delivery_note_no" label="送货单号" width="100">
          <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
        </el-table-column>
        <el-table-column prop="arrival_date" label="到货日期" width="100">
          <template #default="{ row }">{{ row.arrival_date || '—' }}</template>
        </el-table-column>
        <el-table-column prop="item_name" label="名称" min-width="120" />
        <el-table-column prop="spec" label="规格" min-width="90">
          <template #default="{ row }">{{ row.spec || '—' }}</template>
        </el-table-column>
        <el-table-column label="数量" width="70" align="right">
          <template #default="{ row }">{{ row.qty ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="单价" width="90" align="right">
          <template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template>
        </el-table-column>
        <el-table-column label="收货金额" width="105" align="right">
          <template #default="{ row }"><b>{{ fmtMoney(row.received_amount) }}</b></template>
        </el-table-column>
        <el-table-column prop="invoice_date" label="开票日期" width="100">
          <template #default="{ row }">{{ row.invoice_date || '—' }}</template>
        </el-table-column>
        <el-table-column label="开票金额" width="105" align="right">
          <template #default="{ row }">{{ row.invoice_amount ? fmtMoney(row.invoice_amount) : '—' }}</template>
        </el-table-column>
        <el-table-column label="已付款" width="105" align="right">
          <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80" fixed="right">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.invoice_status)" size="small">{{ row.invoice_status }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <!-- ==================== 打包下载抽屉（采购部用）==================== -->
    <el-drawer v-model="dlVisible" :title="`${dlRow?.code || ''} · 采购资料打包下载`"
               direction="rtl" size="440px" destroy-on-close>
      <template v-if="dlRow">
        <div class="dl-tip">勾选需要的表格与附件，一键打包成 zip 下载（表格导出为 Excel）。</div>

        <div class="dl-sec">
          <div class="dl-sec-head">
            <span class="dl-sec-title">📄 采购数据表</span>
            <el-checkbox v-if="dlSheets.length"
                         :model-value="dlSelSheets.length === dlSheets.length"
                         :indeterminate="dlSelSheets.length > 0 && dlSelSheets.length < dlSheets.length"
                         @change="toggleAllSheets">全选</el-checkbox>
          </div>
          <el-checkbox-group v-model="dlSelSheets" class="dl-list">
            <el-checkbox v-for="s in dlSheets" :key="s.id" :value="s.id" class="dl-item">{{ s.label }}</el-checkbox>
          </el-checkbox-group>
          <div v-if="!dlSheets.length" class="muted dl-empty">该项目暂无采购数据表</div>
        </div>

        <div class="dl-sec">
          <div class="dl-sec-head">
            <span class="dl-sec-title">📎 设计推送附件</span>
            <el-checkbox v-if="dlAtts.length"
                         :model-value="dlSelAtts.length === dlAtts.length"
                         :indeterminate="dlSelAtts.length > 0 && dlSelAtts.length < dlAtts.length"
                         @change="toggleAllAtts">全选</el-checkbox>
          </div>
          <el-checkbox-group v-model="dlSelAtts" class="dl-list">
            <el-checkbox v-for="a in dlAtts" :key="a.id" :value="a.id" class="dl-item">
              <span class="dl-kind">{{ a.kind }}</span>{{ a.name }}
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="!dlAtts.length" class="muted dl-empty">暂无设计推送附件</div>
        </div>
      </template>
      <template #footer>
        <el-button @click="dlVisible = false">取消</el-button>
        <el-button type="primary" :loading="dlPacking" :icon="Download"
                   :disabled="!dlSelCount" @click="packDownload">
          打包下载（{{ dlSelCount }}/{{ dlTotal }}）
        </el-button>
      </template>
    </el-drawer>

    <!-- ==================== 数据表预览弹窗（采购部用）==================== -->
    <el-dialog v-model="previewVisible" :title="previewTitle" fullscreen destroy-on-close>
      <el-table :data="previewRecords" stripe v-loading="previewLoading"
                max-height="calc(100vh - 130px)" :scrollbar-always-on="true" size="small">
        <el-table-column
          v-for="f in previewFields" :key="f.id"
          :label="f.name" :min-width="previewColWidth">
          <template #default="{ row }">{{ cellVal(row, f.id) }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 弹窗表单：标签与控件之间留出间距，行间距不过于紧凑 */
:deep(.el-dialog .el-form-item__label) { padding-right: 10px; box-sizing: border-box; }
:deep(.el-dialog .el-form-item) { margin-bottom: 20px; }
:deep(.el-dialog .el-form-item:last-child) { margin-bottom: 0; }

.filter-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }
.summary-bar { display: flex; gap: 24px; padding: 12px 16px; background: var(--el-fill-color-light); border-radius: 6px; margin-top: 12px; font-size: 14px; }
.kpi-row { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.kpi-card { flex: 1; min-width: 140px; padding: 16px; background: var(--el-fill-color-light); border-radius: 8px; border: 1px solid var(--el-border-color-lighter); }
.kpi-label { font-size: 13px; color: var(--el-text-color-secondary); margin-bottom: 6px; }
.kpi-value { font-size: 22px; font-weight: 700; color: var(--el-color-primary); }
.danger-card { border-color: var(--el-color-danger-light-5); }
.report-section { margin-bottom: 16px; }
.section-title { font-weight: 600; font-size: 14px; margin-bottom: 8px; color: var(--el-text-color-primary); }
.code { color: var(--el-color-primary, #2563eb); }
.sup-name { font-weight: 500; }
.amt { color: var(--el-color-primary); }
.warn { color: var(--el-color-warning); }
.danger { color: var(--el-color-danger); }
.pr-item-row { display: flex; align-items: center; margin-bottom: 4px; font-size: 13px; }
.drawer-summary { display: flex; gap: 20px; padding: 12px 0 16px; font-size: 14px; border-bottom: 1px solid var(--el-border-color-lighter); margin-bottom: 12px; }
.monthly-collapse { margin-bottom: 14px; }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
.tip-line { line-height: 1.7; }
.dl-tip { font-size: 12.5px; color: var(--el-text-color-secondary); margin-bottom: 14px; line-height: 1.6; }
.dl-sec { margin-bottom: 20px; }
.dl-sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--el-border-color-lighter); }
.dl-sec-title { font-weight: 600; color: #0f172a; font-size: 13px; }
.dl-list { display: flex; flex-direction: column; gap: 6px; }
.dl-item { width: 100%; margin-right: 0; height: auto; }
.dl-item :deep(.el-checkbox__label) { white-space: normal; word-break: break-all; line-height: 1.5; }
.dl-kind { display: inline-block; margin-right: 6px; padding: 0 6px; font-size: 11px; color: #1d4ed8; background: #dbeafe; border-radius: 8px; }
.dl-empty { padding: 4px 0; }
.form-section-title {
  font-size: 13px; font-weight: 600; color: var(--el-color-primary);
  padding: 4px 0 10px; margin-top: 6px; border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 16px;
}
.supplier-form :deep(.el-form-item),
.item-form :deep(.el-form-item),
.order-form :deep(.el-form-item) { margin-bottom: 16px; }
.supplier-form :deep(.el-form-item__label),
.item-form :deep(.el-form-item__label),
.order-form :deep(.el-form-item__label) { font-size: 13px; color: var(--el-text-color-secondary); padding-bottom: 4px; }
.form-section-title:first-child { margin-top: 0; }
.po-no { font-family: monospace; font-size: 12.5px; color: #7c3aed; }
.order-lines-head { display: flex; align-items: center; justify-content: space-between; margin: 4px 0 8px; }
.order-lines :deep(.el-input__wrapper),
.order-lines :deep(.el-input-number) { width: 100%; }
.order-lines :deep(.cell) { padding-left: 6px; padding-right: 6px; }
.order-total-bar { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; padding: 10px 14px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 14px; }
</style>
