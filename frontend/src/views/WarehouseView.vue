<script setup lang="ts">
// 🆕 v3 M07 仓库组：总览/出入库/收发存/流水/物料主数据/发货清单 六 tab
import { ref, onMounted, reactive, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Lock, View, Download, Printer, Setting, Delete, ArrowLeft } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { whApi, type WhMaterial, type WhTxn, type WhSummaryRow, type ShipListFile, type ShipListPendingRow, type WhCustomField } from '@/api/warehouse'
import { canInlinePreview, attachmentBlobUrl, isPdfAtt, isImageAtt } from '@/api/attachments'
import { downloadAttachment } from '@/api/orders'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import AttachmentPreview from '@/components/AttachmentPreview.vue'
import { fmtDate, fmtMoney } from '@/utils/format'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasRole('warehouse', 'warehouse_lead', 'admin', 'manager'))
const isManager = computed(() => auth.hasRole('admin', 'manager'))   // #5：单价/总价仅管理层可见
const tv = (name: string) => auth.tabVisible('warehouse', name)   // 🆕 #7 按账号二级菜单授权
// 🆕 需求十五：仓库总监/管理层可一键清空
const canClear = computed(() => auth.hasRole('warehouse_lead', 'admin', 'manager'))
async function clearAll() {
  let word = ''
  try {
    const res = await ElMessageBox.prompt(
      '⚠ 高危操作：将清空全部「出入库流水」+「物料主数据」（试运行数据清理，不影响采购/供应商/项目/字典）。此操作不可恢复！\n请输入「清空仓库」以确认：',
      '一键清空仓库', {
        inputPattern: /^清空仓库$/, inputErrorMessage: '请输入「清空仓库」',
        confirmButtonText: '确认清空', confirmButtonClass: 'el-button--danger', type: 'warning',
      })
    word = res.value
  } catch { return }
  try {
    const r = await whApi.clearAll(word)
    ElMessage.success(r.message || '已清空')
    await Promise.all([loadMaterials(), loadTxns(), loadBadgeCounts()])
  } catch { /* 拦截器已提示 */ }
}

const tab = ref('ov')
const loading = ref(false)
const materials = ref<WhMaterial[]>([])
const lowCount = ref(0)
const kw = ref('')

async function loadMaterials() {
  loading.value = true
  try {
    const j = await whApi.materials(kw.value || undefined)
    materials.value = j.materials; lowCount.value = j.low_count
  } finally { loading.value = false }
}
onMounted(() => { loadMaterials(); loadMatDict(); loadCustomFields(); loadBadgeCounts() })

const totalStock = computed(() => materials.value.reduce((s, m) => s + m.stock, 0))
const totalValue = computed(() => materials.value.reduce((s, m) => s + (m.stock_value || 0), 0))  // 🆕 需求三：库存总价
const lowList = computed(() => materials.value.filter(m => m.low))

// ===== 出入库登记 =====
const ioVisible = ref(false)
const ioForm = reactive({ material_id: undefined as number | undefined, direction: 'in', qty: 1,
  unit_price: null as number | null, biz_date: new Date().toISOString().slice(0, 10), source: '', party: '',
  project_id: undefined as number | undefined })
function openIo(dir: string) {
  Object.assign(ioForm, { material_id: undefined, direction: dir, qty: 1, unit_price: null,
    biz_date: new Date().toISOString().slice(0, 10), source: '', party: '', project_id: undefined })
  if (dir === 'out' && !projects.value.length) loadProjects()   // 🆕 出库要选领用项目→项目材料成本
  ioVisible.value = true
}
const ioAmount = computed(() => ioForm.unit_price != null ? Number((ioForm.qty * ioForm.unit_price).toFixed(2)) : null)
const ioSubmitting = ref(false)
async function submitIo() {
  if (!ioForm.material_id) { ElMessage.warning('请选择物料'); return }
  if (!ioForm.qty || ioForm.qty <= 0) { ElMessage.warning('数量须为正'); return }
  ioSubmitting.value = true
  try {
    const r: any = await whApi.createTxn({ ...ioForm })
    ElMessage.success(r.message || '已登记')
    ioVisible.value = false
    await Promise.all([loadMaterials(), loadTxns()])
  } catch { /* 超量等错误由拦截器提示 */ } finally { ioSubmitting.value = false }
}
function matLabel(m: WhMaterial) { return `${m.name}${m.spec ? '·' + m.spec : ''}（现存 ${m.stock}）` }

// ===== 流水 =====
const txns = ref<WhTxn[]>([])
const txnDir = ref('')
async function loadTxns() {
  txns.value = await whApi.txns({ direction: txnDir.value || undefined })
}
async function reverseTxn(t: WhTxn) {
  await ElMessageBox.confirm(`冲红单据 ${t.ref_no}？将生成反向单据回滚库存，原单保留。`, '冲红', { type: 'warning' })
  const r: any = await whApi.reverse(t.id)
  ElMessage.success(r.message || '已冲红')
  await Promise.all([loadTxns(), loadMaterials()])
}

// ===== 收发存 =====
const period = ref(new Date().toISOString().slice(0, 7))
const summary = ref<WhSummaryRow[]>([])
async function loadSummary() { summary.value = await whApi.summary(period.value) }

// ===== 物料主数据 =====
const matVisible = ref(false)
const matForm = reactive<any>({ id: null, name: '', spec: '', category: '', unit: '个', unit_price: null, location: '', safety_stock: 0, init_stock: 0, custom_values: {} })

