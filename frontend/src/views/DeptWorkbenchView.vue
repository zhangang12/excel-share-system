<script setup lang="ts">
// 🆕 v3 部门工作台（设计/电工/生产共用）：
// 工人视角 = 我的待办（卡片）/ 已完成（表格）
// 负责人/管理层视角 = 待分派（卡片）/ 任务跟踪（表格，含换人/作废/改回）
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Document, Download, Close, UploadFilled, Check, RefreshLeft, Switch as SwitchIcon, Lock,
  Promotion, CircleCheck, Delete, View,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import {
  ordersApi, produceApi, downloadAttachment, ORDER_STATUS_TEXT, ORDER_STATUS_TAG,
  type DeptOrder, type DeptOptions, type OrderAttachment, type GroupProjectRow, type DispatchOptions,
} from '@/api/orders'
import { datasheetsApi } from '@/api/datasheets'
import { http } from '@/api'
import SheetmetalGrid from '@/components/SheetmetalGrid.vue'
import FeedbackPanel from '@/components/FeedbackPanel.vue'
import AttachmentPackDialog from '@/components/AttachmentPackDialog.vue'
import AttachmentPreview from '@/components/AttachmentPreview.vue'
import StockQueryDialog from '@/components/StockQueryDialog.vue'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtDate } from '@/utils/format'
import { reportsApi, type DeptReport } from '@/api/reports'
import { salesApi } from '@/api/sales'   // 复用建议编号

// el-tag type → StatusPill variant 映射（ORDER_STATUS_TAG 用）
const PILL_VARIANT: Record<string, 'success' | 'warn' | 'info' | 'danger' | 'primary' | 'muted'> = {
  success: 'success', warning: 'warn', info: 'info', danger: 'danger', primary: 'primary',
}

const route = useRoute()
const auth = useAuthStore()

const dept = computed(() => (route.name as string) || 'design')

const WORKER_ROLES: Record<string, string> = { design: 'designer', electric: 'electrician', produce: 'assembler' }
const LEAD_ROLES: Record<string, string> = { design: 'design_lead', electric: 'electric_lead', produce: 'pm_lead' }

const isMgr = computed(() => auth.isAdmin)
const isLead = computed(() => auth.hasRole(LEAD_ROLES[dept.value]))
const isWorker = computed(() => auth.hasRole(WORKER_ROLES[dept.value]))
// 🆕 生产部分组（钣金组/装配组）；钣金组角色 sheetmetal、装配组角色 assembler
const isSheetmetal = computed(() => auth.hasRole('sheetmetal'))
const isAssembler = computed(() => auth.hasRole('assembler'))
const isProduce = computed(() => dept.value === 'produce')

// 🆕 备机下单：仅设计部工作台、且 设计部负责人/管理层 可见
const canSpare = computed(() => dept.value === 'design' && (isLead.value || auth.hasRole('admin', 'manager')))
const spareVisible = ref(false)
const spareSubmitting = ref(false)
const spareForm = ref({ code: '', name: '', qty: 1, unit: '台', depts: ['produce', 'electric'], req_text: '' })
const spareFiles = ref<File[]>([])   // 🆕 #5 备机下单的合同技术资料
async function openSpare() {
  let suggested = ''
  try { suggested = await salesApi.nextCode() } catch { /* 留空人工填 */ }
  spareForm.value = { code: suggested, name: '', qty: 1, unit: '台', depts: ['produce', 'electric'], req_text: '' }
  spareFiles.value = []
  spareVisible.value = true
}
function onPickSpareFiles(e: Event) {
  const fs = Array.from((e.target as HTMLInputElement).files || [])
  if (fs.length) spareFiles.value.push(...fs)
  ;(e.target as HTMLInputElement).value = ''
}
async function submitSpare() {
  const f = spareForm.value
  if (!f.code.trim()) { ElMessage.warning('请填写项目编号'); return }
  if (!f.name.trim()) { ElMessage.warning('请填写设备名称'); return }
  if (!f.depts.length) { ElMessage.warning('请至少选择一个派往部门'); return }
  spareSubmitting.value = true
  try {
    const r = await ordersApi.spareOrder({ ...f })
    // 🆕 #5 把合同技术资料随附给生成的各部门任务单（下发资料 order_input）
    if (spareFiles.value.length && r.order_ids?.length) {
      const files = spareFiles.value.slice()
      try { await Promise.all(r.order_ids.map(oid => ordersApi.inputUpload(oid, files))) }
      catch { ElMessage.warning('备机已下单，但部分技术资料上传失败，可到部门任务单补传') }
    }
    ElMessage.success(`备机下单成功（${r.code}），已派 ${r.order_ids.length} 个部门`)
    spareVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '下单失败')
  } finally {
    spareSubmitting.value = false
  }
}

const loading = ref(false)
const orders = ref<DeptOrder[]>([])
const options = ref<DeptOptions | null>(null)
const activeTab = ref('')
// 🆕 生产部两组项目列表
const sheetmetalRows = ref<GroupProjectRow[]>([])
const assemblyRows = ref<GroupProjectRow[]>([])
// 🆕 #1 钣金/装配组 人员筛选
const smWorkerFilter = ref('')
const asmWorkerFilter = ref('')
const smWorkers = computed(() => Array.from(new Set(sheetmetalRows.value.map(r => r.worker_name).filter((n): n is string => !!n))))
const asmWorkers = computed(() => Array.from(new Set(assemblyRows.value.map(r => r.worker_name).filter((n): n is string => !!n))))
const smRowsView = computed(() => smWorkerFilter.value ? sheetmetalRows.value.filter(r => (r.worker_name || '') === smWorkerFilter.value) : sheetmetalRows.value)
const asmRowsView = computed(() => asmWorkerFilter.value ? assemblyRows.value.filter(r => (r.worker_name || '') === asmWorkerFilter.value) : assemblyRows.value)

const curYear = String(new Date().getFullYear())
const yearFilter = ref(curYear)
const yearOptions = computed(() => {
  const y = parseInt(curYear); return [y - 1, y, y + 1].map(String)
})
const projStatusFilter = ref('进行中')
// 月份筛选（按接单/制图开始 start_date 月份）：'' = 全部月份，否则 '01'..'12'
const monthFilter = ref('')
const monthOptions = Array.from({ length: 12 }, (_, i) => {
  const v = String(i + 1).padStart(2, '0')
  return { v, l: `${i + 1}月` }
})
// 拼成 YYYY-MM 传给后端；未选月份返回 undefined
const ymParam = () => (monthFilter.value ? `${yearFilter.value}-${monthFilter.value}` : undefined)
// 负责人筛选（仅负责人/管理层可用）：undefined = 全部负责人
const workerFilter = ref<number | undefined>(undefined)

// ---- 数据加载 ----
async function load() {
  loading.value = true
  try {
    const ym = ymParam()
    if (isProduce.value) {
      // 生产部：主管/管理层走「派发/跟踪」+ 两组概览；钣金组/装配组仅取本组项目
      const tasks: Promise<any>[] = []
      if (isLead.value || isMgr.value) {
        tasks.push(ordersApi.list('produce', undefined, yearFilter.value, projStatusFilter.value, ym).then((os) => { orders.value = os }))
        tasks.push(ordersApi.options('produce').then((o) => { options.value = o }))
        tasks.push(produceApi.sheetmetalProjects(yearFilter.value, projStatusFilter.value).then((r) => { sheetmetalRows.value = r }))
        tasks.push(produceApi.assemblyProjects(yearFilter.value, projStatusFilter.value).then((r) => { assemblyRows.value = r }))
      } else {
        if (isSheetmetal.value) tasks.push(produceApi.sheetmetalProjects(yearFilter.value, projStatusFilter.value).then((r) => { sheetmetalRows.value = r }))
        if (isAssembler.value) tasks.push(produceApi.assemblyProjects(yearFilter.value, projStatusFilter.value).then((r) => { assemblyRows.value = r }))
      }
      await Promise.all(tasks)
      if (!activeTab.value) {
        activeTab.value = (isLead.value || isMgr.value) ? 'assign' : (isSheetmetal.value ? 'sm' : 'asm')
      }
      return
    }
    const [os, opt] = await Promise.all([
      ordersApi.list(dept.value, undefined, yearFilter.value, projStatusFilter.value, ym, workerFilter.value),
      ordersApi.options(dept.value),
    ])
    orders.value = os
    options.value = opt
    // 🆕 设计部：拉本人进行中任务的五表导入状态（卡片内「上传一个 Excel 导入五表」）
    if (dept.value === 'design') {
      for (const o of os) {
        if (o.status === 'in_progress') loadSheetStatus(o.project_id)
      }
    }
    // 负责人(含同时是工人的多角色)默认进「待分派」；纯工人进「待接单」
    if (!activeTab.value) activeTab.value = (isLead.value || isMgr.value) ? 'assign' : 'pending'
  } finally {
    loading.value = false
  }
}

