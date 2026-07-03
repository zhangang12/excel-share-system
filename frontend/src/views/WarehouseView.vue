<script setup lang="ts">
// 🆕 v3 M07 仓库组：总览/出入库/收发存/流水/物料主数据/发货清单 六 tab
import { ref, onMounted, reactive, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Lock, View, Download, Delete, Printer } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { whApi, type WhMaterial, type WhTxn, type WhSummaryRow, type ShipListItem, type ShipListPendingRow } from '@/api/warehouse'
import { canInlinePreview, attachmentBlobUrl, isPdfAtt, isImageAtt } from '@/api/attachments'
import { downloadAttachment } from '@/api/orders'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import AttachmentPreview from '@/components/AttachmentPreview.vue'
import { fmtDate } from '@/utils/format'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasRole('warehouse', 'warehouse_lead', 'admin', 'manager'))

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
onMounted(loadMaterials)

const totalStock = computed(() => materials.value.reduce((s, m) => s + m.stock, 0))
const lowList = computed(() => materials.value.filter(m => m.low))

// ===== 出入库登记 =====
const ioVisible = ref(false)
const ioForm = reactive({ material_id: undefined as number | undefined, direction: 'in', qty: 1,
  biz_date: new Date().toISOString().slice(0, 10), source: '', party: '' })
function openIo(dir: string) {
  Object.assign(ioForm, { material_id: undefined, direction: dir, qty: 1,
    biz_date: new Date().toISOString().slice(0, 10), source: '', party: '' })
  ioVisible.value = true
}
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
const matForm = reactive<any>({ id: null, name: '', spec: '', category: '', unit: '个', location: '', safety_stock: 0, init_stock: 0 })
function openMat(m?: WhMaterial) {
  if (m) Object.assign(matForm, { ...m })
  else Object.assign(matForm, { id: null, name: '', spec: '', category: '', unit: '个', location: '', safety_stock: 0, init_stock: 0 })
  matVisible.value = true
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

// ===== 🆕 发货清单：待备货（设计推送）=====
const shipPending = ref<ShipListPendingRow[]>([])
const shipPendingLoading = ref(false)
async function loadShipPending() {
  shipPendingLoading.value = true
  try { shipPending.value = await whApi.shipListPending() }
  finally { shipPendingLoading.value = false }
}
async function markShipReady(row: ShipListPendingRow) {
  try {
    await ElMessageBox.confirm(`确认「${row.code} ${row.name}」发货清单已备货完成？将通知物流可安排发货。`, '备货完成', { type: 'success' })
  } catch { return }
  const r: any = await whApi.shipListReady(row.project_id)
  ElMessage.success(r?.message || '已标记备货完成')
  await loadShipPending()
}

// ===== 🆕 采购收货：仓库对采购下单的物料确认收货、补送货单号/到货日期/后填价格 =====
interface RecvItem {
  id: number; po_no?: string | null; supplier_id: number; supplier_name: string
  project_code?: string | null; item_name: string; spec?: string | null
  qty?: number | null; unit_price?: number | null; received_amount: number
  delivery_note_no?: string | null; arrival_date?: string | null
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
  recvVisible.value = true
}
function onRecvCalc() {
  if (recvForm.qty != null && recvForm.unit_price != null) {
    recvForm.received_amount = Number((recvForm.qty * recvForm.unit_price).toFixed(2))
  }
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
    ElMessage.success('已确认收货')
    recvVisible.value = false
    await loadReceiving()
  } catch { /* handled */ } finally { recvSaving.value = false }
}