// ===== 🆕 物料自定义字段（可配置列，跟采购 R6 同一套做法） =====
const canConfigFields = computed(() => auth.hasRole('warehouse_lead', 'admin', 'manager'))
const customFields = ref<WhCustomField[]>([])
async function loadCustomFields() {
  try { customFields.value = await whApi.customFields() } catch { customFields.value = [] }
}
const listCustomFields = computed(() => customFields.value.filter(f => f.enabled && f.show_in_list))
const formCustomFields = computed(() => customFields.value.filter(f => f.enabled))
function cfDisplay(cv: Record<string, any> | undefined, f: WhCustomField): string {
  const v = cv?.[String(f.id)]
  return v == null || v === '' ? '—' : String(v)
}
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
function cfEdit(f: WhCustomField) {
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
    if (cfEditingId.value) { await whApi.updateCustomField(cfEditingId.value, payload); ElMessage.success('已更新') }
    else { await whApi.createCustomField(payload); ElMessage.success('已新增字段') }
    cfResetForm(); await loadCustomFields()
  } catch { /* handled */ } finally { cfSaving.value = false }
}
async function cfDelete(f: WhCustomField) {
  try { await ElMessageBox.confirm(`删除字段「${f.label}」？已录入物料的历史值保留但不再显示/校验。`, '删除字段', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await whApi.deleteCustomField(f.id); ElMessage.success('已删除'); await loadCustomFields() } catch { /* handled */ }
}
// 🆕 类别/单位 改为受管理字典：只从启用项里选（采购主管/admin 在采购管理页维护）
interface MatDictItem { id: number; dtype: string; value: string; sort_order: number; enabled: boolean }
const matDict = ref<MatDictItem[]>([])
async function loadMatDict() {
  try { matDict.value = (await http.get<MatDictItem[]>('/wh/material-dict', { params: { enabled_only: true } })).data }
  catch { matDict.value = [] }
}
const matCatOptions = computed(() => matDict.value.filter(d => d.dtype === 'category').map(d => d.value))
const matUnitOptions = computed(() => matDict.value.filter(d => d.dtype === 'unit').map(d => d.value))
const matGradeOptions = computed(() => matDict.value.filter(d => d.dtype === 'material_grade').map(d => d.value))
function openMat(m?: WhMaterial) {
  if (m) Object.assign(matForm, { ...m, custom_values: { ...(m.custom_values || {}) } })
  else Object.assign(matForm, { id: null, name: '', spec: '', category: '', material_grade: '', unit: '个', unit_price: null, location: '', safety_stock: 0, init_stock: 0, custom_values: {} })
  matVisible.value = true
}
// 🆕 删除物料（有出入库流水的后端会拦截）
async function deleteMat(m: WhMaterial) {
  try { await ElMessageBox.confirm(`删除物料「${m.name}${m.spec ? '·' + m.spec : ''}」？删除后不可恢复。\n（有出入库流水的物料不能删除）`, '删除物料', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await whApi.deleteMaterial(m.id); ElMessage.success('物料已删除'); await loadMaterials() } catch { /* 拦截器已提示 */ }
}
const matSubmitting = ref(false)
async function submitMat() {
  if (!matForm.name.trim()) { ElMessage.warning('请填写物料名称'); return }
  matSubmitting.value = true
  try {
    if (matForm.id) await whApi.updateMaterial(matForm.id, matForm)
    else await whApi.createMaterial(matForm)
    ElMessage.success('已保存')
    matVisible.value = false
    await loadMaterials()
  } catch { /* 查重等错误由拦截器提示 */ } finally { matSubmitting.value = false }
}

// ===== 🆕 发货清单目录（设计推送 → 仓库只看/下载/打印 → 点「已备齐」通知物流）=====
const shipPending = ref<ShipListPendingRow[]>([])
const shipPendingLoading = ref(false)
const shipFilter = ref<'requested' | 'ready' | 'all'>('requested')
async function loadShipPending() {
  shipPendingLoading.value = true
  try { shipPending.value = await whApi.shipListPending(shipFilter.value) }
  finally { shipPendingLoading.value = false }
}
watch(shipFilter, () => loadShipPending())
async function markShipReady(row: ShipListPendingRow) {
  try {
    await ElMessageBox.confirm(`确认「${row.code} ${row.name}」已按发货清单备齐货物？将通知物流发货部可安排发货。`, '已备齐', { type: 'success', confirmButtonText: '已备齐' })
  } catch { return }
  const r: any = await whApi.shipListReady(row.project_id)
  ElMessage.success(r?.message || '已标记备齐，已通知物流')
  await loadShipPending(); loadBadgeCounts()
}

// ===== 🆕 采购收货：仓库对采购下单的物料确认收货、补送货单号/到货日期/后填价格 =====
interface RecvItem {
  id: number; po_no?: string | null; supplier_id: number; supplier_name: string
  project_code?: string | null; item_name: string; spec?: string | null
  qty?: number | null; unit_price?: number | null; received_amount: number
  delivery_note_no?: string | null; arrival_date?: string | null
  receipt_count?: number   // 🆕 需求十四：已上传收货单数量
}
const recvItems = ref<RecvItem[]>([])
const recvLoading = ref(false)
const recvReceived = ref(false)        // false=待收货 / true=已收货
const recvSupplier = ref<number | ''>('')
const recvPo = ref('')
const recvSupplierOptions = computed(() => {
  const m = new Map<number, string>()
  for (const i of recvItems.value) m.set(i.supplier_id, i.supplier_name)
  return Array.from(m, ([id, name]) => ({ id, name }))
})

// 🆕 需求二：采购收货列表也按采购单号(po_no)合并——同一采购单(≥2行)收成一个可展开的主汇总父行，
//   收货/送货单号、到货日期在父行上体现并在父行统一维护（合并收货写整批各行）。单行/无采购单号散单平铺。
const recvRowKey = (row: any) => (row._isGroup ? row._key : 'i' + row.id)
// 🆕 折叠层级配色：父(汇总)行 grp-row；子零件行由 Element 自动加 --level-1，走全局样式
const grpRowClass = ({ row }: { row: any }) => (row._isGroup ? 'grp-row' : '')
const groupedRecv = computed<any[]>(() => {
  const groups = new Map<string, any>()
  const out: any[] = []
  for (const it of recvItems.value) {
    const po = it.po_no
    if (!po) { out.push(it); continue }
    let g = groups.get(po)
    if (!g) {
      g = {
        _isGroup: true, _key: 'g:' + po, po_no: po,
        supplier_name: it.supplier_name, supplier_id: it.supplier_id,
        qty: 0, received_amount: 0, receipt_count: 0,
        _codes: new Set<string>(), _dnotes: new Set<string>(), _arrivals: new Set<string>(),
        children: [] as RecvItem[],
      }
      groups.set(po, g); out.push(g)
    }
    g.children.push(it)
    g.qty += it.qty || 0
    g.received_amount += it.received_amount || 0
    g.receipt_count += it.receipt_count || 0
    if (it.project_code) g._codes.add(it.project_code)
    if (it.delivery_note_no) g._dnotes.add(it.delivery_note_no)
    if (it.arrival_date) g._arrivals.add(it.arrival_date)
  }
  return out.map((r) => {
    if (!r._isGroup) return r
    if (r.children.length === 1) return r.children[0]   // 单行采购单直接平铺
    r._count = r.children.length
    const codes = Array.from(r._codes) as string[]
    r.project_code = codes.length === 0 ? null : codes.length === 1 ? codes[0] : '多个'
    const dnotes = Array.from(r._dnotes) as string[]
    r.delivery_note_no = dnotes.length === 0 ? null : dnotes.length === 1 ? dnotes[0] : '多个'
    const arrivals = Array.from(r._arrivals) as string[]
    r.arrival_date = arrivals.length === 0 ? null : arrivals.length === 1 ? arrivals[0] : '多个'
    return r
  })
})
async function loadReceiving() {
  recvLoading.value = true
  try {
    const r = await http.get<RecvItem[]>('/purchase-mgmt/receiving', {
      params: {
        received: recvReceived.value,
        supplier_id: recvSupplier.value || undefined,
        po_no: recvPo.value || undefined,
      },
    })
    recvItems.value = r.data
  } finally { recvLoading.value = false }
}

// 🆕 #141 tab 待办数徽标：待收货 / 待备货（红色角标，进页面就能看到有几条待处理）
const recvPendingCount = ref(0)
const recvDoneCount = ref(0)   // 已收货条数
const shipPendingCount = ref(0)
async function loadBadgeCounts() {
  try {
    const [recv, done, ship] = await Promise.all([
      http.get<RecvItem[]>('/purchase-mgmt/receiving', { params: { received: false } }),
      http.get<RecvItem[]>('/purchase-mgmt/receiving', { params: { received: true } }),
      whApi.shipListPending('requested'),
    ])
    recvPendingCount.value = recv.data.length
    recvDoneCount.value = done.data.length
    shipPendingCount.value = ship.length
  } catch { /* 徽标非关键，失败忽略 */ }
}
const recvVisible = ref(false)
const recvSaving = ref(false)
const recvForm = reactive({
  id: 0, po_no: '', supplier_name: '', item_name: '', spec: '', qty: null as number | null,
  delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10),
  unit_price: null as number | null, received_amount: null as number | null,
})
function openReceive(it: RecvItem) {
  Object.assign(recvForm, {
    id: it.id, po_no: it.po_no || '', supplier_name: it.supplier_name,
    item_name: it.item_name, spec: it.spec || '', qty: it.qty ?? null,
    delivery_note_no: it.delivery_note_no || '',
    arrival_date: it.arrival_date || new Date().toISOString().slice(0, 10),
    unit_price: it.unit_price ?? null,
    received_amount: it.received_amount || null,
  })
  recvReceiptFile.value = null
  recvVisible.value = true
}
function onRecvCalc() {   // 填单价 → 算总价（收货金额）
  if (recvForm.qty != null && recvForm.unit_price != null) {
    recvForm.received_amount = Number((recvForm.qty * recvForm.unit_price).toFixed(2))
  }
}
function onRecvAmountCalc() {   // #186 填总价(收货金额) → 按数量均分算单价
  if (recvForm.qty && recvForm.qty > 0 && recvForm.received_amount != null) {
    recvForm.unit_price = Number((recvForm.received_amount / recvForm.qty).toFixed(4))
  }
}
// 🆕 需求十四：单条收货时可上传收货单（图片/PDF）
const recvReceiptFile = ref<File | null>(null)
function pickRecvReceipt() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.jpg,.jpeg,.png,.pdf,.webp'
  input.onchange = () => { recvReceiptFile.value = input.files?.[0] || null }
  input.click()
}
async function uploadReceipt(itemId: number, file: File) {
  const fd = new FormData(); fd.append('file', file)
  await http.post(`/purchase-mgmt/items/${itemId}/receipt`, fd)
}
async function submitReceive() {
  if (!recvForm.arrival_date) { ElMessage.warning('请填写到货日期'); return }
  recvSaving.value = true
  try {
    await http.put(`/purchase-mgmt/items/${recvForm.id}/receive`, {
      delivery_note_no: recvForm.delivery_note_no || null,
      arrival_date: recvForm.arrival_date,
      unit_price: recvForm.unit_price,
      received_amount: recvForm.received_amount,
    })
    if (recvReceiptFile.value) await uploadReceipt(recvForm.id, recvReceiptFile.value)
    ElMessage.success('已确认收货')
    recvVisible.value = false
    await loadReceiving(); loadBadgeCounts()
  } catch { /* handled */ } finally { recvSaving.value = false }
}