// 🆕 生产派发（主管手动）：分别选钣金组、装配组各一名人员（两组都必选）
const dispatchVisible = ref(false)
const dispatchOrder = ref<DeptOrder | null>(null)
const dispatchOpts = ref<DispatchOptions>({ sheetmetal: [], assembly: [] })
const dispatchSmWid = ref<number | null>(null)
const dispatchAsmWid = ref<number | null>(null)
const dispatching = ref(false)
async function openDispatch(o: DeptOrder) {
  dispatchOrder.value = o
  dispatchSmWid.value = null
  dispatchAsmWid.value = null
  dispatchVisible.value = true
  try { dispatchOpts.value = await produceApi.dispatchOptions() } catch { /* 忽略 */ }
}
async function doDispatch() {
  const o = dispatchOrder.value
  if (!o) return
  if (!dispatchSmWid.value && !dispatchAsmWid.value) { ElMessage.warning('至少选择一组（钣金组或装配组）'); return }
  dispatching.value = true
  try {
    await produceApi.dispatch(o.id, dispatchSmWid.value, dispatchAsmWid.value)
    const label = [dispatchSmWid.value && '钣金组', dispatchAsmWid.value && '装配组'].filter(Boolean).join('、')
    ElMessage.success(`已派发到${label}`)
    dispatchVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '派发失败')
  } finally { dispatching.value = false }
}

// 🆕 设计部卡片内「上传一个 Excel 导入五表」+ 五表导入状态（与项目详情同一导入接口）
// 2026-06-24 起新增「激光件清单」（钣金装配拆出的采购信息），故为五张
const TEMPLATE_SHEETS = ['钣金装配', '标准件清单', '外协加工', '不锈钢原料下料单', '激光件清单']
const sheetStatus = ref<Record<number, { name: string; imported: boolean }[]>>({})
async function loadSheetStatus(pid: number) {
  try {
    const ds: any[] = await datasheetsApi.list(pid)
    sheetStatus.value[pid] = TEMPLATE_SHEETS.map((n) => ({
      name: n, imported: !!ds.find((d) => d.name === n)?.imported,
    }))
  } catch { /* 无权限/异常忽略，不阻塞卡片 */ }
}
function sheetsReady(pid: number) {
  const s = sheetStatus.value[pid]
  return !!s && s.length === TEMPLATE_SHEETS.length && s.every((x) => x.imported)
}
const importingPid = ref<number | null>(null)
function importTemplateTables(o: DeptOrder) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.xls,.xlsx'
  input.onchange = async () => {
    const f = (input.files || [])[0]
    if (!f) return
    try {
      await ElMessageBox.confirm(
        `导入「${f.name}」？将按各 sheet 一次性导入/重建本项目数据表（与项目详情「导入 Excel」一致）。`,
        '导入数据表', { type: 'warning', confirmButtonText: '导入', cancelButtonText: '取消' })
    } catch { return }
    const fd = new FormData(); fd.append('file', f)
    importingPid.value = o.project_id
    try {
      const r: any = await http.post(`/projects/${o.project_id}/import-excel`, fd)
      ElMessage.success(r.data?.message || '导入成功')
      await loadSheetStatus(o.project_id)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '导入失败')
    } finally { importingPid.value = null }
  }
  input.click()
}

// 🆕 组内标记完成（两组都完成→生产任务单 done）
async function toggleGroupDone(row: GroupProjectRow) {
  try {
    await produceApi.groupDone(row.task_id, !row.group_done)
    ElMessage.success(!row.group_done ? '已标记本组完成' : '已撤销完成')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}
// 🆕 本组各自设置「预计完成」(填后锁定，仅管理层可改)
async function setGroupDue(row: GroupProjectRow, val: string | null) {
  if (!val) return
  try {
    await produceApi.setGroupDue(row.task_id, val)
    ElMessage.success('已设置本组预计完成')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '设置失败')
    await load()   // 失败回滚显示
  }
}
// 🆕 任务跟踪父视图：取生产单某组(钣金/装配)的预计完成 / 完成日期
function pgDue(row: DeptOrder, group: string): string {
  const b = (row.produce_groups || []).find(g => g.group === group)
  return b && b.due_date ? fmtDate(b.due_date) : '—'
}
function pgDone(row: DeptOrder, group: string): string {
  const b = (row.produce_groups || []).find(g => g.group === group)
  return b && b.done_date ? fmtDate(b.done_date) : '—'
}

// 🆕 #9 任务跟踪：资料预览 + 打包下载（复用 AttachmentPackDialog）
const packVisible = ref(false)
const packTitle = ref('')
const packZipname = ref('资料')
const packGroups = ref<{ label: string; items: { id: number; name: string }[] }[]>([])
function openPack(row: DeptOrder) {
  packTitle.value = `${row.project_code} 资料`
  packZipname.value = `${row.project_code}_资料`
  packGroups.value = [
    { label: '合同技术资料', items: (row.input_files || []).map(f => ({ id: f.id, name: f.name })) },
    { label: '上传资料', items: [...(row.start_files || []), ...(row.output_files || [])].map(f => ({ id: f.id, name: f.name })) },
  ]
  packVisible.value = true
}

// 🆕 #6 电工部：标准件清单只读引用（复用 SheetmetalGrid canEdit=false 读任意数据表）
const stdVisible = ref(false)
const stdRow = ref<DeptOrder | null>(null)
const stdTitle = ref('')
function openStdSheet(o: DeptOrder) {
  stdRow.value = o
  stdTitle.value = `${o.project_code} · 标准件清单（只读）`
  stdVisible.value = true
}

// 🆕 设计完成第一步（设计完成按钮）
const markingDesignDone = ref<number | null>(null)
function canDesignDone(o: DeptOrder) {
  // 🆕 #4 放开文件必传：图纸/外购图/五表均改为可选，进行中即可点设计完成
  return o.status === 'in_progress'
}
async function doDesignDone(o: DeptOrder) {
  markingDesignDone.value = o.id
  try {
    const r: any = await ordersApi.markDesignDone(o.id)
    ElMessage.success(r?.message || '设计完成，已计入考核')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    markingDesignDone.value = null
  }
}

// 🆕 电工部两步完成流
const markingElectricDone = ref<number | null>(null)
function canElectricDone(o: DeptOrder) {
  return o.status === 'in_progress' && !o.electric_done_flag  // 🆕 #4 采购清单可选
}
function canElectricShipReady(o: DeptOrder) {
  return o.electric_done_flag  // 🆕 #4 电路图可选，接线完成后即可完成
}
async function doElectricDone(o: DeptOrder) {
  markingElectricDone.value = o.id
  try {
    await ordersApi.markElectricDone(o.id)
    ElMessage.success('已标记接线完成，请上传电路图')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    markingElectricDone.value = null
  }
}
function cardOutputFiles(o: DeptOrder, kind: string) {
  return o.output_files.filter(f => f.kind === kind)
}
async function pickCardOutputUpload(o: DeptOrder, kind: string) {
  const input = document.createElement('input')
  input.type = 'file'
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    await ordersApi.outputUpload(o.id, kind, files)
    ElMessage.success('已上传')
    await load()
  }
  input.click()
}
// 🆕 #5 替换/删除已上传产物（删后可重新上传替换）；已完成的设计单产物也可替换
async function removeCardOutput(o: DeptOrder, fileId: number) {
  try { await ElMessageBox.confirm('删除该文件？删除后可重新上传替换。', '替换 / 删除', { type: 'warning' }) } catch { return }
  try {
    await ordersApi.removeAttachment(o.id, fileId)
    ElMessage.success('已删除，可重新上传')
    await load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}
// 🆕 #5 资料/产物管理弹窗（已完成单）：
//   shipprep=发货准备(说明书/铭牌, 产物 order_output) / design=设计资料更换(CAD激光图纸/外购附图, 接单产物 order_start_output)
type ResMode = 'shipprep' | 'design'
const resVisible = ref(false)
const resId = ref<number | null>(null)
const resMode = ref<ResMode>('shipprep')
const resOrder = computed(() => orders.value.find(o => o.id === resId.value) || null)
const resTitle = computed(() => resMode.value === 'shipprep' ? '📦 发货准备' : '🔧 设计资料更换')
const resSections = computed(() => resMode.value === 'shipprep'
  ? [{ k: 'manual', label: '产品说明书 (Word)', btn: '上传说明书' }, { k: 'nameplate', label: '铭牌 (CAD)', btn: '上传铭牌' }]
  : [{ k: 'sheetpkg', label: 'CAD激光图纸', btn: '上传CAD激光图纸' }, { k: 'outsource_img', label: '外购附图', btn: '上传外购附图' }])
function openRes(o: DeptOrder, mode: ResMode) { resId.value = o.id; resMode.value = mode; resVisible.value = true }
function resFiles(o: DeptOrder, kind: string) {
  return (resMode.value === 'design' ? o.start_files : o.output_files).filter(f => f.kind === kind)
}
async function resPick(o: DeptOrder, kind: string) {
  const input = document.createElement('input'); input.type = 'file'
  input.onchange = async () => {
    const files = Array.from(input.files || []); if (!files.length) return
    try {
      if (resMode.value === 'design') await ordersApi.startUpload(o.id, kind, files)
      else await ordersApi.outputUpload(o.id, kind, files)
      ElMessage.success('已上传'); await load()
    } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '上传失败') }
  }
  input.click()
}
async function doShipPrepDone(o: DeptOrder | null) {
  if (!o) return
  try {
    const r: any = await ordersApi.shipPrepDone(o.id)
    ElMessage.success(r?.message || '已标记发货准备完成')
    await load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '操作失败') }
}
// 🆕 #5 打包下载：复用已有的 openPack(row) / packVisible / AttachmentPackDialog（含合同技术资料 + 上传资料）