// ===== 🆕 项目物料需求（清单→仓库）=====
interface DemandRow {
  item_name: string; spec?: string | null; demand_qty?: number | null
  stock: number; suggest_purchase: number; purchase_status: string; in_stock: boolean
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

// ===== 发货清单上传 =====
const projects = ref<{ id: number; code: string; name: string }[]>([])
const shipProj = ref<number | undefined>()
async function loadProjects() {
  // 复用一览接口取项目（仓库有详单权限）
  try { projects.value = (await http.get('/projects')).data.map((p: any) => ({ id: p.id, code: p.code, name: p.name })) }
  catch { projects.value = [] }
}
// 🆕 #9 历史发货清单 列表 / 预览 / 更换(删除+重传)
const shipLists = ref<ShipListItem[]>([])
const shipListsLoading = ref(false)
async function loadShipLists() {
  if (!shipProj.value) { shipLists.value = []; return }
  shipListsLoading.value = true
  try { shipLists.value = await whApi.shipLists(shipProj.value) }
  finally { shipListsLoading.value = false }
}
watch(shipProj, () => loadShipLists())  // 选项目即加载其历史发货清单
async function uploadShipList() {
  if (!shipProj.value) { ElMessage.warning('请选择项目'); return }
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.xlsx,.xls,.pdf'
  input.onchange = async () => {
    const f = input.files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    await http.post(`/wh/ship-list/${shipProj.value}`, fd)
    ElMessage.success('发货清单已上传并推送物流')
    await loadShipLists()  // 上传后刷新列表
  }
  input.click()
}
async function deleteShipList(item: ShipListItem) {
  try {
    await ElMessageBox.confirm(`确认删除发货清单「${item.name}」？删除后物流看板将同步移除。`, '删除发货清单', { type: 'warning', confirmButtonText: '删除' })
  } catch { return }
  await whApi.deleteShipList(item.id)
  ElMessage.success('已删除')
  await loadShipLists()
}
// 预览：图片弹窗 / PDF 新标签 / 其它直接下载
const previewRef = ref<InstanceType<typeof AttachmentPreview>>()
function previewShipList(item: ShipListItem) { previewRef.value?.open({ id: item.id, name: item.name }) }

// 🆕 打印发货清单：PDF/图片经隐藏 iframe 直接调起打印；Excel 等格式提示下载后打印
async function printShipList(item: ShipListItem) {
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
  if (name === 'demand' && !projects.value.length) loadProjects()
  if (name === 'ship') {
    if (!projects.value.length) loadProjects()
    loadShipPending()
  }
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
        <el-tab-pane label="库存总览" name="ov">
          <div class="kpi-grid">
            <div class="kpi"><div class="kpi-v">{{ materials.length }}</div><div class="kpi-l">物料种类</div></div>
            <div class="kpi"><div class="kpi-v">{{ totalStock }}</div><div class="kpi-l">库存总量</div></div>
            <div class="kpi" :class="lowCount ? 'is-bad' : ''"><div class="kpi-v">{{ lowCount }}</div><div class="kpi-l">低于安全库存</div></div>
          </div>
          <el-alert v-if="lowList.length" type="warning" :closable="false" style="margin:10px 0"
                    :title="`⚠ 低库存预警：${lowList.map(m => m.name + (m.spec ? '·' + m.spec : '')).join('、')}`" />
          <div style="display:flex;gap:10px;margin-bottom:10px">
            <el-input v-model="kw" placeholder="搜索物料" :prefix-icon="Search" clearable style="width:240px" @change="loadMaterials" />
          </div>
          <el-table :data="materials" stripe size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="spec" label="规格型号" min-width="120"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="category" label="类别" width="100"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column label="现存" width="90">
              <template #default="{ row }"><b :class="{ bad: row.low }">{{ row.stock }}</b></template>
            </el-table-column>
            <el-table-column prop="safety_stock" label="安全库存" width="90" />
            <el-table-column prop="location" label="库位" width="90"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
          </el-table>
          <EmptyHint v-if="!materials.length" text="暂无物料，去「物料主数据」新增" size="sm" />
        </el-tab-pane>

        <!-- 出入库登记 -->
        <el-tab-pane label="出入库登记" name="io">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可登记出入库" :icon="Lock" />
          <template v-else>
            <el-button type="primary" :icon="Plus" @click="openIo('in')">入库登记</el-button>
            <el-button type="warning" :icon="Plus" @click="openIo('out')">出库登记</el-button>
            <div class="muted small" style="margin-top:10px">入库单号 RK+日期+序号；出库单号 CK…；出库超现存将被拦截。</div>
          </template>
        </el-tab-pane>

        <!-- 收发存汇总 -->
        <el-tab-pane label="收发存汇总" name="sum">
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
        <el-tab-pane label="出入库流水" name="txn">
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
        <el-tab-pane label="物料主数据" name="mat">
          <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openMat()" style="margin-bottom:10px">新增物料</el-button>
          <el-table :data="materials" stripe size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="spec" label="规格型号" min-width="120"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
            <el-table-column prop="category" label="类别" width="100"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column prop="unit" label="单位" width="60" />
            <el-table-column prop="safety_stock" label="安全库存" width="90" />
            <el-table-column prop="init_stock" label="期初库存" width="90" />
            <el-table-column prop="location" label="库位" width="90"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
            <el-table-column v-if="canWrite" label="操作" width="80"><template #default="{ row }"><el-button size="small" link type="primary" @click="openMat(row)">编辑</el-button></template></el-table-column>
          </el-table>
          <EmptyHint v-if="!materials.length" text="暂无物料主数据，点「新增物料」开始" size="sm" />
        </el-tab-pane>

        <!-- 🆕 项目物料需求（清单→仓库）-->
        <el-tab-pane label="物料需求" name="demand">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <el-select v-model="demandProj" filterable clearable placeholder="选择项目" style="width:300px">
              <el-option v-for="p in projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
            </el-select>
            <span class="muted small">读项目「标准件清单」,逐行看 需求量 / 现有库存 / 建议采购量。有货的可直接出库,缺的走采购。</span>
          </div>
          <el-table v-if="demandProj" :data="demandRows" v-loading="demandLoading" stripe size="small"
                    max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="wrap-cells">
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
          </el-table>
          <EmptyHint v-if="demandProj && !demandLoading && !demandRows.length" text="该项目暂无标准件清单或清单为空" size="sm" />
          <EmptyHint v-if="!demandProj" text="选择项目查看物料需求" size="sm" />
        </el-tab-pane>

        <!-- 🆕 采购收货 -->
        <el-tab-pane label="采购收货" name="recv">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可确认收货" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
              <el-radio-group v-model="recvReceived" @change="loadReceiving" size="small">
                <el-radio-button :value="false">待收货</el-radio-button>
                <el-radio-button :value="true">已收货</el-radio-button>
              </el-radio-group>
              <el-select v-model="recvSupplier" placeholder="全部供应商" clearable style="width:180px" @change="loadReceiving">
                <el-option v-for="s in recvSupplierOptions" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
              <el-input v-model="recvPo" placeholder="采购单号" clearable style="width:150px" @change="loadReceiving" />
              <el-button :icon="Search" @click="loadReceiving">查询</el-button>
              <span class="muted small">采购下单的物料到货后，在这里核对规格、填送货单号/到货日期；单价未填的（后填价格）在此补上。</span>
            </div>
            <el-table :data="recvItems" v-loading="recvLoading" stripe size="small"
                      max-height="calc(100vh - 260px)" :scrollbar-always-on="true" class="wrap-cells">
              <el-table-column prop="po_no" label="采购单号" width="128">
                <template #default="{ row }"><span class="code">{{ row.po_no || '—' }}</span></template>
              </el-table-column>
              <el-table-column prop="supplier_name" label="供应商" min-width="130" />
              <el-table-column prop="project_code" label="订单编号" width="110">
                <template #default="{ row }">{{ row.project_code || '—' }}</template>
              </el-table-column>
              <el-table-column prop="item_name" label="名称" min-width="120" />
              <el-table-column prop="spec" label="规格型号" min-width="120">
                <template #default="{ row }">{{ row.spec || '—' }}</template>
              </el-table-column>
              <el-table-column label="数量" width="72" align="right">
                <template #default="{ row }">{{ row.qty ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="单价" width="92" align="right">
                <template #default="{ row }">
                  <span v-if="row.unit_price != null">{{ row.unit_price }}</span>
                  <el-tag v-else size="small" type="warning" effect="plain">后填</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="送货单号" width="110">
                <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
              </el-table-column>
              <el-table-column label="到货日期" width="110">
                <template #default="{ row }">{{ row.arrival_date || '—' }}</template>
              </el-table-column>
              <el-table-column label="操作" width="96" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" :type="recvReceived ? 'default' : 'primary'" plain @click="openReceive(row)">
                    {{ recvReceived ? '修改' : '收货' }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <EmptyHint v-if="!recvLoading && !recvItems.length" :text="recvReceived ? '暂无已收货记录' : '暂无待收货物料'" size="sm" />
          </template>
        </el-tab-pane>

        <!-- 发货清单 -->
        <el-tab-pane label="发货清单" name="ship">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可上传发货清单" :icon="Lock" />
          <template v-else>
            <!-- 🆕 待备货：设计部已推送、尚未标记完成的项目 -->
            <div class="ship-pending-sec">
              <div class="ship-pending-title">📋 待备货清单（设计部已推送）</div>
              <el-table :data="shipPending" v-loading="shipPendingLoading" stripe size="small" max-height="220">
                <el-table-column label="项目编号" width="110"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
                <el-table-column prop="name" label="项目名称" min-width="160" show-overflow-tooltip />
                <el-table-column label="推送时间" width="170"><template #default="{ row }">{{ fmtDate(row.requested_at) }}</template></el-table-column>
                <el-table-column label="推送人" width="100"><template #default="{ row }">{{ row.requested_by_name || '—' }}</template></el-table-column>
                <el-table-column label="操作" width="120" align="center">
                  <template #default="{ row }">
                    <el-button size="small" type="success" plain @click="markShipReady(row)">备货完成</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <EmptyHint v-if="!shipPendingLoading && !shipPending.length" text="暂无待备货项目" size="sm" />
            </div>

            <div style="display:flex;gap:10px;align-items:center;margin-top:18px">
              <el-select v-model="shipProj" filterable placeholder="选择项目" style="width:300px">
                <el-option v-for="p in projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
              </el-select>
              <el-button type="primary" @click="uploadShipList">上传 / 更换发货清单 → 推物流</el-button>
            </div>
            <div class="muted small" style="margin:10px 0">上传后物流发货部看板「仓库发货清单」列出现该文件；如需更换，上传新文件后删除旧的即可。</div>

            <!-- 🆕 #9 历史发货清单列表（预览 / 下载 / 删除） -->
            <el-table v-if="shipProj" :data="shipLists" v-loading="shipListsLoading" stripe size="small"
                      max-height="calc(100vh - 360px)" :scrollbar-always-on="true">
              <el-table-column type="index" label="#" width="50" align="center" />
              <el-table-column prop="name" label="发货清单文件" min-width="240" show-overflow-tooltip />
              <el-table-column label="上传时间" width="170">
                <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="290" align="center">
                <template #default="{ row }">
                  <el-button v-if="canInlinePreview(row.name)" size="small" link type="primary" :icon="View" @click="previewShipList(row)">预览</el-button>
                  <el-button size="small" link :icon="Download" @click="downloadAttachment({ id: row.id, name: row.name })">下载</el-button>
                  <el-button size="small" link :icon="Printer" @click="printShipList(row)">打印</el-button>
                  <el-button v-if="canWrite" size="small" link type="danger" :icon="Delete" @click="deleteShipList(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <EmptyHint v-if="shipProj && !shipListsLoading && !shipLists.length" text="该项目暂无发货清单" size="sm" />
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>

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
          <el-form-item :label="ioForm.direction === 'in' ? '来源' : '用途'" style="flex:1">
            <el-input v-model="ioForm.source" :placeholder="ioForm.direction === 'in' ? '采购入库' : '领料出库'" />
          </el-form-item>
          <el-form-item :label="ioForm.direction === 'in' ? '供应商' : '领用方'" style="flex:1"><el-input v-model="ioForm.party" /></el-form-item>
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
          <el-form-item label="类别" style="flex:1"><el-input v-model="matForm.category" /></el-form-item>
          <el-form-item label="单位" style="flex:1"><el-input v-model="matForm.unit" /></el-form-item>
          <el-form-item label="库位" style="flex:1"><el-input v-model="matForm.location" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="安全库存" style="flex:1"><el-input-number v-model="matForm.safety_stock" :min="0" :controls="false" style="width:100%" /></el-form-item>
          <el-form-item label="期初库存" style="flex:1"><el-input-number v-model="matForm.init_stock" :min="0" :controls="false" :disabled="!!matForm.id" style="width:100%" /></el-form-item>
        </div>
        <div v-if="matForm.id" class="muted small">期初库存建档后不可改（避免破坏库存勾稽，调整请用出入库）。</div>
      </el-form>
      <template #footer>
        <el-button @click="matVisible = false">取消</el-button>
        <el-button type="primary" :loading="matSubmitting" @click="submitMat">保存</el-button>
      </template>
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
          <el-form-item label="收货金额">
            <el-input-number v-model="recvForm.received_amount" :min="0" :precision="2" :controls="false" style="width:100%" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="recvVisible = false">取消</el-button>
        <el-button type="primary" :loading="recvSaving" @click="submitReceive">确认收货</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 #9 发货清单统一预览（图片/PDF/Excel/Word） -->
    <AttachmentPreview ref="previewRef" />
  </div>
</template>

<style scoped>
.bad { color: var(--danger); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.frow { display: flex; gap: 12px; flex-wrap: wrap; }
.frow > * { flex: 1; min-width: 140px; }
.ship-pending-sec {
  border: 1px solid var(--el-border-color-lighter); border-radius: 8px;
  padding: 12px; background: var(--el-fill-color-lighter);
}
.ship-pending-title { font-weight: 600; font-size: 13.5px; margin-bottom: 10px; color: var(--el-text-color-primary); }
.code { color: var(--el-color-primary, #2563eb); }
.recv-info { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 18px; padding: 12px 14px;
  background: var(--el-fill-color-light); border-radius: 8px; font-size: 13px; }
.recv-info .k { display: inline-block; min-width: 60px; color: var(--el-text-color-secondary); margin-right: 6px; }
</style>
