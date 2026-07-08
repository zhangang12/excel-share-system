<script setup lang="ts">
// 采购管理（含采购部）：采购部 / 采购明细 / 供应商账目 / 汇总报表
import { ref, computed, onMounted, onBeforeUnmount, reactive, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Download, Refresh, RefreshLeft, View, Plus, Delete, Printer, Upload, ArrowDown, ArrowLeft, Search, Tickets, EditPen, Setting, Collection, Box, Lock } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { datasheetsApi } from '@/api/datasheets'
import EmptyHint from '@/components/EmptyHint.vue'
import LineChart from '@/components/LineChart.vue'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasRole('buyer', 'buyer_lead', 'buyer_standard', 'buyer_outsource', 'admin', 'manager'))
const isLeadOrAbove = computed(() => auth.hasRole('buyer_lead', 'finance', 'admin', 'manager'))
const showPurchaseTab = computed(() => auth.hasRole('buyer', 'buyer_lead', 'buyer_standard', 'buyer_outsource', 'admin', 'manager'))
// 🆕 R6：采购自定义字段
const canConfigFields = computed(() => auth.hasRole('buyer_lead', 'admin', 'manager'))
interface CustomField {
  id: number; label: string; ftype: string; options: string[]
  required: boolean; show_in_list: boolean; sort_order: number; enabled: boolean
}
const customFields = ref<CustomField[]>([])
async function loadCustomFields() {
  try { customFields.value = (await http.get<CustomField[]>('/purchase-mgmt/custom-fields')).data }
  catch { customFields.value = [] }
}
const listCustomFields = computed(() => customFields.value.filter(f => f.enabled && f.show_in_list))
const formCustomFields = computed(() => customFields.value.filter(f => f.enabled))
function cfDisplay(cv: Record<string, any> | undefined, f: CustomField): string {
  const v = cv?.[String(f.id)]
  return v == null || v === '' ? '—' : String(v)
}
// 🆕 R6 字段管理器
const CF_TYPES = [{ v: 'text', l: '文本' }, { v: 'number', l: '数字' }, { v: 'date', l: '日期' }, { v: 'select', l: '下拉选项' }]
const cfManagerVisible = ref(false)
const cfEditingId = ref<number | null>(null)
const cfSaving = ref(false)
const cfForm = reactive({ label: '', ftype: 'text', options: '', required: false, show_in_list: true, sort_order: 0, enabled: true })
function cfResetForm() {
  cfEditingId.value = null
  Object.assign(cfForm, { label: '', ftype: 'text', options: '', required: false, show_in_list: true, sort_order: 0, enabled: true })
}
function openFieldManager() { cfResetForm(); loadCustomFields(); cfManagerVisible.value = true }
function cfEdit(f: CustomField) {
  cfEditingId.value = f.id
  Object.assign(cfForm, {
    label: f.label, ftype: f.ftype, options: (f.options || []).join('\n'),
    required: f.required, show_in_list: f.show_in_list, sort_order: f.sort_order, enabled: f.enabled,
  })
}
async function cfSave() {
  if (!cfForm.label.trim()) { ElMessage.warning('请填写字段名称'); return }
  const payload = {
    label: cfForm.label.trim(), ftype: cfForm.ftype,
    options: cfForm.ftype === 'select' ? cfForm.options.split('\n').map(s => s.trim()).filter(Boolean) : [],
    required: cfForm.required, show_in_list: cfForm.show_in_list, sort_order: cfForm.sort_order, enabled: cfForm.enabled,
  }
  cfSaving.value = true
  try {
    if (cfEditingId.value) { await http.put(`/purchase-mgmt/custom-fields/${cfEditingId.value}`, payload); ElMessage.success('已更新') }
    else { await http.post('/purchase-mgmt/custom-fields', payload); ElMessage.success('已新增字段') }
    cfResetForm()
    await loadCustomFields()
  } catch { /* handled */ } finally { cfSaving.value = false }
}
async function cfDelete(f: CustomField) {
  try { await ElMessageBox.confirm(`删除字段「${f.label}」？已录入明细的历史值保留但不再显示/校验。`, '删除字段', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await http.delete(`/purchase-mgmt/custom-fields/${f.id}`); ElMessage.success('已删除'); await loadCustomFields() } catch { /* handled */ }
}

// 🆕 物料字典（类别/单位）维护 —— 采购主管/admin/manager；仓库物料表单只能从字典选
interface MatDictItem { id: number; dtype: string; value: string; sort_order: number; enabled: boolean }
const matDictVisible = ref(false)
const matDict = ref<MatDictItem[]>([])
const mdTab = ref<'category' | 'unit' | 'supplier_category' | 'material_grade'>('category')
const mdEditingId = ref<number | null>(null)
const mdSaving = ref(false)
const mdForm = reactive({ value: '', sort_order: 0, enabled: true })
const mdList = computed(() => matDict.value.filter(d => d.dtype === mdTab.value))
const MD_TAB_LABELS: Record<string, string> = { category: '类别', unit: '单位', supplier_category: '供应商分类', material_grade: '材质' }
const MD_TAB_PLACEHOLDERS: Record<string, string> = {
  category: '如 标准件 / 不锈钢', unit: '如 个 / 米 / 公斤',
  supplier_category: '如 外协 / 运输', material_grade: '如 304不锈钢 / 碳钢',
}
const MD_TAB_ALERTS: Record<string, string> = {
  category: '维护仓库物料的「类别 / 单位」可选值。仓库录入物料时只能从这里"启用"的取值中选（不再自由输入）；停用的值不再出现在下拉里但保留历史；改名会同步更新已用该值的物料。',
  unit: '维护仓库物料的「类别 / 单位」可选值。仓库录入物料时只能从这里"启用"的取值中选（不再自由输入）；停用的值不再出现在下拉里但保留历史；改名会同步更新已用该值的物料。',
  material_grade: '维护仓库物料的「材质」可选值，跟类别是两套独立字典。仓库录入物料时只能从这里"启用"的取值中选；停用的值不再出现在下拉里但保留历史；改名会同步更新已用该值的物料。',
  supplier_category: '维护供应商的「分类」可选值。新增/编辑供应商时只能从这里"启用"的取值中选（不再自由输入）；停用的值不再出现在下拉里但保留历史；改名会同步更新已用该值的供应商。与物料类别是两套独立字典，互不混用。',
}
async function loadMatDict() {
  try { matDict.value = (await http.get<MatDictItem[]>('/wh/material-dict')).data }
  catch { matDict.value = [] }
}
function mdResetForm() { mdEditingId.value = null; Object.assign(mdForm, { value: '', sort_order: 0, enabled: true }) }
function openMatDictManager() { mdResetForm(); mdTab.value = 'category'; loadMatDict(); matDictVisible.value = true }
function mdEdit(d: MatDictItem) {
  mdEditingId.value = d.id; mdTab.value = d.dtype as 'category' | 'unit' | 'supplier_category' | 'material_grade'
  Object.assign(mdForm, { value: d.value, sort_order: d.sort_order, enabled: d.enabled })
}
async function mdSave() {
  if (!mdForm.value.trim()) { ElMessage.warning('请填写取值'); return }
  const payload = { dtype: mdTab.value, value: mdForm.value.trim(), sort_order: mdForm.sort_order, enabled: mdForm.enabled }
  mdSaving.value = true
  try {
    if (mdEditingId.value) { await http.put(`/wh/material-dict/${mdEditingId.value}`, payload); ElMessage.success('已更新') }
    else { await http.post('/wh/material-dict', payload); ElMessage.success('已新增') }
    mdResetForm(); await loadMatDict()
  } catch { /* handled */ } finally { mdSaving.value = false }
}
async function mdDelete(d: MatDictItem) {
  try { await ElMessageBox.confirm(`删除字典项「${d.value}」？若已被物料使用会被拦截，可改为「停用」。`, '删除取值', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await http.delete(`/wh/material-dict/${d.id}`); ElMessage.success('已删除'); await loadMatDict() } catch { /* handled */ }
}
async function mdToggle(d: MatDictItem) {
  try {
    await http.put(`/wh/material-dict/${d.id}`, { dtype: d.dtype, value: d.value, sort_order: d.sort_order, enabled: !d.enabled })
    await loadMatDict()
  } catch { /* handled */ }
}

function fmtMoney(v: number | undefined | null) {
  if (v == null) return '¥0'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// 🆕 付款方式下拉选项（可自定义追加）
const PAY_METHODS = ['现金全款', '对公全款', '账期', '现金预付', '对公预付']
const PREPAY_METHODS = ['现金预付', '对公预付']   // 🆕 选中这两种才需要填「预付比例」
function isPrepayMethod(m?: string | null) { return !!m && PREPAY_METHODS.includes(m) }
function payMethodLabel(m?: string | null, ratio?: number | null): string {
  if (!m) return ''
  return isPrepayMethod(m) && ratio != null ? `${m}（预付${ratio}%）` : m
}

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
  item_name: string; spec?: string | null; brand?: string | null; qty?: number | null; unit_price?: number | null
  received_amount: number; invoice_date?: string | null; invoice_no?: string | null; tax_rate?: string | null
  invoice_amount: number; paid_amount: number; paid_date?: string | null
  payment_method?: string | null; prepay_ratio?: number | null; pay_status?: string
  custom_values?: Record<string, any>
  invoice_status: string; buyer_id?: number | null; buyer_name?: string | null
  is_kit?: boolean; kit_parts?: { name: string; spec?: string | null; qty?: number | null }[] | null
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
  finance_approver_id?: number | null; approver_name?: string | null
  approved_at?: string; paid_amount?: number; paid_date?: string
  payment_method?: string; reject_reason?: string; created_at: string
  supplier_bank_name?: string | null; supplier_bank_account?: string | null; supplier_tax_no?: string | null
  po_nos?: string[]
  items: Array<{ item_id: number; item_name: string; allocated_amount: number; po_no?: string | null; spec?: string | null; project_code?: string | null; received_amount?: number }>
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
// 🆕 窗口宽度实时跟踪：全屏预览的列宽随窗口缩放重算
const winW = ref(typeof window !== 'undefined' ? window.innerWidth : 1280)
function onWinResize() { winW.value = window.innerWidth }
onMounted(() => window.addEventListener('resize', onWinResize))
onBeforeUnmount(() => window.removeEventListener('resize', onWinResize))
const previewColWidth = computed(() => {
  const n = previewFields.value.length
  if (!n) return 120
  const usable = winW.value - 50 - 32
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
const itemsTableRef = ref<{ clearSelection: () => void }>()
const filterSupplierId = ref<number | ''>('')
const filterProjectCode = ref('')
const filterMonth = ref('')
const filterInvoiceStatus = ref('')
const filterCategory = ref('')   // 🆕 按供应商分类筛选
const itemSheetType = ref('')    // 🆕 ④ 采购明细按清单类型分 tab('' = 全部, 'loose' = 散单)
function onItemSheetTab() { page.value = 1; loadItems() }

// 🆕 服务端分页（总条数用 summary.count，随筛选联动）
const page = ref(1)
const pageSize = ref(100)
async function onFilterChange() { page.value = 1; await loadItems() }

// 🆕 一键清空所有筛选
const hasFilter = computed(() =>
  !!(filterSupplierId.value || filterProjectCode.value || filterMonth.value || filterInvoiceStatus.value || filterCategory.value))
async function resetFilters() {
  filterSupplierId.value = ''; filterProjectCode.value = ''
  filterMonth.value = ''; filterInvoiceStatus.value = ''; filterCategory.value = ''
  page.value = 1
  await loadItems()
}

// 🆕 需求九：勾选合并父行时展开成其子明细（叶子），与直接勾选的叶子去重
const selLeaves = computed<PurchaseItemOut[]>(() => {
  const seen = new Set<number>(); const out: PurchaseItemOut[] = []
  for (const r of selectedItems.value as any[]) {
    for (const it of (r._isGroup ? (r.children || []) : [r])) {
      if (!seen.has(it.id)) { seen.add(it.id); out.push(it) }
    }
  }
  return out
})
// 🆕 勾选行的汇总（浮出操作条：请款金额一目了然；跨供应商时禁止请款并提示）
const selUnpaidTotal = computed(() =>
  selLeaves.value.reduce((s, i) => s + (i.received_amount - i.paid_amount), 0))
const selSameSupplier = computed(() =>
  selLeaves.value.length > 0 && selLeaves.value.every(i => i.supplier_id === selLeaves.value[0].supplier_id))
function clearSelection() { itemsTableRef.value?.clearSelection() }

// 🆕 #144 采购明细按采购单号折叠分组：同一采购单(≥2行)收成一个可展开的父行，
//    单行采购单/无采购单号的散单仍平铺。父行显示汇总(共N件+收货/开票/已付合计)。
const rowKey = (row: any) => (row._isGroup ? row._key : 'i' + row.id)
const groupedItems = computed<any[]>(() => {
  const groups = new Map<string, any>()
  const out: any[] = []
  for (const it of items.value) {
    const po = it.po_no
    if (!po) { out.push(it); continue }
    let g = groups.get(po)
    if (!g) {
      g = {
        _isGroup: true, _key: 'g:' + po, po_no: po,
        supplier_name: it.supplier_name, supplier_id: it.supplier_id, delivery_date: it.delivery_date,
        received_amount: 0, invoice_amount: 0, paid_amount: 0,
        _codes: new Set<string>(), _dnotes: new Set<string>(), _arrivals: new Set<string>(),
        _invnos: new Set<string>(), _invdates: new Set<string>(),
        children: [] as PurchaseItemOut[],
      }
      groups.set(po, g); out.push(g)
    }
    g.children.push(it)
    g.received_amount += it.received_amount || 0
    g.invoice_amount += it.invoice_amount || 0
    g.paid_amount += it.paid_amount || 0
    if (it.project_code) g._codes.add(it.project_code)
    if (it.delivery_note_no) g._dnotes.add(it.delivery_note_no)  // 🆕 需求九
    if (it.arrival_date) g._arrivals.add(it.arrival_date)        // 🆕 需求九
    if (it.invoice_no) g._invnos.add(it.invoice_no)              // 🆕 需求三：开票号上主汇总单
    if (it.invoice_date) g._invdates.add(it.invoice_date)        // 🆕 需求三：开票日期上主汇总单
  }
  return out.map((r) => {
    if (!r._isGroup) return r
    if (r.children.length === 1) return r.children[0]   // 单行采购单直接平铺
    r._count = r.children.length
    const codes = Array.from(r._codes) as string[]
    r.project_code = codes.length === 0 ? null : codes.length === 1 ? codes[0] : '多个'
    // 🆕 需求九：送货单号 / 到货日期 体现在合并父行（同单同批时二级列表与父行一致）
    const dnotes = Array.from(r._dnotes) as string[]
    r.delivery_note_no = dnotes.length === 0 ? null : dnotes.length === 1 ? dnotes[0] : '多个'
    const arrivals = Array.from(r._arrivals) as string[]
    r.arrival_date = arrivals.length === 0 ? null : arrivals.length === 1 ? arrivals[0] : '多个'
    // 🆕 需求三：开票号 / 开票日期体现在合并父行
    const invnos = Array.from(r._invnos) as string[]
    r.invoice_no = invnos.length === 0 ? null : invnos.length === 1 ? invnos[0] : '多个'
    const invdates = Array.from(r._invdates) as string[]
    r.invoice_date = invdates.length === 0 ? null : invdates.length === 1 ? invdates[0] : '多个'
    return r
  })
})

// suppliers & statements
const suppliers = ref<SupplierOut[]>([])
const filterSuppliers = ref<SupplierOut[]>([])   // #152：采购明细供应商筛选下拉(只本人)
const statementData = ref<StatementList | null>(null)
const drawerVisible = ref(false)
const drawerSupplier = ref<SupplierStatementRow | null>(null)
const drawerItems = ref<PurchaseItemOut[]>([])
const drawerLoading = ref(false)

// 🆕 按月「合计开票」汇总：按到货日期分月，看某月 收货合计 / 已开票 / 未开票
const drawerMonthly = computed(() => {
  const map = new Map<string, { month: string; received: number; invoiced: number; uninvoiced: number; paid: number; count: number }>()
  for (const it of drawerItems.value) {
    const m = (it.arrival_date || '').slice(0, 7) || '未收货'
    let g = map.get(m)
    if (!g) { g = { month: m, received: 0, invoiced: 0, uninvoiced: 0, paid: 0, count: 0 }; map.set(m, g) }
    g.received += it.received_amount || 0
    if (it.invoice_status === '已开票') g.invoiced += (it.invoice_amount || it.received_amount || 0)
    else g.uninvoiced += it.received_amount || 0
    g.paid += it.paid_amount || 0    // #166
    g.count++
  }
  return Array.from(map.values()).sort((a, b) => (a.month < b.month ? 1 : -1))
})
// #166：供应商明细抽屉加报表——月度 收货/开票/已付 趋势图（复用 LineChart）
const drawerTrendChart = computed(() => {
  const asc = [...drawerMonthly.value].filter(m => m.month !== '未收货').sort((a, b) => (a.month < b.month ? -1 : 1))
  return {
    labels: asc.map(m => m.month.slice(2)),
    series: [
      { name: '收货', points: asc.map(m => m.received), color: '#2a78d6' },
      { name: '已开票', points: asc.map(m => m.invoiced), color: '#eda100' },
      { name: '已付', points: asc.map(m => m.paid), color: '#008300' },
    ] as { name: string; points: (number | null)[]; color?: string }[],
  }
})

// 🆕 供应商分类——独立字典（dtype=supplier_category，见「字典设置」)，不与物料类别混用
const categoryOptions = computed(() => matDict.value.filter(d => d.dtype === 'supplier_category' && d.enabled).map(d => d.value))
// 🆕 供应商账目筛选（名称 + 分类）——#153 改回原来的平铺列表（去掉需求十一的分类卡片下钻）
const stmtNameFilter = ref('')
const stmtCatFilter = ref('')
const filteredStatementRows = computed(() => {
  const rows = statementData.value?.rows || []
  const kw = stmtNameFilter.value.trim().toLowerCase()
  return rows.filter(r =>
    (!kw || r.supplier_name.toLowerCase().includes(kw)) &&
    (!stmtCatFilter.value || (r.category || '') === stmtCatFilter.value))
})
// 🆕 供应商状态查询（避免模板里反复 find）+ 操作收进下拉菜单
function supActive(supplierId: number): boolean {
  return suppliers.value.find(s => s.id === supplierId)?.status === 'active'
}
function onSupCmd(cmd: string, row: SupplierStatementRow) {
  if (cmd === 'edit') {
    const s = suppliers.value.find(x => x.id === row.supplier_id)
    if (s) openEditSupplier(s)
  } else if (cmd === 'balance') openOpeningBalance(row)
  else if (cmd === 'toggle') toggleSupplier(row)
  else if (cmd === 'export') exportSupplierStatement(row.supplier_id, row.supplier_name)
  else if (cmd === 'delete') deleteSupplier(row)
}

// reports
const kpi = ref<PurchaseKPI | null>(null)
const monthlyTrend = ref<MonthlyPoint[]>([])
const byBuyer = ref<BuyerRow[]>([])
const byProject = ref<{ project_code: string; amount: number; count: number }[]>([])
const topSuppliers = ref<TopSupplier[]>([])
const projectSearch = ref('')
// 🆕 需求十二：供应商月度采购额趋势（多折线）
interface SupplierTrend { months: string[]; series: { supplier_id: number; supplier_name: string; points: number[]; total: number }[] }
const supplierTrend = ref<SupplierTrend>({ months: [], series: [] })

// dialogs
const itemDialogVisible = ref(false)
const editingItem = ref<PurchaseItemOut | null>(null)
const itemSaving = ref(false)
const itemFormRef = ref<FormInstance>()
const itemRules: FormRules = {
  supplier_id: [{ required: true, message: '请选择供应商', trigger: 'change' }],
  item_name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
}
const itemForm = reactive({
  supplier_id: '' as number | '',
  delivery_date: '', contract_no: '', project_code: '', delivery_note_no: '',
  item_name: '', spec: '', brand: '', qty: null as number | null, unit_price: null as number | null,
  received_amount: 0, invoice_date: '', tax_rate: '', invoice_amount: 0,
  payment_method: '', prepay_ratio: null as number | null, invoice_status: '待对账', notes: '',
  custom_values: {} as Record<string, any>,   // 🆕 R6
})
// 🆕 数量×单价 自动带出合计（与采购单行为一致，仍可手改）
function onItemCalc() {
  if (itemForm.qty != null && itemForm.unit_price != null)
    itemForm.received_amount = Number((itemForm.qty * itemForm.unit_price).toFixed(2))
}

// 🆕 采购单：同一供应商 + 多个零件行（表头共享，单价选填以支持「先填/后填价格」两种流程）
interface OrderLine {
  item_name: string; spec: string; project_code: string
  qty: number | null; unit_price: number | null; received_amount: number | null
  tax_rate: string; notes: string; custom_values: Record<string, any>
}
function blankLine(): OrderLine {
  return { item_name: '', spec: '', project_code: '', qty: null, unit_price: null, received_amount: null, tax_rate: '', notes: '', custom_values: {} }
}
const orderDialogVisible = ref(false)
const orderSaving = ref(false)
const orderForm = reactive({
  supplier_id: '' as number | '',
  delivery_date: '', contract_no: '', project_code: '', payment_method: '',
  prepay_ratio: null as number | null,
  lines: [blankLine()] as OrderLine[],
})
// 采购商抬头（打印采购单用；如公司全称有出入，改这里即可）
const PO_COMPANY = '同辉智能装备有限公司'
const orderTotal = computed(() =>
  orderForm.lines.reduce((s, l) => s + (l.received_amount ?? ((l.qty || 0) * (l.unit_price || 0))), 0))

// 🆕 订单编号可选：历史项目编号供下拉选择（allow-create 仍可手输新编号）
// #156：没有项目编号的采购（固定资产/耗材等）给几个常用非项目选项，也可手输
const NON_PROJECT_CODES = ['固定资产', '耗材', '办公用品', '劳保用品', '外购工具']
const projectCodeOptions = ref<string[]>([])
async function loadProjectCodes() {
  try {
    const r = await http.get<{ code: string }[]>('/purchase/projects')
    projectCodeOptions.value = Array.from(new Set(r.data.map(p => p.code).filter(Boolean)))
  } catch { projectCodeOptions.value = [] }
}

function openNewOrder() {
  Object.assign(orderForm, {
    supplier_id: '', delivery_date: new Date().toISOString().slice(0, 10),
    contract_no: '', project_code: '', payment_method: '', prepay_ratio: null, lines: [blankLine()],
  })
  loadProjectCodes()
  orderDialogVisible.value = true
}
function addOrderLine() { orderForm.lines.push(blankLine()) }
function removeOrderLine(idx: number) {
  orderForm.lines.splice(idx, 1)
  if (!orderForm.lines.length) orderForm.lines.push(blankLine())
}
// 🆕 已填了零件行时，点遮罩/叉号先确认，避免误关丢掉整单录入
function onOrderDialogClose(done: () => void) {
  if (orderForm.lines.some(l => l.item_name.trim())) {
    ElMessageBox.confirm('已填写零件行，关闭将丢失当前录入。确认放弃？', '提示',
      { type: 'warning', confirmButtonText: '放弃录入', cancelButtonText: '继续编辑' })
      .then(() => done()).catch(() => {})
  } else done()
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
    const resp = await http.post<PurchaseItemOut[]>('/purchase-mgmt/orders', {
      supplier_id: orderForm.supplier_id,
      delivery_date: orderForm.delivery_date || null,
      contract_no: orderForm.contract_no || null,
      project_code: orderForm.project_code || null,
      payment_method: orderForm.payment_method || null,
      prepay_ratio: isPrepayMethod(orderForm.payment_method) ? orderForm.prepay_ratio : null,
      lines: lines.map(l => ({
        item_name: l.item_name.trim(),
        spec: l.spec || null,
        project_code: l.project_code || null,
        qty: l.qty,
        unit_price: l.unit_price,
        received_amount: l.received_amount,
        tax_rate: l.tax_rate || null,
        notes: l.notes || null,
        custom_values: l.custom_values || {},
      })),
    })
    ElMessage.success(`采购单已保存（${lines.length} 个零件行）`)
    orderDialogVisible.value = false
    await loadItems()
    offerPrint(resp.data[0]?.po_no)
  } catch { /* handled */ } finally { orderSaving.value = false }
}


// ===== 打印采购单（保存前预览打印 / 保存后按单号补打印共用一套模板）=====
interface POLine {
  project_code?: string | null; item_name: string; spec?: string | null
  qty?: number | null; unit_price?: number | null; amount?: number | null; notes?: string | null
}
function renderPOHtml(o: { poNo?: string; supplierName: string; orderDate: string; payMethod: string; defaultProject?: string; lines: POLine[] }): string {
  const esc = (v: unknown) => String(v ?? '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string))
  const total = o.lines.reduce((s, l) =>
    s + (l.amount ?? ((l.qty || 0) * (l.unit_price || 0))), 0)
  const rows = o.lines.map((l, i) => `<tr>
    <td class="c">${i + 1}</td>
    <td class="c">${esc(l.project_code || o.defaultProject || '')}</td>
    <td>${esc(l.item_name)}</td><td>${esc(l.spec)}</td>
    <td class="r">${l.qty ?? ''}</td><td class="r">${l.unit_price ?? ''}</td>
    <td class="r">${l.amount ?? (l.qty != null && l.unit_price != null ? (l.qty * l.unit_price).toFixed(2) : '')}</td>
    <td>${esc(l.notes)}</td></tr>`).join('')
  return `<!doctype html><html><head><meta charset="utf-8"><title>采购单${o.poNo ? ' ' + esc(o.poNo) : ''}</title>
    <style>@page{size:A4;margin:16mm}body{font-family:'Microsoft YaHei',SimSun,sans-serif;color:#111;font-size:13px}
    h1{text-align:center;font-size:22px;margin:0 0 12px;letter-spacing:2px}
    table{width:100%;border-collapse:collapse}
    table.meta td{border:1px solid #333;padding:7px 10px}
    table.items{margin-top:-1px}
    table.items th,table.items td{border:1px solid #333;padding:6px 8px}
    table.items th{background:#e8f5ee}.r{text-align:right}.c{text-align:center}
    .foot{margin-top:22px;display:flex;justify-content:space-between}.sign{margin-top:36px}
    /* 🆕 下载PDF入口：屏幕上常驻显示，真正打印/另存为PDF时(@media print)自动隐藏，不会印到纸上/PDF里 */
    .dl-bar{position:sticky;top:0;background:#fffbe6;border:1px solid #f0d060;border-radius:6px;
      padding:10px 14px;margin-bottom:16px;display:flex;align-items:center;gap:12px;font-size:13px;color:#7a5c00}
    .dl-bar button{background:#1a7f4b;color:#fff;border:none;border-radius:4px;padding:7px 16px;
      font-size:13px;cursor:pointer;white-space:nowrap}
    .dl-bar button:hover{background:#146138}
    @media print{.dl-bar{display:none}}</style></head>
    <body>
    <div class="dl-bar no-print">
      <span>💡 下载PDF：点右边按钮 → 弹出的打印窗口里，目标/打印机选「另存为PDF」即可保存到本地。</span>
      <button onclick="window.print()">🖨️ 打印 / 下载PDF</button>
    </div>
    <h1>采购单</h1>
    <table class="meta">
      <tr><td style="width:14%"><b>需方</b></td><td style="width:40%">${esc(PO_COMPANY)}</td>
          <td style="width:14%"><b>下单日期</b></td><td>${esc(o.orderDate)}</td></tr>
      <tr><td><b>供方</b></td><td>${esc(o.supplierName)}</td>
          <td><b>付款方式</b></td><td>${esc(o.payMethod)}</td></tr>
      ${o.poNo ? `<tr><td><b>采购单号</b></td><td colspan="3">${esc(o.poNo)}</td></tr>` : ''}
    </table>
    <table class="items"><thead><tr>
      <th style="width:40px">序号</th><th style="width:100px">订单编号</th><th>名称</th><th>规格型号</th>
      <th style="width:60px">数量</th><th style="width:82px">单价</th><th style="width:104px">合计金额</th><th style="width:120px">备注</th>
    </tr></thead><tbody>${rows}
      <tr><td class="c" colspan="6"><b>合计</b></td>
      <td class="r"><b>￥${total.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</b></td><td></td></tr>
    </tbody></table>
    <div class="foot"><div class="sign">采购（签字）：____________</div><div class="sign">供方（盖章）：____________</div></div>
    </body></html>`
}
function openPrintWindow(html: string) {
  const w = window.open('', '_blank', 'width=900,height=700')
  if (!w) { ElMessage.warning('请允许弹窗以打印采购单'); return }
  w.document.write(html); w.document.close()
  w.onload = () => { w.focus(); w.print() }
}
// 保存前：直接打当前表单内容（无单号）
function printPurchaseOrder() {
  if (!orderForm.supplier_id) { ElMessage.warning('请先选择供应商'); return }
  const sup = suppliers.value.find(s => s.id === orderForm.supplier_id)
  const lines = orderForm.lines.filter(l => l.item_name.trim())
  if (!lines.length) { ElMessage.warning('请至少填写一行零件'); return }
  openPrintWindow(renderPOHtml({
    supplierName: sup?.name || '', orderDate: orderForm.delivery_date,
    payMethod: payMethodLabel(orderForm.payment_method, orderForm.prepay_ratio), defaultProject: orderForm.project_code,
    lines: lines.map(l => ({
      project_code: l.project_code, item_name: l.item_name, spec: l.spec,
      qty: l.qty, unit_price: l.unit_price, amount: l.received_amount, notes: l.notes,
    })),
  }))
}
// 🆕 保存后随时补打印：按采购单号取整单明细（列表里点采购单号即可）
async function printPO(poNo?: string | null) {
  if (!poNo) return
  // 🆕 修复"点了没反应"：必须在 await 之前、点击的同一时刻就把窗口打开——
  // 隔着一次网络请求(await http.get)之后再 window.open，多数浏览器会把它当成
  // "非用户直接触发"而静默拦截弹窗（且很多情况下 window.open 并不返回 null，
  // 原有的 if(!w) 判断也拦不住，看起来就是"点击后完全没反应"）。
  const w = window.open('', '_blank', 'width=900,height=700')
  if (!w) { ElMessage.warning('请允许弹窗以打印采购单'); return }
  w.document.write('<p style="font-family:sans-serif;color:#888;padding:24px">加载中…</p>')
  try {
    const r = await http.get<PurchaseItemOut[]>(`/purchase-mgmt/orders/${encodeURIComponent(poNo)}`)
    const rows = r.data
    if (!rows.length) { w.close(); ElMessage.info('该采购单没有明细'); return }
    const html = renderPOHtml({
      poNo, supplierName: rows[0].supplier_name, orderDate: rows[0].delivery_date || '',
      payMethod: payMethodLabel(rows[0].payment_method, rows[0].prepay_ratio),
      lines: rows.map(x => ({
        project_code: x.project_code, item_name: x.item_name, spec: x.spec,
        qty: x.qty, unit_price: x.unit_price, amount: x.received_amount, notes: x.notes,
      })),
    })
    w.document.open(); w.document.write(html); w.document.close()
    w.onload = () => { w.focus(); w.print() }
  } catch { w.close(); /* 全局拦截器已提示 */ }
}
// 🆕 采购单下载PDF：服务端直出真正的 PDF 文件，不依赖浏览器弹窗/window.print
//（原来那套弹窗打印在移动端/微信浏览器经常无响应，#124 反馈的就是这个）
async function downloadPoPdf(poNo?: string | null) {
  if (!poNo) return
  try {
    const res = await http.get(`/purchase-mgmt/orders/${encodeURIComponent(poNo)}/pdf`, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url; a.download = `采购单_${poNo}.pdf`
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success('PDF 已开始下载')
  } catch { ElMessage.error('下载PDF失败') }
}
// 保存成功后询问是否下载PDF（带正式单号）
function offerPrint(poNo?: string | null) {
  if (!poNo) return
  ElMessageBox.confirm(`采购单已保存，单号 ${poNo}。是否下载 PDF？（之后也可在明细列表点「PDF」补下载）`,
    '保存成功', { type: 'success', confirmButtonText: '下载PDF', cancelButtonText: '暂不' })
    .then(() => downloadPoPdf(poNo)).catch(() => {})
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

// 🆕 从清单下单：项目标准件清单 → 筛选 → 选供应商 → 生成采购单
interface PurchasableRow {
  sheet_id: number; record_id: number; item_name: string; spec?: string | null
  brand?: string | null; material?: string | null; drawing?: string | null
  qty?: number | null; stock: number; suggest_purchase: number
  notes?: string | null; status: string
  sheet_key?: string | null; project_id?: number | null   // 🆕 跨项目待下单聚合
  project_code?: string | null; project_name?: string | null
  _checked: boolean; _price: number | null; _buyqty: number | null
  _supplier_id: number | ''; _brand: string   // 🆕 逐行选供应商/品牌
  _payment_method: string   // 🆕 逐行付款方式（不同批次可能不一样，不跟供应商绑死）
  _prepay_ratio: number | null   // 🆕 逐行预付比例(%)，仅现金预付/对公预付时有意义
}
// 🆕 ① 清单类型描述符：驱动列的显示名/显隐（治"每类清单列名不一样"）。前后端一致。
const SHEET_META: Record<string, { label: string; nameLabel: string; specLabel: string; hasBrand: boolean; hasQty: boolean; hasMaterial: boolean; hasDrawing: boolean }> = {
  standard:  { label: '标准件', nameLabel: '名称',     specLabel: '规格型号', hasBrand: true,  hasQty: true,  hasMaterial: false, hasDrawing: false },
  elec_po:   { label: '电工',   nameLabel: '名称',     specLabel: '规格型号', hasBrand: true,  hasQty: true,  hasMaterial: false, hasDrawing: false },
  material:  { label: '不锈钢', nameLabel: '材料类别', specLabel: '规格型号', hasBrand: false, hasQty: true,  hasMaterial: true,  hasDrawing: true  },
  outsource: { label: '外协',   nameLabel: '名称',     specLabel: '图纸名称', hasBrand: false, hasQty: false, hasMaterial: false, hasDrawing: false },
  laser:     { label: '激光',   nameLabel: '名称',     specLabel: '图纸名称', hasBrand: false, hasQty: false, hasMaterial: false, hasDrawing: false },
}
const sheetMeta = (k?: string | null) => SHEET_META[k || ''] || SHEET_META.standard
// #159/#160：不锈钢有独立"图纸名称"列——下单时折进 spec 一起带上采购单（图纸名称 · 规格型号）
function foldDrawingSpec(r: { drawing?: string | null; spec?: string | null }): string | null {
  const d = (r.drawing || '').trim(), s = (r.spec || '').trim()
  if (d && s) return `${d} · ${s}`
  return d || s || null
}
const listOrderVisible = ref(false)
const listOrderSaving = ref(false)
const purchasableLoading = ref(false)
// 🆕 成套采购「按套下单」= 从清单下单的成套模式：勾选一组清单零件 → 打包成一套(一条成套明细)
const listOrderMode = ref<'normal' | 'kit'>('normal')
const kitSet = reactive({
  supplier_id: '' as number | '', kit_name: '',
  kit_qty: 1 as number | null, kit_total: null as number | null,
  payment_method: '', prepay_ratio: null as number | null,
})
const kitSetUnitPrice = computed(() => {
  const q = kitSet.kit_qty || 0, t = kitSet.kit_total || 0
  return q > 0 ? Number((t / q).toFixed(4)) : 0
})
// 🆕 R1：5 张来源清单（key ↔ 中文名 ↔ 项目字段名，用于判断项目是否有该表）
const SHEET_TYPES = [
  { key: 'standard', label: '标准件清单', field: 'standard_sheet_id', hasQty: true },
  { key: 'elec_po', label: '电工采购单', field: 'elec_po_sheet_id', hasQty: true },
  { key: 'material', label: '不锈钢原料下料单', field: 'material_sheet_id', hasQty: true },
  { key: 'outsource', label: '外协加工', field: 'outsource_sheet_id', hasQty: false },
  { key: 'laser', label: '激光件清单', field: 'laser_sheet_id', hasQty: false },
] as const
interface ListProject { id: number; code: string; name: string; sheets: Record<string, number | null> }
const listProjects = ref<ListProject[]>([])
const listSheet = ref<string>('standard')
const purchasableRows = ref<PurchasableRow[]>([])
const purchasableFilter = ref('')
const onlyGap = ref(false)   // 🆕 只看有缺口（建议采购>0）的行
// 🆕 A4：去掉表头供应商，改逐行选；🆕 付款方式也改逐行(见 _payment_method)，表头只留 项目/清单/下单日期
const listOrderForm = reactive({
  project_id: '' as number | '', project_code: '',
  delivery_date: '',
})
// 🆕 R4/A6：沿用采购部项目目录「按人分表」可见性——采购员只对自己负责的清单下单
//（lixinxin=标准件+电工 / wangqin=不锈钢+激光 / fangbusen=外协；其余人看全部）
const SHEET_VIS: Record<string, () => boolean> = {
  standard: () => showStandardSheet.value,
  elec_po: () => showElecPo.value,
  material: () => showMaterial.value,
  outsource: () => showOutsource.value,
  laser: () => showLaser.value,
}
const sheetVisible = (key: string) => SHEET_VIS[key]?.() ?? true
// 🆕 #2 下拉可选清单类型 = 本采购员有权限看的（跟采购部项目一览的列权限一致），
//    不再按「当前项目是否有该表」收窄——保证下拉权限与页面能看的列一致。
const availableSheets = computed(() => SHEET_TYPES.filter(t => sheetVisible(t.key)))
// 本采购员默认清单类型（权限内第一张）
const myDefaultSheet = computed(() => availableSheets.value[0]?.key || 'standard')
// 选定项目后的默认清单：优先「权限内 且 该项目有」的（有数据），否则退到权限内第一张
function defaultSheetFor(pid: number | ''): string {
  const p = listProjects.value.find(x => x.id === pid)
  const withData = p ? availableSheets.value.find(t => p.sheets[t.field]) : undefined
  return (withData || availableSheets.value[0])?.key || 'standard'
}
const curSheetHasQty = computed(() => SHEET_TYPES.find(t => t.key === listSheet.value)?.hasQty ?? true)
// 🆕 品牌历史选项（allow-create + 已用过的品牌，方便下拉复选）
const brandOptions = computed(() => {
  const set = new Set<string>()
  for (const r of purchasableRows.value) { if (r.brand) set.add(r.brand); if (r._brand) set.add(r._brand) }
  return Array.from(set)
})
// 🆕 批量把供应商/品牌/付款方式填给已勾选行（逐行选太麻烦时用）
const batchSupplier = ref<number | ''>('')
const batchBrand = ref('')
const batchPaymentMethod = ref('')
const batchPrepayRatio = ref<number | null>(null)
function applyBatchSupplier() {
  if (!batchSupplier.value) { ElMessage.info('先选一个供应商'); return }
  const t = purchasableRows.value.filter(r => r._checked)
  if (!t.length) { ElMessage.info('先勾选要设置的行'); return }
  t.forEach(r => { r._supplier_id = batchSupplier.value })
  ElMessage.success(`已把供应商填给 ${t.length} 个勾选行`)
}
function applyBatchBrand() {
  if (!batchBrand.value) { ElMessage.info('先选/输入一个品牌'); return }
  const t = purchasableRows.value.filter(r => r._checked)
  if (!t.length) { ElMessage.info('先勾选要设置的行'); return }
  t.forEach(r => { r._brand = batchBrand.value })
  ElMessage.success(`已把品牌填给 ${t.length} 个勾选行`)
}
function applyBatchPaymentMethod() {
  if (!batchPaymentMethod.value) { ElMessage.info('先选一个付款方式'); return }
  const t = purchasableRows.value.filter(r => r._checked)
  if (!t.length) { ElMessage.info('先勾选要设置的行'); return }
  t.forEach(r => { r._payment_method = batchPaymentMethod.value })
  ElMessage.success(`已把付款方式填给 ${t.length} 个勾选行`)
}
function applyBatchPrepayRatio() {
  if (batchPrepayRatio.value == null) { ElMessage.info('先填一个预付比例'); return }
  const t = purchasableRows.value.filter(r => r._checked)
  if (!t.length) { ElMessage.info('先勾选要设置的行'); return }
  t.forEach(r => { r._prepay_ratio = batchPrepayRatio.value })
  ElMessage.success(`已把预付比例填给 ${t.length} 个勾选行`)
}
const filteredPurchasable = computed(() => {
  const kw = purchasableFilter.value.trim().toLowerCase()
  return purchasableRows.value.filter(r =>
    (!onlyGap.value || r.suggest_purchase > 0) &&
    (!kw || r.item_name.toLowerCase().includes(kw) || (r.spec || '').toLowerCase().includes(kw)))
})
const listSelCount = computed(() => purchasableRows.value.filter(r => r._checked).length)
// 🆕 表头全选：作用于当前筛选出的行
const allFilteredChecked = computed(() =>
  filteredPurchasable.value.length > 0 && filteredPurchasable.value.every(r => r._checked))
const someFilteredChecked = computed(() =>
  filteredPurchasable.value.some(r => r._checked) && !allFilteredChecked.value)
function toggleAllPurchasable(v: any) {
  filteredPurchasable.value.forEach(r => { r._checked = !!v })
}
async function openListOrder(mode?: unknown) {
  listOrderMode.value = mode === 'kit' ? 'kit' : 'normal'
  if (listOrderMode.value === 'kit') {
    Object.assign(kitSet, { supplier_id: '', kit_name: '', kit_qty: 1, kit_total: null, payment_method: '', prepay_ratio: null })
  }
  Object.assign(listOrderForm, {
    project_id: '', project_code: '',
    delivery_date: new Date().toISOString().slice(0, 10),
  })
  purchasableRows.value = []; purchasableFilter.value = ''; onlyGap.value = false
  batchSupplier.value = ''; batchBrand.value = ''; batchPaymentMethod.value = ''; batchPrepayRatio.value = null; listSheet.value = myDefaultSheet.value
  try {
    const r = await http.get<any[]>('/purchase/projects', { params: { proj_status: '进行中' } })
    // 只要有任意一张「本采购员负责」的来源清单就可选（R4/A6）
    listProjects.value = r.data
      .filter(p => SHEET_TYPES.some(t => p[t.field] && sheetVisible(t.key)))
      .map(p => ({
        id: p.project_id, code: p.code, name: p.name,
        sheets: Object.fromEntries(SHEET_TYPES.map(t => [t.field, p[t.field] || null])),
      }))
  } catch { listProjects.value = [] }
  listOrderVisible.value = true
}
async function onListProjectChange() {
  const pid = listOrderForm.project_id
  listOrderForm.project_code = listProjects.value.find(x => x.id === pid)?.code || ''
  if (!pid) { purchasableRows.value = []; return }
  // 选项目后：默认选中「本采购员权限内 且 该项目有」的第一张（无则权限内第一张），然后加载
  listSheet.value = defaultSheetFor(pid)
  await loadPurchasable()
}
async function loadPurchasable() {
  const pid = listOrderForm.project_id
  if (!pid) { purchasableRows.value = []; return }
  purchasableLoading.value = true
  try {
    const r = await http.get<PurchasableRow[]>(`/purchase-mgmt/purchasable/${pid}`, { params: { sheet: listSheet.value } })
    // 默认只勾选「未下单且有缺口(建议采购>0)」的行；采购数量默认取建议采购量，避免买多
    purchasableRows.value = r.data.map(x => ({
      ...x,
      _checked: x.status === '未下单' && (x.suggest_purchase || 0) > 0,
      _price: null,
      _buyqty: x.suggest_purchase > 0 ? x.suggest_purchase : (x.qty ?? null),
      _supplier_id: '' as number | '',
      _brand: x.brand || '',
      _payment_method: '',
      _prepay_ratio: null,
    }))
  } finally { purchasableLoading.value = false }
}
function supplierName(sid: number | ''): string {
  return suppliers.value.find(s => s.id === sid)?.name || ''
}
async function submitListOrder() {
  const sel = purchasableRows.value.filter(r => r._checked)
  if (!sel.length) { ElMessage.error('请勾选要下单的清单行'); return }
  // 🆕 采购数量为0/未填的行拦下来，避免下出0数量的单
  const badQty = sel.filter(r => !((r._buyqty ?? 0) > 0))
  if (badQty.length) {
    ElMessage.error(`以下零件采购数量未填或为0：${badQty.map(b => b.item_name).slice(0, 3).join('、')}${badQty.length > 3 ? ` 等${badQty.length}行` : ''}`)
    return
  }
  // 🆕 A5：勾选了却没选供应商的行，直接拦截报错
  const noSup = sel.filter(r => !r._supplier_id)
  if (noSup.length) {
    ElMessage.error(`有 ${noSup.length} 行未选供应商：${noSup.map(b => b.item_name).slice(0, 3).join('、')}${noSup.length > 3 ? ' 等' : ''}。请逐行选好供应商再生成`)
    return
  }
  // 🆕 有勾选行正被筛选/缺口开关隐藏时，先确认再提交
  const hidden = sel.filter(r => !filteredPurchasable.value.includes(r))
  if (hidden.length) {
    try {
      await ElMessageBox.confirm(
        `有 ${hidden.length} 行已勾选的零件当前被筛选条件隐藏，也会一并下单。确认继续？`,
        '提示', { type: 'warning', confirmButtonText: '继续下单', cancelButtonText: '返回检查' })
    } catch { return }
  }
  // 🆕 A3/R3：按供应商分组，一个供应商生成一张采购单
  const groups = new Map<number, PurchasableRow[]>()
  for (const r of sel) {
    const sid = r._supplier_id as number
    if (!groups.has(sid)) groups.set(sid, [])
    groups.get(sid)!.push(r)
  }
  const supNames = Array.from(groups.keys()).map(supplierName)
  try {
    await ElMessageBox.confirm(
      `将按供应商拆成 ${groups.size} 张采购单：${supNames.join('、')}。确认生成？`,
      '按供应商拆单', { type: 'info', confirmButtonText: '生成', cancelButtonText: '取消' })
  } catch { return }
  listOrderSaving.value = true
  try {
    let firstPo: string | undefined
    for (const [sid, rows] of groups) {
      const resp = await http.post<PurchaseItemOut[]>('/purchase-mgmt/orders/from-list', {
        supplier_id: sid,
        delivery_date: listOrderForm.delivery_date || null,
        project_code: listOrderForm.project_code || null,
        lines: rows.map(r => ({
          source_sheet_id: r.sheet_id, source_record_id: r.record_id,
          item_name: r.item_name, spec: foldDrawingSpec(r), brand: r._brand || null,
          payment_method: r._payment_method || null,
          prepay_ratio: isPrepayMethod(r._payment_method) ? r._prepay_ratio : null,
          qty: r._buyqty ?? r.suggest_purchase ?? r.qty, unit_price: r._price,
        })),
      })
      if (!firstPo) firstPo = resp.data[0]?.po_no || undefined
    }
    ElMessage.success(`已按供应商生成 ${groups.size} 张采购单（共 ${sel.length} 行），已回写清单`)
    listOrderVisible.value = false
    await loadItems()
    if (groups.size === 1) offerPrint(firstPo)
  } catch { /* handled */ } finally { listOrderSaving.value = false }
}
function listStatusTag(s: string): 'info' | 'warning' | 'success' {
  return s === '已到货' ? 'success' : s === '已下单' ? 'warning' : 'info'
}

// 🆕 按套下单（从清单打包成套）：把勾选的清单行打包成「一套」= 一条成套明细，并回写清单
async function submitKitFromList() {
  const sel = purchasableRows.value.filter(r => r._checked)
  if (!sel.length) { ElMessage.error('请勾选要打包成套的清单零件'); return }
  if (!kitSet.supplier_id) { ElMessage.error('请选择供应商（一套=同一供应商）'); return }
  if (!kitSet.kit_name.trim()) { ElMessage.error('请填写套名称'); return }
  if (!kitSet.kit_qty || kitSet.kit_qty <= 0) { ElMessage.error('请填写套数(>0)'); return }
  if (kitSet.kit_total == null || kitSet.kit_total < 0) { ElMessage.error('请填写套总价'); return }
  // 勾选行被筛选隐藏时先确认（与从清单下单一致）
  const hidden = sel.filter(r => !filteredPurchasable.value.includes(r))
  if (hidden.length) {
    try {
      await ElMessageBox.confirm(
        `有 ${hidden.length} 行已勾选的零件当前被筛选隐藏，也会一并打包进这套。确认继续？`,
        '提示', { type: 'warning', confirmButtonText: '继续', cancelButtonText: '返回检查' })
    } catch { return }
  }
  listOrderSaving.value = true
  try {
    const resp = await http.post<PurchaseItemOut>('/purchase-mgmt/orders/kit-from-list', {
      supplier_id: kitSet.supplier_id,
      delivery_date: listOrderForm.delivery_date || null,
      project_code: listOrderForm.project_code || null,
      payment_method: kitSet.payment_method || null,
      prepay_ratio: isPrepayMethod(kitSet.payment_method) ? kitSet.prepay_ratio : null,
      source_sheet_id: sel[0]?.sheet_id ?? null,
      kit_name: kitSet.kit_name.trim(),
      kit_qty: kitSet.kit_qty,
      kit_total: kitSet.kit_total,
      parts: sel.map(r => ({
        source_record_id: r.record_id, name: r.item_name,
        spec: foldDrawingSpec(r), qty: r._buyqty ?? r.qty ?? null,
      })),
    })
    ElMessage.success(`成套采购单已生成（${kitSet.kit_qty} 套 · ${sel.length} 项零件），已回写清单`)
    listOrderVisible.value = false
    await loadItems()
    offerPrint(resp.data?.po_no)
  } catch { /* handled */ } finally { listOrderSaving.value = false }
}

const supplierDialogVisible = ref(false)
const editingSupplier = ref<SupplierOut | null>(null)
const supplierSaving = ref(false)
const supplierFormRef = ref<FormInstance>()
const supplierRules: FormRules = {
  name: [{ required: true, message: '请输入供应商名称', trigger: 'blur' }],
}
const supplierForm = reactive({
  name: '', code: '', category: '', contact: '', phone: '', address: '',
  tax_no: '', bank_name: '', bank_account: '', settlement_type: '', credit_days: null as number | null,
  notes: '',
})

const payReqVisible = ref(false)
const payReqSaving = ref(false)
const payReqForm = reactive({
  supplier_id: '' as number | '',
  requested_amount: 0,
  notes: '',
  items: [] as Array<{ item_id: number; item_name: string; allocated_amount: number; max: number }>,
})
// 🆕 改动任一明细的分配金额 → 请款总额自动跟着汇总（避免总额与分配对不上）
watch(() => payReqForm.items.map(i => i.allocated_amount), () => {
  if (!payReqVisible.value) return
  payReqForm.requested_amount = Number(
    payReqForm.items.reduce((s, i) => s + (i.allocated_amount || 0), 0).toFixed(2))
})

const openingBalanceVisible = ref(false)
const openingBalanceLoading = ref(false)
const openingBalanceSaving = ref(false)
const openingBalanceSupplierId = ref<number | null>(null)
const openingBalanceSupplierName = ref('')
const openingBalanceForm = reactive({ balance_date: '', outstanding_amount: 0, notes: '' })

// ===== loaders =====
async function loadSuppliers() {
  const r = await http.get<SupplierOut[]>('/purchase-mgmt/suppliers')
  suppliers.value = r.data
  // #152：采购明细「供应商」筛选下拉只列本人的供应商(受限采购员排除遗留共享;管理层仍是全部)
  const rf = await http.get<SupplierOut[]>('/purchase-mgmt/suppliers', { params: { owned_only: true } })
  filterSuppliers.value = rf.data
}

async function loadItems() {
  loading.value = true
  try {
    const fparams: Record<string, string> = {}
    if (filterSupplierId.value) fparams.supplier_id = String(filterSupplierId.value)
    if (filterProjectCode.value) fparams.project_code = filterProjectCode.value
    if (filterMonth.value) fparams.month = filterMonth.value
    if (filterInvoiceStatus.value) fparams.invoice_status = filterInvoiceStatus.value
    if (filterCategory.value) fparams.category = filterCategory.value
    if (itemSheetType.value) fparams.sheet_type = itemSheetType.value
    const [ir, sr] = await Promise.all([
      http.get<PurchaseItemOut[]>('/purchase-mgmt/items', {
        params: { ...fparams, page: String(page.value), page_size: String(pageSize.value) } }),
      http.get<ItemSummary>('/purchase-mgmt/items/summary', { params: fparams }),
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
    const [k, t, b, ts, st] = await Promise.all([
      http.get<PurchaseKPI>('/purchase-mgmt/reports/overview'),
      http.get<MonthlyPoint[]>('/purchase-mgmt/reports/monthly-trend'),
      http.get<BuyerRow[]>('/purchase-mgmt/reports/by-buyer'),
      http.get<TopSupplier[]>('/purchase-mgmt/reports/top-suppliers'),
      http.get<SupplierTrend>('/purchase-mgmt/reports/supplier-trend', { params: { months: 12, top: 5 } }),
    ])
    kpi.value = k.data
    monthlyTrend.value = t.data
    byBuyer.value = b.data
    topSuppliers.value = ts.data
    supplierTrend.value = st.data
  } finally { loading.value = false }
}

async function loadProjectReport() {
  const r = await http.get<{ project_code: string; amount: number; count: number }[]>(
    '/purchase-mgmt/reports/by-project',
    { params: projectSearch.value ? { q: projectSearch.value } : {} }
  )
  byProject.value = r.data
}

// 🆕 需求十二：图表数据源（折线/曲线）
const trendChart = computed(() => ({
  labels: monthlyTrend.value.map(m => m.month.slice(2)),
  series: [
    { name: '收货金额', points: monthlyTrend.value.map(m => m.amount) },
    { name: '已付款', points: monthlyTrend.value.map(m => m.paid), color: '#16a34a' },
  ] as { name: string; points: (number | null)[]; color?: string }[],
}))
const supTrendChart = computed(() => ({
  labels: supplierTrend.value.months.map(m => m.slice(2)),
  series: supplierTrend.value.series.map(s => ({ name: s.supplier_name, points: s.points as (number | null)[] })),
}))

onMounted(async () => {
  await Promise.all([loadSuppliers(), loadCustomFields(), loadMatDict()])
  if (showPurchaseTab.value) {
    await loadPurchaseRows()
    loadIncomingReqs()   // #167：加载采购申请以显示角标
  } else {
    await loadItems()
  }
})

async function onTabChange(name: string) {
  if (name === 'purchase') await loadPurchaseRows()
  else if (name === 'items') await loadItems()
  else if (name === 'statements') { await loadSuppliers(); await loadStatements() }
  else if (name === 'payreq') await loadPayReqs()
  else if (name === 'preq') await loadIncomingReqs()
  else if (name === 'reports' && (isLeadOrAbove.value || canWrite.value)) { await loadReports(); await loadProjectReport() }
}

// ===== item CRUD =====
function openNewItem() {
  editingItem.value = null
  Object.assign(itemForm, {
    supplier_id: '', delivery_date: new Date().toISOString().slice(0, 10), contract_no: '', project_code: '',
    delivery_note_no: '', item_name: '', spec: '', brand: '', qty: null, unit_price: null,
    received_amount: 0, invoice_date: '', tax_rate: '', invoice_amount: 0,
    payment_method: '', prepay_ratio: null, invoice_status: '待对账', notes: '',
    custom_values: {},
  })
  itemDialogVisible.value = true
}

function openEditItem(row: PurchaseItemOut) {
  editingItem.value = row
  Object.assign(itemForm, {
    supplier_id: row.supplier_id, delivery_date: row.delivery_date || '',
    contract_no: row.contract_no || '', project_code: row.project_code || '',
    delivery_note_no: row.delivery_note_no || '', item_name: row.item_name,
    spec: row.spec || '', brand: row.brand || '', qty: row.qty, unit_price: row.unit_price,
    received_amount: row.received_amount, invoice_date: row.invoice_date || '',
    tax_rate: row.tax_rate || '', invoice_amount: row.invoice_amount,
    payment_method: row.payment_method || '', prepay_ratio: row.prepay_ratio ?? null,
    invoice_status: row.invoice_status, notes: row.notes || '',
    custom_values: { ...(row.custom_values || {}) },
  })
  itemDialogVisible.value = true
}

async function saveItem() {
  try { await itemFormRef.value?.validate() } catch { return }
  itemSaving.value = true
  try {
    const payload = {
      supplier_id: itemForm.supplier_id,
      delivery_date: itemForm.delivery_date || null,
      contract_no: itemForm.contract_no || null,
      project_code: itemForm.project_code || null,
      delivery_note_no: itemForm.delivery_note_no || null,
      item_name: itemForm.item_name,
      spec: itemForm.spec || null,
      brand: itemForm.brand || null,
      qty: itemForm.qty,
      unit_price: itemForm.unit_price,
      received_amount: itemForm.received_amount,
      invoice_date: itemForm.invoice_date || null,
      tax_rate: itemForm.tax_rate || null,
      invoice_amount: itemForm.invoice_amount,
      payment_method: itemForm.payment_method || null,
      prepay_ratio: isPrepayMethod(itemForm.payment_method) ? itemForm.prepay_ratio : null,
      invoice_status: itemForm.invoice_status,
      notes: itemForm.notes || null,
      custom_values: itemForm.custom_values,
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
  } catch { /* handled by axios interceptor */ } finally { itemSaving.value = false }
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
  const leaves = selLeaves.value   // 🆕 需求九：合并父行已展开成子明细
  if (!leaves.length) { ElMessage.warning('请先勾选明细行（可勾选合并行汇总请款）'); return }
  const firstSid = leaves[0].supplier_id
  if (!leaves.every(i => i.supplier_id === firstSid)) {
    ElMessage.error('请款单只能关联同一供应商的明细')
    return
  }
  payReqForm.supplier_id = firstSid
  payReqForm.requested_amount = leaves.reduce((s, i) => s + (i.received_amount - i.paid_amount), 0)
  payReqForm.notes = ''
  payReqForm.items = leaves.map(i => ({
    item_id: i.id,
    item_name: i.item_name,
    allocated_amount: i.received_amount - i.paid_amount,
    max: i.received_amount - i.paid_amount,
  }))
  payReqVisible.value = true
}

async function submitPaymentRequest() {
  if (!payReqForm.requested_amount || payReqForm.requested_amount <= 0) {
    ElMessage.error('请款金额必须大于0')
    return
  }
  payReqSaving.value = true
  try {
    await http.post('/purchase-mgmt/payment-requests', {
      supplier_id: payReqForm.supplier_id,
      requested_amount: payReqForm.requested_amount,
      notes: payReqForm.notes || null,
      items: payReqForm.items.map(i => ({ item_id: i.item_id, allocated_amount: i.allocated_amount })),
    })
    ElMessage.success('请款单已提交，等待财务审批')
    payReqVisible.value = false
    clearSelection()
  } catch { /* handled */ } finally { payReqSaving.value = false }
}

// 🆕 需求十三：批量维护开票号（对多个零件统一维护同一开票号，开票金额=收货金额）
const invoiceNoDialogVisible = ref(false)
const invoiceNoForm = reactive({ invoice_no: '', invoice_date: '' })
const invoiceNoSaving = ref(false)
function openBatchInvoiceNo() {
  const leaves = selLeaves.value
  if (!leaves.length) { ElMessage.warning('请先勾选要维护开票号的明细（可勾选合并行）'); return }
  // #170：一个开票号=一张发票=一个供应商，禁止跨供应商批量盖同号（与请款一致）
  if (!selSameSupplier.value) { ElMessage.error('跨供应商不能一起维护开票号（一个开票号=一张发票=一个供应商），请只勾选同一供应商的明细'); return }
  // #154：仅当所勾选明细已是同一个开票号时才预填(编辑场景)，否则一律留空，避免残留上次输入
  const nos = new Set(leaves.map(i => i.invoice_no).filter(Boolean))
  invoiceNoForm.invoice_no = nos.size === 1 ? String([...nos][0]) : ''
  invoiceNoForm.invoice_date = ''
  invoiceNoDialogVisible.value = true
}
async function submitBatchInvoiceNo() {
  if (!invoiceNoForm.invoice_no.trim()) { ElMessage.warning('请填写开票号'); return }
  const ids = selLeaves.value.map(i => i.id)
  invoiceNoSaving.value = true
  try {
    const r = await http.post<{ updated: number }>('/purchase-mgmt/items/set-invoice-no', {
      item_ids: ids, invoice_no: invoiceNoForm.invoice_no.trim(),
      invoice_date: invoiceNoForm.invoice_date || null,
    })
    ElMessage.success(`已对 ${r.data.updated} 条明细维护开票号（开票金额=收货金额）`)
    invoiceNoDialogVisible.value = false
    clearSelection()
    await loadItems()
  } catch { /* handled */ } finally { invoiceNoSaving.value = false }
}

// 🆕 #4 合并父行「整单维护」：开票金额/已付款作为整单总额(不分摊,记在首行)，对账状态套所有子行
const groupSumVisible = ref(false)
const groupSumSaving = ref(false)
const groupSumRow = ref<any>(null)
const groupSumForm = reactive({ invoice_amount: null as number | null, paid_amount: null as number | null, paid_date: '', invoice_status: '' })
function openGroupSummary(row: any) {
  groupSumRow.value = row
  groupSumForm.invoice_amount = row.invoice_amount || null
  groupSumForm.paid_amount = row.paid_amount || null
  groupSumForm.paid_date = ''
  groupSumForm.invoice_status = ''
  groupSumVisible.value = true
}
async function submitGroupSummary() {
  const row = groupSumRow.value
  if (!row?.children?.length) return
  groupSumSaving.value = true
  try {
    const r = await http.post<{ updated: number }>('/purchase-mgmt/items/set-group-summary', {
      item_ids: row.children.map((c: any) => c.id),
      invoice_amount: groupSumForm.invoice_amount,
      paid_amount: groupSumForm.paid_amount,
      paid_date: groupSumForm.paid_date || null,
      invoice_status: groupSumForm.invoice_status || null,
    })
    ElMessage.success(`整单维护完成（${r.data.updated} 条零件）`)
    groupSumVisible.value = false
    await loadItems()
  } catch { /* handled */ } finally { groupSumSaving.value = false }
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
  try { await supplierFormRef.value?.validate() } catch { return }
  supplierSaving.value = true
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
  } catch { /* handled */ } finally { supplierSaving.value = false }
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
  openingBalanceLoading.value = true
  http.get<{ balance_date: string; outstanding_amount: number; notes?: string }>(
    `/purchase-mgmt/suppliers/${row.supplier_id}/opening-balance`
  ).then(r => {
    if (r.data) {
      openingBalanceForm.balance_date = r.data.balance_date || ''
      openingBalanceForm.outstanding_amount = r.data.outstanding_amount
      openingBalanceForm.notes = r.data.notes || ''
    }
  }).catch(() => {}).finally(() => { openingBalanceLoading.value = false })
}

async function saveOpeningBalance() {
  if (!openingBalanceForm.balance_date) { ElMessage.error('截止日期为必填项'); return }
  openingBalanceSaving.value = true
  try {
    await http.post(`/purchase-mgmt/suppliers/${openingBalanceSupplierId.value}/opening-balance`, {
      balance_date: openingBalanceForm.balance_date,
      outstanding_amount: openingBalanceForm.outstanding_amount,
      notes: openingBalanceForm.notes || null,
    })
    ElMessage.success('期初余额已保存')
    openingBalanceVisible.value = false
    await loadStatements()
  } catch { /* handled */ } finally { openingBalanceSaving.value = false }
}

// 🆕 供应商账目合计行（列对齐：供应商/分类/状态/期初/收货/开票/待开票/已付/欠款/明细数/操作）
function stmtSummary() {
  const rows = filteredStatementRows.value
  const sum = (k: keyof SupplierStatementRow) => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0)
  return ['合计', '', '',
    fmtMoney(sum('opening_balance')), fmtMoney(sum('received_total')),
    fmtMoney(sum('invoice_total')), fmtMoney(sum('uninvoiced')),
    fmtMoney(sum('paid_total')), fmtMoney(sum('outstanding')),
    String(rows.reduce((a, r) => a + (r.item_count || 0), 0)), '']
}

// 🆕 请款记录页签：采购员能看到自己请款单的审批状态/驳回原因（后端已按角色过滤：普通采购员只看自己的）
const payReqs = ref<PaymentRequestOut[]>([])
const prLoading = ref(false)
const prStatusFilter = ref('')   // #165：改成显示条(客户端筛选),'' = 全部
async function loadPayReqs() {
  prLoading.value = true
  try {
    // #165：一次拉全，状态改成横向显示条+计数，客户端筛选
    payReqs.value = (await http.get<PaymentRequestOut[]>('/purchase-mgmt/payment-requests')).data
  } finally { prLoading.value = false }
}

// 🆕 #167 采购申请处理（仓库提 → 采购部处理/驳回）
interface IncomingReq { id: number; status: string; notes?: string | null; created_at: string
  requester_name?: string | null; buyer_name?: string | null; handler_name?: string | null; reject_reason?: string | null
  lines: { item_name: string; spec?: string | null; qty?: number | null; project_code?: string | null; notes?: string | null }[] }
const PREQ_STATUS: Record<string, string> = { pending: '待处理', done: '已处理', rejected: '已驳回' }
const incomingReqs = ref<IncomingReq[]>([])
const incomingLoading = ref(false)
async function loadIncomingReqs() {
  incomingLoading.value = true
  try { incomingReqs.value = (await http.get<IncomingReq[]>('/purchase-mgmt/purchase-requests')).data }
  finally { incomingLoading.value = false }
}
const incomingPending = computed(() => incomingReqs.value.filter(r => r.status === 'pending').length)
async function handleIncoming(row: IncomingReq) {
  try { await ElMessageBox.confirm(`把采购申请 #${row.id} 标记为「已处理」（已按此下单）？`, '处理确认', { type: 'info' }) } catch { return }
  await http.put(`/purchase-mgmt/purchase-requests/${row.id}/handle`)
  ElMessage.success('已标记处理')
  await loadIncomingReqs()
}
async function rejectIncoming(row: IncomingReq) {
  let reason = ''
  try { reason = (await ElMessageBox.prompt('驳回原因', `驳回采购申请 #${row.id}`, { inputPlaceholder: '填写驳回原因' })).value } catch { return }
  await http.put(`/purchase-mgmt/purchase-requests/${row.id}/reject`, { reason })
  ElMessage.success('已驳回')
  await loadIncomingReqs()
}
function preqStatusTag(s: string): 'warning' | 'success' | 'danger' | 'info' {
  return s === 'done' ? 'success' : s === 'rejected' ? 'danger' : 'warning'
}
const prCounts = computed(() => {
  const c: Record<string, number> = { '': payReqs.value.length, pending: 0, approved: 0, paid: 0, rejected: 0 }
  for (const r of payReqs.value) c[r.status] = (c[r.status] || 0) + 1
  return c
})
const filteredPayReqs = computed(() =>
  prStatusFilter.value ? payReqs.value.filter(r => r.status === prStatusFilter.value) : payReqs.value)

// 🆕 报表小表合计
function trendSummary() {
  const a = monthlyTrend.value.reduce((s, r) => s + (r.amount || 0), 0)
  const p = monthlyTrend.value.reduce((s, r) => s + (r.paid || 0), 0)
  return ['合计', fmtMoney(a), fmtMoney(p), fmtMoney(a - p)]
}
function buyerSummary() {
  return ['合计',
    fmtMoney(byBuyer.value.reduce((s, r) => s + (r.amount || 0), 0)),
    String(byBuyer.value.reduce((s, r) => s + (r.count || 0), 0))]
}

// 🆕 C6：导出某供应商采购对账单（Excel）
async function exportSupplierStatement(supplierId: number, supplierName: string) {
  try {
    const res = await http.get(`/purchase-mgmt/statements/${supplierId}/export`, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url; a.download = `${supplierName}_采购对账单.xlsx`
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { ElMessage.error('导出失败') }
}

// ===== statement drawer =====
async function openDrawer(row: SupplierStatementRow) {
  drawerSupplier.value = row
  drawerItems.value = []   // 清掉上一个供应商的数据，避免切换时残影
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
// #163/#169：钱和票都对上（开票金额≥收货 且 已付≥收货）→ 对账状态显示「已清」
function isCleared(row: any): boolean {
  const r = row.received_amount || 0
  return r > 0 && (row.invoice_amount || 0) >= r - 0.005 && (row.paid_amount || 0) >= r - 0.005
}
// 对账状态只表达对账口径：待对账 / 已对账 / 已清；「已开票」不算对账状态，归为「已对账」（用户要求去掉已开票）
function reconcileText(row: any): string {
  if (isCleared(row)) return '已清'
  return row.invoice_status === '已开票' ? '已对账' : row.invoice_status
}
function reconcileTag(row: any) { return isCleared(row) ? 'success' : statusTag(reconcileText(row)) }

function prStatusTag(s: string) {
  if (s === 'paid') return 'success'
  if (s === 'approved') return 'warning'
  if (s === 'rejected') return 'danger'
  return 'info'
}
// 🆕 采购明细付款状态标签（B1=a）
function payStatusTag(s?: string): 'success' | 'warning' | 'info' | 'primary' | 'danger' {
  if (s === '已付款') return 'success'
  if (s === '部分付款') return 'warning'
  if (s === '已批待付') return 'primary'
  if (s === '已请款') return 'info'
  return 'danger'   // 未付款
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

          <el-table :data="purchaseRows" stripe v-loading="purchaseLoading" max-height="max(320px, calc(100vh - 310px))" :scrollbar-always-on="true" class="wrap-cells">
            <el-table-column type="index" label="#" width="50" fixed />
            <el-table-column label="项目编号" width="116" fixed>
              <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
            </el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="190" />
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
            <template #empty><EmptyHint text="暂无项目" size="sm" /></template>
          </el-table>
        </el-tab-pane>

        <!-- ==================== 🆕 #167 采购申请（仓库提 → 采购部处理）==================== -->
        <el-tab-pane v-if="showPurchaseTab" name="preq" lazy>
          <template #label>📥 采购申请<span v-if="incomingPending">（{{ incomingPending }}）</span></template>
          <div class="filter-bar">
            <el-button :icon="Refresh" size="small" @click="loadIncomingReqs">刷新</el-button>
            <span class="muted small">仓库提交的采购申请汇到这里。核对后「已处理」（表示已按此下单）或「驳回」；仓库会收到通知。</span>
          </div>
          <el-table :data="incomingReqs" v-loading="incomingLoading" stripe size="small" max-height="max(320px, calc(100vh - 300px))" :scrollbar-always-on="true" class="wrap-cells">
            <el-table-column type="expand" width="36">
              <template #default="{ row }">
                <el-table :data="row.lines" size="small" border style="margin:6px 12px">
                  <el-table-column type="index" label="#" width="44" />
                  <el-table-column label="名称" prop="item_name" min-width="140" />
                  <el-table-column label="规格" min-width="120"><template #default="{ row: l }">{{ l.spec || '—' }}</template></el-table-column>
                  <el-table-column label="数量" width="90" align="right"><template #default="{ row: l }">{{ l.qty ?? '—' }}</template></el-table-column>
                  <el-table-column label="项目" width="110"><template #default="{ row: l }">{{ l.project_code || '—' }}</template></el-table-column>
                  <el-table-column label="备注" min-width="120"><template #default="{ row: l }">{{ l.notes || '—' }}</template></el-table-column>
                </el-table>
              </template>
            </el-table-column>
            <el-table-column label="申请编号" width="90"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
            <el-table-column label="申请人" width="90"><template #default="{ row }">{{ row.requester_name || '—' }}</template></el-table-column>
            <el-table-column label="指定采购员" width="100"><template #default="{ row }"><b v-if="row.buyer_name">{{ row.buyer_name }}</b><span v-else class="muted small">未指定</span></template></el-table-column>
            <el-table-column label="物料" min-width="220"><template #default="{ row }">{{ row.lines.map((l: any) => l.item_name).slice(0, 3).join('、') }}{{ row.lines.length > 3 ? ` 等${row.lines.length}项` : '' }}</template></el-table-column>
            <el-table-column label="状态" width="90" align="center"><template #default="{ row }"><el-tag :type="preqStatusTag(row.status)" size="small">{{ PREQ_STATUS[row.status] || row.status }}</el-tag></template></el-table-column>
            <el-table-column label="提交时间" width="110"><template #default="{ row }">{{ (row.created_at || '').slice(0, 10) }}</template></el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <template v-if="row.status === 'pending' && canWrite">
                  <el-button size="small" type="primary" @click="handleIncoming(row)">已处理</el-button>
                  <el-button size="small" type="danger" link @click="rejectIncoming(row)">驳回</el-button>
                </template>
                <span v-else-if="row.status === 'done'" class="muted small">{{ row.handler_name }} 已处理</span>
                <span v-else-if="row.status === 'rejected'" class="danger small" :title="row.reject_reason || ''">已驳回</span>
              </template>
            </el-table-column>
            <template #empty><EmptyHint text="暂无采购申请（仓库在仓库页「采购申请」tab 提交）" size="sm" /></template>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Tab 1: 采购明细 ==================== -->
        <el-tab-pane label="📦 采购明细" name="items" lazy>
          <div class="filter-bar">
            <el-select v-model="filterSupplierId" placeholder="全部供应商" clearable filterable style="width:160px" @change="onFilterChange">
              <el-option v-for="s in filterSuppliers" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
            <el-input v-model="filterProjectCode" placeholder="项目编号" clearable :prefix-icon="Search"
                      style="width:140px" @change="onFilterChange" />
            <el-date-picker v-model="filterMonth" type="month" placeholder="下单月份" value-format="YYYY-MM"
                            clearable style="width:130px" @change="onFilterChange" />
            <el-select v-model="filterInvoiceStatus" placeholder="对账状态" clearable style="width:110px" @change="onFilterChange">
              <el-option label="待对账" value="待对账" />
              <el-option label="已对账" value="已对账" />
            </el-select>
            <el-select v-model="filterCategory" placeholder="供应商分类" clearable style="width:120px" @change="onFilterChange">
              <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
            </el-select>
            <el-tooltip content="刷新" placement="top">
              <el-button :icon="Refresh" @click="loadItems" />
            </el-tooltip>
            <el-button v-if="hasFilter" link type="info" :icon="RefreshLeft" @click="resetFilters">清空筛选</el-button>
            <span class="flex-spacer" />
            <template v-if="canWrite">
              <el-button type="primary" :icon="Tickets" @click="openListOrder">从清单下单</el-button>
              <el-button :icon="Plus" @click="openNewOrder">新建采购单</el-button>
              <el-button :icon="Box" @click="openListOrder('kit')">按套下单</el-button>
              <!-- 🆕 「单条明细」按用户要求隐藏（openNewItem 及弹窗保留，需要时恢复本按钮即可） -->
              <el-dropdown trigger="click">
                <el-button :loading="importing">
                  更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item :icon="Upload" @click="importItems">导入历史数据</el-dropdown-item>
                    <el-dropdown-item :icon="Download" @click="downloadImportTemplate">下载导入模板</el-dropdown-item>
                    <el-dropdown-item v-if="canConfigFields" :icon="Setting" divided @click="openFieldManager">自定义字段设置</el-dropdown-item>
                    <el-dropdown-item v-if="canConfigFields" :icon="Collection" @click="openMatDictManager">字典设置</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </div>

          <!-- 🆕 勾选后浮出操作条：金额一目了然，请款入口就近 -->
          <div v-if="selectedItems.length" class="sel-bar">
            <span>已选 <b>{{ selLeaves.length }}</b> 条明细</span>
            <span>未付合计 <b class="amt">{{ fmtMoney(selUnpaidTotal) }}</b></span>
            <span v-if="!selSameSupplier" class="warn">跨供应商不能一起请款/维护开票号，请选择同一供应商的明细</span>
            <el-button size="small" type="warning" :disabled="!selSameSupplier" @click="openPaymentRequest">发起请款</el-button>
            <el-button size="small" type="primary" plain :disabled="!selSameSupplier" @click="openBatchInvoiceNo">维护开票号</el-button>
            <el-button size="small" link @click="clearSelection">取消选择</el-button>
          </div>

          <!-- 🆕 ④ 按清单类型分二级 tab（列标签随类型变；散单=无来源清单）-->
          <el-tabs v-model="itemSheetType" @tab-change="onItemSheetTab" class="pending-subtabs" style="margin-bottom:6px">
            <el-tab-pane label="全部" name="" />
            <el-tab-pane v-for="t in availableSheets" :key="t.key" :label="sheetMeta(t.key).label" :name="t.key" />
            <el-tab-pane label="散单" name="loose" />
          </el-tabs>

          <el-table
            ref="itemsTableRef"
            :data="groupedItems" stripe
            :row-key="rowKey" :tree-props="{ children: 'children' }"
            @selection-change="(v: PurchaseItemOut[]) => selectedItems = v"
            max-height="max(320px, calc(100vh - 340px))"
            :scrollbar-always-on="true"
            class="wrap-cells compact-tbl"
          >
            <!-- 🆕 需求九：合并父行也可勾选（汇总请款/维护开票号会自动展开成子明细） -->
            <el-table-column v-if="canWrite" type="selection" width="40" fixed />
            <el-table-column label="供应商" min-width="170" fixed>
              <template #default="{ row }"><span class="sup-name">{{ row.supplier_name }}</span></template>
            </el-table-column>
            <el-table-column prop="po_no" label="采购单号" width="170">
              <template #default="{ row }">
                <template v-if="row.po_no">
                  <el-tooltip content="点击打印预览" placement="top">
                    <el-button link type="primary" class="po-no" @click="printPO(row.po_no)">{{ row.po_no }}</el-button>
                  </el-tooltip>
                  <el-tooltip content="下载PDF（移动端/微信推荐）" placement="top">
                    <el-button link type="success" size="small" @click="downloadPoPdf(row.po_no)">⬇PDF</el-button>
                  </el-tooltip>
                </template>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="delivery_date" label="下单日期" width="98" sortable />
            <el-table-column prop="project_code" label="项目编号" width="92">
              <template #default="{ row }"><b class="code">{{ row.project_code || '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="delivery_note_no" label="送货单号" width="106">
              <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
            </el-table-column>
            <el-table-column prop="arrival_date" label="到货日期" width="98" sortable>
              <template #default="{ row }">
                <span v-if="row.arrival_date">{{ row.arrival_date }}</span>
                <el-tag v-else-if="!row._isGroup" size="small" type="info" effect="plain">待收货</el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="item_name" :label="itemSheetType && itemSheetType !== 'loose' ? sheetMeta(itemSheetType).nameLabel : '名称'" min-width="130">
              <template #default="{ row }">
                <b v-if="row._isGroup">共 {{ row._count }} 个零件</b>
                <template v-else>
                  <el-popover v-if="row.is_kit && row.kit_parts?.length" placement="right" :width="360" trigger="hover">
                    <template #reference>
                      <span><el-tag size="small" type="success" effect="dark" style="margin-right:4px">套</el-tag>{{ row.item_name }}</span>
                    </template>
                    <div class="small" style="font-weight:600;margin-bottom:6px">套内零件清单（每套 · 共 {{ row.kit_parts.length }} 项）</div>
                    <el-table :data="row.kit_parts" size="small" border max-height="300">
                      <el-table-column type="index" label="#" width="40" />
                      <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
                      <el-table-column prop="spec" label="规格/图纸" min-width="110" show-overflow-tooltip><template #default="{ row: p }">{{ p.spec || '—' }}</template></el-table-column>
                      <el-table-column label="每套" width="64" align="right"><template #default="{ row: p }">{{ p.qty ?? '—' }}</template></el-table-column>
                    </el-table>
                  </el-popover>
                  <span v-else>
                    <el-tag v-if="row.is_kit" size="small" type="success" effect="dark" style="margin-right:4px">套</el-tag>{{ row.item_name }}
                  </span>
                </template>
              </template>
            </el-table-column>
            <el-table-column prop="spec" :label="itemSheetType && itemSheetType !== 'loose' ? sheetMeta(itemSheetType).specLabel : '规格'" min-width="120">
              <template #default="{ row }">{{ row.spec || '—' }}</template>
            </el-table-column>
            <el-table-column prop="brand" label="品牌" width="88">
              <template #default="{ row }">{{ row.brand || '—' }}</template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" width="68" align="right">
              <template #default="{ row }">{{ row.qty == null ? '—' : (row.is_kit ? `${row.qty} 套` : row.qty) }}</template>
            </el-table-column>
            <el-table-column label="单价" width="96" align="right">
              <template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="received_amount" label="收货金额" width="116" align="right" sortable>
              <template #default="{ row }"><b>{{ fmtMoney(row.received_amount) }}</b></template>
            </el-table-column>
            <el-table-column prop="invoice_date" label="开票日期" width="104">
              <template #default="{ row }">{{ row.invoice_date || '—' }}</template>
            </el-table-column>
            <el-table-column prop="invoice_no" label="开票号" width="150">
              <template #default="{ row }">{{ row.invoice_no || '—' }}</template>
            </el-table-column>
            <el-table-column prop="invoice_amount" label="开票金额" width="116" align="right" sortable>
              <template #default="{ row }">{{ row.invoice_amount ? fmtMoney(row.invoice_amount) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="paid_amount" label="已付款" width="116" align="right" sortable>
              <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
            </el-table-column>
            <el-table-column label="付款状态" width="84" align="center">
              <template #default="{ row }">
                <el-tag v-if="!row._isGroup" :type="payStatusTag(row.pay_status)" size="small" effect="light">{{ row.pay_status || '未付款' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="对账状态" width="76">
              <template #default="{ row }">
                <el-tag v-if="!row._isGroup" :type="reconcileTag(row)" size="small">{{ reconcileText(row) }}</el-tag>
              </template>
            </el-table-column>
            <!-- 🆕 R6 自定义列 -->
            <el-table-column v-for="f in listCustomFields" :key="f.id" :label="f.label" min-width="100">
              <template #default="{ row }">{{ cfDisplay(row.custom_values, f) }}</template>
            </el-table-column>
            <el-table-column v-if="isLeadOrAbove" prop="buyer_name" label="采购员" width="80">
              <template #default="{ row }">{{ row.buyer_name || '—' }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="120" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <template v-if="!row._isGroup">
                  <el-button size="small" link type="primary" @click="openEditItem(row)">编辑</el-button>
                  <el-button size="small" link type="danger" @click="deleteItem(row)">删除</el-button>
                </template>
                <el-button v-else size="small" link type="primary" @click="openGroupSummary(row)">整单维护</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <EmptyHint :text="hasFilter ? '没有匹配的采购明细，试试清空筛选' : '暂无采购明细，点右上角「新建采购单」开始'" size="sm" />
            </template>
          </el-table>

          <div v-if="itemSummary && items.length" class="summary-bar">
            <span>共 <b>{{ itemSummary.count }}</b> 条</span>
            <span>收货合计 <b class="amt">{{ fmtMoney(itemSummary.received_total) }}</b></span>
            <span>待开票 <b class="amt warn">{{ fmtMoney(itemSummary.uninvoiced) }}</b></span>
            <span>已付款 <b class="amt">{{ fmtMoney(itemSummary.paid_total) }}</b></span>
            <span>欠款 <b class="amt danger">{{ fmtMoney(itemSummary.outstanding) }}</b></span>
          </div>
          <div class="pager-bar">
            <el-pagination background layout="total, sizes, prev, pager, next, jumper"
              :total="itemSummary?.count || 0" v-model:current-page="page" v-model:page-size="pageSize"
              :page-sizes="[50, 100, 200, 500]"
              @current-change="loadItems" @size-change="onFilterChange" />
          </div>
        </el-tab-pane>

        <!-- ==================== Tab 2: 供应商账目一览 ==================== -->
        <el-tab-pane label="📊 供应商账目" name="statements" lazy>
          <div class="filter-bar">
            <el-input v-model="stmtNameFilter" placeholder="搜索供应商名称" clearable :prefix-icon="Search" style="width:200px" />
            <el-select v-model="stmtCatFilter" placeholder="全部分类" clearable style="width:140px">
              <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
            </el-select>
            <el-tooltip content="刷新" placement="top">
              <el-button :icon="Refresh" @click="loadStatements" />
            </el-tooltip>
            <span class="flex-spacer" />
            <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openNewSupplier">新增供应商</el-button>
          </div>
          <el-table
            :data="filteredStatementRows"
            stripe show-summary
            :summary-method="stmtSummary"
            :default-sort="{ prop: 'outstanding', order: 'descending' }"
            max-height="max(320px, calc(100vh - 280px))"
            :scrollbar-always-on="true"
            class="wrap-cells compact-tbl"
          >
            <el-table-column prop="supplier_name" label="供应商" min-width="200" fixed sortable />
            <el-table-column prop="category" label="分类" width="92">
              <template #default="{ row }">{{ row.category || '—' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="76">
              <template #default="{ row }">
                <el-tag :type="supActive(row.supplier_id) ? 'success' : 'info'" size="small">
                  {{ supActive(row.supplier_id) ? '启用' : '停用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="opening_balance" label="期初欠款" width="118" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.opening_balance) }}</template>
            </el-table-column>
            <el-table-column prop="received_total" label="收货合计" width="122" align="right" sortable>
              <template #default="{ row }"><b>{{ fmtMoney(row.received_total) }}</b></template>
            </el-table-column>
            <el-table-column prop="invoice_total" label="开票合计" width="122" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.invoice_total) }}</template>
            </el-table-column>
            <el-table-column prop="uninvoiced" label="待开票" width="114" align="right" sortable>
              <template #default="{ row }"><span class="warn">{{ fmtMoney(row.uninvoiced) }}</span></template>
            </el-table-column>
            <el-table-column prop="paid_total" label="已付款" width="114" align="right" sortable>
              <template #default="{ row }">{{ fmtMoney(row.paid_total) }}</template>
            </el-table-column>
            <el-table-column prop="outstanding" label="欠款余额" width="122" align="right" sortable>
              <template #default="{ row }"><b class="danger">{{ fmtMoney(row.outstanding) }}</b></template>
            </el-table-column>
            <el-table-column label="明细数" width="72" align="center">
              <template #default="{ row }">{{ row.item_count }}</template>
            </el-table-column>
            <el-table-column label="操作" :width="canWrite ? 210 : 100" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openDrawer(row)">查看明细</el-button>
                <template v-if="canWrite">
                  <el-button size="small" link @click="onSupCmd('edit', row)">编辑</el-button>
                  <el-dropdown trigger="click" @command="(cmd: string) => onSupCmd(cmd, row)">
                    <el-button size="small" link>
                      更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon>
                    </el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="export">导出对账单</el-dropdown-item>
                        <el-dropdown-item command="balance">期初余额</el-dropdown-item>
                        <el-dropdown-item command="toggle">{{ supActive(row.supplier_id) ? '停用' : '启用' }}</el-dropdown-item>
                        <el-dropdown-item command="delete" divided><span class="danger">删除供应商</span></el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </template>
              </template>
            </el-table-column>
            <template #empty>
              <EmptyHint :text="stmtNameFilter || stmtCatFilter ? '没有匹配的供应商，试试清空筛选' : '暂无供应商，点右上角「新增供应商」开始'" size="sm" />
            </template>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Tab: 请款记录（采购员跟进审批进度） ==================== -->
        <el-tab-pane v-if="showPurchaseTab" label="💳 请款记录" name="payreq" lazy>
          <div class="filter-bar">
            <el-radio-group v-model="prStatusFilter" size="small">
              <el-radio-button value="">全部 ({{ prCounts[''] }})</el-radio-button>
              <el-radio-button value="pending">待审 ({{ prCounts.pending }})</el-radio-button>
              <el-radio-button value="approved">已批 ({{ prCounts.approved }})</el-radio-button>
              <el-radio-button value="paid">已付 ({{ prCounts.paid }})</el-radio-button>
              <el-radio-button value="rejected">已驳 ({{ prCounts.rejected }})</el-radio-button>
            </el-radio-group>
            <el-tooltip content="刷新" placement="top">
              <el-button :icon="Refresh" @click="loadPayReqs" />
            </el-tooltip>
            <span class="muted">发起请款后在这里跟进财务审批进度，被驳回会显示原因；点行首箭头看关联明细</span>
          </div>
          <el-table :data="filteredPayReqs" stripe v-loading="prLoading"
                    max-height="max(320px, calc(100vh - 300px))" :scrollbar-always-on="true" class="wrap-cells compact-tbl">
            <el-table-column type="expand" width="36">
              <template #default="{ row }">
                <div class="pr-expand">
                  <div v-for="it in row.items" :key="it.item_id" class="pr-expand-row">
                    <span>{{ it.item_name }}</span><b>{{ fmtMoney(it.allocated_amount) }}</b>
                  </div>
                  <div v-if="!row.items?.length" class="muted">无关联明细</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="申请时间" width="108">
              <template #default="{ row }">{{ (row.created_at || '').slice(0, 10) }}</template>
            </el-table-column>
            <el-table-column prop="supplier_name" label="供应商" min-width="170" />
            <el-table-column prop="requested_amount" label="请款金额" width="130" align="right" sortable>
              <template #default="{ row }"><b>{{ fmtMoney(row.requested_amount) }}</b></template>
            </el-table-column>
            <el-table-column label="状态" width="82" align="center">
              <template #default="{ row }">
                <el-tag :type="prStatusTag(row.status)" size="small">{{ PR_STATUS_LABEL[row.status] || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="驳回原因" min-width="130">
              <template #default="{ row }"><span :class="{ danger: !!row.reject_reason }">{{ row.reject_reason || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="实付金额" width="118" align="right">
              <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
            </el-table-column>
            <el-table-column prop="paid_date" label="付款日期" width="102">
              <template #default="{ row }">{{ row.paid_date || '—' }}</template>
            </el-table-column>
            <el-table-column v-if="isLeadOrAbove" prop="requester_name" label="申请人" width="88">
              <template #default="{ row }">{{ row.requester_name || '—' }}</template>
            </el-table-column>
            <el-table-column prop="notes" label="备注" min-width="110">
              <template #default="{ row }">{{ row.notes || '—' }}</template>
            </el-table-column>
            <template #empty><EmptyHint text="暂无请款记录：在采购明细勾选行后点「发起请款」" size="sm" /></template>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Tab 3: 汇总报表（需求五：采购员也可见，数据按本人隔离）==================== -->
        <el-tab-pane v-if="isLeadOrAbove || canWrite" label="📈 汇总报表" name="reports" lazy>
          <div v-if="kpi" class="kpi-grid" style="margin-bottom:16px">
            <div class="kpi is-primary">
              <div class="kpi-v">{{ fmtMoney(kpi.month_amount) }}</div>
              <div class="kpi-l">本月采购额</div>
            </div>
            <div class="kpi">
              <div class="kpi-v">{{ fmtMoney(kpi.quarter_amount) }}</div>
              <div class="kpi-l">本季采购额</div>
            </div>
            <div class="kpi">
              <div class="kpi-v">{{ fmtMoney(kpi.year_amount) }}</div>
              <div class="kpi-l">本年采购额</div>
            </div>
            <div class="kpi is-bad">
              <div class="kpi-v">{{ fmtMoney(kpi.total_outstanding) }}</div>
              <div class="kpi-l">应付总额</div>
            </div>
            <div class="kpi" :class="{ 'is-warn': kpi.pending_requests > 0 }">
              <div class="kpi-v">{{ kpi.pending_requests }} 单</div>
              <div class="kpi-l">待审请款</div>
            </div>
          </div>

          <!-- 🆕 需求十二：供应商报表图表（折线/曲线图）-->
          <el-row :gutter="16" style="margin-bottom:4px">
            <el-col :xs="24" :md="14">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">月度采购趋势（收货 vs 已付 · 近12个月）</div>
                <LineChart :labels="trendChart.labels" :series="trendChart.series" :money-fmt="fmtMoney" :height="280" />
              </div>
            </el-col>
            <el-col :xs="24" :md="10">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">Top供应商月度采购额趋势</div>
                <LineChart :labels="supTrendChart.labels" :series="supTrendChart.series" :money-fmt="fmtMoney" :height="280" />
              </div>
            </el-col>
          </el-row>

          <el-row :gutter="16">
            <el-col :xs="24" :sm="24" :md="14">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">月度趋势明细（近12个月）</div>
                <el-table :data="monthlyTrend" stripe size="small" max-height="320" show-summary :summary-method="trendSummary" class="wrap-cells">
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
            <el-col :xs="24" :sm="24" :md="10">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">Top 供应商</div>
                <el-table :data="topSuppliers" stripe size="small" max-height="320" class="wrap-cells">
                  <el-table-column type="index" width="40" />
                  <el-table-column prop="supplier_name" label="供应商" min-width="100" />
                  <el-table-column label="采购额" align="right">
                    <template #default="{ row }"><b>{{ fmtMoney(row.amount) }}</b></template>
                  </el-table-column>
                </el-table>
              </div>
            </el-col>
          </el-row>

          <el-row :gutter="16">
            <el-col :xs="24" :sm="24" :md="10">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">按采购员</div>
                <el-table :data="byBuyer" stripe size="small" max-height="260" show-summary :summary-method="buyerSummary" class="wrap-cells">
                  <el-table-column prop="buyer_name" label="采购员" min-width="90" />
                  <el-table-column label="采购额" align="right">
                    <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
                  </el-table-column>
                  <el-table-column prop="count" label="条数" width="60" align="right" />
                </el-table>
              </div>
            </el-col>
            <el-col :xs="24" :sm="24" :md="14">
              <div class="report-section">
                <div class="sec-title" style="margin-top:0">按项目编号</div>
                <div class="filter-bar">
                  <el-input v-model="projectSearch" placeholder="搜索项目编号" clearable :prefix-icon="Search" style="width:180px" @change="loadProjectReport" />
                  <el-button @click="loadProjectReport">查询</el-button>
                </div>
                <el-table :data="byProject" stripe size="small" max-height="220" class="wrap-cells">
                  <el-table-column label="项目编号" min-width="110">
                    <template #default="{ row }"><b class="code">{{ row.project_code }}</b></template>
                  </el-table-column>
                  <el-table-column prop="amount" label="采购额" align="right" sortable>
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

    <!-- ==================== 从清单下单弹窗（清单→按供应商拆单）==================== -->
    <el-dialog v-model="listOrderVisible" :title="listOrderMode === 'kit' ? '按套下单（从清单打包成套）' : '从清单下单'" width="min(1580px, 98vw)" top="3vh" class="compact-dialog-scroll compact-tbl" :close-on-click-modal="false">
      <el-alert v-if="listOrderMode === 'kit'" type="success" :closable="false" style="margin-bottom:14px"
        title="按套下单：选项目 + 清单 → 勾选一组零件 → 填「套名称/套数/套总价/供应商」→ 打包成一套（一条成套明细）。勾中的零件成为套内清单并回写清单为「已下单」；整套按一个总走收货/入库/开票/请款/付款，作一个库存单位入库、按套领料。一套=同一供应商。" />
      <el-alert v-else type="info" :closable="false" style="margin-bottom:14px"
        title="选项目 + 清单类型（标准件/电工/不锈钢/外协/激光）→ 逐行选「供应商」「品牌」（可批量填）→ 点生成，系统按供应商自动拆成多张采购单。下单会回写清单的下单日期/采购负责人。外协/激光无数量，采购数量手填。" />
      <el-form :model="listOrderForm" label-position="top" class="order-form listorder-head-form">
        <el-row :gutter="14">
          <el-col :xs="24" :sm="10" :md="10">
            <el-form-item label="项目 *">
              <el-select v-model="listOrderForm.project_id" filterable placeholder="选择项目"
                         style="width:100%" @change="onListProjectChange">
                <el-option v-for="p in listProjects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="7" :md="7">
            <el-form-item label="清单类型 *">
              <el-select v-model="listSheet" :disabled="!listOrderForm.project_id" placeholder="选清单"
                         style="width:100%" @change="loadPurchasable">
                <el-option v-for="t in availableSheets" :key="t.key" :label="t.label" :value="t.key" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="7" :md="7">
            <el-form-item label="下单日期">
              <el-date-picker v-model="listOrderForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <!-- 🆕 成套模式：套级信息(供应商/套名/套数/套总价/付款)——一套一个总 -->
      <el-form v-if="listOrderMode === 'kit'" :model="kitSet" label-position="top" class="order-form" style="background:var(--el-fill-color-light);padding:10px 12px;border-radius:8px;margin-bottom:6px">
        <el-row :gutter="14">
          <el-col :xs="24" :sm="8" :md="6">
            <el-form-item label="供应商 *（一套同一家）">
              <el-select v-model="kitSet.supplier_id" filterable placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8" :md="6">
            <el-form-item label="套名称 *"><el-input v-model="kitSet.kit_name" placeholder="如：100L均质机外协件一套" /></el-form-item>
          </el-col>
          <el-col :xs="8" :sm="4" :md="3">
            <el-form-item label="套数 *"><el-input-number v-model="kitSet.kit_qty" :min="0" :precision="2" :controls="false" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :xs="8" :sm="4" :md="4">
            <el-form-item label="套总价 *"><el-input-number v-model="kitSet.kit_total" :min="0" :precision="2" :controls="false" style="width:100%" placeholder="整套总金额" /></el-form-item>
          </el-col>
          <el-col :xs="8" :sm="6" :md="5">
            <el-form-item label="套单价（自动）"><el-input :value="fmtMoney(kitSetUnitPrice)" disabled /></el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8" :md="6">
            <el-form-item label="付款方式">
              <el-select v-model="kitSet.payment_method" clearable placeholder="选填" style="width:100%">
                <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="isPrepayMethod(kitSet.payment_method)" :xs="12" :sm="8" :md="6">
            <el-form-item label="预付比例(%)"><el-input-number v-model="kitSet.prepay_ratio" :min="0" :max="100" :controls="false" style="width:100%" /></el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <div class="order-lines-head listorder-toolbar" style="flex-wrap:wrap;gap:8px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <el-input v-model="purchasableFilter" placeholder="筛选名称/规格" clearable :prefix-icon="Search" style="width:150px" />
          <el-switch v-if="curSheetHasQty" v-model="onlyGap" active-text="只看有缺口" style="--el-switch-on-color: var(--el-color-danger)" />
          <!-- 🆕 批量填：逐行选太麻烦时，勾好行后一键填供应商/品牌/付款方式（成套模式无需，隐藏）-->
          <template v-if="listOrderMode !== 'kit'">
            <el-divider direction="vertical" />
            <el-select v-model="batchSupplier" filterable clearable placeholder="批量供应商" style="width:136px">
              <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
            <el-button size="small" @click="applyBatchSupplier">填给勾选行</el-button>
            <el-select v-model="batchBrand" filterable allow-create clearable default-first-option placeholder="批量品牌" style="width:116px">
              <el-option v-for="b in brandOptions" :key="b" :label="b" :value="b" />
            </el-select>
            <el-button size="small" @click="applyBatchBrand">填给勾选行</el-button>
            <el-select v-model="batchPaymentMethod" clearable filterable placeholder="批量付款方式" style="width:110px">
              <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
            </el-select>
            <el-button size="small" @click="applyBatchPaymentMethod">填给勾选行</el-button>
            <el-input-number v-if="isPrepayMethod(batchPaymentMethod)" v-model="batchPrepayRatio"
              :min="0" :max="100" placeholder="预付%" controls-position="right" style="width:100px" />
            <el-button v-if="isPrepayMethod(batchPaymentMethod)" size="small" @click="applyBatchPrepayRatio">填给勾选行</el-button>
          </template>
        </div>
        <span class="muted">已勾选 <b>{{ listSelCount }}</b> / {{ purchasableRows.length }} 行</span>
      </div>
      <el-table :data="filteredPurchasable" v-loading="purchasableLoading" size="small" border stripe
                :empty-text="listOrderForm.project_id ? '该清单为空' : '请先选择项目和清单类型'"
                max-height="calc(100vh - 430px)" class="wrap-cells">
        <el-table-column width="46" align="center" fixed>
          <template #header>
            <el-checkbox :model-value="allFilteredChecked" :indeterminate="someFilteredChecked"
                         @change="toggleAllPurchasable" />
          </template>
          <template #default="{ row }"><el-checkbox v-model="row._checked" /></template>
        </el-table-column>
        <el-table-column label="名称" min-width="150" prop="item_name" fixed show-overflow-tooltip />
        <el-table-column v-if="sheetMeta(listSheet).hasDrawing" label="图纸名称" min-width="120" show-overflow-tooltip><template #default="{ row }">{{ row.drawing || '—' }}</template></el-table-column>
        <el-table-column label="规格型号" min-width="130" show-overflow-tooltip><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
        <el-table-column v-if="listOrderMode !== 'kit'" label="品牌" width="118">
          <template #default="{ row }">
            <el-select v-model="row._brand" filterable allow-create clearable default-first-option
                       placeholder="选/填" size="small" style="width:100%" @change="row._checked = true">
              <el-option v-for="b in brandOptions" :key="b" :label="b" :value="b" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column v-if="curSheetHasQty" label="需求量" width="70" align="right"><template #default="{ row }">{{ row.qty ?? '—' }}</template></el-table-column>
        <el-table-column v-if="curSheetHasQty" label="现有库存" width="78" align="right">
          <template #default="{ row }"><span :class="{ 'stock-has': row.stock > 0 }">{{ row.stock }}</span></template>
        </el-table-column>
        <el-table-column v-if="curSheetHasQty" label="建议采购" width="80" align="right">
          <template #default="{ row }"><b :class="row.suggest_purchase > 0 ? 'sugg-buy' : 'sugg-none'">{{ row.suggest_purchase }}</b></template>
        </el-table-column>
        <el-table-column :label="listOrderMode === 'kit' ? '每套数量' : '采购数量'" width="104">
          <template #default="{ row }">
            <el-input-number v-model="row._buyqty" :min="0" :controls="false" style="width:100%" @change="row._checked = true" />
          </template>
        </el-table-column>
        <el-table-column v-if="listOrderMode !== 'kit'" label="单价(选填)" width="104">
          <template #default="{ row }">
            <el-input-number v-model="row._price" :min="0" :precision="4" :controls="false" style="width:100%" placeholder="后填留空" @change="row._checked = true" />
          </template>
        </el-table-column>
        <el-table-column v-if="listOrderMode !== 'kit'" label="供应商 *" width="150">
          <template #default="{ row }">
            <el-select v-model="row._supplier_id" filterable clearable placeholder="必选" size="small"
                       :class="{ 'sup-missing': row._checked && !row._supplier_id }" style="width:100%" @change="row._checked = true">
              <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column v-if="listOrderMode !== 'kit'" label="付款方式" width="100">
          <template #default="{ row }">
            <el-select v-model="row._payment_method" clearable filterable
                       placeholder="选择" size="small" style="width:100%" @change="row._checked = true">
              <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column v-if="listOrderMode !== 'kit'" label="预付%" width="84">
          <template #default="{ row }">
            <el-input-number v-if="isPrepayMethod(row._payment_method)" v-model="row._prepay_ratio"
              :min="0" :max="100" size="small" controls-position="right" style="width:100%" />
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="采购状态" width="80" align="center">
          <template #default="{ row }"><el-tag size="small" :type="listStatusTag(row.status)">{{ row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="备注" min-width="110" show-overflow-tooltip><template #default="{ row }">{{ row.notes || '—' }}</template></el-table-column>
      </el-table>
      <template #footer>
        <div class="listorder-footer">
          <span v-if="listOrderMode === 'kit'" class="muted lo-hint">勾选的 <b>{{ listSelCount }}</b> 项零件打包成 <b>{{ kitSet.kit_qty || 0 }}</b> 套，作一条成套明细（一个总）；上方填好套名/套数/套总价/供应商。已勾选行会回写清单为「已下单」。</span>
          <span v-else class="muted lo-hint"><b>每行必须选供应商</b>，生成时按供应商自动拆成多张采购单。<span v-if="curSheetHasQty">建议采购 = 需求量 − 现有库存，数量已默认填好可改。</span><span v-else>本清单无数量，采购数量请手填。</span></span>
          <span class="lo-actions">
            <el-button @click="listOrderVisible = false">取消</el-button>
            <el-button v-if="listOrderMode === 'kit'" type="primary" :loading="listOrderSaving" :disabled="!listSelCount" @click="submitKitFromList">
              打包成套下单（{{ listSelCount }} 项）
            </el-button>
            <el-button v-else type="primary" :loading="listOrderSaving" :disabled="!listSelCount" @click="submitListOrder">
              生成采购单（{{ listSelCount }} 行）
            </el-button>
          </span>
        </div>
      </template>
    </el-dialog>

    <!-- ==================== 采购单弹窗（同一供应商多个零件行）==================== -->
    <el-dialog v-model="orderDialogVisible" title="新建采购单" width="min(1180px, 98vw)" top="4vh" class="compact-dialog-scroll compact-tbl" :close-on-click-modal="false" :before-close="onOrderDialogClose">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="同一供应商一次录入多个零件：表头（供应商 / 下单日期 / 合同 / 默认项目）在上，零件逐行填。单价「选填」——已谈好价先填；激光板材等到货送货单才带价的，单价留空，货到仓库再补。保存后自动生成采购单号；可「打印采购单」发供应商。" />
      <el-form :model="orderForm" label-position="top" class="order-form">
        <el-row :gutter="20">
          <el-col :xs="24" :sm="12" :md="7">
            <el-form-item label="供应商 *">
              <el-select v-model="orderForm.supplier_id" filterable placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="12" :md="5">
            <el-form-item label="下单日期">
              <el-date-picker v-model="orderForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8" :md="4">
            <el-form-item label="付款方式">
              <el-select v-model="orderForm.payment_method" clearable filterable
                         placeholder="选择" style="width:100%">
                <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="isPrepayMethod(orderForm.payment_method)" :xs="12" :sm="8" :md="4">
            <el-form-item label="预付比例(%)">
              <el-input-number v-model="orderForm.prepay_ratio" :min="0" :max="100" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8" :md="4">
            <el-form-item label="合同编号">
              <el-input v-model="orderForm.contract_no" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8" :md="4">
            <el-form-item label="默认订单编号">
              <el-select v-model="orderForm.project_code" filterable allow-create clearable default-first-option
                         placeholder="选/输项目编号；无项目可填 固定资产/耗材 等" style="width:100%">
                <el-option-group label="非项目（无编号时选/输）">
                  <el-option v-for="c in NON_PROJECT_CODES" :key="'np-'+c" :label="c" :value="c" />
                </el-option-group>
                <el-option-group label="项目编号">
                  <el-option v-for="c in projectCodeOptions" :key="c" :label="c" :value="c" />
                </el-option-group>
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <div class="order-lines-head">
          <span class="order-lines-title">零件明细（{{ orderForm.lines.length }} 行）</span>
          <el-button size="small" :icon="Plus" @click="addOrderLine">添加一行</el-button>
        </div>
        <el-table :data="orderForm.lines" size="small" border :scrollbar-always-on="true" max-height="max(240px, 40vh)" class="order-lines">
          <el-table-column type="index" label="#" width="44" align="center" />
          <el-table-column label="名称 *" min-width="150">
            <template #default="{ row }"><el-input v-model="row.item_name" placeholder="零件名称" /></template>
          </el-table-column>
          <el-table-column label="规格型号" min-width="150">
            <template #default="{ row }"><el-input v-model="row.spec" placeholder="规格/型号" /></template>
          </el-table-column>
          <el-table-column label="订单编号" width="150">
            <template #default="{ row }">
              <el-select v-model="row.project_code" filterable allow-create clearable default-first-option
                         :placeholder="orderForm.project_code || '默认'" style="width:100%">
                <el-option v-for="c in projectCodeOptions" :key="c" :label="c" :value="c" />
              </el-select>
            </template>
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
          <!-- 🆕 R6 自定义列（逐行填） -->
          <el-table-column v-for="f in formCustomFields" :key="f.id" :label="f.label + (f.required ? ' *' : '')" min-width="130">
            <template #default="{ row }">
              <el-select v-if="f.ftype === 'select'" v-model="row.custom_values[String(f.id)]" clearable filterable placeholder="选择" style="width:100%">
                <el-option v-for="o in f.options" :key="o" :label="o" :value="o" />
              </el-select>
              <el-date-picker v-else-if="f.ftype === 'date'" v-model="row.custom_values[String(f.id)]" type="date" value-format="YYYY-MM-DD" style="width:100%" />
              <el-input-number v-else-if="f.ftype === 'number'" v-model="row.custom_values[String(f.id)]" :controls="false" style="width:100%" />
              <el-input v-else v-model="row.custom_values[String(f.id)]" placeholder="选填" />
            </template>
          </el-table-column>
          <el-table-column label="" width="50" align="center" fixed="right">
            <template #default="{ $index }">
              <el-button size="small" link type="danger" :icon="Delete" @click="removeOrderLine($index)" />
            </template>
          </el-table-column>
        </el-table>
        <div class="order-total-bar">
          <div style="display:flex;gap:14px;align-items:center">
            <el-button size="small" :icon="Plus" @click="addOrderLine">添加一行</el-button>
            <span>合计金额 <b class="amt">{{ fmtMoney(orderTotal) }}</b></span>
          </div>
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
    <el-dialog v-model="itemDialogVisible" :title="editingItem ? '编辑采购明细' : '新增采购明细'" width="860px" top="5vh" class="v3-scroll-dialog" :close-on-click-modal="false" @closed="itemFormRef?.clearValidate()">
      <el-form ref="itemFormRef" :model="itemForm" :rules="itemRules" label-position="top" class="item-form">
        <div class="form-section-title">基本信息</div>
        <el-row :gutter="24">
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="供应商" prop="supplier_id">
              <el-select v-model="itemForm.supplier_id" filterable placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="下单日期">
              <el-date-picker v-model="itemForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="项目编号">
              <el-input v-model="itemForm.project_code" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="名称" prop="item_name">
              <el-input v-model="itemForm.item_name" placeholder="零件/物料名称" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="规格型号">
              <el-input v-model="itemForm.spec" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="品牌">
              <el-input v-model="itemForm.brand" placeholder="选填" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="付款方式">
              <el-select v-model="itemForm.payment_method" clearable filterable
                         placeholder="选择" style="width:100%">
                <el-option v-for="m in PAY_METHODS" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="isPrepayMethod(itemForm.payment_method)" :xs="24" :sm="12" :md="8">
            <el-form-item label="预付比例(%)">
              <el-input-number v-model="itemForm.prepay_ratio" :min="0" :max="100" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section-title">数量与金额</div>
        <el-row :gutter="24">
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="数量">
              <el-input-number v-model="itemForm.qty" :precision="2" :min="0" style="width:100%" @change="onItemCalc" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="单价">
              <el-input-number v-model="itemForm.unit_price" :precision="4" :min="0" style="width:100%" @change="onItemCalc" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
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

        <!-- 🆕 R6 自定义字段 -->
        <template v-if="formCustomFields.length">
          <div class="form-section-title">自定义字段</div>
          <el-row :gutter="24">
            <el-col v-for="f in formCustomFields" :key="f.id" :xs="24" :sm="12" :md="8">
              <el-form-item>
                <template #label>{{ f.label }}<span v-if="f.required" style="color:var(--el-color-danger)"> *</span></template>
                <el-select v-if="f.ftype === 'select'" v-model="itemForm.custom_values[String(f.id)]" clearable filterable placeholder="请选择" style="width:100%">
                  <el-option v-for="o in f.options" :key="o" :label="o" :value="o" />
                </el-select>
                <el-date-picker v-else-if="f.ftype === 'date'" v-model="itemForm.custom_values[String(f.id)]" type="date" value-format="YYYY-MM-DD" style="width:100%" />
                <el-input-number v-else-if="f.ftype === 'number'" v-model="itemForm.custom_values[String(f.id)]" :controls="false" style="width:100%" />
                <el-input v-else v-model="itemForm.custom_values[String(f.id)]" placeholder="选填" />
              </el-form-item>
            </el-col>
          </el-row>
        </template>

        <!-- 开票 / 对账 / 送货单号：仅编辑时显示（新增时精简，货到仓库或需要开票时再补） -->
        <template v-if="editingItem">
          <div class="form-section-title">送货 · 开票与对账</div>
          <el-row :gutter="24">
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="送货单号">
                <el-input v-model="itemForm.delivery_note_no" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="合同编号">
                <el-input v-model="itemForm.contract_no" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="开票日期">
                <el-date-picker v-model="itemForm.invoice_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="税率">
                <el-select v-model="itemForm.tax_rate" style="width:100%" clearable placeholder="请选择">
                  <el-option label="13%" value="13%" />
                  <el-option label="9%" value="9%" />
                  <el-option label="6%" value="6%" />
                  <el-option label="/" value="/" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="开票金额">
                <el-input-number v-model="itemForm.invoice_amount" :precision="2" :min="0" style="width:100%" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="8">
              <el-form-item label="对账状态">
                <el-select v-model="itemForm.invoice_status" style="width:100%">
                  <el-option label="待对账" value="待对账" />
                  <el-option label="已对账" value="已对账" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="itemDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="itemSaving" @click="saveItem">保存</el-button>
      </template>
    </el-dialog>


    <!-- ==================== 请款弹窗 ==================== -->
    <el-dialog v-model="payReqVisible" title="发起请款" width="620px" class="v3-scroll-dialog" :close-on-click-modal="false">
      <el-form :model="payReqForm" label-position="top">
        <el-form-item label="供应商">
          <b>{{ suppliers.find(s => s.id === payReqForm.supplier_id)?.name }}</b>
        </el-form-item>
        <el-form-item :label="`关联明细（${payReqForm.items.length} 条，金额可按行调整）`">
          <div class="pr-item-list">
            <div v-for="it in payReqForm.items" :key="it.item_id" class="pr-item-row">
              <span class="pr-item-name">{{ it.item_name }}</span>
              <el-input-number v-model="it.allocated_amount" :precision="2" :min="0" :max="it.max" size="small" style="width:130px" />
            </div>
          </div>
        </el-form-item>
        <el-form-item label="请款总额">
          <el-input-number v-model="payReqForm.requested_amount" :precision="2" :min="0" style="width:100%" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="payReqForm.notes" type="textarea" :rows="2" placeholder="选填：付款说明、账期等" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="payReqVisible = false">取消</el-button>
        <el-button type="primary" :loading="payReqSaving" @click="submitPaymentRequest">提交请款</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 需求十三：批量维护开票号 -->
    <el-dialog v-model="invoiceNoDialogVisible" title="维护开票号" width="460px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        :title="`将对已勾选的 ${selLeaves.length} 条明细统一维护同一开票号；每条明细的开票金额将取其收货金额，并标记为「已开票」。`" />
      <el-form label-position="top">
        <el-form-item label="开票号" required>
          <el-input v-model="invoiceNoForm.invoice_no" placeholder="填写发票号码" />
        </el-form-item>
        <el-form-item label="开票日期（选填）">
          <el-date-picker v-model="invoiceNoForm.invoice_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="invoiceNoDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="invoiceNoSaving" @click="submitBatchInvoiceNo">确认维护</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 🆕 #4 合并父行「整单维护」==================== -->
    <el-dialog v-model="groupSumVisible" title="整单维护（合并单）" width="480px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        :title="`对采购单「${groupSumRow?.po_no || ''}」整单维护：开票金额/已付款按整单总额维护(不拆分到各零件，记在汇总)，对账状态套用到全部 ${groupSumRow?._count || 0} 项零件。留空的字段不改。`" />
      <el-form label-position="top">
        <el-form-item :label="`开票金额（整单总额，当前 ${fmtMoney(groupSumRow?.invoice_amount || 0)}）`">
          <el-input-number v-model="groupSumForm.invoice_amount" :min="0" :precision="2" :controls="false" style="width:100%" placeholder="留空不改" />
        </el-form-item>
        <el-form-item :label="`已付款（整单总额，当前 ${fmtMoney(groupSumRow?.paid_amount || 0)}）`">
          <el-input-number v-model="groupSumForm.paid_amount" :min="0" :precision="2" :controls="false" style="width:100%" placeholder="留空不改" />
        </el-form-item>
        <el-form-item label="付款日期（选填）">
          <el-date-picker v-model="groupSumForm.paid_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
        <el-form-item label="对账状态（套用到全部子零件）">
          <el-select v-model="groupSumForm.invoice_status" clearable placeholder="留空不改" style="width:100%">
            <el-option label="待对账" value="待对账" />
            <el-option label="已对账" value="已对账" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="groupSumVisible = false">取消</el-button>
        <el-button type="primary" :loading="groupSumSaving" @click="submitGroupSummary">确认维护</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 供应商弹窗 ==================== -->
    <el-dialog v-model="supplierDialogVisible" :title="editingSupplier ? '编辑供应商' : '新增供应商'" width="780px" top="5vh" class="v3-scroll-dialog" :close-on-click-modal="false" @closed="supplierFormRef?.clearValidate()">
      <el-form ref="supplierFormRef" :model="supplierForm" :rules="supplierRules" label-position="top" class="supplier-form">
        <div class="form-section-title">基本信息</div>
        <el-row :gutter="24">
          <el-col :xs="24" :sm="12">
            <el-form-item label="供应商名称" prop="name">
              <el-input v-model="supplierForm.name" placeholder="请输入供应商名称" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item label="编码">
              <el-input v-model="supplierForm.code" placeholder="内部编码（可选）" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="分类">
              <el-select v-model="supplierForm.category" style="width:100%" clearable filterable placeholder="请选择分类">
                <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="结算方式">
              <el-select v-model="supplierForm.settlement_type" style="width:100%" clearable placeholder="请选择">
                <el-option label="现金" value="现金" />
                <el-option label="月结" value="月结" />
                <el-option label="无账期" value="无账期" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="账期天数" v-if="supplierForm.settlement_type === '月结'">
              <el-input-number v-model="supplierForm.credit_days" :min="0" :max="365" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section-title">联系方式</div>
        <el-row :gutter="24">
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="联系人">
              <el-input v-model="supplierForm.contact" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
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
          <el-col :xs="24" :sm="12" :md="8">
            <el-form-item label="税号">
              <el-input v-model="supplierForm.tax_no" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12" :md="8">
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
        <div style="display:flex;align-items:center">
          <el-button v-if="editingSupplier" type="danger" plain @click="deleteEditingSupplier">删除供应商</el-button>
          <div style="margin-left:auto">
            <el-button @click="supplierDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="supplierSaving" @click="saveSupplier">保存</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- ==================== 期初余额弹窗 ==================== -->
    <el-dialog v-model="openingBalanceVisible" :title="`录入期初余额 — ${openingBalanceSupplierName}`" width="500px">
      <el-form :model="openingBalanceForm" label-position="top" v-loading="openingBalanceLoading">
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
        <el-button type="primary" :loading="openingBalanceSaving" @click="saveOpeningBalance">保存</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 供应商明细下钻抽屉 ==================== -->
    <el-drawer v-model="drawerVisible" :title="drawerSupplier ? `${drawerSupplier.supplier_name} — 采购明细` : ''" size="75%" direction="rtl">
      <div v-if="drawerSupplier" class="drawer-summary">
        <span>收货合计 <b class="amt">{{ fmtMoney(drawerSupplier.received_total) }}</b></span>
        <span>已开票 <b class="amt" style="color:var(--el-color-success)">{{ fmtMoney(drawerSupplier.invoice_total) }}</b></span>
        <span>未开票 <b class="amt" style="color:var(--el-color-warning)">{{ fmtMoney(drawerSupplier.uninvoiced) }}</b></span>
        <span>已付款 <b class="amt">{{ fmtMoney(drawerSupplier.paid_total) }}</b></span>
        <span>欠款 <b class="danger">{{ fmtMoney(drawerSupplier.outstanding) }}</b></span>
        <span class="flex-spacer" />
        <el-button size="small" type="primary" plain :icon="Download"
                   @click="exportSupplierStatement(drawerSupplier.supplier_id, drawerSupplier.supplier_name)">导出对账单</el-button>
      </div>

      <!-- 🆕 #166：月度收货/开票/付款 趋势报表 -->
      <div v-if="drawerTrendChart.labels.length" class="report-section" style="margin-bottom:12px">
        <div class="sec-title" style="margin-top:0">月度趋势（收货 / 已开票 / 已付）</div>
        <LineChart :labels="drawerTrendChart.labels" :series="drawerTrendChart.series" :money-fmt="fmtMoney" :height="220" />
      </div>

      <!-- 🆕 按月合计开票：按到货日期分月，未开票/已开票一目了然 -->
      <el-collapse v-if="drawerMonthly.length" class="monthly-collapse">
        <el-collapse-item :title="`按月收货/开票/付款汇总（${drawerMonthly.length} 个月）`" name="m">
          <el-table :data="drawerMonthly" size="small" stripe class="wrap-cells">
            <el-table-column label="月份（按到货日期）" min-width="140">
              <template #default="{ row }"><b>{{ row.month }}</b></template>
            </el-table-column>
            <el-table-column label="收货合计" width="120" align="right">
              <template #default="{ row }">{{ fmtMoney(row.received) }}</template>
            </el-table-column>
            <el-table-column label="已开票" width="120" align="right">
              <template #default="{ row }"><span class="amt">{{ fmtMoney(row.invoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="未开票" width="120" align="right">
              <template #default="{ row }"><span class="warn">{{ fmtMoney(row.uninvoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="已付款" width="120" align="right">
              <template #default="{ row }"><span style="color:#16a34a">{{ fmtMoney(row.paid) }}</span></template>
            </el-table-column>
            <el-table-column label="明细数" width="72" align="center">
              <template #default="{ row }">{{ row.count }}</template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <el-table v-loading="drawerLoading" :data="drawerItems" stripe size="small"
                max-height="max(300px, calc(100vh - 180px))" :scrollbar-always-on="true" class="wrap-cells compact-tbl">
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
        <el-table-column label="未开票" width="105" align="right">
          <template #default="{ row }">
            <span class="warn">{{ fmtMoney(Math.max(0, (row.received_amount || 0) - (row.invoice_amount || 0))) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="已付款" width="105" align="right">
          <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
        </el-table-column>
        <el-table-column label="未付款" width="105" align="right">
          <template #default="{ row }">
            <span class="danger">{{ fmtMoney(Math.max(0, (row.received_amount || 0) - (row.paid_amount || 0))) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="付款状态" width="90">
          <template #default="{ row }">
            <el-tag :type="payStatusTag(row.pay_status)" size="small" effect="light">{{ row.pay_status || '未付款' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="对账状态" width="80" fixed="right">
          <template #default="{ row }">
            <el-tag :type="reconcileTag(row)" size="small">{{ reconcileText(row) }}</el-tag>
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
                max-height="calc(100vh - 130px)" :scrollbar-always-on="true" size="small" class="wrap-cells">
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

    <!-- ==================== 🆕 R6 自定义字段设置 ==================== -->
    <el-dialog v-model="cfManagerVisible" title="采购单自定义字段设置" width="min(860px, 96vw)" top="6vh" class="v3-scroll-dialog">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="在这里给采购明细增删自定义列（如 用途 / 项目阶段 / 交货周期 等）。新增字段只对之后录入/编辑的明细生效；删除字段不影响已录入的历史值。" />
      <el-table :data="customFields" size="small" border stripe max-height="34vh" class="wrap-cells">
        <el-table-column type="index" label="#" width="46" align="center" />
        <el-table-column prop="label" label="字段名称" min-width="120" />
        <el-table-column label="类型" width="90"><template #default="{ row }">{{ CF_TYPES.find(t => t.v === row.ftype)?.l || row.ftype }}</template></el-table-column>
        <el-table-column label="必填" width="64" align="center"><template #default="{ row }"><el-tag v-if="row.required" size="small" type="danger" effect="plain">必填</el-tag><span v-else class="muted">—</span></template></el-table-column>
        <el-table-column label="列表显示" width="80" align="center"><template #default="{ row }"><el-tag :type="row.show_in_list ? 'success' : 'info'" size="small" effect="plain">{{ row.show_in_list ? '显示' : '隐藏' }}</el-tag></template></el-table-column>
        <el-table-column label="启用" width="64" align="center"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="排序" width="60" align="center" prop="sort_order" />
        <el-table-column label="操作" width="110" align="center" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="cfEdit(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="cfDelete(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyHint text="还没有自定义字段，下面新增一个" size="sm" /></template>
      </el-table>

      <div class="form-section-title" style="margin-top:16px">{{ cfEditingId ? '编辑字段' : '新增字段' }}</div>
      <el-form :model="cfForm" label-position="top">
        <el-row :gutter="16">
          <el-col :xs="24" :sm="8">
            <el-form-item label="字段名称 *"><el-input v-model="cfForm.label" placeholder="如 用途 / 交货周期" /></el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8">
            <el-form-item label="类型">
              <el-select v-model="cfForm.ftype" style="width:100%">
                <el-option v-for="t in CF_TYPES" :key="t.v" :label="t.l" :value="t.v" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="12" :sm="8">
            <el-form-item label="排序（小在前）"><el-input-number v-model="cfForm.sort_order" :controls="false" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="24" v-if="cfForm.ftype === 'select'">
            <el-form-item label="下拉选项（每行一个）">
              <el-input v-model="cfForm.options" type="textarea" :rows="3" placeholder="选项A&#10;选项B&#10;选项C" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
              <span>必填 <el-switch v-model="cfForm.required" /></span>
              <span>列表显示 <el-switch v-model="cfForm.show_in_list" /></span>
              <span>启用 <el-switch v-model="cfForm.enabled" /></span>
            </div>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button v-if="cfEditingId" @click="cfResetForm">取消编辑</el-button>
        <el-button @click="cfManagerVisible = false">关闭</el-button>
        <el-button type="primary" :loading="cfSaving" @click="cfSave">{{ cfEditingId ? '保存修改' : '新增字段' }}</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 🆕 字典设置（物料类别 / 单位 / 材质 / 供应商分类）==================== -->
    <el-dialog v-model="matDictVisible" title="字典设置（物料类别 / 单位 / 材质 / 供应商分类）" width="min(720px, 96vw)" top="6vh" class="v3-scroll-dialog">
      <el-alert type="info" :closable="false" style="margin-bottom:14px" :title="MD_TAB_ALERTS[mdTab]" />
      <el-radio-group v-model="mdTab" style="margin-bottom:12px" @change="mdResetForm">
        <el-radio-button value="category">物料类别</el-radio-button>
        <el-radio-button value="unit">计量单位</el-radio-button>
        <el-radio-button value="material_grade">材质</el-radio-button>
        <el-radio-button value="supplier_category">供应商分类</el-radio-button>
      </el-radio-group>
      <el-table :data="mdList" size="small" border stripe max-height="34vh">
        <el-table-column type="index" label="#" width="46" align="center" />
        <el-table-column prop="value" label="取值" min-width="150" />
        <el-table-column label="排序" width="64" align="center" prop="sort_order" />
        <el-table-column label="状态" width="72" align="center">
          <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="160" align="center" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="mdEdit(row)">编辑</el-button>
            <el-button size="small" link :type="row.enabled ? 'warning' : 'success'" @click="mdToggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button size="small" link type="danger" @click="mdDelete(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyHint text="还没有取值，下面新增一个" size="sm" /></template>
      </el-table>

      <div class="form-section-title" style="margin-top:16px">{{ mdEditingId ? '编辑取值' : '新增取值' }}（{{ MD_TAB_LABELS[mdTab] }}）</div>
      <el-form :model="mdForm" label-position="top">
        <el-row :gutter="16">
          <el-col :xs="14" :sm="14">
            <el-form-item label="取值 *"><el-input v-model="mdForm.value" :placeholder="MD_TAB_PLACEHOLDERS[mdTab]" /></el-form-item>
          </el-col>
          <el-col :xs="10" :sm="6">
            <el-form-item label="排序（小在前）"><el-input-number v-model="mdForm.sort_order" :controls="false" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="24">
            <span>启用 <el-switch v-model="mdForm.enabled" /></span>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button v-if="mdEditingId" @click="mdResetForm">取消编辑</el-button>
        <el-button @click="matDictVisible = false">关闭</el-button>
        <el-button type="primary" :loading="mdSaving" @click="mdSave">{{ mdEditingId ? '保存修改' : '新增取值' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 弹窗表单：行距统一 16px */
:deep(.el-dialog .el-form-item) { margin-bottom: 16px; }
:deep(.el-dialog .el-form-item:last-child) { margin-bottom: 0; }

.filter-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }
/* 间距全交给 gap：抵消 Element 默认按钮相邻 margin，换行后左缘对齐 */
.filter-bar :deep(.el-button + .el-button) { margin-left: 0; }
.flex-spacer { flex: 1; min-width: 8px; }
/* 勾选后浮出的批量操作条 */
.sel-bar {
  display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
  padding: 8px 14px; margin-bottom: 10px; font-size: 13.5px;
  background: var(--el-color-warning-light-9); border: 1px solid var(--el-color-warning-light-7);
  border-radius: 6px;
}
.summary-bar { display: flex; gap: 24px; flex-wrap: wrap; padding: 12px 16px; background: var(--el-fill-color-light); border-radius: 6px; margin-top: 12px; font-size: 14px; }
.report-section { margin-bottom: 16px; }
/* 🆕 需求十一：供应商账目分类卡片 */
.cat-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
.cat-card { border: 1px solid var(--el-border-color-light); border-radius: 10px; padding: 14px 16px;
  background: var(--el-bg-color-overlay, #fff); cursor: pointer; transition: box-shadow .15s, transform .15s, border-color .15s; }
.cat-card:hover { box-shadow: 0 6px 20px rgba(0,0,0,.10); transform: translateY(-2px); border-color: var(--el-color-primary-light-5); }
.cat-card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.cat-card-head .cat-name { font-weight: 600; font-size: 15px; color: var(--el-text-color-primary); }
.cat-card-body { display: flex; flex-direction: column; gap: 6px; }
.cat-metric { display: flex; align-items: baseline; justify-content: space-between; font-size: 13px; color: var(--el-text-color-secondary); }
.cat-metric b { font-size: 15px; color: var(--el-text-color-primary); }
.cat-card-foot { margin-top: 10px; font-size: 12px; color: var(--el-color-primary); }
.drill-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.drill-head .drill-cat { font-weight: 600; font-size: 15px; }
.small { font-size: 12px; }
.code { color: var(--el-color-primary, #2563eb); }
.sup-name { font-weight: 500; }
.amt { color: var(--el-color-primary); }
.warn { color: var(--el-color-warning); }
.danger { color: var(--el-color-danger); }
.pr-item-list { width: 100%; max-height: 220px; overflow-y: auto; }
.pr-item-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; font-size: 13px; }
.pr-item-name { flex: 1; min-width: 0; overflow-wrap: anywhere; line-height: 1.4; }
.drawer-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 22px; padding: 12px 0 16px; font-size: 14px; border-bottom: 1px solid var(--el-border-color-lighter); margin-bottom: 12px; }
.monthly-collapse { margin-bottom: 14px; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.sup-missing :deep(.el-select__wrapper) { box-shadow: 0 0 0 1px var(--el-color-danger) inset; }
.stock-has { color: var(--el-color-success); font-weight: 600; }
.sugg-buy { color: var(--el-color-danger); }
.sugg-none { color: var(--el-text-color-secondary); font-weight: 400; }
.tip-line { line-height: 1.7; }
.dl-tip { font-size: 12.5px; color: var(--el-text-color-secondary); margin-bottom: 14px; line-height: 1.6; }
.dl-sec { margin-bottom: 20px; }
.dl-sec-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--el-border-color-lighter); }
.dl-sec-title { font-weight: 600; color: var(--el-text-color-primary); font-size: 13px; }
.dl-list { display: flex; flex-direction: column; gap: 6px; }
.dl-item { width: 100%; margin-right: 0; height: auto; }
.dl-item :deep(.el-checkbox__label) { white-space: normal; word-break: break-all; line-height: 1.5; }
.dl-kind { display: inline-block; margin-right: 6px; padding: 0 6px; font-size: 11px; color: var(--primary-dark); background: var(--el-color-primary-light-9); border-radius: 8px; }
.dl-empty { padding: 4px 0; }
.form-section-title {
  font-size: 13px; font-weight: 600; color: var(--el-color-primary);
  padding: 4px 0 10px; margin-top: 6px; border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 16px;
}
.form-section-title:first-child { margin-top: 0; }
.po-no { font-variant-numeric: tabular-nums; font-size: 13px; padding: 0 !important; }
.order-lines-head { display: flex; align-items: center; justify-content: space-between; margin: 4px 0 8px; }
.order-lines :deep(.cell) { padding-left: 6px; padding-right: 6px; }
.order-total-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; padding: 10px 14px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 14px; }
.order-lines-title { font-weight: 600; font-size: 14px; }
.pager-bar { display: flex; justify-content: flex-end; margin-top: 10px; }
.pr-expand { padding: 6px 12px 6px 48px; }
.pr-expand-row { display: flex; justify-content: space-between; gap: 16px; padding: 3px 0; font-size: 13px; border-bottom: 1px dashed var(--el-border-color-lighter); max-width: 560px; }
.pr-expand-row:last-of-type { border-bottom: none; }
</style>
