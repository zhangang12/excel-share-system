<script setup lang="ts">
// 🆕 v3 M07 仓库组：总览/出入库/收发存/流水/物料主数据/发货清单 六 tab
import { ref, onMounted, reactive, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Lock, View, Download, Printer, Setting, Delete, ArrowLeft, ArrowRight, QuestionFilled, Upload, Sort } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { whApi, type WhMaterial, type WhTxn, type WhSummaryRow, type ShipListFile, type ShipListPendingRow, type WhCustomField , type WhLocation } from '@/api/warehouse'
import { canInlinePreview, attachmentBlobUrl, isPdfAtt, isImageAtt } from '@/api/attachments'
import { downloadAttachment } from '@/api/orders'
import ProjectFlowButton from '@/components/ProjectFlowButton.vue'   // 🆕 #385 全流程进度同步到各部门
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import AttachmentPreview from '@/components/AttachmentPreview.vue'
import { fmtDate, fmtMoney, fmtMatCode, specOf } from '@/utils/format'
import PageRefresh from '@/components/PageRefresh.vue'   // 反馈#359：每个页面都有刷新

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
    await Promise.all([loadMaterials(), loadMatList(), loadTxns(), loadBadgeCounts()])
  } catch { /* 拦截器已提示 */ }
}

const tab = ref('ov')
const loading = ref(false)
const materials = ref<WhMaterial[]>([])   // 全量（库存总览 + 出入库下拉），永远不带筛选
const matList = ref<WhMaterial[]>([])    // 物料主数据页签的结果，带 kw/库位/只看缺料
const lowCount = ref(0)
const kw = ref('')
// 🆕 仓库反馈：551 个物料只能按名称/规格找。加库位筛选和"只看缺料"，
//    kw 本身在后端也扩到了 单位/库位/材质。
const matLoc = ref('')
const matLowOnly = ref(false)
const matTotal = ref(0)
let matTimer: ReturnType<typeof setTimeout> | null = null
function onMatSearch() {
  if (matTimer) clearTimeout(matTimer)
  matTimer = setTimeout(loadMatList, 300)   // ⚠️ 只刷物料主数据那张表，别动全量 materials
}
function resetMatFilter() {
  kw.value = ''; matLoc.value = ''; matLowOnly.value = false
  loadMatList()
}

// ⚠️⚠️ 反馈#404（王利利）：「收货完了，都不会及时更新，出库的时候啥也找不到，
//   都要从新点一下项目目录，在点出库，才有东西」——根因是**一个数组喂了三个地方**：
//   物料主数据的表格、库存总览的表格和 KPI、出入库登记的物料下拉，全都读同一个 `materials`。
//   而物料主数据的搜索是**走服务端**的（kw 传给后端），一搜就把这个共享数组换成了命中的那几条。
//   后果：
//     · 在物料主数据里搜过「轴承」，再去开出库登记 → 下拉里只剩 35 条，刚收的货根本不在里面
//     · 「只看缺料」勾上更狠：下拉里只剩缺料的
//     · 收货后 refreshAfterReceive 调 loadMaterials，又把同一个 kw 带上去查 → 刷了等于没刷
//     · 切到别的菜单再回来，组件重挂载、kw 归空 → 又好了（这就是她说的"点项目目录再点出库"）
//   所以拆成两个数组，各管各的：
//     `materials`  = **全量、不带任何筛选**，给库存总览和出入库下拉用
//     `matList`    = 物料主数据页签自己的结果，带 kw/库位/只看缺料
async function loadMaterials() {
  // 全量：任何筛选都不带。库存总览的 KPI 和出库下拉都靠它，掺进筛选就是上面那串 bug。
  const j = await whApi.materials({})
  materials.value = j.materials
}