// 🆕 查看某明细的收货单（预览最新一张）
async function viewReceipts(item: RecvItem) {
  try {
    const list = (await http.get<{ id: number; name: string }[]>(`/purchase-mgmt/items/${item.id}/receipts`)).data
    if (!list.length) { ElMessage.info('暂无收货单'); return }
    previewRef.value?.open({ id: list[0].id, name: list[0].name })
  } catch { ElMessage.error('打开收货单失败') }
}

// ===== 🆕 需求四：合并零件收货（勾选多条 → 只填合并总价 或 逐行单价）=====
const recvSelected = ref<RecvItem[]>([])
function onRecvSelect(rows: RecvItem[]) { recvSelected.value = rows }
const batchRecvVisible = ref(false)
const batchRecvSaving = ref(false)
const batchRecvMode = ref<'total' | 'lines'>('lines')   // #1：去掉「合并总价按数量分摊」，只逐行填价
const batchRecvForm = reactive({ delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10), total_amount: null as number | null })
const batchRecvLines = ref<{ item_id: number; item_name: string; spec?: string | null; qty: number | null; unit_price: number | null; received_amount: number | null }[]>([])
const batchReceiptFile = ref<File | null>(null)
function pickBatchReceipt() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.jpg,.jpeg,.png,.pdf,.webp'
  input.onchange = () => { batchReceiptFile.value = input.files?.[0] || null }
  input.click()
}
const batchTotalQty = computed(() => batchRecvLines.value.reduce((s, l) => s + (l.qty || 0), 0))
function splitShare(line: { qty: number | null }): number {
  if (batchRecvForm.total_amount == null) return 0
  const tq = batchTotalQty.value
  if (tq > 0) return Number((batchRecvForm.total_amount * (line.qty || 0) / tq).toFixed(2))
  return Number((batchRecvForm.total_amount / (batchRecvLines.value.length || 1)).toFixed(2))
}
function openBatchReceive() {
  if (recvSelected.value.length < 1) { ElMessage.info('请先在列表勾选要合并收货的明细'); return }
  batchRecvMode.value = 'lines'
  Object.assign(batchRecvForm, { delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10), total_amount: null })
  batchReceiptFile.value = null
  batchRecvLines.value = recvSelected.value.map(i => ({
    item_id: i.id, item_name: i.item_name, spec: i.spec, qty: i.qty ?? null,
    unit_price: i.unit_price ?? null, received_amount: i.received_amount || null,
  }))
  batchRecvVisible.value = true
}
// 🆕 需求二：主汇总父行「合并收货」——直接对该采购单下所有零件行整批收货/维护送货单号
function openBatchReceiveGroup(row: any) {
  const children = (row.children || []) as RecvItem[]
  if (!children.length) return
  batchRecvMode.value = 'lines'
  const dnote = row.delivery_note_no && row.delivery_note_no !== '多个' ? row.delivery_note_no : ''
  const adate = row.arrival_date && row.arrival_date !== '多个' ? row.arrival_date : new Date().toISOString().slice(0, 10)
  Object.assign(batchRecvForm, { delivery_note_no: dnote, arrival_date: adate, total_amount: null })
  batchReceiptFile.value = null
  batchRecvLines.value = children.map(i => ({
    item_id: i.id, item_name: i.item_name, spec: i.spec, qty: i.qty ?? null,
    unit_price: i.unit_price ?? null, received_amount: i.received_amount || null,
  }))
  batchRecvVisible.value = true
}
async function submitBatchReceive() {
  if (!batchRecvForm.arrival_date) { ElMessage.warning('请填写到货日期'); return }
  batchRecvSaving.value = true
  try {
    const body: any = {
      item_ids: batchRecvLines.value.map(l => l.item_id),
      delivery_note_no: batchRecvForm.delivery_note_no || null,
      arrival_date: batchRecvForm.arrival_date,
    }
    body.lines = batchRecvLines.value.map(l => ({ item_id: l.item_id, unit_price: l.unit_price, received_amount: l.received_amount }))
    await http.post('/purchase-mgmt/items/receive-batch', body)
    if (batchReceiptFile.value) {
      for (const l of batchRecvLines.value) await uploadReceipt(l.item_id, batchReceiptFile.value)
    }
    ElMessage.success(`已合并收货 ${batchRecvLines.value.length} 条`)
    batchRecvVisible.value = false
    await loadReceiving(); loadBadgeCounts()
  } catch { /* handled */ } finally { batchRecvSaving.value = false }
}

// ===== 🆕 项目物料需求（清单→仓库）=====
interface DemandRow {
  item_name: string; spec?: string | null; material_id?: number | null
  demand_qty?: number | null; stock: number; suggest_purchase: number
  purchase_status: string; in_stock: boolean; issued_qty: number
}
const demandProj = ref<number | undefined>()
const demandRows = ref<DemandRow[]>([])
const demandLoading = ref(false)
async function loadDemand() {
  if (!demandProj.value) { demandRows.value = []; return }
  demandLoading.value = true
  try { demandRows.value = (await http.get<DemandRow[]>(`/wh/demand/${demandProj.value}`)).data }
  finally { demandLoading.value = false }
}
watch(demandProj, () => loadDemand())

// 🆕 #157：物料需求总览——不用下拉选项目，直接列出有清单的项目 + 待出库/已出库条数
interface DemandOverviewRow {
  project_id: number; code: string; name: string
  total_lines: number; pending_out: number; issued_out: number
}
const demandOverview = ref<DemandOverviewRow[]>([])
const demandOverviewLoading = ref(false)
async function loadDemandOverview() {
  demandOverviewLoading.value = true
  try { demandOverview.value = (await http.get<DemandOverviewRow[]>('/wh/demand-overview')).data }
  finally { demandOverviewLoading.value = false }
}
const demandProjLabel = computed(() => {
  const p = demandOverview.value.find(x => x.project_id === demandProj.value)
    || projects.value.find(x => x.id === demandProj.value)
  return p ? `${p.code} · ${p.name}` : ''
})
function openDemandProject(pid: number) { demandProj.value = pid }
function backToDemandOverview() { demandProj.value = undefined; loadDemandOverview() }

// 🆕 需求二：物料需求「领用出库」——按需求把有货物料自动登记出库到项目（计入项目材料成本）
function demandRemain(r: DemandRow) { return Math.max(0, (r.demand_qty || 0) - (r.issued_qty || 0)) }
async function issueOne(row: DemandRow) {
  if (!row.material_id) { ElMessage.warning('该物料尚未在仓库建档，无法出库'); return }
  const def = Math.min(demandRemain(row) || row.stock, row.stock)
  let qty = def
  try {
    const res = await ElMessageBox.prompt(
      `领用「${row.item_name}${row.spec ? '·' + row.spec : ''}」出库数量（现存 ${row.stock}，未领需求 ${demandRemain(row)}）：`,
      '领用出库', { inputValue: String(def), inputPattern: /^\d+(\.\d+)?$/, inputErrorMessage: '请输入数字', confirmButtonText: '出库' })
    qty = Number(res.value)
  } catch { return }
  if (!qty || qty <= 0) return
  try {
    const r = await whApi.issueDemand(demandProj.value!, [{ material_id: row.material_id, qty }])
    ElMessage.success(r.message || '已出库')
    await Promise.all([loadDemand(), loadMaterials()])
  } catch { /* 拦截器已提示 */ }
}
async function issueAll() {
  const lines = demandRows.value
    .filter(r => r.material_id && r.stock > 0 && demandRemain(r) > 0)
    .map(r => ({ material_id: r.material_id!, qty: Math.min(demandRemain(r), r.stock) }))
  if (!lines.length) { ElMessage.info('没有可领用出库的物料（需有货且仍有未领用需求）'); return }
  try {
    await ElMessageBox.confirm(`将按需求把 ${lines.length} 种有货物料领用出库到本项目？会自动登记出库并计入项目材料成本。`,
      '一键领用出库', { type: 'warning', confirmButtonText: '领用出库' })
  } catch { return }
  try {
    const r = await whApi.issueDemand(demandProj.value!, lines)
    ElMessage.success(r.message || '已出库')
    await Promise.all([loadDemand(), loadMaterials()])
  } catch { /* 拦截器已提示 */ }
}

// ===== 项目列表（物料需求 tab 与发货清单目录共用）=====
const projects = ref<{ id: number; code: string; name: string }[]>([])
async function loadProjects() {
  // 复用一览接口取项目（仓库有详单权限）
  try { projects.value = (await http.get('/projects')).data.map((p: any) => ({ id: p.id, code: p.code, name: p.name })) }
  catch { projects.value = [] }
}
// 发货清单文件：预览（图片弹窗 / PDF 新标签 / 其它直接下载）
const previewRef = ref<InstanceType<typeof AttachmentPreview>>()
function previewShipList(item: ShipListFile) { previewRef.value?.open({ id: item.id, name: item.name }) }