// 🆕 #4 合同技术资料在线预览（图片/PDF/Excel/Word；doc/dwg 自动下载兜底）
const previewRef = ref<InstanceType<typeof AttachmentPreview>>()
function preview(f: OrderAttachment) { previewRef.value?.open(f) }

// 🆕 #1 对合同技术资料提修订意见 → 推送对应销售员
const revVisible = ref(false)
const revOrder = ref<DeptOrder | null>(null)
const revReason = ref('')
const revSubmitting = ref(false)
function openRevision(o: DeptOrder) { revOrder.value = o; revReason.value = ''; revVisible.value = true }
async function submitRevision() {
  if (!revOrder.value) return
  if (!revReason.value.trim()) { ElMessage.warning('请填写修订意见'); return }
  revSubmitting.value = true
  try {
    const r: any = await ordersApi.revisionRequest(revOrder.value.id, revReason.value.trim())
    ElMessage.success(r?.message || '修订意见已提交')
    revVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  } finally { revSubmitting.value = false }
}

// 🆕 钣金装配表可编辑预览（钣金组/装配组共用）
const viewVisible = ref(false)
const viewTitle = ref('')
const viewRow = ref<GroupProjectRow | null>(null)
const canEditSheet = computed(() => isSheetmetal.value || isAssembler.value || isLead.value || isMgr.value)
function viewSheet(row: GroupProjectRow) {
  if (!row.sheetmetal_datasheet_id) { ElMessage.info('该项目暂无钣金装配表'); return }
  viewTitle.value = `${row.code} · 钣金装配表`
  viewRow.value = row
  viewVisible.value = true
}
watch(dept, () => { activeTab.value = ''; load() })
onMounted(load)

// ---- 工作台搜索 ----
const searchQuery = ref('')
function matchSearch(o: DeptOrder) {
  if (!searchQuery.value) return true
  const q = searchQuery.value.toLowerCase()
  return (o.project_code || '').toLowerCase().includes(q) ||
         (o.project_name || '').toLowerCase().includes(q)
}
// ---- 工人视角数据（只显示派给自己的订单，兼容多角色账号） ----
const myUid = computed(() => auth.user?.id)
function isMyOrder(o: DeptOrder) { return o.worker_id === myUid.value }
const myPending  = computed(() => orders.value.filter(o => o.status === 'assigned'    && isMyOrder(o) && matchSearch(o)))
const myWorking  = computed(() => orders.value.filter(o => o.status === 'in_progress' && isMyOrder(o) && matchSearch(o)))
const myDone     = computed(() => orders.value.filter(o => o.status === 'done'        && isMyOrder(o) && matchSearch(o)))
// ---- 负责人视角数据 ----
const pendingAssign = computed(() => orders.value.filter(o => o.status === 'pending_assign' && matchSearch(o)))

// ---- 接单（填时间开始） ----
const startDates = ref<Record<number, { start: string; due: string }>>({})
function dateOf(o: DeptOrder) {
  if (!startDates.value[o.id]) {
    startDates.value[o.id] = { start: new Date().toISOString().slice(0, 10), due: '' }
  }
  return startDates.value[o.id]
}
async function doStart(o: DeptOrder) {
  const d = dateOf(o)
  if (!d.start || !d.due) { ElMessage.warning('请填写开始与预计完成日期'); return }
  await ordersApi.start(o.id, d.start, d.due)
  ElMessage.success('已开始' + (dept.value !== 'produce' ? '，已回传项目目录' : ''))
  await load()
}

// ---- 接单后上传（图纸包/采购清单） ----
async function pickStartUpload(o: DeptOrder, kind: string) {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    await ordersApi.startUpload(o.id, kind, files)
    ElMessage.success('已上传并推送下游')
    await load()
  }
  input.click()
}
async function removeAtt(o: DeptOrder, att: OrderAttachment) {
  // #68 移除已上传资料（可能已推送下游）为破坏性操作，二次确认
  try {
    await ElMessageBox.confirm(
      `移除已上传的「${att.name}」？该资料可能已推送下游部门。`, '移除文件', { type: 'warning' })
  } catch { return }
  await ordersApi.removeAttachment(o.id, att.id)
  ElMessage.success('已移除')
  await load()
}
function startFilesOf(o: DeptOrder, kind: string) {
  return o.start_files.filter(f => f.kind === kind)
}

// ---- 完成弹窗 ----
const completeVisible = ref(false)
const completeOrder = ref<DeptOrder | null>(null)
const notifyUserId = ref<number | null>(null)
const completing = ref(false)
function openComplete(o: DeptOrder) {
  completeOrder.value = o
  notifyUserId.value = null
  completeVisible.value = true
}
function outputFilesOf(kind: string) {
  return completeOrder.value?.output_files.filter(f => f.kind === kind) || []
}
async function pickOutputUpload(kind: string) {
  const o = completeOrder.value
  if (!o) return
  const input = document.createElement('input')
  input.type = 'file'
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    await ordersApi.outputUpload(o.id, kind, files)
    const fresh = await ordersApi.list(dept.value)
    orders.value = fresh
    completeOrder.value = fresh.find(x => x.id === o.id) || o
    ElMessage.success('产物已上传')
  }
  input.click()
}
async function doComplete() {
  const o = completeOrder.value
  if (!o) return
  if (!notifyUserId.value) { ElMessage.warning(options.value?.notify_label || '请选择通知人'); return }
  // 🆕 #4 已放开必传产物校验：产物按需可选，不再拦截完成
  completing.value = true
  try {
    const r: any = await ordersApi.complete(o.id, notifyUserId.value)
    ElMessage.success(r.message || '已完成')
    completeVisible.value = false
    await load()
  } finally {
    completing.value = false
  }
}

// ---- 负责人：分派 / 作废 / 换人 / 改回 ----
const assignSel = ref<Record<number, number | null>>({})
const assigning = ref<number | null>(null)
async function doAssign(o: DeptOrder) {
  const wid = assignSel.value[o.id]
  if (!wid) { ElMessage.warning('请选择分派对象'); return }
  assigning.value = o.id
  try {
    const r: any = await ordersApi.assign(o.id, wid)
    ElMessage.success(r.message || '已分派')
    await load()
  } finally { assigning.value = null }
}
async function doVoid(o: DeptOrder) {
  await ElMessageBox.confirm(`确认作废 ${o.project_code} 的任务单？作废后留痕，管理层将收到通知。`, '作废单号', { type: 'warning' })
  await ordersApi.void(o.id)
  ElMessage.success('已作废并通知管理层')
  await load()
}
async function doReopen(o: DeptOrder) {
  await ElMessageBox.confirm('改回进行中将清除完成日期（已推送下游的资料联动撤回）', '改回进行中', { type: 'warning' })
  await ordersApi.reopen(o.id)
  ElMessage.success('已改回进行中')
  await load()
}
async function doDelete(o: DeptOrder) {
  await ElMessageBox.confirm(
    `确认彻底删除 ${o.project_code} 的任务单？将一并删除其上传附件，且不可恢复（区别于「作废」留痕）。`,
    '删除任务单', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
  await ordersApi.del(o.id)
  ElMessage.success('已删除任务单')
  await load()
}

const reassignVisible = ref(false)
const reassignOrder = ref<DeptOrder | null>(null)
const reassignWid = ref<number | null>(null)
function openReassign(o: DeptOrder) {
  reassignOrder.value = o
  reassignWid.value = null
  reassignVisible.value = true
}

// 🆕 管理层改预计完成时间（设计/电工任务跟踪表内直接改）
async function doEditDue(o: DeptOrder, due: string) {
  if (!due || due === o.due_date) return
  try {
    await ordersApi.editDue(o.id, due)
    ElMessage.success('已更新预计完成时间')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '更新失败')
  }
}
const reassigning = ref(false)
async function doReassign() {
  const o = reassignOrder.value
  if (!o || !reassignWid.value) { ElMessage.warning('请选择转交对象'); return }
  reassigning.value = true
  try {
    const r: any = await ordersApi.reassign(o.id, reassignWid.value)
    ElMessage.success(r.message || '已转交')
    reassignVisible.value = false
    await load()
  } finally { reassigning.value = false }
}
const reassignCandidates = computed(() =>
  (options.value?.workers || []).filter(w => w.id !== reassignOrder.value?.worker_id))