async function loadMatList() {
  // 物料主数据页签专用：搜索/库位/只看缺料走服务端（全库都能搜到，见早期反馈）
  loading.value = true
  try {
    const j = await whApi.materials({
      kw: kw.value.trim() || undefined,
      location: matLoc.value || undefined,
      low_only: matLowOnly.value || undefined,
    })
    matList.value = j.materials; matTotal.value = j.total
    // ⚠️ 跟 matTotal 同口径（都是**筛选后**的）。一个取筛选后、一个取全量，
    //    就会出现「共 35 条，其中 120 条低于安全库存」这种自相矛盾的话。
    lowCount.value = j.low_count
  } finally { loading.value = false }
}
// ===== 🆕 库位管理（主数据;采购下单/物料/出入库共用取值） =====
const locations = ref<WhLocation[]>([])
async function loadLocations() {
  try { locations.value = await whApi.locations() } catch { locations.value = [] }
}
const enabledLocations = computed(() => locations.value.filter(l => l.enabled))
const locVisible = ref(false)
const locForm = reactive({ id: null as number | null, name: '', note: '', sort_order: 0, enabled: true })
function openLoc(row?: WhLocation) {
  if (row) Object.assign(locForm, { id: row.id, name: row.name, note: row.note || '', sort_order: row.sort_order, enabled: row.enabled })
  else Object.assign(locForm, { id: null, name: '', note: '', sort_order: 0, enabled: true })
  locVisible.value = true
}
const locSaving = ref(false)
async function submitLoc() {
  if (!locForm.name.trim()) { ElMessage.warning('请填写库位名称'); return }
  locSaving.value = true
  try {
    if (locForm.id) await whApi.updateLocation(locForm.id, { name: locForm.name.trim(), note: locForm.note, sort_order: locForm.sort_order, enabled: locForm.enabled })
    else await whApi.createLocation({ name: locForm.name.trim(), note: locForm.note, sort_order: locForm.sort_order, enabled: locForm.enabled })
    ElMessage.success('已保存')
    locVisible.value = false
    await loadLocations()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
  finally { locSaving.value = false }
}
async function deleteLoc(row: WhLocation) {
  try {
    await ElMessageBox.confirm(`删除库位「${row.name}」？在用的库位删不掉，可改用停用。`, '删除库位', { type: 'warning' })
  } catch { return }
  try {
    await whApi.deleteLocation(row.id)
    ElMessage.success('已删除')
    await loadLocations()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}

onMounted(() => { loadMaterials(); loadMatList(); loadMatDict(); loadCustomFields(); loadBadgeCounts(); loadLocations() })

// 🆕 反馈#373/#374：「库存总览」只列**通用物料**。买给具体项目的料（收货时填了订单编号的）
//   归「出入库 / 物料需求」那个 tab 管，混在这里既看不出公司真正备了多少货，
//   也让人以为那些料还能随便领。改之前生产上 745 个物料里 511 个是项目料，
//   库存金额 ¥148,099 中 ¥116,718(79%) 其实早已名花有主。
//   ⚠️ 只过滤这个 tab：materials 同时喂着「物料主数据」和出库选料，那两处必须还是全量。
const ovSearch = ref('')
// 本页签的全量通用物料：KPI 卡片、低库存清单都用它。
// ⚠️ **不受搜索框影响**——「库存总价」是管理层当公司库存值看的数，
//    敲个搜索词它就变成 ¥806,236，读的人不会意识到那只是命中那几条的合计。
//    低库存清单同理：「一键提采购申请」要提的是全部缺料，不是搜出来的那几条。
const ovAll = computed(() => materials.value.filter(m => !m.is_project_material))
// 表格用的：在上面这份里做本地过滤（数据本来就全在内存，不用再打一趟后端）
const ovMaterials = computed(() => {
  const k = ovSearch.value.trim().toLowerCase()
  if (!k) return ovAll.value
  return ovAll.value.filter(m => [
    m.name, m.spec, m.code, m.location, m.material_grade, m.unit,
  ].some(f => (f || '').toLowerCase().includes(k)))
})
const projMatCount = computed(() => materials.value.filter(m => m.is_project_material).length)
const totalStock = computed(() => ovAll.value.reduce((s, m) => s + m.stock, 0))
const totalValue = computed(() => ovAll.value.reduce((s, m) => s + (m.stock_value || 0), 0))  // 🆕 需求三：库存总价
const lowList = computed(() => ovAll.value.filter(m => m.low))

// ===== 出入库登记 =====
const ioVisible = ref(false)
const ioForm = reactive({ material_id: undefined as number | undefined, direction: 'in', qty: 1,
  unit_price: null as number | null, biz_date: new Date().toISOString().slice(0, 10), source: '', party: '',
  project_id: undefined as number | undefined, location: '' as string | null,   // 🆕 库位(入库用)
  non_project: false, non_project_reason: '' })   // 🆕 盈利改善1b：出库必选项目或明确非项目领用
// 🆕 #325 批量出库行：逐行选物料+数量（共用 用途/领用方/领用项目/业务日期），消耗品一次出多种
const ioLines = ref<{ material_id: number | undefined; qty: number }[]>([{ material_id: undefined, qty: 1 }])
function addIoLine() { ioLines.value.push({ material_id: undefined, qty: 1 }) }
function removeIoLine(i: number) { if (ioLines.value.length > 1) ioLines.value.splice(i, 1) }
function openIo(dir: string) {
  Object.assign(ioForm, { material_id: undefined, direction: dir, qty: 1, unit_price: null,
    biz_date: new Date().toISOString().slice(0, 10), source: '', party: '', project_id: undefined,
    location: '', non_project: false, non_project_reason: '' })
  ioLines.value = [{ material_id: undefined, qty: 1 }]   // 🆕 #325 出库默认一行，可再加
  if (dir === 'out' && !projects.value.length) loadProjects()   // 🆕 出库要选领用项目→项目材料成本
  ioVisible.value = true
}
const ioAmount = computed(() => ioForm.unit_price != null ? Number((ioForm.qty * ioForm.unit_price).toFixed(2)) : null)
const ioSubmitting = ref(false)
async function submitIo() {
  // 🆕 #325 出库=批量：逐行物料+数量，共用 用途/领用方/领用项目/业务日期，走批量端点（任一行失败整体回滚）
  if (ioForm.direction === 'out') {
    const lines = ioLines.value.filter(ln => ln.material_id)
    if (!lines.length) { ElMessage.warning('请选择物料'); return }
    if (lines.length !== ioLines.value.length) { ElMessage.warning('每行都要选择物料（多余的行请删除）'); return }
    if (lines.some(ln => !ln.qty || ln.qty <= 0)) { ElMessage.warning('每行数量须为正'); return }
    const dup = new Set(lines.map(ln => ln.material_id))
    if (dup.size !== lines.length) { ElMessage.warning('同一物料重复了，请合并成一行'); return }
    // 盈利改善1b·堵「无主领料」：出库必须挂项目，或明确勾「非项目领用」+原因（口径同单条）
    if (!ioForm.project_id) {
      if (!ioForm.non_project) { ElMessage.warning('出库必须选择领用项目；确属非项目领用请勾选「非项目领用」'); return }
      if (!ioForm.non_project_reason.trim()) { ElMessage.warning('请填写非项目领用原因'); return }
    }
    ioSubmitting.value = true
    try {
      const r: any = await http.post('/wh/txns/batch-out', {   // 🆕 #325 批量出库端点（直接 http，同 loadMatCatTree 先例）
        biz_date: ioForm.biz_date, source: ioForm.source, party: ioForm.party,
        project_id: ioForm.project_id, non_project: ioForm.non_project,
        non_project_reason: ioForm.non_project_reason,
        lines: lines.map(ln => ({ material_id: ln.material_id!, qty: ln.qty })),
      }).then(res => res.data)
      ElMessage.success(r.message || '已登记')
      ioVisible.value = false
      await Promise.all([loadMaterials(), loadMatList(), loadTxns()])
    } catch { /* 超量/行错误由拦截器提示（后端指明第几行） */ } finally { ioSubmitting.value = false }
    return
  }
  if (!ioForm.material_id) { ElMessage.warning('请选择物料'); return }
  if (!ioForm.qty || ioForm.qty <= 0) { ElMessage.warning('数量须为正'); return }
  ioSubmitting.value = true
  try {
    const r: any = await whApi.createTxn({ ...ioForm })
    ElMessage.success(r.message || '已登记')
    ioVisible.value = false
    await Promise.all([loadMaterials(), loadMatList(), loadTxns()])
  } catch { /* 超量等错误由拦截器提示 */ } finally { ioSubmitting.value = false }
}
// 出库下拉的每一行。⚠️ **项目料必须一眼看得出来**：它是买给某个项目的，
//   仓库拿它去出给别的项目/别的用途之前，起码得知道自己在动谁的料。
//   原来只有"只挂过一个项目"的才显示编号——生产上 399 个有货的项目料里
//   有 135 个是多项目收过货的，一个标签都没有，跟公司备货长得一模一样。
function matProjTag(m: WhMaterial): string {
  if (!m.is_project_material) return ''
  if (m.project_code) return `【${m.project_code}】`
  const n = (m.project_codes || []).length
  return n > 1 ? `【项目料·${n} 个项目】` : '【项目料】'
}
function matLabel(m: WhMaterial) {
  return `${m.name}${m.spec ? '·' + m.spec : ''}（现存 ${m.stock}）${matProjTag(m) ? ' ' + matProjTag(m) : ''}`
}
// 鼠标移上去看具体是哪几个项目（多项目时标签只写个数，写全了一行放不下）
function matProjTitle(m: WhMaterial): string {
  const cs = m.project_codes || []
  if (!cs.length) return ''
  return cs.length === 1 ? `收货时挂的项目：${cs[0]}` : `这个料被这些项目收过货：${cs.join('、')}`
}

// ===== 流水 =====
const txns = ref<WhTxn[]>([])
const txnDir = ref('')
// 🆕 出入库流水搜索：**改成服务端搜**（单号/物料/规格/库位/来源/往来单位/项目编号）。
// ⚠️ 原来是拿最近 200 条回来、在前端 filter——生产上流水已经 1083 条，
//    前端能搜到的最早只到昨天，仓库入完料第二天就搜不着了。
const txnSearch = ref('')
const txnRange = ref<[string, string] | null>(null)
const txnTotal = ref(0)     // 命中总数
const txnShown = ref(0)     // 实际返回数；total>shown 说明被截断，要明确告诉用户
const txnLoading = ref(false)
let txnTimer: ReturnType<typeof setTimeout> | null = null
// 搜索走后端，输入时防抖，别每敲一个字打一次接口
function onTxnSearch() {
  if (txnTimer) clearTimeout(txnTimer)
  txnTimer = setTimeout(loadTxns, 300)
}
async function loadTxns() {
  txnLoading.value = true
  try {
    const d = await whApi.txns({
      direction: txnDir.value || undefined,
      kw: txnSearch.value.trim() || undefined,
      date_from: txnRange.value?.[0] || undefined,
      date_to: txnRange.value?.[1] || undefined,
      limit: 500,
    })
    txns.value = d.rows
    txnTotal.value = d.total
    txnShown.value = d.shown
  } finally { txnLoading.value = false }
}
function resetTxnFilter() {
  txnSearch.value = ''; txnRange.value = null; txnDir.value = ''
  loadTxns()
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
const matForm = reactive<any>({ id: null, name: '', spec: '', category: '', unit: '个', unit_price: null, location: '', safety_stock: 0, init_stock: 0, category_id: null, code: '', custom_values: {} })

// 🆕 物料编码分类(3级树)：选到细分类保存时后端自动发码（大类+中类+细分+4位流水）
interface MatCatNode { id: number; parent_id: number | null; level: number; seg_code: string; name: string; sort_order: number; enabled: boolean }
const matCats = ref<MatCatNode[]>([])
async function loadMatCatTree() {
  try { matCats.value = (await http.get<MatCatNode[]>('/wh/material-categories')).data } catch { matCats.value = [] }
}
const matCatCascade = computed(() => {
  const byParent = new Map<number | null, MatCatNode[]>()
  for (const c of matCats.value.filter(x => x.enabled)) {
    const k = c.parent_id ?? null
    if (!byParent.has(k)) byParent.set(k, [])
    byParent.get(k)!.push(c)
  }
  const build = (pid: number | null): any[] => (byParent.get(pid) || []).map(c => {
    const children = build(c.id)
    return { id: c.id, lab: `${c.seg_code} ${c.name}`, ...(children.length ? { children } : {}) }
  })
  return build(null)
})

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
// 🆕 弃用「物料类别」下拉(与编码分类树重复);matCatOptions 已移除
// 🆕 订单编号里合法的**非项目**取值（备用/车间耗材/售后维修…），由字典维护。
//   原来收货弹窗的「订单编号」下拉只列项目，这几个值得靠人手打——于是生产上打出了
//   「车间消耗」「消耗」「消耗品」三个「车间耗材」的变体，按编号汇总的报表被打散，
//   而且随手打错的项目编号（如 2025-087，项目根本不存在）会静默变成无主料，谁也领不到。
//   放进下拉让人选，比事后在「孤儿采购」报表里捞回来便宜得多。
const orderNoOptions = computed(() => matDict.value.filter(d => d.dtype === 'order_no').map(d => d.value))
const matUnitOptions = computed(() => matDict.value.filter(d => d.dtype === 'unit').map(d => d.value))
const matGradeOptions = computed(() => matDict.value.filter(d => d.dtype === 'material_grade').map(d => d.value))
function openMat(m?: WhMaterial) {
  if (m) Object.assign(matForm, { ...m, custom_values: { ...(m.custom_values || {}) } })
  else Object.assign(matForm, { id: null, name: '', spec: '', category: '', material_grade: '', unit: '个', unit_price: null, location: '', safety_stock: 0, init_stock: 0, category_id: null, code: '', custom_values: {} })
  if (!matCats.value.length) loadMatCatTree()
  matVisible.value = true
}
// 🆕 删除物料（有出入库流水的后端会拦截）
async function deleteMat(m: WhMaterial) {
  try { await ElMessageBox.confirm(`删除物料「${m.name}${m.spec ? '·' + m.spec : ''}」？删除后不可恢复。\n（有出入库流水的物料不能删除）`, '删除物料', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await whApi.deleteMaterial(m.id); ElMessage.success('物料已删除'); await Promise.all([loadMaterials(), loadMatList()]) } catch { /* 拦截器已提示 */ }
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
  stock_location?: string | null
  delivery_note_no?: string | null; arrival_date?: string | null
  delivery_date?: string | null   // 🆕 #290 下单时间（采购下单填，PurchaseItem.delivery_date）
  receipt_count?: number   // 🆕 需求十四：已上传收货单数量
  notes?: string | null    // 🆕 #356 采购下单填的备注（外购件的详细尺寸等，仓库对料要看）
}
const recvItems = ref<RecvItem[]>([])
const recvLoading = ref(false)
const recvReceived = ref(false)        // false=待收货 / true=已收货
const recvSupplier = ref<number | ''>('')
const recvPo = ref('')                   // 🆕 #315 采购单号/项目编号关键字（前端模糊过滤）
// 🆕 #321 合并组默认收起（自动合并、手动展开），「全部展开/收起」靠 key 强制重渲染
const recvExpandAll = ref(false)
const recvExpandKey = ref(0)
function toggleRecvExpandAll() { recvExpandAll.value = !recvExpandAll.value; recvExpandKey.value++ }
const recvName = ref('')               // 🆕 #286 物料名称关键字（前端模糊过滤）
const recvOrderMonth = ref('')         // 🆕 #290 下单时间筛选（月份，前端过滤）
// 🆕 #330 供应商下拉、徽标条数改用后端 /receiving/meta：此前两者都是从「已加载的那 300 条」里
//   算出来的，长周期供应商压根不在选项里、徽标永远显示 300，仓库连"按供应商查"这条自救路都被堵死。
interface RecvSupOpt { id: number; name: string }
const recvMeta = ref<{
  pending_count: number; received_count: number
  pending_suppliers: RecvSupOpt[]; received_suppliers: RecvSupOpt[]; limit: number
}>({ pending_count: 0, received_count: 0, pending_suppliers: [], received_suppliers: [], limit: 2000 })
const recvSupplierOptions = computed<RecvSupOpt[]>(() => {
  const fromMeta = recvReceived.value ? recvMeta.value.received_suppliers : recvMeta.value.pending_suppliers
  if (fromMeta.length) return fromMeta
  // 兜底：meta 拿不到时退回旧口径（从当前列表去重），保证下拉不至于空掉
  const m = new Map<number, string>()
  for (const i of recvItems.value) m.set(i.supplier_id, i.supplier_name)
  return Array.from(m, ([id, name]) => ({ id, name }))
})
// 🆕 #330 命中上限提示：列表被截断时明说，别让人以为"数据没了"
const recvTruncated = computed(() => recvItems.value.length >= recvMeta.value.limit)

// 🆕 #330 三个搜索框全部下沉后端（见 loadReceiving），这里的本地过滤只保留「边打字边缩小」的即时反馈，
//   与后端同口径，回车/点查询后拿到的就是全库匹配结果。
// 🆕 #286 物料名称模糊过滤 + #290 下单时间(月份)过滤
// 🆕 #315 单号框一个框同时模糊匹配 采购单号(po_no)/订单编号(project_code)
const filteredRecv = computed(() => {
  const p = recvPo.value.trim().toLowerCase()
  const k = recvName.value.trim().toLowerCase()
  const m = recvOrderMonth.value
  return recvItems.value.filter(i =>
    (!p || (i.po_no || '').toLowerCase().includes(p) || (i.project_code || '').toLowerCase().includes(p)) &&
    (!k || (i.item_name || '').toLowerCase().includes(k)) &&
    (!m || (i.delivery_date || '').startsWith(m)))
})

// 🆕 需求二：采购收货列表也按采购单号(po_no)合并——同一采购单(≥2行)收成一个可展开的主汇总父行，
//   收货/送货单号、到货日期在父行上体现并在父行统一维护（合并收货写整批各行）。单行/无采购单号散单平铺。
const recvRowKey = (row: any) => (row._isGroup ? row._key : 'i' + row.id)
// 🆕 折叠层级配色：父(汇总)行 grp-row；子零件行由 Element 自动加 --level-1，走全局样式
const grpRowClass = ({ row }: { row: any }) => (row._isGroup ? 'grp-row' : '')
const groupedRecv = computed<any[]>(() => {
  const groups = new Map<string, any>()
  const out: any[] = []
  for (const it of filteredRecv.value) {
    const po = it.po_no
    if (!po) { out.push(it); continue }
    let g = groups.get(po)
    if (!g) {
      g = {
        _isGroup: true, _key: 'g:' + po, po_no: po,
        supplier_name: it.supplier_name, supplier_id: it.supplier_id,
        qty: 0, received_amount: 0, receipt_count: 0, stock_location: null as string | null,
        _noteCount: 0,                // 🆕 #356 该单有几行带备注
        _codes: new Set<string>(), _dnotes: new Set<string>(), _arrivals: new Set<string>(),
        _odates: new Set<string>(),   // 🆕 #290 下单时间聚合
        children: [] as RecvItem[],
      }
      groups.set(po, g); out.push(g)
    }
    g.children.push(it)
    g.qty += it.qty || 0
    g.received_amount += it.received_amount || 0
    if (it.stock_location) g.stock_location = g.stock_location && g.stock_location !== it.stock_location ? '多个' : it.stock_location
    g.receipt_count += it.receipt_count || 0
    if (it.project_code) g._codes.add(it.project_code)
    if (it.delivery_note_no) g._dnotes.add(it.delivery_note_no)
    if (it.arrival_date) g._arrivals.add(it.arrival_date)
    if (it.delivery_date) g._odates.add(it.delivery_date)
    if ((it.notes || '').trim()) g._noteCount++      // 🆕 #356 父行提示"里面有几条备注要看"
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
    const odates = Array.from(r._odates) as string[]   // 🆕 #290
    r.delivery_date = odates.length === 0 ? null : odates.length === 1 ? odates[0] : '多个'
    return r
  })
})

// 🆕 反馈#398（王利利）「卡的动都不动」：采购收货页已收货 1202 条、待收货 300 条，
//   一次性全渲染。**后端不慢**（实测 0.04s / 0.09s），慢的是 el-table ——
//   它不做虚拟滚动，1202 行 × 20 多列还要建父子树，浏览器直接卡住。
//   数据本来就全在内存里（后端一次取完），所以只需在**渲染层**分页，
//   搜索/筛选口径一点不动（那几个框仍然走后端，见 #330）。
const recvPage = ref(1)
const recvPageSize = ref(50)
const pagedRecv = computed(() => {
  const s = (recvPage.value - 1) * recvPageSize.value
  return groupedRecv.value.slice(s, s + recvPageSize.value)
})
// 换页签 / 改筛选后行数会变，停在第 5 页会看到空白表——回到第 1 页
watch([() => groupedRecv.value.length, recvReceived, recvPo, recvName, recvOrderMonth, recvSupplier],
      () => { recvPage.value = 1 })
async function loadReceiving() {
  recvLoading.value = true
  try {
    const r = await http.get<RecvItem[]>('/purchase-mgmt/receiving', {
      params: {
        received: recvReceived.value,
        supplier_id: recvSupplier.value || undefined,
        // 🆕 #330 关键字/物料名/下单月份下沉后端：只在已加载的那批里过滤，超出上限的老单永远搜不到
        keyword: recvPo.value.trim() || undefined,
        item_name: recvName.value.trim() || undefined,
        order_month: recvOrderMonth.value || undefined,
      },
    })
    recvItems.value = r.data
  } finally { recvLoading.value = false }
}
// 🆕 #330 供应商下拉/徽标条数的真实来源（与列表 limit 无关），切页签、收完货都刷
async function loadRecvMeta() {
  try {
    const r = await http.get('/purchase-mgmt/receiving/meta')
    recvMeta.value = r.data
    recvPendingCount.value = r.data.pending_count
    recvDoneCount.value = r.data.received_count
  } catch { /* 非关键，失败保留兜底口径 */ }
}

// 🆕 #141 tab 待办数徽标：待收货 / 待备货（红色角标，进页面就能看到有几条待处理）
const recvPendingCount = ref(0)
const recvDoneCount = ref(0)   // 已收货条数
const shipPendingCount = ref(0)
async function loadBadgeCounts() {
  try {
    // 🆕 #330 待收货/已收货条数改走 /receiving/meta 的 count(*)：原来拉两次全量列表取 length，
    //   既被 limit 截断（永远 ≤300，「待收货(300)」不是真实条数），又白拉两大坨数据。
    const [, ship] = await Promise.all([
      loadRecvMeta(),
      whApi.shipListPending('requested'),
    ])
    shipPendingCount.value = ship.length
  } catch { /* 徽标非关键，失败忽略 */ }
}
const recvVisible = ref(false)
const recvSaving = ref(false)
const recvForm = reactive({
  id: 0, po_no: '', supplier_name: '', item_name: '', spec: '', qty: null as number | null,
  stock_location: '' as string | null, project_code: '' as string | null,   // 🆕 #253 订单编号
  delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10),
  unit_price: null as number | null, received_amount: null as number | null,
})
// 收货弹窗里填的编号既不是项目、也不在字典里 → 提示。
// 判定口径与财务「成本审计·孤儿采购」完全一致（reports_router: 项目编号 ∪ order_no 字典）。
// ⚠️ 必须放在 recvForm 声明之后：`<script setup>` 里 computed 引用后声明的 ref 会踩 TDZ。
const recvUnknownCode = computed(() => {
  const c = (recvForm.project_code || '').trim()
  if (!c) return false
  return !projects.value.some(p => p.code === c) && !orderNoOptions.value.includes(c)
})
function openReceive(it: RecvItem) {
  if (!projects.value.length) loadProjects()   // 🆕 #253 订单编号下拉需要项目列表
  Object.assign(recvForm, {
    id: it.id, po_no: it.po_no || '', supplier_name: it.supplier_name,
    item_name: it.item_name, spec: it.spec || '', qty: it.qty ?? null,
    stock_location: it.stock_location || null, project_code: it.project_code || '',
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
    recvForm.unit_price = Number((recvForm.received_amount / recvForm.qty).toFixed(2))
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
// 🆕 #291：收货成功后本机当前页相关数据立即刷新，不再等关掉页面重进——
//   库存总览只在 onMounted 拉一次、流水/物料需求按 tab 懒加载，这里统一补刷（已拉过的才刷）。
//   注：ws 实时只有 datasheet/overview 房间的 cell_changed/presence 事件，没有仓库域广播机制，
//   收货接口又在采购域——跨客户端推送需另立 ws 事件，本次只做本机刷新。
function refreshAfterReceive() {
  loadMaterials()   // 全量：出库下拉要立刻能选到刚收的货（#404）
  loadMatList()     // 物料主数据那张表按它自己的筛选条件刷
  if (txns.value.length) loadTxns()
  if (demandProj.value) loadDemand()
  else if (demandOverview.value.length) loadDemandOverview()
}
async function submitReceive() {
  if (!recvForm.arrival_date) { ElMessage.warning('请填写到货日期'); return }
  if (!recvForm.stock_location) { ElMessage.warning('请选择库位（这批货放到哪个库；会同步到采购明细/装配/钣金/设计/电工）'); return }
  recvSaving.value = true
  try {
    await http.put(`/purchase-mgmt/items/${recvForm.id}/receive`, {
      delivery_note_no: recvForm.delivery_note_no || null,
      arrival_date: recvForm.arrival_date,
      unit_price: recvForm.unit_price,
      received_amount: recvForm.received_amount,
      stock_location: recvForm.stock_location || null,   // 🆕 #204 库位改由收货时填
      project_code: recvForm.project_code || null,       // 🆕 #253 订单编号补/改
    })
    if (recvReceiptFile.value) await uploadReceipt(recvForm.id, recvReceiptFile.value)
    ElMessage.success('已确认收货')
    recvVisible.value = false
    await loadReceiving(); loadBadgeCounts(); refreshAfterReceive()   // 🆕 #291
  } catch { /* handled */ } finally { recvSaving.value = false }
}

// 🆕 反馈#234/#235：点采购单号 → 新标签打开采购单 PDF(可查看+打印)
async function viewPoPdf(poNo?: string | null) {
  if (!poNo) return
  const w = window.open('', '_blank')   // 先开窗口(避免异步后被拦截)
  try {
    const res = await http.get(`/purchase-mgmt/orders/${encodeURIComponent(poNo)}/pdf`, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    if (w) { w.location.href = url } else { window.open(url, '_blank') }
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  } catch { if (w) w.close(); ElMessage.error('打开采购单失败') }
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
const batchRecvForm = reactive({ delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10), total_amount: null as number | null, stock_location: '' as string | null, project_code: '' as string | null })
const batchRecvLines = ref<{ item_id: number; item_name: string; spec?: string | null; qty: number | null; unit_price: number | null; received_amount: number | null; project_code: string }[]>([])
// 🆕 #376：一次合并收货里出现了几个不同项目编号——提示仓库这批料会分派到各自项目
const batchRecvCodes = computed(() => [...new Set(batchRecvLines.value.map(l => l.project_code).filter(Boolean))])
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
// 🆕 #293 合并收货逐行：填单价 → 收货金额自动=数量×单价(两位小数)；金额仍可手动覆盖，再改单价时重算
function onBatchLinePriceCalc(line: { qty: number | null; unit_price: number | null; received_amount: number | null }) {
  if (line.qty != null && line.unit_price != null) {
    line.received_amount = Number((line.qty * line.unit_price).toFixed(2))
  }
}
function openBatchReceive() {
  if (recvSelected.value.length < 1) { ElMessage.info('请先在列表勾选要合并收货的明细'); return }
  if (!projects.value.length) loadProjects()   // 🆕 #253
  batchRecvMode.value = 'lines'
  const scodes = [...new Set(recvSelected.value.map(i => i.project_code).filter(Boolean))]
  Object.assign(batchRecvForm, { delivery_note_no: '', arrival_date: new Date().toISOString().slice(0, 10), total_amount: null, stock_location: null, project_code: scodes.length === 1 ? scodes[0] : '' })
  batchReceiptFile.value = null
  batchRecvLines.value = recvSelected.value.map(i => ({
    item_id: i.id, item_name: i.item_name, spec: i.spec, qty: i.qty ?? null,
    unit_price: i.unit_price ?? null, received_amount: i.received_amount || null,
    // 🆕 #376：把**每一行自己的**订单编号带出来。一车拉来三个项目的料是常态，
    //   原来整批只有一个编号，收完货三个项目的料全挂到一个项目上——另两个项目
    //   在自己的物料需求里永远看不到这批料，成本也永远归不上去。
    project_code: i.project_code || '',
  }))
  // 🆕 #308：弹窗打开即按预填单价先算一轮（仅补空，已填金额不动）；之后改单价仍走 onBatchLinePriceCalc 重算
  batchRecvLines.value.forEach(l => { if (l.received_amount == null) onBatchLinePriceCalc(l) })
  batchRecvVisible.value = true
}
// 🆕 需求二：主汇总父行「合并收货」——直接对该采购单下所有零件行整批收货/维护送货单号
function openBatchReceiveGroup(row: any) {
  const children = (row.children || []) as RecvItem[]
  if (!children.length) return
  if (!projects.value.length) loadProjects()   // 🆕 #253
  batchRecvMode.value = 'lines'
  const dnote = row.delivery_note_no && row.delivery_note_no !== '多个' ? row.delivery_note_no : ''
  const adate = row.arrival_date && row.arrival_date !== '多个' ? row.arrival_date : new Date().toISOString().slice(0, 10)
  const rloc = row.stock_location && row.stock_location !== '多个' ? row.stock_location : null
  const rpc = row.project_code && row.project_code !== '多个' ? row.project_code : ''
  Object.assign(batchRecvForm, { delivery_note_no: dnote, arrival_date: adate, total_amount: null, stock_location: rloc, project_code: rpc })
  batchReceiptFile.value = null
  batchRecvLines.value = children.map(i => ({
    item_id: i.id, item_name: i.item_name, spec: i.spec, qty: i.qty ?? null,
    unit_price: i.unit_price ?? null, received_amount: i.received_amount || null,
    // 🆕 #376：把**每一行自己的**订单编号带出来。一车拉来三个项目的料是常态，
    //   原来整批只有一个编号，收完货三个项目的料全挂到一个项目上——另两个项目
    //   在自己的物料需求里永远看不到这批料，成本也永远归不上去。
    project_code: i.project_code || '',
  }))
  // 🆕 #308：同 openBatchReceive，打开即补算空金额
  batchRecvLines.value.forEach(l => { if (l.received_amount == null) onBatchLinePriceCalc(l) })
  batchRecvVisible.value = true
}
async function submitBatchReceive() {
  if (!batchRecvForm.arrival_date) { ElMessage.warning('请填写到货日期'); return }
  if (!batchRecvForm.stock_location) { ElMessage.warning('请选择库位（整批放到哪个库；会同步到采购明细/装配/钣金/设计/电工）'); return }
  batchRecvSaving.value = true
  try {
    const body: any = {
      item_ids: batchRecvLines.value.map(l => l.item_id),
      delivery_note_no: batchRecvForm.delivery_note_no || null,
      arrival_date: batchRecvForm.arrival_date,
      stock_location: batchRecvForm.stock_location || null,   // 🆕 #204 整批一个库位,收货时填
      project_code: batchRecvForm.project_code || null,       // 🆕 #253 整批一个订单编号
    }
    // 🆕 #376 逐行订单编号一起提交：后端按「逐行 > 明细原有 > 整批兜底」分派，
    //   整批那个编号只填空行，不再覆盖各行已有的编号。
    body.lines = batchRecvLines.value.map(l => ({
      item_id: l.item_id, unit_price: l.unit_price, received_amount: l.received_amount,
      project_code: l.project_code || null,
    }))
    await http.post('/purchase-mgmt/items/receive-batch', body)
    if (batchReceiptFile.value) {
      for (const l of batchRecvLines.value) await uploadReceipt(l.item_id, batchReceiptFile.value)
    }
    ElMessage.success(`已合并收货 ${batchRecvLines.value.length} 条`)
    batchRecvVisible.value = false
    await loadReceiving(); loadBadgeCounts(); refreshAfterReceive()   // 🆕 #291
  } catch { /* handled */ } finally { batchRecvSaving.value = false }
}

// ===== 🆕 项目物料需求（清单→仓库）=====
interface DemandRow {
  item_name: string; spec?: string | null; material_id?: number | null; location?: string | null
  demand_qty?: number | null; stock: number; suggest_purchase: number
  purchase_status: string; in_stock: boolean; issued_qty: number
  source?: string   // 清单=标准件清单需求；采购=采购单入库到本项目
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
    // 🆕 把到底会出哪几样列出来再确认——原来只说"N 种"，人不知道是哪 N 种，
    //    这正是王利利说的"容易出错"。
    const preview = lines.slice(0, 12)
      .map((r: any) => `${r.item_name}${r.spec ? '·' + r.spec : ''} × ${demandRemain(r)}`)
      .join('<br>')
    const more = lines.length > 12 ? `<br><span style="color:#999">…另有 ${lines.length - 12} 种</span>` : ''
    await ElMessageBox.confirm(
      `<div style="max-height:40vh;overflow:auto;line-height:1.9">${preview}${more}</div>`
      + `<div style="margin-top:8px">共 <b>${lines.length}</b> 种，将登记出库并计入本项目材料成本。</div>`,
      '确认一键领用出库', { type: 'warning', confirmButtonText: '确认出库', dangerouslyUseHTMLString: true })
  } catch { return }
  try {
    const r = await whApi.issueDemand(demandProj.value!, lines)
    ElMessage.success(r.message || '已出库')
    await Promise.all([loadDemand(), loadMaterials()])
  } catch { /* 拦截器已提示 */ }
}
// 🆕 反馈#244：勾选多行 → 只领用勾选的（介于「一键全部」和「一行一行点」之间）
const demandSel = ref<DemandRow[]>([])
// 只有货、且仍有未领需求的行可勾（无货/需求已满的出库不了）
function demandRowSelectable(r: DemandRow) { return !!r.material_id && r.stock > 0 && demandRemain(r) > 0 }
function onDemandSelChange(rows: DemandRow[]) { demandSel.value = rows }
async function issueSelected() {
  const lines = demandSel.value
    .filter(demandRowSelectable)
    .map(r => ({ material_id: r.material_id!, qty: Math.min(demandRemain(r), r.stock) }))
  if (!lines.length) { ElMessage.warning('请先勾选要领用出库的物料（需有货且仍有未领需求）'); return }
  try {
    await ElMessageBox.confirm(`将把勾选的 ${lines.length} 种物料按未领需求领用出库到本项目？会自动登记出库并计入项目材料成本。`,
      '批量领用出库', { type: 'warning', confirmButtonText: '领用出库' })
  } catch { return }
  try {
    const r = await whApi.issueDemand(demandProj.value!, lines)
    ElMessage.success(r.message || '已出库')
    demandSel.value = []
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
// ===== 🆕 #377 库位存量物料 → 调至项目物料（中转）=====
// ⚠️ 放在 projects 声明之后：openTransfer 里要 loadProjects()，写在前面会踩 TDZ。
// 调完这批料就属于那个项目了——会出现在它的物料需求里、退出库存总览、成本归到该项目，
// 全靠后端那两笔流水（无项目出库 + 挂项目入库），前端不需要为它记任何状态。
const ovSelected = ref<WhMaterial[]>([])
const transferVisible = ref(false)
const transferSaving = ref(false)
const transferForm = reactive({
  project_id: undefined as number | undefined,
  biz_date: new Date().toISOString().slice(0, 10),
  location: '' as string,
  note: '',
})
const transferLines = ref<{ material_id: number; name: string; spec?: string | null; unit: string; stock: number; qty: number }[]>([])
function openTransfer() {
  if (!ovSelected.value.length) { ElMessage.info('请先勾选要调拨的物料'); return }
  if (!projects.value.length) loadProjects()
  if (!locations.value.length) loadLocations()
  // 默认整数量全调过去——仓库常见做法是整批划给项目；要留一部分自己改数量
  transferLines.value = ovSelected.value.map(m => ({
    material_id: m.id, name: m.name, spec: m.spec, unit: m.unit, stock: m.stock, qty: m.stock,
  }))
  transferForm.project_id = undefined
  transferForm.biz_date = new Date().toISOString().slice(0, 10)
  transferForm.location = ''
  transferForm.note = ''
  transferVisible.value = true
}
async function submitTransfer() {
  if (!transferForm.project_id) { ElMessage.warning('请选择要调到哪个项目'); return }
  const lines = transferLines.value.filter(l => l.qty > 0).map(l => ({ material_id: l.material_id, qty: l.qty }))
  if (!lines.length) { ElMessage.warning('请填写调拨数量'); return }
  const over = transferLines.value.find(l => l.qty > l.stock)
  if (over) { ElMessage.warning(`${over.name} 调拨数量 ${over.qty} 超过现存 ${over.stock}`); return }
  transferSaving.value = true
  try {
    const r = await http.post<{ message: string }>('/wh/transfer-to-project', {
      project_id: transferForm.project_id, biz_date: transferForm.biz_date,
      location: transferForm.location || null, note: transferForm.note || null, lines,
    })
    ElMessage.success(r.data.message)
    transferVisible.value = false
    ovSelected.value = []
    await Promise.all([loadMaterials(), loadMatList(), loadTxns()])
  } catch { /* 拦截器已提示 */ } finally { transferSaving.value = false }
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
  if (name === 'recv') { loadReceiving(); loadRecvMeta() }   // 🆕 #330 meta 供全量供应商下拉/真实条数
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
// 🆕 #278 名称联想物料主数据（已入库物料）：选中后自动带出「规格型号」。
//   与 #289 项目详单「名称」列共用 GET /wh/materials/suggest 接口
interface MatSuggest { value: string; spec?: string | null }
async function fetchMatSuggestions(q: string, cb: (list: MatSuggest[]) => void) {
  if (!q.trim()) { cb([]); return }
  try {
    const r = await http.get<{ name: string; spec?: string | null }[]>('/wh/materials/suggest', { params: { q } })
    cb(r.data.map(m => ({ value: m.name, spec: m.spec })))
  } catch { cb([]) }
}
interface PreqLine { item_name: string; spec: string; qty: number | null; project_code: string; notes: string }
interface PreqRow { id: number; status: string; notes?: string | null; created_at: string
  need_date?: string | null; need_days?: number | null   // 🆕 #401 需求时间（need_days 由后端算，负=已过期）
  handler_name?: string | null; reject_reason?: string | null
  lines: { item_name: string; spec?: string | null; qty?: number | null; project_code?: string | null; notes?: string | null }[]
  attachments?: { id: number; name: string }[] }   // 🆕 #245/#246 直传文件
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
const preqForm = reactive({ buyer_id: '' as number | '', need_date: '', notes: '', lines: [blankPreqLine()] as PreqLine[] })
// 🆕 #245/#246 二选一：逐行填 或 直接上传文件
const preqMode = ref<'lines' | 'file'>('lines')
const preqFiles = ref<{ id: number; name: string }[]>([])
const preqUploading = ref(false)
const preqBuyers = ref<{ id: number; name: string }[]>([])
async function loadPreqBuyers() {
  try { preqBuyers.value = (await http.get<{ id: number; name: string }[]>('/purchase-mgmt/buyers')).data }
  catch { preqBuyers.value = [] }
}
function openPurchReq(presetLines?: PreqLine[], presetNote?: string) {
  preqForm.buyer_id = ''
  preqForm.need_date = ''
  preqForm.notes = presetNote || ''
  // 🆕 反馈 2026-08-07（杨坛）：「低于安全库存的物料要能展开清单，并且能直接递交采购申请」。
  //    缺料清单可以直接带进来，不用再一行行手抄。
  preqForm.lines = (presetLines && presetLines.length) ? presetLines : [blankPreqLine()]
  preqMode.value = 'lines'; preqFiles.value = []
  if (!preqBuyers.value.length) loadPreqBuyers()
  preqVisible.value = true
}

// 🆕 缺料清单：可展开 + 一键带进采购申请
const lowExpanded = ref(false)
function preqFromLow() {
  if (!lowList.value.length) { ElMessage.info('当前没有低于安全库存的物料'); return }
  const lines: PreqLine[] = lowList.value.map(m => ({
    item_name: m.name,
    spec: m.spec || '',
    // 建议采购量 = 安全库存 - 现存，至少 1；采购可以再改
    qty: Math.max(1, Math.ceil((m.safety_stock || 0) - (m.stock || 0))),
    project_code: '',
    notes: `低于安全库存（现存 ${m.stock}，安全线 ${m.safety_stock}）`,
  }))
  openPurchReq(lines, `仓库缺料补货：${lines.length} 项低于安全库存`)
}
function addPreqLine() { preqForm.lines.push(blankPreqLine()) }
function removePreqLine(i: number) { preqForm.lines.splice(i, 1); if (!preqForm.lines.length) preqForm.lines.push(blankPreqLine()) }
function pickPreqFile() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.xls,.xlsx,.pdf,.doc,.docx,.png,.jpg,.jpeg,.webp,.csv'; input.multiple = true
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    preqUploading.value = true
    try {
      for (const f of files) {
        const fd = new FormData()
        fd.append('file', f); fd.append('biz_type', 'purchase_request')
        const r = await http.post<{ id: number; name: string }>('/attachments', fd)
        preqFiles.value.push({ id: r.data.id, name: r.data.name })
      }
    } catch { /* 拦截器已提示 */ } finally { preqUploading.value = false }
  }
  input.click()
}
function removePreqFile(i: number) { preqFiles.value.splice(i, 1) }
async function submitPurchReq() {
  const payload: any = { buyer_id: preqForm.buyer_id || null, need_date: preqForm.need_date || null,
                        notes: preqForm.notes || null, lines: [], attachment_ids: [] }
  if (preqMode.value === 'file') {
    if (!preqFiles.value.length) { ElMessage.error('请先上传采购文件'); return }
    payload.attachment_ids = preqFiles.value.map(f => f.id)
  } else {
    const lines = preqForm.lines.filter(l => l.item_name.trim())
    if (!lines.length) { ElMessage.error('请至少填写一行要采购的物料（名称必填）'); return }
    payload.lines = lines.map(l => ({ item_name: l.item_name.trim(), spec: l.spec || null, qty: l.qty, project_code: l.project_code || null, notes: l.notes || null }))
  }
  preqSaving.value = true
  try {
    await http.post('/purchase-mgmt/purchase-requests', payload)
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

// ⚠️ 这一段必须放在文件末尾：下面引用的 summary / locations / demandOverview /
//    demandRows / groupedRecv / shipPending 都在前面几百行才声明，
//    computed 写在它们之前会踩 TDZ，页面一打开就白屏。
// ===== 🆕 各 tab 的关键词搜索（仓库反馈：所有功能都要能搜） =====
//
// ⚠️ 这里是**客户端**过滤，跟出入库流水不一样——那边后端会截断，
//    只在返回的那批里找等于搜不全（生产上 1083 条只回 200 条，搜到的最早只到昨天），
//    所以流水改成了服务端搜。而下面这几张表的数据是**整份都在前端**的
//    （库位 41、汇总/需求/收货/发货都是一次取完），客户端过滤就能搜全，不必改接口。
function kwHit(row: any, kw: string, fields: string[]): boolean {
  const k = kw.trim().toLowerCase()
  if (!k) return true
  return fields.some(f => String(row?.[f] ?? '').toLowerCase().includes(k))
}

const sumSearch = ref('')
const filteredSummary = computed(() =>
  summary.value.filter(r => kwHit(r, sumSearch.value, ['name', 'spec', 'unit'])))

const locSearch = ref('')
const filteredLocations = computed(() =>
  locations.value.filter(r => kwHit(r, locSearch.value, ['name', 'note'])))

const demandOvSearch = ref('')
const filteredDemandOverview = computed(() =>
  demandOverview.value.filter(r => kwHit(r, demandOvSearch.value, ['code', 'name'])))

const demandSearch = ref('')
// 🆕 反馈 2026-08-07（王利利）：「已有订单号未出库的能不能不要显示，
//    这样在某个订单一键出库的话容易出错」。
//    列表里混着一堆现在出不了库的行（没货、已领完、还没到货），
//    「一键领用出库」只会处理其中有货且还有需求的那些——所见与所得不一致，
//    人对着一屏行按下去，自然心里没底。默认只显示**真正会被出库的行**，
//    让所见即所得；要看全貌把勾去掉即可。
const demandOnlyIssuable = ref(true)
function demandIssuable(r: any) {
  return !!r.material_id && (r.stock || 0) > 0 && demandRemain(r) > 0
}
const filteredDemandRows = computed(() =>
  demandRows.value
    .filter(r => !demandOnlyIssuable.value || demandIssuable(r))
    .filter(r => kwHit(r, demandSearch.value,
      ['item_name', 'spec', 'location', 'source', 'purchase_status'])))

// 采购收货**不在这里加**：它本来就有三个搜索框且已下沉到后端（见 #330 的 filteredRecv），
// 能搜到上限之外的老单，比我再叠一层本地过滤强。重复造只会两套口径打架。

const shipSearch = ref('')
const filteredShipPending = computed(() =>
  shipPending.value.filter((r: any) => kwHit(r, shipSearch.value, ['code', 'name'])))

// ===== 渲染分页：el-table 没有虚拟滚动（#398 那次的老问题）=====
// 数据本来就一次性全在内存里（后端一把取完），**搜索/筛选/合计口径一点不动**，
// 慢的纯粹是建 DOM：物料主数据 863 行 × 12 列 ≈ 一万个单元格，一次铺出来浏览器就卡住。
// 这里只在渲染层切一片出来。
// ⚠️ 必须放在 materials / txns / filteredSummary 之后：usePager 里的 `watch(total,…)`
//    会立刻求一次值，声明在前面就是 TDZ 报错、整页白屏（本文件已经踩过一次）。
function usePager<T>(src: () => T[], size = 50) {
  const page = ref(1)
  const pageSize = ref(size)
  const total = computed(() => src().length)
  const rows = computed(() => {
    const s = (page.value - 1) * pageSize.value
    return src().slice(s, s + pageSize.value)
  })
  watch(total, () => { page.value = 1 })   // 换了筛选行数就变了，还停在第 8 页会看到一张空表
  // 用 reactive 包一层：模板里直接写 `matPager.rows` / `v-model:current-page="matPager.page"`，
  // 不用到处点 .value（对象上的 ref 在模板里是**不会**自动解包的）
  return reactive({ page, pageSize, total, rows })
}
const ovPager = usePager(() => ovMaterials.value)
const matPager = usePager(() => matList.value)   // 物料主数据页签：搜索走后端(kw)，切片只管渲染
const sumPager = usePager(() => filteredSummary.value)
const txnPager = usePager(() => txns.value)


const preqSearch = ref('')
const preqStatus = ref('')
const filteredPreq = computed(() => {
  const k = preqSearch.value.trim().toLowerCase()
  return preqList.value.filter((r: any) => {
    if (preqStatus.value && r.status !== preqStatus.value) return false
    if (!k) return true
    // ⚠️ 物料名在展开行的 lines 里，主行看不到——不搜 lines 的话
    //    仓库按物料名找自己提过的申请就永远找不到。
    const inLines = (r.lines || []).some((l: any) =>
      String(l.item_name ?? '').toLowerCase().includes(k) ||
      String(l.spec ?? '').toLowerCase().includes(k))
    return kwHit(r, k, ['req_no', 'status', 'note', 'created_by_name']) || inLines
  })
})


// ⚠️ 放文件末尾：下面用到的 ioLines / ioForm / projects 都在前面几百行才声明，
//    写在它们之前会踩 TDZ，页面一开就白屏（这个坑今天已经踩过一次）。
// 🆕 反馈 2026-08-07（杨坛）：「出库登记这里加一个功能，可搜索采购单号，
//    然后选择里面的物料进行出库」。仓库手头拿的是一张采购单，
//    而登记表单只能从几百个物料里翻着找。
interface PoItem {
  id: number; po_no?: string | null; item_name: string; spec?: string | null
  qty?: number | null; arrival_date?: string | null; project_code?: string | null
  stock_location?: string | null
  material_id?: number | null; stock: number; unmatched_reason?: string | null
}
const poPickVisible = ref(false)
const poKw = ref('')
const poItems = ref<PoItem[]>([])
const poLoading = ref(false)
const poSel = ref<PoItem[]>([])
let poTimer: ReturnType<typeof setTimeout> | null = null
function onPoSearch() {
  if (poTimer) clearTimeout(poTimer)
  poTimer = setTimeout(loadPoItems, 300)
}
async function loadPoItems() {
  poLoading.value = true
  try {
    poItems.value = (await http.get<PoItem[]>('/wh/po-items',
      { params: { po_no: poKw.value.trim() || undefined } })).data
  } catch { poItems.value = [] } finally { poLoading.value = false }
}
function openPoPick() {
  poKw.value = ''; poSel.value = []
  poPickVisible.value = true
  loadPoItems()
}
// 匹配不到物料主数据的行不能出库（出库扣的是物料库存），禁选并在行里说明原因
function poSelectable(r: PoItem) { return !!r.material_id }
function applyPoPick() {
  const picked = poSel.value.filter(r => r.material_id)
  if (!picked.length) { ElMessage.warning('请至少勾选一行能出库的物料'); return }
  // 已经填了的空行先清掉，避免带进一堆空行
  const kept = ioLines.value.filter(l => l.material_id)
  for (const r of picked) {
    const exist = kept.find(l => l.material_id === r.material_id)
    // 同一个物料在一张单里出现两次就合并数量，不然会生成两张出库单
    if (exist) exist.qty = (exist.qty || 0) + (r.qty || 1)
    else kept.push({ material_id: r.material_id as number, qty: r.qty || 1 })
  }
  ioLines.value = kept.length ? kept : [{ material_id: undefined, qty: 1 }]
  // 采购单上挂了项目就顺带带上，省得再选一次
  const withProj = picked.find(r => r.project_code)
  if (withProj?.project_code) {
    const p = projects.value.find(x => x.code === withProj.project_code)
    if (p) ioForm.project_id = p.id
  }
  poPickVisible.value = false
  ElMessage.success(`已带入 ${picked.length} 种物料`)
}

</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>仓库</h1>
        <div class="desc">物料主数据 + 出入库（自动单号·超库存拦截）+ 收发存汇总 + 流水（冲红）+ 发货清单</div>
      </div>
      <div class="spacer"></div>
      <PageRefresh :load="() => { loadMaterials(); loadMatList(); loadMatDict(); loadBadgeCounts(); loadLocations(); onTab(tab) }" />
    </div>

    <el-card shadow="never" v-loading="loading">
      <!-- 每个 el-tab-pane 都必须带 `lazy`。Element Plus 的 lazy **默认是 false**，
           意思是九个页签的表格在页面一挂载时就全部渲染成真 DOM（只是 display:none 藏着），
           点都没点过的页签也照渲。生产上物料主数据 863 行 × 12 列、收发存汇总 863 行，
           一进仓库就是一万多个单元格——白屏就是这么来的。
           加了 lazy 之后只渲染当前页签，别的等点到了再建。-->
      <el-tabs v-model="tab" @tab-change="onTab">
        <!-- 总览 -->
        <el-tab-pane lazy v-if="tv('ov')" label="库存总览" name="ov">
          <div class="kpi-grid">
            <div class="kpi"><div class="kpi-v">{{ ovAll.length }}</div><div class="kpi-l">物料种类</div></div>
            <div class="kpi"><div class="kpi-v">{{ totalStock }}</div><div class="kpi-l">库存总量</div></div>
            <div v-if="isManager" class="kpi"><div class="kpi-v">{{ fmtMoney(totalValue) }}</div><div class="kpi-l">库存总价</div></div>
            <!-- ⚠️ 这里用 lowList.length 而不是后端的 low_count：后端那个是全量口径，
                 本 tab 已经滤掉项目物料，两个数摆在一起对不上会被当成 bug -->
            <div class="kpi" :class="lowList.length ? 'is-bad' : ''"><div class="kpi-v">{{ lowList.length }}</div><div class="kpi-l">低于安全库存</div></div>
          </div>
          <!-- 🆕 #373/#374：说清楚少掉的那些料去哪了。不写这一句，仓库只会以为系统把料弄丢了 -->
          <el-alert v-if="projMatCount" type="info" :closable="false" show-icon style="margin-bottom:10px"
            :title="`本页只看通用库存物料；另有 ${projMatCount} 种是买给具体项目的料（收货时填了订单编号），在「出入库 / 物料需求」里按项目管，成本已在收货时计入该项目`" />
          <!-- 🆕 反馈 2026-08-07（杨坛）：缺料要能展开看清单，并直接提采购申请。
               原来只有一条 alert 把名字挤成一行，缺 20 种就是一坨看不清，
               看完还得自己去采购申请里一行行手抄。 -->
          <div v-if="lowList.length" class="low-box">
            <div class="low-head" @click="lowExpanded = !lowExpanded">
              <el-icon class="low-arrow" :class="{ open: lowExpanded }"><ArrowRight /></el-icon>
              <b>⚠ 低库存预警：{{ lowList.length }} 种物料低于安全库存</b>
              <span class="muted small">（点击{{ lowExpanded ? '收起' : '展开' }}清单）</span>
              <span style="flex:1"></span>
              <el-button v-if="canWrite" type="warning" size="small" :icon="Plus"
                         @click.stop="preqFromLow">一键提采购申请</el-button>
            </div>
            <el-table v-if="lowExpanded" :data="lowList" size="small" stripe class="compact-tbl"
                      max-height="260" style="margin-top:8px">
              <el-table-column prop="name" label="物料" min-width="140" />
              <el-table-column prop="spec" label="规格型号" min-width="120">
                <template #default="{ row }">{{ row.spec || '—' }}</template>
              </el-table-column>
              <el-table-column prop="location" label="库位" width="100">
                <template #default="{ row }">{{ row.location || '—' }}</template>
              </el-table-column>
              <el-table-column label="现存" width="90" align="right">
                <template #default="{ row }"><b style="color:var(--el-color-danger)">{{ row.stock }}</b></template>
              </el-table-column>
              <el-table-column prop="safety_stock" label="安全库存" width="90" align="right" />
              <el-table-column label="缺口" width="90" align="right">
                <template #default="{ row }">{{ Math.max(0, (row.safety_stock || 0) - (row.stock || 0)) }}</template>
              </el-table-column>
              <el-table-column prop="unit" label="单位" width="60" />
            </el-table>
          </div>
          <div style="display:flex;gap:10px;margin-bottom:10px;align-items:center">
            <!-- ⚠️ #404：这个框以前绑的是物料主数据那个 kw，一搜就把全量 materials 换成命中的那几条——
                 出库下拉、库存总览的 KPI 全跟着变。改成本页签自己的本地过滤，不碰共享数组。 -->
            <el-input v-model="ovSearch" placeholder="搜索物料/规格/库位" :prefix-icon="Search" clearable style="width:240px" />
            <span v-if="ovSearch" class="muted small">命中 {{ ovMaterials.length }} / {{ ovAll.length }}（上面的合计仍是全部）</span>
            <!-- 🆕 #377：库里躺着的通用料确定要给某个项目用了，在这里调过去。
                 调完它就进那个项目的物料需求（项目上的人才看得到有货），后面统一领料出库。 -->
            <el-button v-if="canWrite && ovSelected.length" type="primary" :icon="Sort" @click="openTransfer">
              调至项目物料 ({{ ovSelected.length }})
            </el-button>
            <span v-else-if="canWrite" class="muted small">勾选物料可「调至项目物料」——存量料划给某个项目后，会进该项目的物料需求统一出库</span>
          </div>
          <!-- 🆕 列宽整齐化：文本列(名称/规格/类别)用 min-width 填充空白；数字/短列用固定宽度右对齐，
               避免数字列被拉伸出大空隙（此前 #140 把数字列也设 min-width 导致宽屏爆宽） -->
          <!-- ⚠️ 分页 + 勾选要一起看：row-key + reserve-selection 让勾过的行**翻页也不丢**，
               否则「调至项目物料」翻一页勾选就清空了，人根本不知道自己刚才勾的没了 -->
          <el-table show-overflow-tooltip :data="ovPager.rows" row-key="id" stripe size="small"
                    max-height="calc(100vh - 290px)"
                    @selection-change="(v: WhMaterial[]) => ovSelected = v">
            <el-table-column v-if="canWrite" type="selection" width="42" reserve-selection />
            <el-table-column prop="code" label="编码" width="120"><template #header><span>编码</span><el-tooltip placement="top" effect="dark"><template #content>物料编码 = 大类(1位) - 中类+细分(4位) - 流水号(4位)<br/>选「编码分类」到细分类时自动生成,如 1-0101-0001</template><el-icon style="vertical-align:-2px;margin-left:3px;color:var(--text-3);cursor:help;font-size:13px"><QuestionFilled /></el-icon></el-tooltip></template><template #default="{ row }"><span v-if="row.code" class="code">{{ fmtMatCode(row.code) }}</span><span v-else class="muted">—</span></template></el-table-column>
            <el-table-column prop="category_path" label="编码说明" min-width="150" show-overflow-tooltip><template #default="{ row }"><span v-if="row.category_path">{{ row.category_path }}</span><span v-else class="muted">—</span></template></el-table-column>
            <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="spec" label="规格型号" min-width="140"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <!-- 🆕 弃用「物料类别」列(与编码分类树重复,改由编码分类统一);字段保留不删 -->
            <el-table-column prop="unit" label="单位" width="64" align="center" />
            <el-table-column label="现存" width="90" align="right">
              <template #default="{ row }"><b :class="{ bad: row.low }">{{ row.stock }}</b></template>
            </el-table-column>
            <el-table-column v-if="isManager" label="单价" width="90" align="right">
              <template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template>
            </el-table-column>
            <el-table-column v-if="isManager" label="总价" width="110" align="right">
              <template #default="{ row }"><b>{{ row.stock_value != null ? fmtMoney(row.stock_value) : '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="safety_stock" label="安全库存" width="90" align="right" />
            <el-table-column prop="location" label="库位" width="100"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
          </el-table>
          <el-pagination v-if="ovPager.total > ovPager.pageSize"
                         v-model:current-page="ovPager.page" v-model:page-size="ovPager.pageSize"
                         :page-sizes="[50, 100, 200, 500]" :total="ovPager.total"
                         layout="total, sizes, prev, pager, next, jumper" size="small"
                         style="margin-top:10px;justify-content:flex-end" />
          <EmptyHint v-if="!materials.length" text="暂无物料，去「物料主数据」新增" size="sm" />
        </el-tab-pane>

        <!-- 出入库登记 -->
        <!-- 「出入库登记」tab 已合并进「出入库 / 物料需求」(见 demand tab 顶部两个按钮) -->

        <!-- 收发存汇总 -->
        <el-tab-pane lazy v-if="tv('sum')" label="收发存汇总" name="sum">
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
            <el-date-picker v-model="period" type="month" value-format="YYYY-MM" @change="loadSummary" />
            <el-input v-model="sumSearch" placeholder="搜物料/规格/单位" :prefix-icon="Search" clearable size="small" style="width:220px" />
            <span class="muted" style="font-size:12.5px" v-if="sumSearch">命中 {{ filteredSummary.length }} / {{ summary.length }}</span>
            <span class="muted small">期初 + 本期入 − 本期出 = 期末</span>
          </div>
          <!-- 合计行本来就是拿全量 summary 自己算的（不是 el-table 默认的"当前数据求和"），
               所以分页之后「合计」仍然是整月全部物料的合计，不会变成本页小计 -->
          <el-table show-overflow-tooltip :data="sumPager.rows" stripe size="small" show-summary
                    :summary-method="(p:any) => ['合计','','', summary.reduce((s,r)=>s+r.opening,0), summary.reduce((s,r)=>s+r.in_qty,0), summary.reduce((s,r)=>s+r.out_qty,0), summary.reduce((s,r)=>s+r.closing,0)]"
                    max-height="calc(100vh - 290px)" :scrollbar-always-on="true">
            <el-table-column prop="name" label="物料" min-width="120" />
            <el-table-column prop="spec" label="规格" min-width="100"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column prop="opening" label="期初" width="90" />
            <el-table-column prop="in_qty" label="本期入" width="90" />
            <el-table-column prop="out_qty" label="本期出" width="90" />
            <el-table-column prop="closing" label="期末" width="90"><template #default="{ row }"><b>{{ row.closing }}</b></template></el-table-column>
          </el-table>
          <el-pagination v-if="sumPager.total > sumPager.pageSize"
                         v-model:current-page="sumPager.page" v-model:page-size="sumPager.pageSize"
                         :page-sizes="[50, 100, 200, 500]" :total="sumPager.total"
                         layout="total, sizes, prev, pager, next, jumper" size="small"
                         style="margin-top:10px;justify-content:flex-end" />
          <EmptyHint v-if="!summary.length" text="该月暂无收发存数据" size="sm" />
        </el-tab-pane>

        <!-- 流水 -->
        <el-tab-pane lazy v-if="tv('txn')" label="出入库流水" name="txn">
          <div style="margin-bottom:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <el-radio-group v-model="txnDir" @change="loadTxns" size="small">
              <el-radio-button value="">全部</el-radio-button>
              <el-radio-button value="in">入库</el-radio-button>
              <el-radio-button value="out">出库</el-radio-button>
            </el-radio-group>
            <!-- 🆕 搜索走服务端：全库都能搜，不再只在最近 200 条里找 -->
            <el-input v-model="txnSearch" placeholder="搜单号/物料/规格/库位/来源/往来单位/项目编号"
                      :prefix-icon="Search" clearable size="small" style="width:300px"
                      @input="onTxnSearch" @clear="loadTxns" />
            <el-date-picker v-model="txnRange" type="daterange" size="small" style="width:230px"
                            value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期"
                            unlink-panels @change="loadTxns" />
            <el-button v-if="txnSearch || txnRange || txnDir" size="small" @click="resetTxnFilter">清空条件</el-button>
            <span class="muted" style="font-size:12.5px">
              共 {{ txnTotal }} 条<template v-if="txnTotal > txnShown">，
                <b style="color:var(--el-color-warning)">只显示了最近 {{ txnShown }} 条，请缩小范围</b>
              </template>
            </span>
          </div>
          <el-table v-loading="txnLoading" show-overflow-tooltip :data="txnPager.rows" stripe size="small" max-height="calc(100vh - 290px)" :scrollbar-always-on="true">
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
            <el-table-column prop="location" label="库位" width="90"><template #default="{ row }"><b v-if="row.location" style="color:var(--el-color-primary)">{{ row.location }}</b><span v-else class="muted">—</span></template></el-table-column>
            <el-table-column prop="source" label="来源/用途" width="100"><template #default="{ row }">{{ row.source || '—' }}</template></el-table-column>
            <el-table-column prop="party" label="供应商/领用方" min-width="110"><template #default="{ row }">{{ row.party || '—' }}</template></el-table-column>
            <el-table-column prop="project_code" label="项目" width="100"><template #default="{ row }">{{ row.project_code || '—' }}</template></el-table-column>
            <el-table-column label="操作" width="90" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <StatusPill v-if="row.is_reversal" text="冲红单" variant="muted" />
                <StatusPill v-else-if="row.reversed" text="已冲红" variant="danger" />
                <el-button v-else-if="canWrite" size="small" link type="danger" @click="reverseTxn(row)">冲红</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination v-if="txnPager.total > txnPager.pageSize"
                         v-model:current-page="txnPager.page" v-model:page-size="txnPager.pageSize"
                         :page-sizes="[50, 100, 200, 500]" :total="txnPager.total"
                         layout="total, sizes, prev, pager, next, jumper" size="small"
                         style="margin-top:10px;justify-content:flex-end" />
          <EmptyHint v-if="!txns.length" :text="(txnSearch || txnRange) ? '没搜到，换个词或放宽日期试试' : '暂无出入库流水'" size="sm" />
        </el-tab-pane>

        <!-- 物料主数据 -->
        <el-tab-pane lazy v-if="tv('mat')" label="物料主数据" name="mat">
          <!-- 🆕 仓库反馈：物料主数据没搜索框，551 条只能一页页翻 -->
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <el-input v-model="kw" placeholder="搜名称/规格/编码/单位/库位/材质" :prefix-icon="Search"
                      clearable size="small" style="width:280px" @input="onMatSearch" @clear="loadMatList" />
            <el-select v-model="matLoc" placeholder="库位(全部)" clearable size="small" style="width:150px" @change="loadMatList">
              <el-option v-for="l in enabledLocations" :key="l.id" :label="l.name" :value="l.name" />
            </el-select>
            <el-checkbox v-model="matLowOnly" size="small" @change="loadMatList">只看低于安全库存</el-checkbox>
            <el-button v-if="kw || matLoc || matLowOnly" size="small" @click="resetMatFilter">清空条件</el-button>
            <span class="muted" style="font-size:12.5px">共 {{ matTotal }} 条<template v-if="lowCount">，其中 {{ lowCount }} 条低于安全库存</template></span>
            <div style="flex:1"></div>
            <el-button v-if="canWrite" type="primary" :icon="Plus" size="small" @click="openMat()">新增物料</el-button>
            <el-button v-if="canConfigFields" :icon="Setting" size="small" @click="openFieldManager">字段设置</el-button>
            <el-button v-if="canClear" type="danger" plain :icon="Delete" size="small" @click="clearAll">一键清空</el-button>
          </div>
          <el-table show-overflow-tooltip :data="matPager.rows" stripe size="small" max-height="calc(100vh - 290px)" :scrollbar-always-on="true">
            <el-table-column prop="code" label="编码" width="120"><template #header><span>编码</span><el-tooltip placement="top" effect="dark"><template #content>物料编码 = 大类(1位) - 中类+细分(4位) - 流水号(4位)<br/>选「编码分类」到细分类时自动生成,如 1-0101-0001</template><el-icon style="vertical-align:-2px;margin-left:3px;color:var(--text-3);cursor:help;font-size:13px"><QuestionFilled /></el-icon></el-tooltip></template><template #default="{ row }"><span v-if="row.code" class="code">{{ fmtMatCode(row.code) }}</span><span v-else class="muted">—</span></template></el-table-column>
            <el-table-column prop="category_path" label="编码说明" min-width="150" show-overflow-tooltip><template #default="{ row }"><span v-if="row.category_path">{{ row.category_path }}</span><span v-else class="muted">—</span></template></el-table-column>
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="spec" label="规格型号" min-width="120"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <!-- 🆕 弃用「物料类别」列(与编码分类树重复);字段保留不删 -->
            <el-table-column prop="material_grade" label="材质" width="100"><template #default="{ row }">{{ row.material_grade || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column label="单价" width="90" align="right"><template #default="{ row }">{{ row.unit_price != null ? fmtMoney(row.unit_price) : '—' }}</template></el-table-column>
            <el-table-column prop="safety_stock" label="安全库存" width="90" />
            <el-table-column prop="init_stock" label="期初库存" width="90" />
            <el-table-column prop="location" label="库位" width="90"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
            <el-table-column v-for="f in listCustomFields" :key="f.id" :label="f.label" min-width="100">
              <template #default="{ row }">{{ cfDisplay(row.custom_values, f) }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="110" fixed="right" :show-overflow-tooltip="false"><template #default="{ row }"><el-button size="small" link type="primary" @click="openMat(row)">编辑</el-button><el-button size="small" link type="danger" @click="deleteMat(row)">删除</el-button></template></el-table-column>
          </el-table>
          <el-pagination v-if="matPager.total > matPager.pageSize"
                         v-model:current-page="matPager.page" v-model:page-size="matPager.pageSize"
                         :page-sizes="[50, 100, 200, 500]" :total="matPager.total"
                         layout="total, sizes, prev, pager, next, jumper" size="small"
                         style="margin-top:10px;justify-content:flex-end" />
          <EmptyHint v-if="!matList.length" :text="(kw || matLoc || matLowOnly) ? '没搜到，换个词或清空条件试试' : '暂无物料主数据，点「新增物料」开始'" size="sm" />
        </el-tab-pane>

        <!-- 🆕 项目物料需求（清单→仓库）-->
        <!-- 🆕 库位管理:主数据维护(采购下单/物料/出入库共用取值) -->
        <el-tab-pane lazy v-if="tv('loc')" label="库位管理" name="loc">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openLoc()">新增库位</el-button>
            <el-input v-model="locSearch" placeholder="搜库位/备注" :prefix-icon="Search" clearable size="small" style="width:200px" />
            <span class="muted" style="font-size:12.5px" v-if="locSearch">命中 {{ filteredLocations.length }} / {{ locations.length }}</span>
            <span class="muted small">采购下单选库位、物料库位、出入库登记都从这里取值；「占用/空闲」跟着出入库走——有货入=占用，出库腾空=空闲；在用的库位删不掉，可停用。</span>
          </div>
          <el-table show-overflow-tooltip :data="filteredLocations" stripe size="small" class="compact-tbl" max-height="calc(100vh - 260px)">
            <el-table-column prop="name" label="库位" min-width="140"><template #default="{ row }"><b style="color:var(--el-color-primary)">{{ row.name }}</b></template></el-table-column>
            <!-- 🆕 #204 占用/空闲:由库存(出入库流水)驱动;占用时悬停看里面放了什么 -->
            <el-table-column label="占用状态" width="140">
              <template #default="{ row }">
                <el-tooltip v-if="row.occupied && row.occupied_items?.length" placement="top">
                  <template #content>
                    <div style="max-width:280px">
                      <div v-for="(it, i) in row.occupied_items" :key="i" style="line-height:1.7">
                        {{ it.name }}{{ it.spec ? '·' + it.spec : '' }} <b>×{{ it.stock }}</b>
                      </div>
                    </div>
                  </template>
                  <el-tag size="small" type="warning" effect="dark" style="cursor:default">占用（{{ row.occupied_items.length }} 项）</el-tag>
                </el-tooltip>
                <el-tag v-else size="small" type="success" effect="plain">空闲</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="备注" min-width="150"><template #default="{ row }">{{ row.note || '—' }}</template></el-table-column>
            <el-table-column prop="mat_count" label="在用物料数" width="100" align="center" />
            <el-table-column prop="sort_order" label="排序" width="70" align="center" />
            <el-table-column label="状态" width="80">
              <template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="120" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openLoc(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="deleteLoc(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!locations.length" text="暂无库位，点「新增库位」开始（如 A区-1排 / 1号库）" size="sm" />
        </el-tab-pane>

        <!-- 🆕 出入库登记 + 物料需求 合并：顶部两个登记按钮(逻辑不变)，下方物料需求(按项目汇总在库物料+复选出库) -->
        <el-tab-pane lazy v-if="tv('demand')" label="出入库 / 物料需求" name="demand">
          <!-- 顶部：出入库登记两个按钮，逻辑独立不变；可对任意物料手工登记（含非项目领用） -->
          <div v-if="canWrite" class="io-bar">
            <el-button type="primary" :icon="Plus" @click="openIo('in')">入库登记</el-button>
            <el-button type="warning" :icon="Plus" @click="openIo('out')">出库登记</el-button>
            <span class="muted small">入库单号 RK+日期+序号；出库单号 CK…；出库超现存将被拦截。手工登记走这里；按项目批量领用走下方需求表。</span>
          </div>
          <!-- #157：默认直接列出有清单的项目 + 待出库/已出库条数，不用先从下拉选项目 -->
          <template v-if="!demandProj">
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <b>项目物料需求总览</b>
              <el-button :icon="Search" size="small" @click="loadDemandOverview">刷新</el-button>
              <el-input v-model="demandOvSearch" placeholder="搜项目编号/名称" :prefix-icon="Search" clearable size="small" style="width:220px" />
              <span class="muted" style="font-size:12.5px" v-if="demandOvSearch">命中 {{ filteredDemandOverview.length }} / {{ demandOverview.length }}</span>
              <span class="muted small">列出有物料需求的项目（材料清单：标准件清单/电工采购单/不锈钢原料下料单，+ 采购单入库关联了项目号的）；点「查看」进入逐行需求并领用出库。待出库=有货且未领完的物料行数，已出库=已领用过的行数。</span>
            </div>
            <el-table show-overflow-tooltip :data="filteredDemandOverview" v-loading="demandOverviewLoading" stripe size="small"
                      max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl" :fit="false">
              <!-- 🆕 #385 全流程进度同步到所有带项目编号的部门 -->
              <el-table-column label="项目编号" width="160"><template #default="{ row }">
                <b class="code">{{ row.code }}</b>
                <ProjectFlowButton :project-id="row.project_id" :code="row.code" />
              </template></el-table-column>
              <el-table-column prop="name" label="项目名称" min-width="200" show-overflow-tooltip />
              <el-table-column label="物料行数" width="100" align="right"><template #default="{ row }">{{ row.total_lines }}</template></el-table-column>
              <el-table-column label="待出库" width="110" align="center">
                <template #default="{ row }"><StatusPill :text="`待出库 ${row.pending_out}`" :variant="row.pending_out > 0 ? 'warn' : 'muted'" /></template>
              </el-table-column>
              <el-table-column label="已出库" width="110" align="center">
                <template #default="{ row }"><StatusPill :text="`已出库 ${row.issued_out}`" :variant="row.issued_out > 0 ? 'success' : 'muted'" /></template>
              </el-table-column>
              <el-table-column label="操作" width="100" align="center" fixed="right" :show-overflow-tooltip="false">
                <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDemandProject(row.project_id)">查看</el-button></template>
              </el-table-column>
              <template #empty><EmptyHint text="暂无有物料需求的项目（无材料清单、也无关联项目的采购入库）" size="sm" /></template>
            </el-table>
          </template>

          <template v-else>
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <el-button :icon="ArrowLeft" size="small" @click="backToDemandOverview">返回项目列表</el-button>
            <b class="code">{{ demandProjLabel }}</b>
            <el-button v-if="canWrite" type="warning" :icon="Plus" size="small" @click="issueAll">一键领用出库</el-button>
            <!-- 🆕 反馈#244：勾选多行只领用勾选的 -->
            <el-button v-if="canWrite" type="warning" plain size="small" :disabled="!demandSel.length"
                       @click="issueSelected">批量领用出库{{ demandSel.length ? `（已选 ${demandSel.length}）` : '' }}</el-button>
            <el-input v-model="demandSearch" placeholder="搜名称/规格/库位/来源" :prefix-icon="Search" clearable size="small" style="width:240px" />
            <!-- 🆕 反馈（王利利）：默认只显示真正能出库的行，一键出库前所见即所得 -->
            <el-checkbox v-model="demandOnlyIssuable" size="small">只看现在能出库的</el-checkbox>
            <span class="muted" style="font-size:12.5px">显示 {{ filteredDemandRows.length }} / 共 {{ demandRows.length }} 行</span>
            <span class="muted small">合并「材料清单需求(标准件清单/电工采购单/不锈钢原料下料单)」与「采购单入库到本项目的物料」(来源列区分),逐行看 需求量 / 现有库存 / 建议采购量。有货的可勾选后批量领用、或单行领用、或一键全部,缺的走采购。</span>
          </div>
          <el-table show-overflow-tooltip :data="filteredDemandRows" v-loading="demandLoading" stripe size="small"
                    max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl"
                    @selection-change="onDemandSelChange">
            <el-table-column v-if="canWrite" type="selection" width="42" :selectable="demandRowSelectable" fixed="left" />
            <el-table-column prop="item_name" label="名称" min-width="150" />
            <el-table-column prop="spec" label="规格型号" min-width="150"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column label="来源" width="82" align="center">
              <template #default="{ row }"><StatusPill :text="row.source || '清单'" :variant="row.source === '采购' ? 'primary' : 'muted'" /></template>
            </el-table-column>
            <el-table-column label="需求量" width="104" align="right"><template #default="{ row }">{{ row.demand_qty ?? '—' }}</template></el-table-column>
            <el-table-column label="现有库存" width="112" align="right">
              <template #default="{ row }"><b :class="{ bad: row.stock <= 0 }">{{ row.stock }}</b></template>
            </el-table-column>
            <el-table-column prop="location" label="库位" min-width="130"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
            <el-table-column label="建议采购" width="112" align="right">
              <template #default="{ row }"><span :class="{ bad: row.suggest_purchase > 0 }">{{ row.suggest_purchase }}</span></template>
            </el-table-column>
            <!-- 🆕 反馈#393：原来是 `有货 ? 有货可出 : 需采购`，只看现存。
                 料一领用出库，现存归 0 就跳「需采购」——可它明明已经领到项目上用了，
                 旁边「已领用 2 / 需求 2」「建议采购 0」全都对得上，就这一列在喊要买，
                 三个数自相矛盾。改成先看还差不差：领够了就是「已领完」。 -->
            <el-table-column label="库存" width="94" align="center">
              <template #default="{ row }">
                <StatusPill v-if="demandRemain(row) <= 0" text="已领完" variant="muted" />
                <StatusPill v-else-if="row.in_stock" text="有货可出" variant="success" />
                <StatusPill v-else text="需采购" variant="warn" />
              </template>
            </el-table-column>
            <el-table-column label="采购状态" width="100" align="center">
              <template #default="{ row }">
                <StatusPill :text="row.purchase_status" :variant="row.purchase_status === '已到货' ? 'success' : row.purchase_status === '已下单' ? 'primary' : 'muted'" />
              </template>
            </el-table-column>
            <el-table-column label="已领用" width="90" align="right">
              <template #default="{ row }">{{ row.issued_qty || 0 }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="110" align="center" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button v-if="row.material_id && row.stock > 0" size="small" type="warning" plain @click="issueOne(row)">领用出库</el-button>
                <span v-else class="muted small">—</span>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!demandLoading && !demandRows.length" text="该项目暂无物料需求（无材料清单，也无关联本项目的采购入库）" size="sm" />
          </template>
        </el-tab-pane>

        <!-- 🆕 采购收货 -->
        <el-tab-pane lazy v-if="tv('recv')" name="recv">
          <template #label>采购收货<span v-if="recvPendingCount" class="wh-tab-badge">{{ recvPendingCount > 99 ? '99+' : recvPendingCount }}</span></template>
          <EmptyHint v-if="!canWrite" text="仅仓库角色可确认收货" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <el-radio-group v-model="recvReceived" @change="loadReceiving" size="large" class="recv-toggle">
                <el-radio-button :value="false">待收货（{{ recvPendingCount }}）</el-radio-button>
                <el-radio-button :value="true">已收货（{{ recvDoneCount }}）</el-radio-button>
              </el-radio-group>
              <!-- 🆕 #310 供应商下拉可搜索 -->
              <el-select v-model="recvSupplier" placeholder="全部供应商" filterable clearable style="width:180px" @change="loadReceiving">
                <el-option v-for="s in recvSupplierOptions" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
              <!-- 🆕 #315 一个框同时模糊匹配采购单号/订单编号(项目编号)；🆕 #330 改为回车/查询走后端全库搜 -->
              <el-input v-model="recvPo" placeholder="采购单号/项目编号" clearable style="width:170px"
                        @keyup.enter="loadReceiving" @clear="loadReceiving" />
              <!-- 🆕 #286 物料名称关键字 + #290 下单月份；🆕 #330 同样下沉后端，搜得到上限之外的老单 -->
              <el-input v-model="recvName" placeholder="物料名称" clearable style="width:140px"
                        @keyup.enter="loadReceiving" @clear="loadReceiving" />
              <el-date-picker v-model="recvOrderMonth" type="month" value-format="YYYY-MM" placeholder="下单月份" clearable
                              style="width:130px" @change="loadReceiving" />
              <el-button :icon="Search" @click="loadReceiving">查询</el-button>
              <!-- 🆕 #321 自动合并、手动展开：合并组默认收起，一键全展/全收 -->
              <el-button link type="primary" size="small" @click="toggleRecvExpandAll">
                {{ recvExpandAll ? '全部收起' : '全部展开' }}
              </el-button>
              <el-button v-if="recvSelected.length" type="primary" @click="openBatchReceive">合并收货 ({{ recvSelected.length }})</el-button>
              <span class="muted small">采购下单的物料到货后，在这里核对规格、填送货单号/到货日期；单价未填的（后填价格）在此补上。合并零件可勾选多条「合并收货」只填总价。</span>
              <!-- 🆕 #330 命中上限时明说是被截断了，并指路用搜索框（关键字/物料名走后端，能搜到上限之外的老单） -->
              <el-alert v-if="recvTruncated" type="warning" show-icon :closable="false" style="width:100%"
                        :title="`当前${recvReceived ? '已收货' : '待收货'}共 ${recvReceived ? recvMeta.received_count : recvMeta.pending_count} 条，本页最多显示 ${recvMeta.limit} 条。用上面的供应商/单号/物料名筛选可搜全部（含更早的长周期订单）。`" />
            </div>
            <el-table show-overflow-tooltip :data="pagedRecv" v-loading="recvLoading" stripe size="small" @selection-change="onRecvSelect"
                      :key="recvExpandKey" :row-key="recvRowKey" :tree-props="{ children: 'children' }" :default-expand-all="recvExpandAll"
                      :row-class-name="grpRowClass"
                      max-height="calc(100vh - 320px)" :scrollbar-always-on="true" class="compact-tbl">
              <el-table-column type="selection" width="40" :selectable="(row: any) => !row._isGroup" />
              <el-table-column prop="po_no" label="采购单号" width="205" :show-overflow-tooltip="false">
                <template #default="{ row }">
                  <el-tag v-if="row._isGroup" size="small" type="warning" effect="plain" style="margin-right:4px">合并{{ row._count }}</el-tag>
                  <!-- 🆕 反馈#234/#235：点采购单号查看/打印采购单 -->
                  <el-button v-if="row.po_no" link type="primary" class="code" style="padding:0" @click="viewPoPdf(row.po_no)">{{ row.po_no }}</el-button>
                  <span v-else class="code">—</span>
                </template>
              </el-table-column>
              <el-table-column prop="supplier_name" label="供应商" min-width="130" />
              <el-table-column prop="project_code" label="订单编号" width="110">
                <template #default="{ row }">{{ row.project_code || '—' }}</template>
              </el-table-column>
              <!-- 🆕 #290 下单时间（采购下单填的下单日期；合并行多日期显示「多个」） -->
              <el-table-column prop="delivery_date" label="下单时间" width="104">
                <template #default="{ row }">{{ row.delivery_date || '—' }}</template>
              </el-table-column>
              <el-table-column prop="stock_location" label="库位" width="92">
                <template #default="{ row }"><b v-if="row.stock_location" style="color:var(--el-color-primary)">{{ row.stock_location }}</b><span v-else class="muted">—</span></template>
              </el-table-column>
              <el-table-column prop="item_name" label="名称" min-width="120">
                <template #default="{ row }">{{ row._isGroup ? `共 ${row._count} 项零件` : row.item_name }}</template>
              </el-table-column>
              <el-table-column prop="spec" label="规格型号" min-width="120">
                <template #default="{ row }">{{ row._isGroup ? '' : (row.spec || '—') }}</template>
              </el-table-column>
              <!-- 🆕 反馈#356（李新新）：外购件常常只有图纸，仓库对料时得知道详细尺寸。
                   采购下单时本来就能填「备注」，但收货这张表从来不显示——填了也白填。
                   紧挨规格型号放，对料时一眼扫过去；合并父行提示里面有几条，提醒展开看。 -->
              <el-table-column prop="notes" label="备注（采购填）" min-width="150">
                <template #default="{ row }">
                  <span v-if="row._isGroup" class="muted small">{{ row._noteCount ? `📝 ${row._noteCount} 项有备注，展开看` : '' }}</span>
                  <el-tooltip v-else-if="row.notes" :content="row.notes" placement="top" :show-after="200">
                    <span class="recv-note">{{ row.notes }}</span>
                  </el-tooltip>
                  <span v-else class="muted">—</span>
                </template>
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
                  <span v-if="row._isGroup" class="muted small">{{ row.receipt_count ? `凭证 ${row.receipt_count}` : '—' }}</span>
                  <el-button v-else-if="row.receipt_count" size="small" link type="primary" @click="viewReceipts(row)">凭证 {{ row.receipt_count }}</el-button>
                  <span v-else class="muted small">—</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="168" align="center" fixed="right" :show-overflow-tooltip="false">
                <template #default="{ row }">
                  <!-- 🆕 固定列直达采购单：合并/单行都能一键点开采购单PDF，不再依赖会被截断的采购单号链接 -->
                  <el-button v-if="row.po_no" size="small" link type="primary" @click="viewPoPdf(row.po_no)">采购单</el-button>
                  <el-button v-if="row._isGroup" size="small" :type="recvReceived ? 'default' : 'primary'" plain @click="openBatchReceiveGroup(row)">
                    {{ recvReceived ? '合并修改' : '合并收货' }}
                  </el-button>
                  <el-button v-else size="small" :type="recvReceived ? 'default' : 'primary'" plain @click="openReceive(row)">
                    {{ recvReceived ? '修改' : '收货' }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <!-- 🆕 #398：分页只影响**渲染**，搜索/筛选仍然走后端全库（#330），
                 所以翻页找不到的东西用上面的搜索框照样搜得到。 -->
            <el-pagination v-if="groupedRecv.length > recvPageSize"
                           v-model:current-page="recvPage" v-model:page-size="recvPageSize"
                           :page-sizes="[50, 100, 200, 500]" :total="groupedRecv.length"
                           layout="total, sizes, prev, pager, next, jumper" size="small"
                           style="margin-top:10px;justify-content:flex-end" />
            <EmptyHint v-if="!recvLoading && !filteredRecv.length" :text="recvReceived ? '暂无已收货记录' : '暂无待收货物料'" size="sm" />
          </template>
        </el-tab-pane>

        <!-- 发货清单目录：设计部下发 → 仓库核对备齐 → 通知物流 -->
        <el-tab-pane lazy v-if="tv('ship')" name="ship">
          <template #label>发货清单<span v-if="shipPendingCount" class="wh-tab-badge">{{ shipPendingCount > 99 ? '99+' : shipPendingCount }}</span></template>
          <EmptyHint v-if="!canWrite" text="仅仓库角色可查看发货清单目录" :icon="Lock" />
          <template v-else>
            <div class="ship-cat-head">
              <div class="ship-pending-title" style="margin:0">发货清单目录</div>
              <el-radio-group v-model="shipFilter" size="small">
                <el-radio-button label="requested">待备货</el-radio-button>
                <el-radio-button label="ready">已备齐</el-radio-button>
                <el-radio-button label="all">全部</el-radio-button>
              </el-radio-group>
              <el-input v-model="shipSearch" placeholder="搜项目编号/名称" :prefix-icon="Search" clearable size="small" style="width:210px" />
              <span class="muted" style="font-size:12.5px" v-if="shipSearch">命中 {{ filteredShipPending.length }} / {{ shipPending.length }}</span>
            </div>
            <div class="muted small" style="margin:4px 0 12px">
              发货清单由设计部下发（同时直推发货部与仓库）。仓库只需按清单核对、备好货物后点「已备齐」，物流发货部即可安排发货——无需在此上传。
            </div>

            <el-table show-overflow-tooltip :data="filteredShipPending" v-loading="shipPendingLoading" stripe size="small"
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
              <el-table-column label="操作" width="110" align="center" fixed="right" :show-overflow-tooltip="false">
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
        <el-tab-pane lazy v-if="tv('preq')" label="采购申请" name="preq">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可提采购申请" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px">
              <el-button type="primary" :icon="Plus" @click="openPurchReq">提采购申请</el-button>
              <el-button :icon="Search" size="small" @click="loadPurchReqs">刷新</el-button>
              <!-- 🆕 仓库反馈：这个 tab 也没搜索框，提过几十条之后翻不动 -->
              <el-input v-model="preqSearch" placeholder="搜申请单号/物料/规格/备注" :prefix-icon="Search"
                        clearable size="small" style="width:250px" />
              <el-select v-model="preqStatus" placeholder="状态(全部)" clearable size="small" style="width:130px">
                <el-option label="待处理" value="pending" />
                <el-option label="已处理" value="done" />
                <el-option label="已驳回" value="rejected" />
              </el-select>
              <span class="muted" style="font-size:12.5px" v-if="preqSearch || preqStatus">命中 {{ filteredPreq.length }} / {{ preqList.length }}</span>
              <span class="muted small">仓库发现要买的东西（缺料/耗材/工具等）在这里提申请，采购部会收到通知并处理。</span>
            </div>
            <el-table show-overflow-tooltip :data="filteredPreq" v-loading="preqLoading" stripe size="small" max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="compact-tbl">
              <el-table-column type="expand" width="36">
                <template #default="{ row }">
                  <!-- 🆕 #245/#246 直传文件（可下载） -->
                  <div v-if="row.attachments && row.attachments.length" style="margin:6px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                    <span class="muted small">采购文件：</span>
                    <el-button v-for="a in row.attachments" :key="a.id" size="small" link type="primary" @click="downloadAttachment(a)">{{ a.name }}</el-button>
                  </div>
                  <el-table v-if="row.lines && row.lines.length" show-overflow-tooltip :data="row.lines" size="small" border style="margin:6px 12px">
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
              <el-table-column label="物料" min-width="200"><template #default="{ row }">
                <span v-if="row.lines && row.lines.length">{{ row.lines.map((l: any) => l.item_name).slice(0, 3).join('、') }}{{ row.lines.length > 3 ? ` 等${row.lines.length}项` : '' }}</span>
                <span v-else-if="row.attachments && row.attachments.length" class="muted">📎 上传文件（{{ row.attachments.length }}）</span>
                <span v-else class="muted">—</span>
              </template></el-table-column>
              <el-table-column label="状态" width="100" align="center"><template #default="{ row }"><StatusPill :text="PREQ_STATUS[row.status] || row.status" :variant="preqStatusVariant(row.status)" /></template></el-table-column>
              <el-table-column label="处理" min-width="140"><template #default="{ row }"><span v-if="row.status === 'done'" class="muted small">{{ row.handler_name }} 已处理</span><span v-else-if="row.status === 'rejected'" class="danger small">驳回：{{ row.reject_reason || '—' }}</span><span v-else class="muted small">等待采购部处理</span></template></el-table-column>
              <!-- 🆕 #401 需求时间：过期标红、当天标橙，自己也能一眼看出哪张还压着 -->
              <el-table-column label="需求时间" width="120"><template #default="{ row }">
                <span v-if="!row.need_date" class="muted">—</span>
                <span v-else :class="row.need_days != null && row.need_days < 0 ? 'danger' : (row.need_days != null && row.need_days <= 2 ? 'warn' : '')">
                  {{ row.need_date }}
                  <span v-if="row.need_days != null && row.status === 'pending'" class="small">
                    （{{ row.need_days < 0 ? `已过期 ${-row.need_days} 天` : (row.need_days === 0 ? '就是今天' : `还剩 ${row.need_days} 天`) }}）
                  </span>
                </span>
              </template></el-table-column>
              <el-table-column label="提交时间" width="110"><template #default="{ row }">{{ (row.created_at || '').slice(0, 10) }}</template></el-table-column>
              <el-table-column label="操作" width="70" align="center" fixed="right" :show-overflow-tooltip="false"><template #default="{ row }"><el-button size="small" link type="danger" @click="deletePurchReq(row)">删除</el-button></template></el-table-column>
              <template #empty><EmptyHint text="暂无采购申请，点「提采购申请」开始" size="sm" /></template>
            </el-table>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 🆕 #167 提采购申请弹窗 -->
    <el-dialog v-model="preqVisible" title="提采购申请" width="min(880px, 96vw)" top="5vh">
      <el-form label-position="top" style="margin-bottom:6px">
        <div style="display:flex;gap:18px;flex-wrap:wrap">
          <el-form-item label="指定采购员（推送给他；不选则通知全体采购员）">
            <el-select v-model="preqForm.buyer_id" filterable clearable placeholder="选择采购员" style="width:320px">
              <el-option v-for="b in preqBuyers" :key="b.id" :label="b.name" :value="b.id" />
            </el-select>
          </el-form-item>
          <!-- 🆕 反馈#401（李新新）：没有需求时间，采购不知道你急不急，也没法凑单 -->
          <el-form-item label="需求时间（什么时候要用）">
            <el-date-picker v-model="preqForm.need_date" type="date" value-format="YYYY-MM-DD"
                            placeholder="选择日期（选填）" style="width:200px" />
            <div class="muted small" style="line-height:1.5;margin-top:2px">
              填了采购好安排：不急的可以攒一起凑单，急的优先下单
            </div>
          </el-form-item>
        </div>
      </el-form>
      <!-- 🆕 #245/#246 二选一：逐行填 或 直接上传文件 -->
      <el-radio-group v-model="preqMode" style="margin-bottom:10px">
        <el-radio-button value="lines">逐行填写</el-radio-button>
        <el-radio-button value="file">上传文件</el-radio-button>
      </el-radio-group>

      <template v-if="preqMode === 'lines'">
        <div class="order-lines-head">
          <span class="order-lines-title">采购物料（{{ preqForm.lines.length }} 行）</span>
          <el-button size="small" :icon="Plus" @click="addPreqLine">添加一行</el-button>
        </div>
        <el-table show-overflow-tooltip :data="preqForm.lines" size="small" border max-height="46vh">
          <el-table-column type="index" label="#" width="44" align="center" />
          <el-table-column label="名称 *" min-width="160">
            <template #default="{ row }">
              <!-- 🆕 #278 名称联想物料主数据里已入库物料，选中自动带出规格型号 -->
              <el-autocomplete v-model="row.item_name" :fetch-suggestions="fetchMatSuggestions"
                               placeholder="物料名称" style="width:100%"
                               @select="(it: any) => { if (it.spec) row.spec = it.spec }">
                <template #default="{ item }">
                  <span>{{ item.value }}</span><span v-if="item.spec" class="muted small"> · {{ item.spec }}</span>
                </template>
              </el-autocomplete>
            </template>
          </el-table-column>
          <el-table-column label="规格型号" min-width="140"><template #default="{ row }"><el-input v-model="row.spec" placeholder="规格/型号" /></template></el-table-column>
          <el-table-column label="数量" width="110"><template #default="{ row }"><el-input-number v-model="row.qty" :min="0" :controls="false" style="width:100%" /></template></el-table-column>
          <el-table-column label="项目编号" width="120"><template #default="{ row }"><el-input v-model="row.project_code" placeholder="选填" /></template></el-table-column>
          <el-table-column label="备注" min-width="120"><template #default="{ row }"><el-input v-model="row.notes" placeholder="选填" /></template></el-table-column>
          <el-table-column label="操作" width="60" align="center" fixed="right" :show-overflow-tooltip="false"><template #default="{ $index }"><el-button size="small" link type="danger" :icon="Delete" @click="removePreqLine($index)" /></template></el-table-column>
        </el-table>
      </template>

      <template v-else>
        <el-alert type="info" :closable="false" style="margin-bottom:10px" title="直接上传采购文件（清单 Excel/PDF/图片，可多个），不用逐行填。采购员在采购申请里能下载。" />
        <el-button :icon="Upload" :loading="preqUploading" @click="pickPreqFile">选择文件</el-button>
        <div v-if="preqFiles.length" style="margin-top:10px;display:flex;flex-direction:column;gap:6px">
          <div v-for="(f, i) in preqFiles" :key="f.id" style="display:flex;align-items:center;gap:8px">
            <el-tag type="success" effect="plain">📎 {{ f.name }}</el-tag>
            <el-button size="small" link type="danger" :icon="Delete" @click="removePreqFile(i)" />
          </div>
        </div>
      </template>

      <el-input v-model="preqForm.notes" type="textarea" :rows="2" placeholder="整单备注（选填）" style="margin-top:12px" />
      <template #footer>
        <el-button @click="preqVisible = false">取消</el-button>
        <el-button type="primary" :loading="preqSaving" @click="submitPurchReq">提交申请</el-button>
      </template>
    </el-dialog>

    <!-- 出入库弹窗 -->
    <!-- 🆕 #377 调至项目物料 -->
    <el-dialog v-model="transferVisible" title="调至项目物料（中转）" width="720px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px"
        title="把库位上的存量物料划给某个项目。库存净额不变（记一笔转出、一笔转入），调完这批料会出现在该项目的「物料需求」里统一领料出库，并从「库存总览 / 库存金额」转入该项目的材料成本。" />
      <el-form label-position="top">
        <div style="display:flex;gap:12px">
          <el-form-item label="调到哪个项目" required style="flex:1">
            <el-select v-model="transferForm.project_id" filterable placeholder="选择项目" style="width:100%">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code} ${p.name}`" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="日期" style="width:170px">
            <el-date-picker v-model="transferForm.biz_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
          </el-form-item>
          <el-form-item label="转入库位（中转库，不填沿用原库位）" style="width:210px">
            <el-select v-model="transferForm.location" clearable placeholder="沿用原库位" style="width:100%">
              <el-option v-for="l in locations" :key="l.id" :label="l.name" :value="l.name" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="物料与调拨数量（默认整批调过去，要留一部分就改数量）">
          <el-table :data="transferLines" size="small" stripe class="compact-tbl" max-height="320" style="width:100%">
            <el-table-column label="物料" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">{{ row.name }}<span v-if="specOf(row.name, row.spec)" class="muted small"> · {{ specOf(row.name, row.spec) }}</span></template>
            </el-table-column>
            <el-table-column label="现存" width="90" align="right"><template #default="{ row }">{{ row.stock }} {{ row.unit }}</template></el-table-column>
            <el-table-column label="调拨数量" width="150">
              <template #default="{ row }">
                <el-input-number v-model="row.qty" :min="0" :max="row.stock" :controls="false" size="small" style="width:100%" />
              </template>
            </el-table-column>
          </el-table>
        </el-form-item>
        <el-form-item label="备注（选填，会写在流水的往来单位上）">
          <el-input v-model="transferForm.note" placeholder="如：中转库暂存" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="transferVisible = false">取消</el-button>
        <el-button type="primary" :loading="transferSaving" @click="submitTransfer">确认调拨（{{ transferLines.length }} 项）</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ioVisible" :title="ioForm.direction === 'in' ? '入库登记' : '出库登记（可多行批量）'"
               :width="ioForm.direction === 'out' ? '680px' : '480px'">
      <el-form label-position="top">
        <!-- 🆕 #325 出库=多行批量：逐行选物料+数量，消耗品一次出多种；入库保持单行 -->
        <el-form-item v-if="ioForm.direction === 'out'" label="物料（逐行选择，数量填在行内；一次提交每行各生成一张出库单）" required>
          <div v-for="(ln, i) in ioLines" :key="i" style="display:flex;gap:8px;width:100%;margin-bottom:8px">
            <el-select v-model="ln.material_id" filterable placeholder="选择物料" style="flex:1">
              <el-option v-for="m in materials" :key="m.id" :label="matLabel(m)" :value="m.id">
                <span :title="matProjTitle(m)">{{ matLabel(m) }}</span>
              </el-option>
            </el-select>
            <el-input-number v-model="ln.qty" :min="1" :controls="false" placeholder="数量" style="width:110px" />
            <el-button :icon="Delete" circle plain type="danger" :disabled="ioLines.length <= 1" @click="removeIoLine(i)" />
          </div>
          <el-button :icon="Plus" plain size="small" @click="addIoLine">添加一行物料</el-button>
          <!-- 🆕 反馈 2026-08-07（杨坛）：仓库手头拿的是一张采购单，
               让他按单号把该单的物料整批带进来，别在几百个物料里翻 -->
          <el-button :icon="Search" plain size="small" type="primary" @click="openPoPick">按采购单号带入</el-button>
        </el-form-item>
        <el-form-item v-else label="物料" required>
          <el-select v-model="ioForm.material_id" filterable placeholder="选择物料" style="width:100%">
            <el-option v-for="m in materials" :key="m.id" :label="matLabel(m)" :value="m.id">
              <span :title="matProjTitle(m)">{{ matLabel(m) }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <div class="frow">
          <el-form-item v-if="ioForm.direction === 'in'" label="数量" required style="flex:1"><el-input-number v-model="ioForm.qty" :min="1" :controls="false" style="width:100%" /></el-form-item>
          <el-form-item label="业务日期" style="flex:1"><el-date-picker v-model="ioForm.biz_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
        </div>
        <!-- 批量出库单价随物料参考单价自动算金额（同需求领用口径），不再逐行手填 -->
        <div class="frow" v-if="ioForm.direction === 'in'">
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
        <!-- 🆕 库位:入库=放到哪(选填,默认物料当前库位,选了会更新物料库位);批量出库=各行从物料当前库位领(流水自动记录) -->
        <div class="frow" v-if="ioForm.direction === 'in'">
          <el-form-item label="库位（放到哪;不选=物料当前库位）" style="flex:1">
            <el-select v-model="ioForm.location" filterable clearable placeholder="选择库位" style="width:100%">
              <el-option v-for="l in enabledLocations" :key="l.id" :label="l.name" :value="l.name" />
            </el-select>
          </el-form-item>
        </div>
        <!-- 🆕 盈利改善1b：出库必选项目(计入项目材料成本)；确属非项目领用需勾选+填原因 -->
        <div class="frow" v-if="ioForm.direction === 'out'">
          <el-form-item label="领用项目（必选；无主出库的材料钱不会进任何项目成本）" :required="!ioForm.non_project" style="flex:1">
            <el-select v-model="ioForm.project_id" filterable clearable :disabled="ioForm.non_project"
                       placeholder="选择领用到哪个项目" style="width:100%">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow" v-if="ioForm.direction === 'out'" style="align-items:flex-end">
          <el-form-item style="flex-shrink:0;margin-bottom:0">
            <el-checkbox v-model="ioForm.non_project"
                         @change="(v: any) => { if (v) ioForm.project_id = undefined }">非项目领用</el-checkbox>
          </el-form-item>
          <el-form-item v-if="ioForm.non_project" style="flex:1;margin-bottom:0">
            <el-input v-model="ioForm.non_project_reason" placeholder="非项目领用原因（必填，如：车间耗材/工具磨损）" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="ioVisible = false">取消</el-button>
        <el-button type="primary" :loading="ioSubmitting" @click="submitIo">登记</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 按采购单号挑物料出库 -->
    <el-dialog v-model="poPickVisible" title="按采购单号选物料出库" width="760px" append-to-body>
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <el-input v-model="poKw" placeholder="输入采购单号（可只输一段）" :prefix-icon="Search"
                  clearable style="width:280px" @input="onPoSearch" @clear="loadPoItems" />
        <span class="muted small">只列**已到货**的行——没到货的出不了库。勾选后带进出库单，数量可再改。</span>
      </div>
      <el-table :data="poItems" v-loading="poLoading" size="small" stripe max-height="42vh"
                @selection-change="(v: PoItem[]) => poSel = v">
        <el-table-column type="selection" width="42" :selectable="poSelectable" />
        <el-table-column prop="po_no" label="采购单号" width="150" />
        <el-table-column prop="item_name" label="物料" min-width="130" />
        <el-table-column prop="spec" label="规格型号" min-width="120">
          <template #default="{ row }">{{ row.spec || '—' }}</template>
        </el-table-column>
        <el-table-column prop="qty" label="采购量" width="80" align="right" />
        <el-table-column label="现存" width="80" align="right">
          <template #default="{ row }">
            <b :style="row.stock > 0 ? '' : 'color:var(--el-color-danger)'">{{ row.stock }}</b>
          </template>
        </el-table-column>
        <el-table-column prop="project_code" label="项目" width="110">
          <template #default="{ row }">{{ row.project_code || '—' }}</template>
        </el-table-column>
        <el-table-column label="能否出库" min-width="150">
          <template #default="{ row }">
            <span v-if="row.material_id" style="color:var(--el-color-success)">可出库</span>
            <el-tooltip v-else :content="row.unmatched_reason || ''" placement="top">
              <span style="color:var(--el-color-danger)">未建档，不能出库</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty><EmptyHint :text="poKw ? '这个单号下没有已到货的物料' : '暂无已到货的采购明细'" size="sm" /></template>
      </el-table>
      <template #footer>
        <el-button @click="poPickVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!poSel.length" @click="applyPoPick">
          带入出库单{{ poSel.length ? `（已选 ${poSel.length}）` : '' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 🆕 库位弹窗 -->
    <el-dialog v-model="locVisible" :title="locForm.id ? '编辑库位' : '新增库位'" width="420px">
      <el-form label-position="top">
        <el-form-item label="库位名称" required><el-input v-model="locForm.name" placeholder="如 A区-3排 / 1号库" maxlength="64" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="locForm.note" placeholder="选填,如 存放电气件" maxlength="128" /></el-form-item>
        <div class="frow">
          <el-form-item label="排序" style="flex:1"><el-input-number v-model="locForm.sort_order" :controls="false" style="width:100%" /></el-form-item>
          <el-form-item label="启用" style="flex:1"><el-switch v-model="locForm.enabled" /></el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="locVisible = false">取消</el-button>
        <el-button type="primary" :loading="locSaving" @click="submitLoc">保存</el-button>
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
          <el-form-item style="flex:1.4">
            <template #label>编码分类（选到细分类自动发码）</template>
            <el-cascader v-model="matForm.category_id" :options="matCatCascade"
                         :props="{ emitPath: false, value: 'id', label: 'lab' }"
                         clearable filterable placeholder="大类 / 中类 / 细分类" style="width:100%" />
          </el-form-item>
          <el-form-item label="物料编码" style="flex:1">
            <el-input :model-value="matForm.code ? fmtMatCode(matForm.code) : '保存后自动生成'" disabled />
          </el-form-item>
        </div>
        <div class="frow">
          <!-- 🆕 弃用「物料类别」维护(改由编码分类树统一);matForm.category 字段保留、编辑时原值不动 -->
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
          <el-form-item label="库位" style="flex:1">
            <el-select v-model="matForm.location" filterable clearable placeholder="选择库位(库位管理里维护)" style="width:100%">
              <el-option v-for="l in enabledLocations" :key="l.id" :label="l.name" :value="l.name" />
            </el-select>
          </el-form-item>
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
      <el-table show-overflow-tooltip :data="customFields" size="small" border stripe max-height="34vh">
        <el-table-column type="index" label="#" width="46" align="center" />
        <el-table-column prop="label" label="字段名称" min-width="110" />
        <el-table-column label="类型" width="90"><template #default="{ row }">{{ CF_TYPES.find(t => t.v === row.ftype)?.l || row.ftype }}</template></el-table-column>
        <el-table-column label="必填" width="60"><template #default="{ row }">{{ row.required ? '是' : '—' }}</template></el-table-column>
        <el-table-column label="列表显示" width="80"><template #default="{ row }">{{ row.show_in_list ? '是' : '—' }}</template></el-table-column>
        <el-table-column label="排序" width="60" prop="sort_order" />
        <el-table-column label="状态" width="70"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="100" fixed="right" :show-overflow-tooltip="false">
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
        <!-- 🆕 #204 库位统一在收货时填(取代采购下单填);填后同步采购明细/装配/钣金/设计/电工 -->
        <el-form-item label="库位（放到哪个库）" required>
          <el-select v-model="recvForm.stock_location" filterable clearable placeholder="选择库位（库位管理里维护）" style="width:100%">
            <el-option v-for="l in enabledLocations" :key="l.id" :label="l.name" :value="l.name" />
          </el-select>
        </el-form-item>
        <!-- 🆕 #253 订单编号：手工采购单没填的，仓库收货可补/改（选项目 或 直接输订单号） -->
        <el-form-item label="订单编号（属于哪个项目/订单；手工采购单没填的可在此补）">
          <el-select v-model="recvForm.project_code" filterable allow-create default-first-option clearable
                     placeholder="选项目编号，或选「备用/车间耗材」这类订单编号" style="width:100%">
            <el-option-group v-if="orderNoOptions.length" label="非项目（字典维护）">
              <el-option v-for="v in orderNoOptions" :key="'d:' + v" :label="v" :value="v" />
            </el-option-group>
            <el-option-group label="项目">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code}　${p.name}`" :value="p.code" />
            </el-option-group>
          </el-select>
          <!-- 手打了一个既不是项目、也不在字典里的编号：不硬拦（总有临时情况），但必须让人看见。
               静默接受的代价是这批料挂不到任何项目，仓库在「物料需求」里根本看不到它，
               成本也永远归不上去——生产上已经这样丢了 ¥11,637。 -->
          <div v-if="recvUnknownCode" class="small" style="color:var(--el-color-warning-dark-2,#b88230);margin-top:4px">
            ⚠ 「{{ recvForm.project_code }}」不是系统里的项目，也不在订单编号字典里。
            这批料不会出现在任何项目的「物料需求」里，成本也归不到项目上。确认要用请忽略此提示。
          </div>
        </el-form-item>
        <div class="frow">
          <!-- 反馈#346（李新新）：采购用优惠券会单开一行负金额，收货补价时也得能填负数。 -->
          <el-form-item label="单价（后填价格在此补）">
            <el-input-number v-model="recvForm.unit_price" :precision="2" :controls="false" style="width:100%" @change="onRecvCalc" />
          </el-form-item>
          <el-form-item label="收货金额（总价，填此按数量算单价）">
            <el-input-number v-model="recvForm.received_amount" :precision="2" :controls="false" style="width:100%" @change="onRecvAmountCalc" />
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
        <!-- 🆕 #204 库位统一在收货时填(整批一个);填后同步采购明细/装配/钣金/设计/电工 -->
        <el-form-item label="库位（整批放到哪个库）" required>
          <el-select v-model="batchRecvForm.stock_location" filterable clearable placeholder="选择库位（库位管理里维护）" style="width:100%">
            <el-option v-for="l in enabledLocations" :key="l.id" :label="l.name" :value="l.name" />
          </el-select>
        </el-form-item>
        <!-- 🆕 #253 订单编号：整批补/改（手工采购单没填的）
             🆕 #376 改成**只填空行**，不再覆盖每行已有的编号 -->
        <el-form-item label="订单编号（只用来补下面没填编号的行，不会覆盖已有的）">
          <el-select v-model="batchRecvForm.project_code" filterable allow-create default-first-option clearable
                     placeholder="选项目编号，或直接输入订单编号" style="width:100%">
            <el-option v-for="p in projects" :key="p.id" :label="`${p.code}　${p.name}`" :value="p.code" />
          </el-select>
        </el-form-item>
        <!-- 🆕 #375/#376：一车拉来几个项目的料是常态。原来整批一个编号会把各行编号全抹平，
             另外几个项目的料就此从它们的物料需求里消失、成本也归不上去。 -->
        <el-alert v-if="batchRecvCodes.length > 1" type="success" :closable="false" show-icon style="margin-bottom:10px"
          :title="`这批料分属 ${batchRecvCodes.length} 个订单编号（${batchRecvCodes.join('、')}），会各自分派到对应项目，不会合并成一个`" />
        <div class="muted small" style="margin:2px 0 8px">逐行填单价/收货金额（单价可留空，货到再补；填单价自动按数量算收货金额，金额可再手动改）。订单编号可逐行改。</div>
      </el-form>
      <el-table show-overflow-tooltip :data="batchRecvLines" size="small" border max-height="34vh">
        <el-table-column label="名称" min-width="120">
          <template #default="{ row }">{{ row.item_name }}<span v-if="specOf(row.item_name, row.spec)" class="muted small"> · {{ specOf(row.item_name, row.spec) }}</span></template>
        </el-table-column>
        <el-table-column label="数量" width="70" align="right"><template #default="{ row }">{{ row.qty ?? '—' }}</template></el-table-column>
        <el-table-column label="单价" width="110" align="right">
          <template #default="{ row }"><el-input-number v-model="row.unit_price" :precision="2" :controls="false" style="width:96px" @change="onBatchLinePriceCalc(row)" /></template>
        </el-table-column>
        <el-table-column label="收货金额" width="120" align="right">
          <template #default="{ row }"><el-input-number v-model="row.received_amount" :precision="2" :controls="false" style="width:106px" /></template>
        </el-table-column>
        <!-- 🆕 #376 逐行订单编号：带出明细上原有的，可逐行改 -->
        <el-table-column label="订单编号" width="170">
          <template #default="{ row }">
            <el-select v-model="row.project_code" filterable allow-create default-first-option clearable
                       size="small" placeholder="按整批编号" style="width:100%">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code}　${p.name}`" :value="p.code" />
            </el-select>
          </template>
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
.low-box { margin: 10px 0; padding: 10px 14px; border-radius: 8px;
  background: var(--el-color-warning-light-9); border: 1px solid var(--el-color-warning-light-5); }
.low-head { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.low-arrow { transition: transform .15s; }
.low-arrow.open { transform: rotate(90deg); }

.bad { color: var(--danger); }
/* 🆕 出入库+物料需求合并：顶部登记按钮条 */
.io-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 12px; padding-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
/* 采购收货 待收货/已收货 切换：字体更大、按钮更大更醒目（用户要求）*/
.recv-toggle :deep(.el-radio-button__inner) {
  font-size: 18px; font-weight: 700; padding: 15px 34px; line-height: 1.2; min-width: 150px;
}
/* 🆕 #356 采购备注：对料要看的尺寸信息，标成可读的强调色，鼠标悬停出全文 */
.recv-note { color: var(--el-color-warning-dark-2, #b88230); }
/* 🆕 #141 tab 待办数红色角标 */
.wh-tab-badge { display: inline-block; margin-left: 6px; min-width: 16px; height: 16px; line-height: 16px;
  padding: 0 4px; border-radius: 8px; background: var(--el-color-danger); color: #fff; font-size: 11px;
  text-align: center; vertical-align: middle; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
/* ⚠️ `.danger` 本文件里一直没定义，模板却在用（如「驳回：xxx」那处），
   scoped 样式又不会去别的组件里找 —— 也就是说那段红字从来没红过。
   #401 的需求时间要靠它标过期，顺手把这两个补上。 */
.danger { color: var(--el-color-danger); }
.warn { color: var(--el-color-warning); }
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