// 🆕 打印发货清单：PDF/图片经隐藏 iframe 直接调起打印；Excel 等格式提示下载后打印
async function printShipList(item: ShipListFile) {
  if (!isPdfAtt(item.name) && !isImageAtt(item.name)) {
    ElMessage.info('该格式（如 Excel）请下载后打印')
    downloadAttachment({ id: item.id, name: item.name })
    return
  }
  let url = ''
  try { url = await attachmentBlobUrl(item.id) } catch { ElMessage.error('打开文件失败'); return }
  const iframe = document.createElement('iframe')
  iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0'
  if (isImageAtt(item.name)) {
    iframe.srcdoc = `<html><head><style>@page{margin:8mm}html,body{margin:0}img{max-width:100%}</style></head>`
      + `<body><img src="${url}" onload="window.focus();window.print()"></body></html>`
  } else {
    iframe.src = url
    iframe.onload = () => { try { iframe.contentWindow?.focus(); iframe.contentWindow?.print() } catch { /* 弹窗被拦时用户可手动打印 */ } }
  }
  document.body.appendChild(iframe)
  setTimeout(() => { URL.revokeObjectURL(url); iframe.remove() }, 60000)
}

function onTab(name: string) {
  if (name === 'txn' && !txns.value.length) loadTxns()
  if (name === 'sum') loadSummary()
  if (name === 'recv') loadReceiving()
  if (name === 'demand') {
    if (!projects.value.length) loadProjects()
    if (!demandProj.value) loadDemandOverview()   // #157：进 tab 直接看项目总览
  }
  if (name === 'ship') {
    if (!projects.value.length) loadProjects()
    loadShipPending()
  }
  if (name === 'preq') loadPurchReqs()
}

// 🆕 #167 仓库采购申请：仓库列出要买什么 → 提交到采购部
interface PreqLine { item_name: string; spec: string; qty: number | null; project_code: string; notes: string }
interface PreqRow { id: number; status: string; notes?: string | null; created_at: string
  handler_name?: string | null; reject_reason?: string | null
  lines: { item_name: string; spec?: string | null; qty?: number | null; project_code?: string | null; notes?: string | null }[] }