// ---- 展示工具 ----
function effClass(o: DeptOrder) {
  if (o.eff_pct == null) return ''
  return o.eff_pct >= 100 ? 'eff-good' : 'eff-bad'
}
const deptName = computed(() => options.value?.dept_name || '')

// 🆕 M14 部门报表（负责人/管理层）
const reportVisible = ref(false)
const report = ref<DeptReport | null>(null)
const reportMonth = ref('')  // YYYY-MM，空=全年
async function openReport() {
  report.value = await reportsApi.dept(dept.value, yearFilter.value, reportMonth.value || undefined)
  reportVisible.value = true
}

// 🆕 M07 设计部「查库存」只读
const stockVisible = ref(false)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>{{ deptName }}工作台</h1>
        <div class="desc">
          销售/管理层下单 → 负责人分派 → {{ isWorker ? '接单填时间 → 完成（' + (options?.sheet_check ? '五表校验/' : '') + '通知/产物）→ 推送下游' : '跟踪进度与完成效率' }}
        </div>
      </div>
      <div class="spacer"></div>
      <el-input v-model="searchQuery" placeholder="搜索项目编号/名称" clearable size="large" style="width:200px" />
      <el-select v-model="yearFilter" size="large" style="width:100px" @change="load">
        <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
      </el-select>
      <el-select v-model="monthFilter" size="large" style="width:110px" clearable placeholder="全部月份" @change="load">
        <el-option v-for="m in monthOptions" :key="m.v" :label="m.l" :value="m.v" />
      </el-select>
      <el-select v-model="projStatusFilter" size="large" style="width:100px" @change="load">
        <el-option label="进行中" value="进行中" />
        <el-option label="已完成" value="已完成" />
        <el-option label="全部" value="" />
      </el-select>
      <el-select v-if="(isLead || isMgr) && !isProduce" v-model="workerFilter" size="large"
                 style="width:140px" clearable filterable placeholder="全部负责人" @change="load">
        <el-option v-for="w in options?.workers || []" :key="w.id" :label="w.name" :value="w.id" />
      </el-select>
      <el-button v-if="canSpare" type="primary" @click="openSpare">➕ 备机下单</el-button>
      <el-button v-if="dept === 'design'" @click="stockVisible = true">🔎 查库存(只读)</el-button>
      <el-button v-if="isLead || isMgr" type="primary" plain @click="openReport">📊 {{ deptName }}报表</el-button>
    </div>

    <!-- ===== 🆕 备机下单（设计部负责人/管理层）：建项目+派各部门，不建销售台账 ===== -->
    <el-dialog v-model="spareVisible" title="➕ 备机下单" width="560px" :close-on-click-modal="false">
      <el-alert type="info" :closable="false" style="margin-bottom: 12px"
                title="备机不走销售台账；下单后建项目并推送所选部门待分派，同样进入项目目录/详单。" />
      <el-form label-position="top">
        <div style="display:flex; gap:12px">
          <el-form-item label="项目编号" required style="flex:1">
            <el-input v-model="spareForm.code" placeholder="如 2026-058B" maxlength="64" clearable />
          </el-form-item>
          <el-form-item label="设备名称" required style="flex:1">
            <el-input v-model="spareForm.name" placeholder="如 100L真空乳化机(备机)" />
          </el-form-item>
        </div>
        <div style="display:flex; gap:12px">
          <el-form-item label="数量" style="flex:0 0 160px">
            <el-input-number v-model="spareForm.qty" :min="1" :controls="false" style="width:80px" />
            <el-select v-model="spareForm.unit" style="width:64px; margin-left:6px">
              <el-option label="台" value="台" /><el-option label="套" value="套" />
            </el-select>
          </el-form-item>
          <el-form-item label="派往部门" required style="flex:1">
            <el-checkbox-group v-model="spareForm.depts">
              <el-checkbox value="design">📐 设计部</el-checkbox>
              <el-checkbox value="electric">⚡ 电工部</el-checkbox>
              <el-checkbox value="produce">🏭 生产部</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
        </div>
        <el-form-item label="下单要求">
          <el-input v-model="spareForm.req_text" type="textarea" :rows="2" placeholder="技术要求/交底说明（选填）" />
        </el-form-item>
        <!-- 🆕 #5 合同技术资料：随附给各部门任务单（下发资料） -->
        <el-form-item label="合同技术资料（选填，随附各部门任务）">
          <input type="file" multiple @change="onPickSpareFiles" style="display:block" />
          <div v-if="spareFiles.length" class="tc-files" style="margin-top:6px">
            <el-tag v-for="(f, i) in spareFiles" :key="i" size="small" effect="plain" closable
                    @close="spareFiles.splice(i, 1)">{{ f.name }}</el-tag>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="spareVisible = false">取消</el-button>
        <el-button type="primary" :loading="spareSubmitting" @click="submitSpare">确认下单</el-button>
      </template>
    </el-dialog>

    <!-- ===== 部门工作台：负责人(待分派/跟踪) + 工人(待办/已完成) 并存；多角色用户全部显示 ===== -->
    <template v-if="isWorker || isLead || isMgr || isSheetmetal">
      <el-tabs v-model="activeTab">
        <!-- ===== 待接单 tab ===== -->
        <el-tab-pane v-if="isWorker && !isProduce" :label="`📩 我的订单（待接单 ${myPending.length}）`" name="pending">
          <EmptyHint v-if="!loading && myPending.length === 0" text="暂无待接单任务" />
          <div v-else class="todo-grid" v-loading="loading">
            <el-card v-for="o in myPending" :key="o.id" shadow="hover"
                     class="todo-card" :class="{ urgent: o.overdue }">
              <div class="tc-head work-card-head">
                <span class="tc-code">{{ o.project_code }}</span>
                <StatusPill :text="ORDER_STATUS_TEXT[o.status]" :variant="PILL_VARIANT[ORDER_STATUS_TAG[o.status]] || 'muted'" />
                <StatusPill v-if="o.overdue" text="已超预计" variant="danger" />
              </div>
              <div class="tc-name">{{ o.project_name }}</div>
              <div v-if="o.req_text" class="tc-req">📌 {{ o.req_text }}</div>
              <div v-if="o.input_files.length" class="tc-files">
                <el-tag v-for="f in o.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" style="cursor:pointer" @click="preview(f)">
                  <el-icon><View /></el-icon>{{ f.name }}<el-icon class="dl" @click.stop="downloadAttachment(f)"><Download /></el-icon>
                </el-tag>
              </div>
              <div class="tc-dates">
                <div class="fd">
                  <label>{{ options?.start_label }}</label>
                  <el-date-picker v-model="dateOf(o).start" type="date" value-format="YYYY-MM-DD" size="small" style="width: 100%" />
                </div>
                <div class="fd">
                  <label>{{ options?.end_label }}</label>
                  <el-date-picker v-model="dateOf(o).due" type="date" value-format="YYYY-MM-DD" size="small" style="width: 100%" />
                </div>
              </div>
              <div class="tc-hint">时间一经填写本人不可改（仅管理层可改）</div>
              <el-button type="primary" size="small" @click="doStart(o)">开始</el-button>
            </el-card>
          </div>
        </el-tab-pane>

        <!-- ===== 进行中 tab ===== -->
        <el-tab-pane v-if="isWorker && !isProduce" :label="`⚙️ 我的订单（进行中 ${myWorking.length}）`" name="working">
          <EmptyHint v-if="!loading && myWorking.length === 0" text="暂无进行中任务" />
          <div v-else class="todo-grid" v-loading="loading">
            <el-card v-for="o in myWorking" :key="o.id" shadow="hover"
                     class="todo-card" :class="{ urgent: o.overdue }">
              <div class="tc-head work-card-head">
                <span class="tc-code">{{ o.project_code }}</span>
                <StatusPill :text="ORDER_STATUS_TEXT[o.status]" :variant="PILL_VARIANT[ORDER_STATUS_TAG[o.status]] || 'muted'" />
                <StatusPill v-if="o.overdue" text="已超预计" variant="danger" />
              </div>
              <div class="tc-name">{{ o.project_name }}</div>
              <div v-if="o.req_text" class="tc-req">📌 {{ o.req_text }}</div>

              <div v-if="o.input_files.length" class="tc-files">
                <el-tag v-for="f in o.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" style="cursor:pointer" @click="preview(f)">
                  <el-icon><View /></el-icon>{{ f.name }}<el-icon class="dl" @click.stop="downloadAttachment(f)"><Download /></el-icon>
                </el-tag>
              </div>
              <!-- 🆕 #1 对合同技术资料提修订意见（设计/电工，进行中）→ 推送对应销售 -->
              <div v-if="(dept === 'design' || dept === 'electric') && o.status === 'in_progress' && o.input_files.length" style="margin:4px 0">
                <el-button size="small" link type="warning" @click="openRevision(o)">✏️ 对技术资料提修订意见</el-button>
              </div>

              <!-- 🆕 #6 电工部：标准件清单只读引用（待接单/进行中卡片均显示） -->
              <div v-if="dept === 'electric' && o.standard_datasheet_id" style="margin:6px 0">
                <el-button size="small" plain :icon="Document" @click="openStdSheet(o)">查看标准件清单（只读）</el-button>
              </div>

              <!-- 进行中：起始上传 + 完成（v-if 让 Vue 编译为片段，避免裸 template 被当作惰性 DOM <template> 元素而不渲染） -->
              <template v-if="o.status === 'in_progress'">
                <div class="tc-kv">
                  {{ options?.start_label }}：<b>{{ fmtDate(o.start_date) }}</b>
                  ｜ {{ options?.end_label }}：<b>{{ fmtDate(o.due_date) }}</b>
                </div>

                <div v-for="so in options?.start_outputs || []" :key="so.k" class="up-sec">
                  <div class="up-h">
                    <el-icon><UploadFilled /></el-icon> {{ so.label }}
                    <span style="margin-left: auto">
                      <StatusPill
                        :text="startFilesOf(o, so.k).length ? `已推送 ${startFilesOf(o, so.k).length} 个` : '待上传 → 推送下游'"
                        :variant="startFilesOf(o, so.k).length ? 'success' : 'warn'" />
                    </span>
                  </div>
                  <div class="up-b">
                    <div v-if="startFilesOf(o, so.k).length" class="tc-files">
                      <el-tag v-for="f in startFilesOf(o, so.k)" :key="f.id" size="small" effect="plain" class="file-chip">
                        <span @click="downloadAttachment(f)" style="cursor: pointer">{{ f.name }}</span>
                        <el-icon class="rm" @click="removeAtt(o, f)"><Close /></el-icon>
                      </el-tag>
                    </div>
                    <el-button size="small" plain type="primary" :icon="UploadFilled" @click="pickStartUpload(o, so.k)">
                      {{ startFilesOf(o, so.k).length ? '继续添加' : '上传' }}{{ so.label }}
                    </el-button>
                  </div>
                </div>

                <!-- 🆕 设计部：五个数据表导入（卡片内上传一个 Excel 一次性导入五表，与项目详情同口径） -->
                <div v-if="options?.sheet_check" class="up-sec">
                  <div class="up-h">
                    📋 五个数据表导入
                    <span style="margin-left:auto">
                      <StatusPill :text="sheetsReady(o.project_id) ? '已齐全' : '未齐全'"
                                  :variant="sheetsReady(o.project_id) ? 'success' : 'warn'" />
                    </span>
                  </div>
                  <div class="up-b">
                    <div class="four-chips">
                      <el-tag v-for="s in sheetStatus[o.project_id] || []" :key="s.name" size="small"
                              :type="s.imported ? 'success' : 'info'" effect="plain">
                        {{ s.name }} {{ s.imported ? '✅' : '⬜' }}
                      </el-tag>
                    </div>
                    <el-button size="small" plain type="primary" :icon="UploadFilled"
                               :loading="importingPid === o.project_id" @click="importTemplateTables(o)">
                      上传一个 Excel 导入五表
                    </el-button>
                    <div class="tc-hint">上传含「钣金装配/标准件清单/外协加工/不锈钢原料下料单/激光件清单」五个 sheet 的 Excel，一次性导入；完成前五表须齐全。</div>
                  </div>
                </div>

                <!-- 设计部：点「设计完成」即完成并计考核；说明书/铭牌为发货准备，选填、不影响考核 -->
                <template v-if="dept === 'design'">
                  <!-- 🆕 #5 说明书/铭牌不再在进行中上传，移到「已完成」tab 的发货准备里 -->
                  <el-button type="primary" size="small" :icon="Check"
                             :disabled="!canDesignDone(o)" :loading="markingDesignDone === o.id"
                             @click="doDesignDone(o)">设计完成</el-button>
                  <div class="tc-hint">{{ canDesignDone(o) ? '设计完成后到「已完成」tab 做发货准备（传说明书/铭牌）与资料更换' : '需上传 CAD激光图纸、外购附图并完成五表导入即可完成' }}</div>
                </template>
                <!-- 电工部两步完成流 -->
                <template v-else-if="dept === 'electric'">
                  <!-- 第一步：接线完成（采购清单已上传） -->
                  <template v-if="!o.electric_done_flag">
                    <el-button type="primary" size="small" :icon="Check"
                               :disabled="!canElectricDone(o)"
                               :loading="markingElectricDone === o.id"
                               @click="doElectricDone(o)">接线完成</el-button>
                    <div v-if="!canElectricDone(o)" class="tc-hint">需上传采购清单</div>
                  </template>
                  <!-- 第二步：上传电路图 + 发货准备 -->
                  <template v-else>
                    <el-tag type="success" size="small" style="margin-bottom:8px">✅ 接线已完成</el-tag>
                    <div class="up-sec">
                      <div class="up-h">
                        <el-icon><UploadFilled /></el-icon> 电路图 (PDF)
                        <span style="margin-left:auto">
                          <StatusPill :text="cardOutputFiles(o,'circuit').length ? `已上传 ${cardOutputFiles(o,'circuit').length} 个` : '待上传'"
                                      :variant="cardOutputFiles(o,'circuit').length ? 'success' : 'warn'" />
                        </span>
                      </div>
                      <div class="up-b">
                        <div v-if="cardOutputFiles(o,'circuit').length" class="tc-files">
                          <el-tag v-for="f in cardOutputFiles(o,'circuit')" :key="f.id" size="small" effect="plain" class="file-chip">
                            <span @click="downloadAttachment(f)" style="cursor:pointer">{{ f.name }}</span>
                          </el-tag>
                        </div>
                        <el-button size="small" plain type="primary" :icon="UploadFilled" @click="pickCardOutputUpload(o,'circuit')">
                          {{ cardOutputFiles(o,'circuit').length ? '继续添加' : '上传电路图' }}
                        </el-button>
                      </div>
                    </div>
                    <el-button type="success" size="small" :icon="Check"
                               :disabled="!canElectricShipReady(o)"
                               @click="openComplete(o)">发货准备完成</el-button>
                    <div v-if="!canElectricShipReady(o)" class="tc-hint">需上传电路图</div>
                  </template>
                </template>
                <!-- 其他部门：原完成按钮 -->
                <el-button v-else type="success" size="small" :icon="Check" @click="openComplete(o)">完成…</el-button>
              </template>
            </el-card>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="isWorker && !isProduce" :label="`✅ 我的订单（已完成 ${myDone.length}）`" name="done">
          <el-table :data="myDone" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" min-width="112">
              <template #default="{ row }"><b>{{ row.project_code }}</b></template>
            </el-table-column>
            <el-table-column prop="project_name" label="项目名称" min-width="180" show-overflow-tooltip />
            <el-table-column prop="start_date" :label="options?.start_label" min-width="116">
              <template #default="{ row }">{{ fmtDate(row.start_date) }}</template>
            </el-table-column>
            <el-table-column prop="due_date" :label="options?.end_label" min-width="116">
              <template #default="{ row }">{{ fmtDate(row.due_date) }}</template>
            </el-table-column>
            <el-table-column prop="done_date" label="完成" min-width="116">
              <template #default="{ row }">{{ fmtDate(row.done_date) }}</template>
            </el-table-column>
            <el-table-column label="完成效率" min-width="100">
              <template #default="{ row }">
                <span v-if="row.eff_pct != null" :class="effClass(row)">{{ row.eff_pct }}%</span>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="上传资料/产物" min-width="150" align="center">
              <template #default="{ row }">
                <span class="muted">共 {{ row.start_files.length + row.output_files.length }} 个</span>
                <el-button size="small" link type="primary" :icon="Download" style="margin-left:8px"
                           :disabled="!(row.start_files.length + row.output_files.length)"
                           @click="openPack(row)">打包下载</el-button>
              </template>
            </el-table-column>
            <el-table-column v-if="dept === 'electric'" label="标准件清单" min-width="116" align="center">
              <template #default="{ row }">
                <el-button v-if="row.standard_datasheet_id" size="small" link type="primary" :icon="Document" @click="openStdSheet(row)">查看</el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="通知" min-width="110">
              <template #default="{ row }">{{ row.notify_user_name ? '📲 ' + row.notify_user_name : '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" :width="dept === 'design' ? 300 : 130" fixed="right">
              <template #default="{ row }">
                <template v-if="dept === 'design'">
                  <el-button size="small" type="success" plain @click="openRes(row, 'shipprep')">
                    发货准备{{ row.ship_prep_done ? ' ✓' : '' }}
                  </el-button>
                  <el-button size="small" type="warning" plain @click="openRes(row, 'design')">设计资料更换</el-button>
                </template>
                <el-button size="small" :icon="RefreshLeft" @click="doReopen(row)">改回进行中</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!loading && !myDone.length" text="还没有已完成的任务" size="sm" />
        </el-tab-pane>
        <!-- ===== 负责人 / 管理层：待分派 + 任务跟踪 ===== -->
        <el-tab-pane v-if="isLead || isMgr" :label="`📥 待分派 (${pendingAssign.length})`" name="assign">
          <EmptyHint v-if="!loading && pendingAssign.length === 0" text="暂无待分派任务" />
          <div v-else class="todo-grid" v-loading="loading">
            <el-card v-for="o in pendingAssign" :key="o.id" shadow="hover" class="todo-card assign">
              <div class="tc-head work-card-head">
                <span class="tc-code">{{ o.project_code }}</span>
                <StatusPill text="待分派" variant="info" />
              </div>
              <div class="tc-name">{{ o.project_name }}</div>
              <div v-if="o.req_text" class="tc-req">📌 {{ o.req_text }}</div>
              <div v-if="o.input_files.length" class="tc-files">
                <el-tag v-for="f in o.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" style="cursor:pointer" @click="preview(f)">{{ f.name }}</el-tag>
              </div>
              <!-- 🆕 生产部：派发到钣金组+装配组（取代单人分派） -->
              <div v-if="isProduce" class="assign-bar">
                <el-button type="primary" size="small" :icon="Promotion" @click="openDispatch(o)">派发钣金/装配</el-button>
                <el-button size="small" @click="doVoid(o)">作废单号</el-button>
              </div>
              <div v-else class="assign-bar">
                <el-select v-model="assignSel[o.id]" placeholder="分派给…" size="small" style="flex: 1">
                  <el-option v-for="w in options?.workers || []" :key="w.id" :label="w.name" :value="w.id" />
                </el-select>
                <el-button type="primary" size="small" :icon="Check" :loading="assigning === o.id" @click="doAssign(o)">分派</el-button>
                <el-button size="small" @click="doVoid(o)">作废单号</el-button>
              </div>
            </el-card>
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="isLead || isMgr" label="📋 任务跟踪" name="track">
          <el-table :data="orders.filter(matchSearch)" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" min-width="112">
              <template #default="{ row }"><b>{{ row.project_code }}</b></template>
            </el-table-column>
            <el-table-column prop="project_name" label="项目名称" min-width="180" show-overflow-tooltip />
            <el-table-column :label="isProduce ? '派发' : '负责人'" min-width="110">
              <template #default="{ row }">
                <template v-if="isProduce">
                  {{ row.status === 'pending_assign' ? '待派发' : (row.status === 'done' ? '钣金/装配已完成' : '已派发钣金/装配') }}
                </template>
                <template v-else>{{ row.worker_name || '待分派' }}</template>
              </template>
            </el-table-column>
            <el-table-column label="合同技术资料" min-width="130" align="center">
              <template #default="{ row }">
                <el-button v-if="row.input_files.length" size="small" link type="primary" :icon="View"
                           @click="row.input_files.length === 1 ? preview(row.input_files[0]) : openPack(row)">
                  预览{{ row.input_files.length > 1 ? '(' + row.input_files.length + ')' : '' }}
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="上传资料" min-width="110" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.start_files.length + row.output_files.length" size="small" type="success" effect="light" round>已收到 {{ row.start_files.length + row.output_files.length }}</el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="100" align="center">
              <template #default="{ row }">
                <StatusPill :text="ORDER_STATUS_TEXT[row.status]" :variant="PILL_VARIANT[ORDER_STATUS_TAG[row.status]] || 'muted'" />
              </template>
            </el-table-column>
            <el-table-column prop="start_date" :label="options?.start_label" min-width="118">
              <template #default="{ row }">{{ row.start_date ? fmtDate(row.start_date) : '—' }}</template>
            </el-table-column>
            <el-table-column :label="options?.end_label" :min-width="isProduce ? 150 : 132">
              <template #default="{ row }">
                <!-- 🆕 生产部：钣金/装配两组各自的预计完成 -->
                <div v-if="isProduce" style="display:flex;flex-direction:column;gap:2px;font-size:12px;line-height:1.35;">
                  <span>钣金 {{ pgDue(row, 'sheetmetal') }}</span>
                  <span>装配 {{ pgDue(row, 'assembly') }}</span>
                </div>
                <!-- 🆕 设计/电工：管理层可直接改预计完成(不受本人锁定) -->
                <el-date-picker v-else-if="isMgr" :model-value="row.due_date || ''" type="date"
                                value-format="YYYY-MM-DD" size="small" style="width: 132px" placeholder="设预计完成"
                                @update:model-value="(v: string | null) => { if (v) doEditDue(row, v) }" />
                <template v-else>{{ row.due_date ? fmtDate(row.due_date) : '—' }}</template>
              </template>
            </el-table-column>
            <el-table-column label="完成" :min-width="isProduce ? 150 : 120">
              <template #default="{ row }">
                <div v-if="isProduce" style="display:flex;flex-direction:column;gap:2px;font-size:12px;line-height:1.35;">
                  <span>钣金 {{ pgDone(row, 'sheetmetal') }}</span>
                  <span>装配 {{ pgDone(row, 'assembly') }}</span>
                </div>
                <template v-else>{{ row.done_date ? fmtDate(row.done_date) : '—' }}</template>
              </template>
            </el-table-column>
            <el-table-column label="完成效率" min-width="100">
              <template #default="{ row }">
                <span v-if="row.eff_pct != null" :class="effClass(row)">{{ row.eff_pct }}%</span>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="资料" min-width="104" align="center">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openPack(row)">预览/下载</el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="248" fixed="right">
              <template #default="{ row }">
                <el-button v-if="['assigned', 'in_progress'].includes(row.status) && !isProduce"
                           size="small" :icon="SwitchIcon" @click="openReassign(row)">换人</el-button>
                <el-button v-if="row.status === 'done'" size="small" :icon="RefreshLeft" @click="doReopen(row)">改回</el-button>
                <el-button v-if="!['done', 'voided'].includes(row.status)" size="small" type="danger" plain
                           @click="doVoid(row)">作废</el-button>
                <!-- 🆕 管理层直接删除任务单（含已作废残留）：彻底删除、不留痕 -->
                <el-button v-if="isMgr" size="small" type="danger" :icon="Delete" @click="doDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ===== 🆕 生产部-钣金组 tab（被派发项目；只读钣金装配表引用） ===== -->
        <el-tab-pane v-if="isProduce && (isSheetmetal || isLead || isMgr)" :label="`🔧 钣金组 (${sheetmetalRows.length})`" name="sm">
          <div class="grp-filter">
            <span class="muted small">按人员筛选：</span>
            <el-select v-model="smWorkerFilter" placeholder="全部人员" clearable filterable size="small" style="width:160px">
              <el-option v-for="w in smWorkers" :key="w" :label="w" :value="w" />
            </el-select>
            <span class="muted small">共 {{ smRowsView.length }} 项</span>
          </div>
          <el-table :data="smRowsView" stripe v-loading="loading" max-height="calc(100vh - 300px)" :scrollbar-always-on="true">
            <el-table-column type="index" label="#" width="56" align="center" />
            <el-table-column label="项目编号" min-width="130"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="240" show-overflow-tooltip />
            <el-table-column label="设计师" min-width="100" align="center"><template #default="{ row }">{{ row.designer || '—' }}</template></el-table-column>
            <el-table-column label="派给" min-width="100" align="center"><template #default="{ row }">{{ row.worker_name || '—' }}</template></el-table-column>
            <el-table-column label="钣金装配表" min-width="200" align="center">
              <template #default="{ row }">
                <template v-if="row.sheetmetal_datasheet_id">
                  <el-button size="small" link type="primary" @click="viewSheet(row)">
                    编辑装配表<el-icon v-if="row.sheetmetal_done" color="var(--success,#10b981)" style="margin-left:2px"><CircleCheck /></el-icon>
                  </el-button>
                </template>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="预计完成" min-width="150" align="center">
              <template #default="{ row }">
                <el-date-picker v-model="row.due_date" type="date" value-format="YYYY-MM-DD"
                  size="small" placeholder="设置" style="width:132px" :clearable="false"
                  :disabled="!!row.due_date && !isMgr" @change="(v: string | null) => setGroupDue(row, v)" />
              </template>
            </el-table-column>
            <el-table-column label="钣金完成" min-width="180" align="center">
              <template #default="{ row }">
                <StatusPill :text="row.group_done ? '已完成' : '进行中'" :variant="row.group_done ? 'success' : 'warn'" />
                <el-button size="small" :type="row.group_done ? 'default' : 'success'" link style="margin-left:8px" @click="toggleGroupDone(row)">
                  {{ row.group_done ? '撤销' : '标记完成' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!loading && !smRowsView.length" text="暂无派发给钣金组的项目" size="sm" />
        </el-tab-pane>

        <!-- ===== 🆕 生产部-装配组 tab（被派发项目 + 标准件清单/外协加工 备齐状态） ===== -->
        <el-tab-pane v-if="isProduce && (isAssembler || isLead || isMgr)" :label="`🔩 装配组 (${assemblyRows.length})`" name="asm">
          <div class="grp-filter">
            <span class="muted small">按人员筛选：</span>
            <el-select v-model="asmWorkerFilter" placeholder="全部人员" clearable filterable size="small" style="width:160px">
              <el-option v-for="w in asmWorkers" :key="w" :label="w" :value="w" />
            </el-select>
            <span class="muted small">共 {{ asmRowsView.length }} 项</span>
          </div>
          <el-table :data="asmRowsView" stripe v-loading="loading" max-height="calc(100vh - 300px)" :scrollbar-always-on="true">
            <el-table-column type="index" label="#" width="56" align="center" />
            <el-table-column label="项目编号" min-width="130"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="220" show-overflow-tooltip />
            <el-table-column label="设计师" min-width="90" align="center"><template #default="{ row }">{{ row.designer || '—' }}</template></el-table-column>
            <el-table-column label="派给" min-width="90" align="center"><template #default="{ row }">{{ row.worker_name || '—' }}</template></el-table-column>
            <el-table-column label="钣金装配表" min-width="180" align="center">
              <template #default="{ row }">
                <template v-if="row.sheetmetal_datasheet_id">
                  <el-button size="small" link type="primary" @click="viewSheet(row)">编辑装配表</el-button>
                </template>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="标准件清单" min-width="120" align="center">
              <template #default="{ row }"><StatusPill :text="row.standard_ready ? '已备齐' : '进行中'" :variant="row.standard_ready ? 'success' : 'warn'" /></template>
            </el-table-column>
            <el-table-column label="外协加工" min-width="120" align="center">
              <template #default="{ row }"><StatusPill :text="row.outsource_ready ? '已备齐' : '进行中'" :variant="row.outsource_ready ? 'success' : 'warn'" /></template>
            </el-table-column>
            <el-table-column label="预计完成" min-width="150" align="center">
              <template #default="{ row }">
                <el-date-picker v-model="row.due_date" type="date" value-format="YYYY-MM-DD"
                  size="small" placeholder="设置" style="width:132px" :clearable="false"
                  :disabled="!!row.due_date && !isMgr" @change="(v: string | null) => setGroupDue(row, v)" />
              </template>
            </el-table-column>
            <el-table-column label="装配完成" min-width="180" align="center">
              <template #default="{ row }">
                <StatusPill :text="row.group_done ? '已完成' : '进行中'" :variant="row.group_done ? 'success' : 'warn'" />
                <el-button size="small" :type="row.group_done ? 'default' : 'success'" link style="margin-left:8px" @click="toggleGroupDone(row)">
                  {{ row.group_done ? '撤销' : '标记完成' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!loading && !assemblyRows.length" text="暂无派发给装配组的项目" size="sm" />
          <!-- grp-filter 样式 -->
        </el-tab-pane>
      </el-tabs>
    </template>

    <EmptyHint v-else text="你没有本工作台权限" :icon="Lock" />

    <!-- 🆕 v3 M13 问题反馈面板（生产部=装配提交/主管审批；设计部=设计师接收） -->
    <FeedbackPanel v-if="dept === 'produce' || dept === 'design'" :key="dept" />

    <!-- 🆕 M07 设计师查库存 -->
    <StockQueryDialog v-if="dept === 'design'" v-model="stockVisible" />

    <!-- 🆕 M14 部门报表弹窗 -->
    <el-dialog v-model="reportVisible" :title="`📊 ${report?.dept_name || ''}报表（仅本部门数据）`" width="1080px" top="6vh" class="v3-scroll-dialog">
      <!-- 筛选栏 -->
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <span style="font-size:13px;color:var(--el-text-color-secondary)">筛选：</span>
        <el-select v-model="yearFilter" size="small" style="width:90px" @change="openReport">
          <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
        </el-select>
        <el-date-picker v-model="reportMonth" type="month" value-format="YYYY-MM" placeholder="全年"
                        clearable size="small" style="width:120px" @change="openReport" />
        <span style="font-size:12px;color:var(--el-text-color-secondary)">留空=全年统计</span>
      </div>
      <div v-if="report">
        <div class="kpi-grid">
          <div class="kpi is-primary"><div class="kpi-v">{{ report.total }}</div><div class="kpi-l">任务总数</div></div>
          <div class="kpi is-good"><div class="kpi-v">{{ report.done }}</div><div class="kpi-l">已完成</div></div>
          <div class="kpi" :class="report.overdue ? 'is-bad' : ''"><div class="kpi-v">{{ report.overdue }}</div><div class="kpi-l">逾期</div></div>
          <div class="kpi"><div class="kpi-v">{{ report.ontime_rate ?? '—' }}%</div><div class="kpi-l">按时率 · 均效率 {{ report.avg_eff ?? '—' }}%</div></div>
        </div>
        <el-table :data="report.workers" size="small" stripe style="margin-top:10px" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="worker_name" label="人员" min-width="100" />
          <el-table-column prop="total" label="任务数" width="80" />
          <el-table-column prop="done" label="完成" width="70" />
          <el-table-column prop="ontime" label="按时" width="70" />
          <el-table-column label="逾期" width="70"><template #default="{ row }">{{ row.over }}</template></el-table-column>
          <el-table-column label="按时率" width="80"><template #default="{ row }">{{ row.rate ?? '—' }}%</template></el-table-column>
          <el-table-column label="平均效率" width="90"><template #default="{ row }"><span :class="row.avg_eff != null && row.avg_eff >= 100 ? 'eff-good' : 'eff-bad'">{{ row.avg_eff ?? '—' }}%</span></template></el-table-column>
        </el-table>
        <div class="sec-title" style="margin-top:16px">逾期任务（{{ report.overdue_items.length }}）</div>
        <el-table v-if="report.overdue_items.length" :data="report.overdue_items" size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="worker_name" label="人员" width="100" />
          <el-table-column prop="code" label="项目编号" width="120" />
          <el-table-column prop="name" label="项目名称" min-width="160" show-overflow-tooltip />
          <el-table-column prop="due_date" label="预计" width="110">
            <template #default="{ row }">{{ fmtDate(row.due_date) }}</template>
          </el-table-column>
          <el-table-column prop="done_date" label="实际" width="110">
            <template #default="{ row }">{{ fmtDate(row.done_date) }}</template>
          </el-table-column>
          <el-table-column label="逾期" width="90"><template #default="{ row }">超 {{ row.over_days }} 天</template></el-table-column>
        </el-table>
        <EmptyHint v-else text="本月无逾期任务" size="sm" />
      </div>
    </el-dialog>

    <!-- ===== 完成弹窗（设计部第二步时仅选通知人；其他部门保持原完整流程） ===== -->
    <el-dialog v-model="completeVisible"
               :title="(dept === 'design' && completeOrder?.design_done_flag) || (dept === 'electric' && completeOrder?.electric_done_flag)
                 ? `✓ 发货准备 · ${completeOrder?.project_code || ''}（${deptName}）`
                 : `✓ 完成任务 · ${completeOrder?.project_code || ''}（${deptName}）`"
               width="560px">
      <el-alert v-if="options?.sheet_check && !(dept === 'design' && completeOrder?.design_done_flag)"
                type="info" :closable="false" style="margin-bottom: 14px"
                title="完成前将校验五个数据表均已通过 Excel 导入（项目详情页头「导入 Excel」）" />
      <el-form label-position="top">
        <el-form-item :label="`${options?.notify_label}（必选，企业微信/站内通知）`" required>
          <el-select v-model="notifyUserId" placeholder="选择通知人" size="large" style="width: 100%">
            <el-option v-for="u in options?.notify_pool || []" :key="u.id" :label="u.name" :value="u.id" />
          </el-select>
        </el-form-item>
        <!-- 两步流第二步时产物已在卡片上传，弹窗不再重复显示 -->
        <el-form-item v-if="options?.outputs.length
                            && !(dept === 'design' && completeOrder?.design_done_flag)
                            && !(dept === 'electric' && completeOrder?.electric_done_flag)"
                      label="上传产物（完成后按目标推送下游）">
          <div class="out-rows">
            <div v-for="ot in options?.outputs || []" :key="ot.k" class="out-row">
              <span class="ol">{{ ot.label }}<span class="opt-hint">（可选）</span></span>
              <el-tag v-for="f in outputFilesOf(ot.k)" :key="f.id" size="small" type="success" effect="plain"><el-icon><Check /></el-icon> {{ f.name }}</el-tag>
              <el-button size="small" plain type="primary" :icon="UploadFilled" @click="pickOutputUpload(ot.k)">上传</el-button>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="completeVisible = false">取消</el-button>
        <el-button type="success" :loading="completing" @click="doComplete">
          {{ (dept === 'design' && completeOrder?.design_done_flag) || (dept === 'electric' && completeOrder?.electric_done_flag)
            ? '确认发货准备' : '确认完成' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ===== 换人弹窗（M17 防离职） ===== -->
    <el-dialog v-model="reassignVisible" :title="`🔄 更换负责人 · ${reassignOrder?.project_code || ''}`" width="480px">
      <el-alert type="warning" :closable="false" style="margin-bottom: 14px"
                title="用于人员离职/请假：任务转交同部门他人，时间与已传产物保留，新负责人接续；设计/电工换人将回传项目目录" />
      <el-form label-position="top">
        <el-form-item label="当前负责人">
          <el-input :model-value="reassignOrder?.worker_name || '—'" disabled />
        </el-form-item>
        <el-form-item label="转交给" required>
          <el-select v-model="reassignWid" placeholder="选择同部门人员" style="width: 100%">
            <el-option v-for="w in reassignCandidates" :key="w.id" :label="w.name" :value="w.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassignVisible = false">取消</el-button>
        <el-button type="primary" :loading="reassigning" @click="doReassign">确认转交</el-button>
      </template>
    </el-dialog>

    <!-- ===== 🆕 生产派发弹窗（派给钣金组+装配组） ===== -->
    <el-dialog v-model="dispatchVisible" :title="`🚀 派发生产任务 · ${dispatchOrder?.project_code || ''}`" width="460px">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px"
                title="钣金组、装配组可各自选派，至少选择一组；各组标记完成后即视为生产完成（可发货）。" />
      <el-form label-position="top">
        <el-form-item label="派给 · 生产部-钣金组（可不选）">
          <el-select v-model="dispatchSmWid" placeholder="不派发钣金组则留空" clearable style="width: 100%">
            <el-option v-for="w in dispatchOpts.sheetmetal" :key="w.id" :label="w.name" :value="w.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="派给 · 生产部-装配组（可不选）">
          <el-select v-model="dispatchAsmWid" placeholder="不派发装配组则留空" clearable style="width: 100%">
            <el-option v-for="w in dispatchOpts.assembly" :key="w.id" :label="w.name" :value="w.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dispatchVisible = false">取消</el-button>
        <el-button type="primary" :icon="Promotion" :loading="dispatching" @click="doDispatch">确认派发</el-button>
      </template>
    </el-dialog>

    <!-- ===== 钣金装配表可编辑预览（钣金组/装配组） ===== -->
    <el-dialog v-model="viewVisible" :title="viewTitle" width="90vw" destroy-on-close>
      <SheetmetalGrid
        v-if="viewRow?.sheetmetal_datasheet_id"
        :datasheetId="viewRow.sheetmetal_datasheet_id"
        :projectCode="viewRow.code"
        :canEdit="canEditSheet"
      />
    </el-dialog>

    <!-- 🆕 #9 任务跟踪：资料预览 + 打包下载 -->
    <AttachmentPackDialog v-model="packVisible" :title="packTitle" :zipname="packZipname" :groups="packGroups" />
    <!-- 🆕 #4 合同技术资料在线预览 -->
    <AttachmentPreview ref="previewRef" />

    <!-- 🆕 #6 电工部：标准件清单只读引用 -->
    <el-dialog v-model="stdVisible" :title="stdTitle" width="90vw" destroy-on-close>
      <SheetmetalGrid
        v-if="stdRow?.standard_datasheet_id"
        :datasheetId="stdRow.standard_datasheet_id"
        :projectCode="stdRow.project_code"
        :canEdit="false"
      />
    </el-dialog>

    <!-- 🆕 #5 资料/产物管理：发货准备(说明书/铭牌) 或 设计资料更换(CAD激光图纸/外购附图) -->
    <el-dialog v-model="resVisible" :title="`${resTitle} · ${resOrder?.project_code || ''}`" width="560px">
      <el-alert type="info" :closable="false" style="margin-bottom: 14px"
                :title="resMode === 'shipprep'
                  ? '补传 / 替换 产品说明书、铭牌（发货资料，选填、不计考核）。文件上点 × 删除后可重新上传替换。备齐后点「发货准备完成」通知物流。'
                  : '更换 CAD激光图纸 / 外购附图：上传新文件，旧文件点 × 删除即替换，更新后自动同步采购部。'" />
      <template v-if="resOrder">
        <div class="up-sec" v-for="g in resSections" :key="g.k">
          <div class="up-h"><el-icon><UploadFilled /></el-icon> {{ g.label }}</div>
          <div class="up-b">
            <div v-if="resFiles(resOrder, g.k).length" class="tc-files">
              <el-tag v-for="f in resFiles(resOrder, g.k)" :key="f.id" size="small" effect="plain"
                      class="file-chip" closable @close="removeCardOutput(resOrder!, f.id)">
                <span @click="downloadAttachment(f)" style="cursor:pointer">{{ f.name }}</span>
              </el-tag>
            </div>
            <el-button size="small" plain :icon="UploadFilled" @click="resPick(resOrder!, g.k)">
              {{ resFiles(resOrder, g.k).length ? '继续添加' : g.btn }}
            </el-button>
          </div>
        </div>
      </template>
      <template #footer>
        <el-tag v-if="resMode === 'shipprep' && resOrder?.ship_prep_done" type="success" effect="plain" style="margin-right:auto">✅ 已标记发货准备完成</el-tag>
        <el-button @click="resVisible = false">关闭</el-button>
        <el-button v-if="resMode === 'shipprep'" type="success" :icon="Check" @click="doShipPrepDone(resOrder)">发货准备完成</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 #1 提技术资料修订意见 -->
    <el-dialog v-model="revVisible" :title="`✏️ 技术资料修订意见 · ${revOrder?.project_code || ''}`" width="520px">
      <el-alert type="warning" :closable="false" style="margin-bottom: 12px"
                title="把合同技术资料里需要修改的地方写清楚，提交后会推送给该项目对应的销售员，由销售更换技术资料。" />
      <el-input v-model="revReason" type="textarea" :rows="5" maxlength="1000" show-word-limit
                placeholder="例如：A3 图纸尺寸与合同不符，应为 …；缺少 XX 部件的外购图……" />
      <template #footer>
        <el-button @click="revVisible = false">取消</el-button>
        <el-button type="warning" :loading="revSubmitting" @click="submitRevision">提交并通知销售</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.todo-grid {
  display: grid;
  /* #71 窄屏(<412px)不再横向溢出：min(380px,100%) 让单列可收缩 */
  grid-template-columns: repeat(auto-fill, minmax(min(380px, 100%), 1fr));
  gap: 14px;
}
.todo-card { border-left: 4px solid var(--primary, #2563eb); border-radius: 10px; }
.todo-card.urgent { border-left-color: var(--danger); }
.todo-card.assign { border-left-color: var(--warning); }
.tc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.tc-code { font-size: 16px; font-weight: 700; color: var(--primary, #2563eb); }
.tc-name { color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 8px; }
.tc-req {
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--primary, #2563eb);
  padding: 7px 10px; font-size: 12.5px; border-radius: 0 6px 6px 0; margin: 8px 0;
}
.tc-files { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.file-chip { cursor: pointer; }
.file-chip .dl, .file-chip .rm { margin-left: 4px; }
.file-chip .rm { color: var(--danger); }
.tc-dates { display: flex; gap: 10px; margin: 10px 0 4px; }
.tc-dates .fd { flex: 1; }
.tc-dates label { font-size: 12px; color: var(--el-text-color-secondary); display: block; margin-bottom: 4px; }
.tc-hint { font-size: 11.5px; color: var(--el-text-color-placeholder); margin: 2px 0 8px; }
.tc-kv { font-size: 12.5px; color: var(--el-text-color-secondary); margin: 6px 0 10px; }
.tc-kv b { color: var(--el-text-color-primary); }
.up-sec {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px; margin-bottom: 10px; overflow: hidden;
}
.up-h {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 12px; background: var(--el-fill-color-light);
  font-size: 12.5px; font-weight: 600;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.up-b { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.four-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.assign-bar { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
.eff-good { color: var(--success); font-weight: 700; }
.eff-bad { color: var(--danger); font-weight: 700; }
.out-rows { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.out-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.out-row .ol { font-size: 13px; min-width: 130px; }
.req-star { color: var(--danger); font-size: 12px; margin-left: 4px; }
.opt-hint { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 4px; }
.grp-filter { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.grp-filter .muted { color: var(--el-text-color-secondary); }
.grp-filter .small { font-size: 12px; }
</style>
