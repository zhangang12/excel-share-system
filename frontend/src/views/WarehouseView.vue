<script setup lang="ts">
// 🆕 v3 M07 仓库组：总览/出入库/收发存/流水/物料主数据/发货清单 六 tab
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Lock } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { whApi, type WhMaterial, type WhTxn, type WhSummaryRow } from '@/api/warehouse'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtDate } from '@/utils/format'

const auth = useAuthStore()
const canWrite = computed(() => ['warehouse', 'warehouse_lead', 'admin', 'manager'].includes(auth.user?.role_code || ''))

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

// ===== 发货清单上传 =====
const projects = ref<{ id: number; code: string; name: string }[]>([])
const shipProj = ref<number | undefined>()
async function loadProjects() {
  // 复用一览接口取项目（仓库有详单权限）
  try { projects.value = (await http.get('/projects')).data.map((p: any) => ({ id: p.id, code: p.code, name: p.name })) }
  catch { projects.value = [] }
}
async function uploadShipList() {
  if (!shipProj.value) { ElMessage.warning('请选择项目'); return }
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.xlsx,.xls,.pdf'
  input.onchange = async () => {
    const f = input.files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    await http.post(`/wh/ship-list/${shipProj.value}`, fd)
    ElMessage.success('发货清单已上传并推送物流')
  }
  input.click()
}

function onTab(name: string) {
  if (name === 'txn' && !txns.value.length) loadTxns()
  if (name === 'sum') loadSummary()
  if (name === 'ship' && !projects.value.length) loadProjects()
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>仓库组</h1>
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

        <!-- 发货清单 -->
        <el-tab-pane label="发货清单" name="ship">
          <EmptyHint v-if="!canWrite" text="仅仓库角色可上传发货清单" :icon="Lock" />
          <template v-else>
            <div style="display:flex;gap:10px;align-items:center">
              <el-select v-model="shipProj" filterable placeholder="选择项目" style="width:300px">
                <el-option v-for="p in projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
              </el-select>
              <el-button type="primary" @click="uploadShipList">上传发货清单 → 推物流</el-button>
            </div>
            <div class="muted small" style="margin-top:10px">上传后物流发货部看板「仓库发货清单」列出现该文件。</div>
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
  </div>
</template>

<style scoped>
.bad { color: var(--danger); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.frow { display: flex; gap: 12px; flex-wrap: wrap; }
.frow > * { flex: 1; min-width: 140px; }
</style>