const PREQ_STATUS: Record<string, string> = { pending: '待处理', done: '已处理', rejected: '已驳回' }
const preqList = ref<PreqRow[]>([])
const preqLoading = ref(false)
async function loadPurchReqs() {
  preqLoading.value = true
  try { preqList.value = (await http.get<PreqRow[]>('/purchase-mgmt/purchase-requests')).data }
  finally { preqLoading.value = false }
}
function blankPreqLine(): PreqLine { return { item_name: '', spec: '', qty: null, project_code: '', notes: '' } }
const preqVisible = ref(false)
const preqSaving = ref(false)
const preqForm = reactive({ buyer_id: '' as number | '', notes: '', lines: [blankPreqLine()] as PreqLine[] })
const preqBuyers = ref<{ id: number; name: string }[]>([])
async function loadPreqBuyers() {
  try { preqBuyers.value = (await http.get<{ id: number; name: string }[]>('/purchase-mgmt/buyers')).data }
  catch { preqBuyers.value = [] }
}
function openPurchReq() {
  preqForm.buyer_id = ''; preqForm.notes = ''; preqForm.lines = [blankPreqLine()]
  if (!preqBuyers.value.length) loadPreqBuyers()
  preqVisible.value = true
}
function addPreqLine() { preqForm.lines.push(blankPreqLine()) }
function removePreqLine(i: number) { preqForm.lines.splice(i, 1); if (!preqForm.lines.length) preqForm.lines.push(blankPreqLine()) }
async function submitPurchReq() {
  const lines = preqForm.lines.filter(l => l.item_name.trim())
  if (!lines.length) { ElMessage.error('请至少填写一行要采购的物料（名称必填）'); return }
  preqSaving.value = true
  try {
    await http.post('/purchase-mgmt/purchase-requests', {
      buyer_id: preqForm.buyer_id || null,
      notes: preqForm.notes || null,
      lines: lines.map(l => ({ item_name: l.item_name.trim(), spec: l.spec || null, qty: l.qty, project_code: l.project_code || null, notes: l.notes || null })),
    })
    ElMessage.success(preqForm.buyer_id ? '采购申请已提交，已通知该采购员' : '采购申请已提交，采购部会收到通知')
    preqVisible.value = false
    await loadPurchReqs()
  } catch { /* handled */ } finally { preqSaving.value = false }
}
async function deletePurchReq(row: PreqRow) {
  try { await ElMessageBox.confirm(`删除采购申请 #${row.id}？`, '提示', { type: 'warning' }) } catch { return }
  try { await http.delete(`/purchase-mgmt/purchase-requests/${row.id}`); ElMessage.success('已删除'); await loadPurchReqs() } catch { /* handled */ }
}
function preqStatusVariant(s: string): 'warn' | 'success' | 'danger' {
  return s === 'done' ? 'success' : s === 'rejected' ? 'danger' : 'warn'
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>仓库</h1>
        <div class="desc">物料主数据 + 出入库（自动单号·超库存拦截）+ 收发存汇总 + 流水（冲红）+ 发货清单</div>
      </div>
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-tabs v-model="tab" @tab-change="onTab">
        <!-- 总览 -->
        <el-tab-pane v-if="tv('ov')" label="库存总览" name="ov">
          <div class="kpi-grid">
            <div class="kpi"><div class="kpi-v">{{ materials.length }}</div><div class="kpi-l">物料种类</div></div>
            <div class="kpi"><div class="kpi-v">{{ totalStock }}</div><div class="kpi-l">库存总量</div></div>
            <div v-if="isManager" class="kpi"><div class="kpi-v">{{ fmtMoney(totalValue) }}</div><div class="kpi-l">库存总价</div></div>
            <div class="kpi" :class="lowCount ? 'is-bad' : ''"><div class="kpi-v">{{ lowCount }}</div><div class="kpi-l">低于安全库存</div></div>
          </div>
          <el-alert v-if="lowList.length" type="warning" :closable="false" style="margin:10px 0"
                    :title="`⚠ 低库存预警：${lowList.map(m => m.name + (m.spec ? '·' + m.spec : '')).join('、')}`" />
          <div style="display:flex;gap:10px;margin-bottom:10px">
            <el-input v-model="kw" placeholder="搜索物料" :prefix-icon="Search" clearable style="width:240px" @change="loadMaterials" />
          </div>
          <!-- 🆕 #140 列宽用 min-width 平均分布，避免名称/规格独占空白、右侧列挤在一起 -->
          <el-table :data="materials" stripe size="small" max-height="calc(100vh - 240px)">
            <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="spec" label="规格型号" min-width="140"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="category" label="类别" min-width="110"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="70" align="center" />
            <el-table-column label="现存" min-width="90" align="right">
              <template #default="{ row }"><b :class="{ bad: row.low }">{{ row.stock }}</b></template>
            </el-table-column>
            <el-table-column v-if="isManager" label="单价" min-width="90" align="right">
              <template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template>
            </el-table-column>
            <el-table-column v-if="isManager" label="总价" min-width="100" align="right">
              <template #default="{ row }"><b>{{ row.stock_value != null ? fmtMoney(row.stock_value) : '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="safety_stock" label="安全库存" min-width="100" align="right" />
            <el-table-column prop="location" label="库位" min-width="100"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
          </el-table>
          <EmptyHint v-if="!materials.length" text="暂无物料，去「物料主数据」新增" size="sm" />
        </el-tab-pane>

        <!-- 出入库登记 -->
        <el-tab-pane v-if="tv('io')" label="出入库登记" name="io">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可登记出入库" :icon="Lock" />
          <template v-else>
            <el-button type="primary" :icon="Plus" @click="openIo('in')">入库登记</el-button>
            <el-button type="warning" :icon="Plus" @click="openIo('out')">出库登记</el-button>
            <div class="muted small" style="margin-top:10px">入库单号 RK+日期+序号；出库单号 CK…；出库超现存将被拦截。</div>
          </template>
        </el-tab-pane>

        <!-- 收发存汇总 -->
        <el-tab-pane v-if="tv('sum')" label="收发存汇总" name="sum">
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
            <el-date-picker v-model="period" type="month" value-format="YYYY-MM" @change="loadSummary" />
            <span class="muted small">期初 + 本期入 − 本期出 = 期末</span>
          </div>
          <el-table :data="summary" stripe size="small" show-summary
                    :summary-method="(p:any) => ['合计','','', summary.reduce((s,r)=>s+r.opening,0), summary.reduce((s,r)=>s+r.in_qty,0), summary.reduce((s,r)=>s+r.out_qty,0), summary.reduce((s,r)=>s+r.closing,0)]"
                    max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column prop="name" label="物料" min-width="120" />
            <el-table-column prop="spec" label="规格" min-width="100"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column prop="opening" label="期初" width="90" />
            <el-table-column prop="in_qty" label="本期入" width="90" />
            <el-table-column prop="out_qty" label="本期出" width="90" />
            <el-table-column prop="closing" label="期末" width="90"><template #default="{ row }"><b>{{ row.closing }}</b></template></el-table-column>
          </el-table>
          <EmptyHint v-if="!summary.length" text="该月暂无收发存数据" size="sm" />
        </el-tab-pane>

        <!-- 流水 -->
        <el-tab-pane v-if="tv('txn')" label="出入库流水" name="txn">
          <div style="margin-bottom:10px">
            <el-radio-group v-model="txnDir" @change="loadTxns" size="small">
              <el-radio-button value="">全部</el-radio-button>
              <el-radio-button value="in">入库</el-radio-button>
              <el-radio-button value="out">出库</el-radio-button>
            </el-radio-group>
          </div>
          <el-table :data="txns" stripe size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column prop="ref_no" label="单号" width="140" />
            <el-table-column prop="biz_date" label="日期" width="110">
              <template #default="{ row }">{{ fmtDate(row.biz_date) }}</template>
            </el-table-column>
            <el-table-column label="物料" min-width="130"><template #default="{ row }">{{ row.material_name }}{{ row.spec ? '·' + row.spec : '' }}</template></el-table-column>
            <el-table-column label="方向" width="70">
              <template #default="{ row }"><StatusPill :text="row.direction === 'in' ? '入库' : '出库'" :variant="row.direction === 'in' ? 'success' : 'warn'" /></template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" width="70" />
            <el-table-column label="单价" width="90"><template #default="{ row }">{{ fmtMoney(row.unit_price) }}</template></el-table-column>
            <el-table-column label="金额" width="100"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column prop="source" label="来源/用途" width="100"><template #default="{ row }">{{ row.source || '—' }}</template></el-table-column>
            <el-table-column prop="party" label="供应商/领用方" min-width="110"><template #default="{ row }">{{ row.party || '—' }}</template></el-table-column>
            <el-table-column prop="project_code" label="项目" width="100"><template #default="{ row }">{{ row.project_code || '—' }}</template></el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <StatusPill v-if="row.is_reversal" text="冲红单" variant="muted" />
                <StatusPill v-else-if="row.reversed" text="已冲红" variant="danger" />
                <el-button v-else-if="canWrite" size="small" link type="danger" @click="reverseTxn(row)">冲红</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!txns.length" text="暂无出入库流水" size="sm" />
        </el-tab-pane>

        <!-- 物料主数据 -->
        <el-tab-pane v-if="tv('mat')" label="物料主数据" name="mat">
          <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openMat()" style="margin-bottom:10px">新增物料</el-button>
          <el-button v-if="canConfigFields" :icon="Setting" @click="openFieldManager" style="margin-bottom:10px;margin-left:8px">字段设置</el-button>
          <el-button v-if="canClear" type="danger" plain :icon="Delete" @click="clearAll" style="margin-bottom:10px;margin-left:8px">一键清空</el-button>
          <el-table :data="materials" stripe size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="spec" label="规格型号" min-width="120"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="category" label="类别" width="100"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column prop="material_grade" label="材质" width="100"><template #default="{ row }">{{ row.material_grade || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column label="单价" width="90" align="right"><template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template></el-table-column>
            <el-table-column prop="safety_stock" label="安全库存" width="90" />
            <el-table-column prop="init_stock" label="期初库存" width="90" />
            <el-table-column prop="location" label="库位" width="90"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
            <el-table-column v-for="f in listCustomFields" :key="f.id" :label="f.label" min-width="100">
              <template #default="{ row }">{{ cfDisplay(row.custom_values, f) }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="110" fixed="right"><template #default="{ row }"><el-button size="small" link type="primary" @click="openMat(row)">编辑</el-button><el-button size="small" link type="danger" @click="deleteMat(row)">删除</el-button></template></el-table-column>
          </el-table>
          <EmptyHint v-if="!materials.length" text="暂无物料主数据，点「新增物料」开始" size="sm" />
        </el-tab-pane>

        <!-- 🆕 项目物料需求（清单→仓库）-->
        <el-tab-pane v-if="tv('demand')" label="物料需求" name="demand">
          <!-- #157：默认直接列出有清单的项目 + 待出库/已出库条数，不用先从下拉选项目 -->
          <template v-if="!demandProj">
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <b>项目物料需求总览</b>
              <el-button :icon="Search" size="small" @click="loadDemandOverview">刷新</el-button>
              <span class="muted small">列出有「标准件清单」的项目；点「查看」进入逐行需求并领用出库。待出库=有货且未领完的物料行数，已出库=已领用过的行数。</span>
            </div>
            <el-table :data="demandOverview" v-loading="demandOverviewLoading" stripe size="small"
                      max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl">
              <el-table-column label="项目编号" width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
              <el-table-column prop="name" label="项目名称" min-width="200" show-overflow-tooltip />
              <el-table-column label="物料行数" width="100" align="right"><template #default="{ row }">{{ row.total_lines }}</template></el-table-column>
              <el-table-column label="待出库" width="110" align="center">
                <template #default="{ row }"><StatusPill :text="`待出库 ${row.pending_out}`" :variant="row.pending_out > 0 ? 'warn' : 'muted'" /></template>
              </el-table-column>
              <el-table-column label="已出库" width="110" align="center">
                <template #default="{ row }"><StatusPill :text="`已出库 ${row.issued_out}`" :variant="row.issued_out > 0 ? 'success' : 'muted'" /></template>
              </el-table-column>
              <el-table-column label="操作" width="100" align="center" fixed="right">
                <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDemandProject(row.project_id)">查看</el-button></template>
              </el-table-column>
              <template #empty><EmptyHint text="暂无带标准件清单的项目" size="sm" /></template>
            </el-table>
          </template>

          <template v-else>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <el-button :icon="ArrowLeft" size="small" @click="backToDemandOverview">返回项目列表</el-button>
            <b class="code">{{ demandProjLabel }}</b>
            <el-button v-if="canWrite" type="warning" :icon="Plus" size="small" @click="issueAll">一键领用出库</el-button>
            <span class="muted small">读项目「标准件清单」,逐行看 需求量 / 现有库存 / 建议采购量。有货的可直接领用出库(自动登记出库),缺的走采购。</span>
          </div>
          <el-table :data="demandRows" v-loading="demandLoading" stripe size="small"
                    max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl">
            <el-table-column prop="item_name" label="名称" min-width="150" />
            <el-table-column prop="spec" label="规格型号" min-width="150"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column label="需求量" width="90" align="right"><template #default="{ row }">{{ row.demand_qty ?? '—' }}</template></el-table-column>
            <el-table-column label="现有库存" width="100" align="right">
              <template #default="{ row }"><b :class="{ bad: row.stock <= 0 }">{{ row.stock }}</b></template>
            </el-table-column>
            <el-table-column label="建议采购" width="100" align="right">
              <template #default="{ row }"><span :class="{ bad: row.suggest_purchase > 0 }">{{ row.suggest_purchase }}</span></template>
            </el-table-column>
            <el-table-column label="库存" width="90" align="center">
              <template #default="{ row }"><StatusPill :text="row.in_stock ? '有货可出' : '需采购'" :variant="row.in_stock ? 'success' : 'warn'" /></template>
            </el-table-column>
            <el-table-column label="采购状态" width="100" align="center">
              <template #default="{ row }">
                <StatusPill :text="row.purchase_status" :variant="row.purchase_status === '已到货' ? 'success' : row.purchase_status === '已下单' ? 'primary' : 'muted'" />
              </template>
            </el-table-column>
            <el-table-column label="已领用" width="90" align="right">
              <template #default="{ row }">{{ row.issued_qty || 0 }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="110" align="center" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.material_id && row.stock > 0" size="small" type="warning" plain @click="issueOne(row)">领用出库</el-button>
                <span v-else class="muted small">—</span>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!demandLoading && !demandRows.length" text="该项目暂无标准件清单或清单为空" size="sm" />
          </template>
        </el-tab-pane>

        <!-- 🆕 采购收货 -->
        <el-tab-pane v-if="tv('recv')" name="recv">
          <template #label>采购收货<span v-if="recvPendingCount" class="wh-tab-badge">{{ recvPendingCount > 99 ? '99+' : recvPendingCount }}</span></template>
          <EmptyHint v-if="!canWrite" text="仅仓库角色可确认收货" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <el-radio-group v-model="recvReceived" @change="loadReceiving" size="large" class="recv-toggle">
                <el-radio-button :value="false">待收货（{{ recvPendingCount }}）</el-radio-button>
                <el-radio-button :value="true">已收货（{{ recvDoneCount }}）</el-radio-button>
              </el-radio-group>
              <el-select v-model="recvSupplier" placeholder="全部供应商" clearable style="width:180px" @change="loadReceiving">
                <el-option v-for="s in recvSupplierOptions" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
              <el-input v-model="recvPo" placeholder="采购单号" clearable style="width:150px" @change="loadReceiving" />
              <el-button :icon="Search" @click="loadReceiving">查询</el-button>
              <el-button v-if="recvSelected.length" type="primary" @click="openBatchReceive">合并收货 ({{ recvSelected.length }})</el-button>
              <span class="muted small">采购下单的物料到货后，在这里核对规格、填送货单号/到货日期；单价未填的（后填价格）在此补上。合并零件可勾选多条「合并收货」只填总价。</span>
            </div>
            <el-table :data="groupedRecv" v-loading="recvLoading" stripe size="small" @selection-change="onRecvSelect"
                      :row-key="recvRowKey" :tree-props="{ children: 'children' }" default-expand-all
                      :row-class-name="grpRowClass"
                      max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl">
              <el-table-column type="selection" width="40" :selectable="(row: any) => !row._isGroup" />
              <el-table-column prop="po_no" label="采购单号" width="150">
                <template #default="{ row }">
                  <el-tag v-if="row._isGroup" size="small" type="warning" effect="plain" style="margin-right:4px">合并{{ row._count }}</el-tag>
                  <span class="code">{{ row.po_no || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="supplier_name" label="供应商" min-width="130" />
              <el-table-column prop="project_code" label="订单编号" width="110">
                <template #default="{ row }">{{ row.project_code || '—' }}</template>
              </el-table-column>
              <el-table-column prop="item_name" label="名称" min-width="120">
                <template #default="{ row }">{{ row._isGroup ? `共 ${row._count} 项零件` : row.item_name }}</template>
              </el-table-column>
              <el-table-column prop="spec" label="规格型号" min-width="120">
                <template #default="{ row }">{{ row._isGroup ? '' : (row.spec || '—') }}</template>
              </el-table-column>
              <el-table-column label="数量" width="72" align="right">
                <template #default="{ row }">{{ row.qty ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="单价" width="92" align="right">
                <template #default="{ row }">
                  <span v-if="row._isGroup"></span>
                  <span v-else-if="row.unit_price != null">{{ row.unit_price }}</span>
                  <el-tag v-else size="small" type="warning" effect="plain">后填</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="总价" width="104" align="right">
                <template #default="{ row }"><b>{{ row.received_amount ? fmtMoney(row.received_amount) : '—' }}</b></template>
              </el-table-column>
              <el-table-column label="送货单号" width="110">
                <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
              </el-table-column>
              <el-table-column label="到货日期" width="110">
                <template #default="{ row }">{{ row.arrival_date || '—' }}</template>
              </el-table-column>
              <el-table-column label="收货单" width="82" align="center">
                <template #default="{ row }">
                  <span v-if="row._isGroup" class="muted small">{{ row.receipt_count ? `📎 ${row.receipt_count}` : '—' }}</span>
                  <el-button v-else-if="row.receipt_count" size="small" link type="primary" @click="viewReceipts(row)">📎 {{ row.receipt_count }}</el-button>
                  <span v-else class="muted small">—</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="112" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button v-if="row._isGroup" size="small" :type="recvReceived ? 'default' : 'primary'" plain @click="openBatchReceiveGroup(row)">
                    {{ recvReceived ? '合并修改' : '合并收货' }}
                  </el-button>
                  <el-button v-else size="small" :type="recvReceived ? 'default' : 'primary'" plain @click="openReceive(row)">
                    {{ recvReceived ? '修改' : '收货' }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <EmptyHint v-if="!recvLoading && !recvItems.length" :text="recvReceived ? '暂无已收货记录' : '暂无待收货物料'" size="sm" />
          </template>
        </el-tab-pane>

        <!-- 发货清单目录：设计部下发 → 仓库核对备齐 → 通知物流 -->
        <el-tab-pane v-if="tv('ship')" name="ship">
          <template #label>发货清单<span v-if="shipPendingCount" class="wh-tab-badge">{{ shipPendingCount > 99 ? '99+' : shipPendingCount }}</span></template>
          <EmptyHint v-if="!canWrite" text="仅仓库角色可查看发货清单目录" :icon="Lock" />
          <template v-else>
            <div class="ship-cat-head">
              <div class="ship-pending-title" style="margin:0">📋 发货清单目录</div>
              <el-radio-group v-model="shipFilter" size="small">
                <el-radio-button label="requested">待备货</el-radio-button>
                <el-radio-button label="ready">已备齐</el-radio-button>
                <el-radio-button label="all">全部</el-radio-button>
              </el-radio-group>
            </div>
            <div class="muted small" style="margin:4px 0 12px">
              发货清单由设计部下发（同时直推发货部与仓库）。仓库只需按清单核对、备好货物后点「已备齐」，物流发货部即可安排发货——无需在此上传。
            </div>

            <el-table :data="shipPending" v-loading="shipPendingLoading" stripe size="small"
                      max-height="calc(100vh - 320px)" :scrollbar-always-on="true">
              <el-table-column label="项目编号" width="118"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
              <el-table-column prop="name" label="项目名称" min-width="150" show-overflow-tooltip />
              <el-table-column label="发货清单文件（设计下发）" min-width="300">
                <template #default="{ row }">
                  <div v-if="row.files.length" class="ship-files">
                    <div v-for="f in row.files" :key="f.id" class="ship-file">
                      <span class="ship-file-name" :title="f.name">📄 {{ f.name }}</span>
                      <el-button v-if="canInlinePreview(f.name)" size="small" link type="primary" :icon="View" @click="previewShipList(f)">预览</el-button>
                      <el-button size="small" link :icon="Download" @click="downloadAttachment({ id: f.id, name: f.name })">下载</el-button>
                      <el-button size="small" link :icon="Printer" @click="printShipList(f)">打印</el-button>
                    </div>
                  </div>
                  <span v-else class="muted">— 设计部尚未上传文件</span>
                </template>
              </el-table-column>
              <el-table-column label="下发人 / 时间" width="164">
                <template #default="{ row }">
                  <div>{{ row.requested_by_name || '—' }}</div>
                  <div class="muted small">{{ fmtDate(row.requested_at) }}</div>
                </template>
              </el-table-column>
              <el-table-column label="备货状态" width="158" align="center">
                <template #default="{ row }">
                  <template v-if="row.packlist_status === 'ready'">
                    <el-tag type="success" effect="light" size="small">✅ 已备齐</el-tag>
                    <div class="muted small" style="margin-top:3px">{{ row.ready_by_name || '' }} {{ fmtDate(row.ready_at) }}</div>
                  </template>
                  <el-tag v-else type="warning" effect="light" size="small">⏳ 待备货</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="110" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button v-if="row.packlist_status !== 'ready'" size="small" type="success" @click="markShipReady(row)">已备齐</el-button>
                  <span v-else class="muted">已完成</span>
                </template>
              </el-table-column>
              <template #empty>
                <EmptyHint :text="shipFilter === 'ready' ? '暂无已备齐项目' : shipFilter === 'all' ? '暂无发货清单，等待设计部下发' : '暂无待备货项目，等待设计部下发发货清单'" size="sm" />
              </template>
            </el-table>
          </template>
        </el-tab-pane>

        <!-- 🆕 #167 采购申请：仓库列出要买什么 → 提交到采购部 -->
        <el-tab-pane v-if="tv('preq')" label="采购申请" name="preq">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可提采购申请" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
              <el-button type="primary" :icon="Plus" @click="openPurchReq">提采购申请</el-button>
              <el-button :icon="Search" size="small" @click="loadPurchReqs">刷新</el-button>
              <span class="muted small">仓库发现要买的东西（缺料/耗材/工具等）在这里提申请，采购部会收到通知并处理。</span>
            </div>
            <el-table :data="preqList" v-loading="preqLoading" stripe size="small" max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl">
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
              <el-table-column label="物料" min-width="200"><template #default="{ row }">{{ row.lines.map((l: any) => l.item_name).slice(0, 3).join('、') }}{{ row.lines.length > 3 ? ` 等${row.lines.length}项` : '' }}</template></el-table-column>
              <el-table-column label="状态" width="100" align="center"><template #default="{ row }"><StatusPill :text="PREQ_STATUS[row.status] || row.status" :variant="preqStatusVariant(row.status)" /></template></el-table-column>
              <el-table-column label="处理" min-width="140"><template #default="{ row }"><span v-if="row.status === 'done'" class="muted small">{{ row.handler_name }} 已处理</span><span v-else-if="row.status === 'rejected'" class="danger small">驳回：{{ row.reject_reason || '—' }}</span><span v-else class="muted small">等待采购部处理</span></template></el-table-column>
              <el-table-column label="提交时间" width="110"><template #default="{ row }">{{ (row.created_at || '').slice(0, 10) }}</template></el-table-column>
              <el-table-column label="操作" width="70" align="center"><template #default="{ row }"><el-button v-if="row.status !== 'done'" size="small" link type="danger" @click="deletePurchReq(row)">删除</el-button></template></el-table-column>
              <template #empty><EmptyHint text="暂无采购申请，点「提采购申请」开始" size="sm" /></template>
            </el-table>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 🆕 #167 提采购申请弹窗 -->
    <el-dialog v-model="preqVisible" title="提采购申请" width="min(880px, 96vw)" top="5vh">
      <el-alert type="info" :closable="false" style="margin-bottom:12px" title="列出要采购的物料（名称必填，规格/数量/项目/备注选填）→ 指定采购员后提交，该采购员会收到通知。" />
      <el-form label-position="top" style="margin-bottom:6px">
        <el-form-item label="指定采购员（推送给他；不选则通知全体采购员）">
          <el-select v-model="preqForm.buyer_id" filterable clearable placeholder="选择采购员" style="width:320px">
            <el-option v-for="b in preqBuyers" :key="b.id" :label="b.name" :value="b.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="order-lines-head">
        <span class="order-lines-title">采购物料（{{ preqForm.lines.length }} 行）</span>
        <el-button size="small" :icon="Plus" @click="addPreqLine">添加一行</el-button>
      </div>
      <el-table :data="preqForm.lines" size="small" border max-height="46vh">
        <el-table-column type="index" label="#" width="44" align="center" />
        <el-table-column label="名称 *" min-width="160"><template #default="{ row }"><el-input v-model="row.item_name" placeholder="物料名称" /></template></el-table-column>
        <el-table-column label="规格型号" min-width="140"><template #default="{ row }"><el-input v-model="row.spec" placeholder="规格/型号" /></template></el-table-column>
        <el-table-column label="数量" width="110"><template #default="{ row }"><el-input-number v-model="row.qty" :min="0" :controls="false" style="width:100%" /></template></el-table-column>
        <el-table-column label="项目编号" width="120"><template #default="{ row }"><el-input v-model="row.project_code" placeholder="选填" /></template></el-table-column>
        <el-table-column label="备注" min-width="120"><template #default="{ row }"><el-input v-model="row.notes" placeholder="选填" /></template></el-table-column>
        <el-table-column label="操作" width="60" align="center"><template #default="{ $index }"><el-button size="small" link type="danger" :icon="Delete" @click="removePreqLine($index)" /></template></el-table-column>
      </el-table>
      <el-input v-model="preqForm.notes" type="textarea" :rows="2" placeholder="整单备注（选填）" style="margin-top:12px" />
      <template #footer>
        <el-button @click="preqVisible = false">取消</el-button>
        <el-button type="primary" :loading="preqSaving" @click="submitPurchReq">提交申请</el-button>
      </template>
    </el-dialog>

    <!-- 出入库弹窗 -->
    <el-dialog v-model="ioVisible" :title="ioForm.direction === 'in' ? '📥 入库登记' : '📤 出库登记'" width="480px">
      <el-form label-position="top">
        <el-form-item label="物料" required>
          <el-select v-model="ioForm.material_id" filterable placeholder="选择物料" style="width:100%">
            <el-option v-for="m in materials" :key="m.id" :label="matLabel(m)" :value="m.id" />
          </el-select>
        </el-form-item>
        <div class="frow">
          <el-form-item label="数量" required style="flex:1"><el-input-number v-model="ioForm.qty" :min="1" :controls="false" style="width:100%" /></el-form-item>
          <el-form-item label="业务日期" style="flex:1"><el-date-picker v-model="ioForm.biz_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="单价" style="flex:1">
            <el-input-number v-model="ioForm.unit_price" :min="0" :controls="false" placeholder="选填" style="width:100%" />
          </el-form-item>
          <el-form-item label="金额" style="flex:1">
            <el-input :model-value="ioAmount ?? '—'" disabled style="width:100%" />
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item :label="ioForm.direction === 'in' ? '来源' : '用途'" style="flex:1">
            <el-input v-model="ioForm.source" :placeholder="ioForm.direction === 'in' ? '采购入库' : '领料出库'" />
          </el-form-item>
          <el-form-item :label="ioForm.direction === 'in' ? '供应商' : '领用方'" style="flex:1"><el-input v-model="ioForm.party" /></el-form-item>
        </div>
        <!-- 🆕 出库领用项目：填了才计入「项目材料成本」(项目成本=领料出库×单价) -->
        <div class="frow" v-if="ioForm.direction === 'out'">
          <el-form-item label="领用项目（选填，填了才计入项目材料成本）" style="flex:1">
            <el-select v-model="ioForm.project_id" filterable clearable placeholder="选择领用到哪个项目" style="width:100%">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
            </el-select>
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="ioVisible = false">取消</el-button>
        <el-button type="primary" :loading="ioSubmitting" @click="submitIo">登记</el-button>
      </template>
    </el-dialog>

    <!-- 物料弹窗 -->
    <el-dialog v-model="matVisible" :title="matForm.id ? '编辑物料' : '新增物料'" width="500px">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="名称" required style="flex:1"><el-input v-model="matForm.name" /></el-form-item>
          <el-form-item label="规格型号" style="flex:1"><el-input v-model="matForm.spec" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="类别" style="flex:1">
            <el-select v-model="matForm.category" filterable clearable
                       placeholder="从字典选择" style="width:100%">
              <el-option v-for="c in matCatOptions" :key="c" :label="c" :value="c" />
            </el-select>
          </el-form-item>
          <el-form-item label="材质" style="flex:1">
            <el-select v-model="matForm.material_grade" filterable clearable
                       placeholder="从字典选择" style="width:100%">
              <el-option v-for="g in matGradeOptions" :key="g" :label="g" :value="g" />
            </el-select>
          </el-form-item>
          <el-form-item label="单位" style="flex:1">
            <el-select v-model="matForm.unit" filterable clearable
                       placeholder="从字典选择" style="width:100%">
              <el-option v-for="u in matUnitOptions" :key="u" :label="u" :value="u" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="单价(元)" style="flex:1">
            <el-input-number v-model="matForm.unit_price" :min="0" :controls="false" placeholder="参考单价" style="width:100%" />
          </el-form-item>
          <el-form-item label="库位" style="flex:1"><el-input v-model="matForm.location" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="安全库存" style="flex:1"><el-input-number v-model="matForm.safety_stock" :min="0" :controls="false" style="width:100%" /></el-form-item>
          <el-form-item label="期初库存" style="flex:1"><el-input-number v-model="matForm.init_stock" :min="0" :controls="false" :disabled="!!matForm.id" style="width:100%" /></el-form-item>
        </div>
        <!-- 🆕 自定义字段（仓库主管在「字段设置」里配置） -->
        <div class="frow" v-for="f in formCustomFields" :key="f.id">
          <el-form-item :label="f.required ? f.label + ' *' : f.label" style="flex:1">
            <el-select v-if="f.ftype === 'select'" v-model="matForm.custom_values[String(f.id)]" clearable filterable style="width:100%" placeholder="请选择">
              <el-option v-for="o in f.options" :key="o" :label="o" :value="o" />
            </el-select>
            <el-date-picker v-else-if="f.ftype === 'date'" v-model="matForm.custom_values[String(f.id)]" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            <el-input-number v-else-if="f.ftype === 'number'" v-model="matForm.custom_values[String(f.id)]" :controls="false" style="width:100%" />
            <el-input v-else v-model="matForm.custom_values[String(f.id)]" />
          </el-form-item>
        </div>
        <div v-if="matForm.id" class="muted small">期初库存建档后不可改（避免破坏库存勾稽，调整请用出入库）。</div>
      </el-form>
      <template #footer>
        <el-button @click="matVisible = false">取消</el-button>
        <el-button type="primary" :loading="matSubmitting" @click="submitMat">保存</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 物料自定义字段管理器 -->
    <el-dialog v-model="cfManagerVisible" title="物料自定义字段设置" width="640px">
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
        title="给物料表单加自定义字段（文本/数字/日期/下拉）。启用后新增/编辑物料会出现对应输入框；勾选「列表显示」的字段在物料主数据表里显示成一列。删除字段不影响已录入的历史值。" />
      <el-table :data="customFields" size="small" border stripe max-height="34vh">
        <el-table-column type="index" label="#" width="46" align="center" />
        <el-table-column prop="label" label="字段名称" min-width="110" />
        <el-table-column label="类型" width="90"><template #default="{ row }">{{ CF_TYPES.find(t => t.v === row.ftype)?.l || row.ftype }}</template></el-table-column>
        <el-table-column label="必填" width="60"><template #default="{ row }">{{ row.required ? '是' : '—' }}</template></el-table-column>
        <el-table-column label="列表显示" width="80"><template #default="{ row }">{{ row.show_in_list ? '是' : '—' }}</template></el-table-column>
        <el-table-column label="排序" width="60" prop="sort_order" />
        <el-table-column label="状态" width="70"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="cfEdit(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="cfDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top:14px;font-weight:600">{{ cfEditingId ? '编辑字段' : '新增字段' }}</div>
      <el-form :model="cfForm" label-position="top">
        <el-row :gutter="12">
          <el-col :xs="24" :sm="8"><el-form-item label="字段名称 *"><el-input v-model="cfForm.label" placeholder="如 品牌/保质期" /></el-form-item></el-col>
          <el-col :xs="12" :sm="6"><el-form-item label="类型"><el-select v-model="cfForm.ftype" style="width:100%"><el-option v-for="t in CF_TYPES" :key="t.v" :label="t.l" :value="t.v" /></el-select></el-form-item></el-col>
          <el-col :xs="6" :sm="4"><el-form-item label="排序"><el-input-number v-model="cfForm.sort_order" :controls="false" style="width:100%" /></el-form-item></el-col>
          <el-col :xs="9" :sm="3"><el-form-item label="必填"><el-switch v-model="cfForm.required" /></el-form-item></el-col>
          <el-col :xs="9" :sm="3"><el-form-item label="列表显示"><el-switch v-model="cfForm.show_in_list" /></el-form-item></el-col>
          <el-col :xs="24" v-if="cfForm.ftype === 'select'"><el-form-item label="下拉选项（每行一个）"><el-input v-model="cfForm.options" type="textarea" :rows="3" placeholder="选项1&#10;选项2" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <div style="display:flex;gap:10px">
        <el-button v-if="cfEditingId" @click="cfResetForm">取消编辑</el-button>
        <el-button type="primary" :loading="cfSaving" @click="cfSave">{{ cfEditingId ? '保存修改' : '新增字段' }}</el-button>
      </div>
      <template #footer><el-button @click="cfManagerVisible = false">关闭</el-button></template>
    </el-dialog>

    <!-- 🆕 采购收货弹窗 -->
    <el-dialog v-model="recvVisible" title="采购收货" width="560px">
      <div class="recv-info">
        <div><span class="k">采购单号</span><span class="code">{{ recvForm.po_no || '—' }}</span></div>
        <div><span class="k">供应商</span>{{ recvForm.supplier_name }}</div>
        <div><span class="k">物料</span>{{ recvForm.item_name }}<span v-if="recvForm.spec"> · {{ recvForm.spec }}</span></div>
        <div><span class="k">数量</span>{{ recvForm.qty ?? '—' }}</div>
      </div>
      <el-form label-position="top" style="margin-top:6px">
        <div class="frow">
          <el-form-item label="送货单号">
            <el-input v-model="recvForm.delivery_note_no" placeholder="送货单上的编号" />
          </el-form-item>
          <el-form-item label="到货日期" required>
            <el-date-picker v-model="recvForm.arrival_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="单价（后填价格在此补）">
            <el-input-number v-model="recvForm.unit_price" :min="0" :precision="4" :controls="false" style="width:100%" @change="onRecvCalc" />
          </el-form-item>
          <el-form-item label="收货金额（总价，填此按数量算单价）">
            <el-input-number v-model="recvForm.received_amount" :min="0" :precision="2" :controls="false" style="width:100%" @change="onRecvAmountCalc" />
          </el-form-item>
        </div>
        <!-- 🆕 需求十四：上传收货单（图片/PDF） -->
        <el-form-item label="收货单（图片/PDF，选填）">
          <el-button size="small" :icon="Download" @click="pickRecvReceipt">选择收货单</el-button>
          <span v-if="recvReceiptFile" class="muted small" style="margin-left:8px">{{ recvReceiptFile.name }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="recvVisible = false">取消</el-button>
        <el-button type="primary" :loading="recvSaving" @click="submitReceive">确认收货</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 需求四：合并零件收货（只填合并总价 或 逐行单价）+ 需求十四 收货单 -->
    <el-dialog v-model="batchRecvVisible" title="合并零件收货" width="720px">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="送货单号">
            <el-input v-model="batchRecvForm.delivery_note_no" placeholder="整批共用一个送货单号" />
          </el-form-item>
          <el-form-item label="到货日期" required>
            <el-date-picker v-model="batchRecvForm.arrival_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
          </el-form-item>
        </div>
        <div class="muted small" style="margin:2px 0 8px">逐行填单价/收货金额（单价可留空，货到再补）。</div>
      </el-form>
      <el-table :data="batchRecvLines" size="small" border max-height="34vh">
        <el-table-column label="名称" min-width="130">
          <template #default="{ row }">{{ row.item_name }}<span v-if="row.spec" class="muted small"> · {{ row.spec }}</span></template>
        </el-table-column>
        <el-table-column label="数量" width="80" align="right"><template #default="{ row }">{{ row.qty ?? '—' }}</template></el-table-column>
        <el-table-column label="单价" width="130" align="right">
          <template #default="{ row }"><el-input-number v-model="row.unit_price" :min="0" :precision="4" :controls="false" style="width:110px" /></template>
        </el-table-column>
        <el-table-column label="收货金额" width="140" align="right">
          <template #default="{ row }"><el-input-number v-model="row.received_amount" :min="0" :precision="2" :controls="false" style="width:120px" /></template>
        </el-table-column>
      </el-table>
      <el-form label-position="top" style="margin-top:12px">
        <el-form-item label="收货单（图片/PDF，选填，整批共用）">
          <el-button size="small" :icon="Download" @click="pickBatchReceipt">选择收货单</el-button>
          <span v-if="batchReceiptFile" class="muted small" style="margin-left:8px">{{ batchReceiptFile.name }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="batchRecvVisible = false">取消</el-button>
        <el-button type="primary" :loading="batchRecvSaving" @click="submitBatchReceive">确认合并收货（{{ batchRecvLines.length }} 条）</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 #9 发货清单统一预览（图片/PDF/Excel/Word） -->
    <AttachmentPreview ref="previewRef" />
  </div>
</template>

<style scoped>
.bad { color: var(--danger); }
/* 采购收货 待收货/已收货 切换：字体更大、按钮更大更醒目（用户要求）*/
.recv-toggle :deep(.el-radio-button__inner) {
  font-size: 18px; font-weight: 700; padding: 15px 34px; line-height: 1.2; min-width: 150px;
}
/* 🆕 #141 tab 待办数红色角标 */
.wh-tab-badge { display: inline-block; margin-left: 6px; min-width: 16px; height: 16px; line-height: 16px;
  padding: 0 4px; border-radius: 8px; background: var(--el-color-danger); color: #fff; font-size: 11px;
  text-align: center; vertical-align: middle; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.frow { display: flex; gap: 12px; flex-wrap: wrap; }
.frow > * { flex: 1; min-width: 140px; }
.ship-pending-title { font-weight: 600; font-size: 14px; margin-bottom: 10px; color: var(--el-text-color-primary); }
.ship-cat-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 2px; }
.ship-files { display: flex; flex-direction: column; gap: 4px; }
.ship-file { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.ship-file-name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.code { color: var(--el-color-primary, #2563eb); }
.recv-info { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; padding: 12px 14px;
  background: var(--el-fill-color-light); border-radius: 8px; font-size: 13px; }
.recv-info .k { display: inline-block; min-width: 60px; color: var(--el-text-color-secondary); margin-right: 6px; }
</style>
