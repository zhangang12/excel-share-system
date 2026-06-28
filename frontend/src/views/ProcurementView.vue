<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Download, Money, Check, Document } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import {
  procureApi, SUPPLIER_CATEGORIES, SETTLE_TYPES, RECON_STATUSES,
  type SupplierRow, type SupplierForm, type SupplierOption,
  type PurchaseItemRow, type PurchaseItemForm, type PurchaseItemList, type ProcureSummary,
} from '@/api/procurement'

const auth = useAuthStore()
const isManager = computed(() => auth.hasRole('admin', 'manager'))
const activeTab = ref<'items' | 'suppliers' | 'summary'>('items')

function fmt(n?: number | null) {
  return Number(n || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function wan(n?: number | null) {
  return (Number(n || 0) / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}

// ===================== 供应商下拉（明细录入/筛选共用） =====================
const supplierOptions = ref<SupplierOption[]>([])
async function loadSupplierOptions() {
  try { supplierOptions.value = await procureApi.supplierOptions() } catch { /* ignore */ }
}

// ===================== Tab1 采购明细 =====================
const itemFilters = reactive({ project_no: '', supplier_id: undefined as number | undefined, month: '', recon_status: '', kw: '' })
const itemList = ref<PurchaseItemList>({ rows: [], total: 0, recv_total: 0, invoiced: 0, to_invoice: 0, paid: 0, owed: 0 })
const itemLoading = ref(false)
const itemPage = ref(1)
const itemPageSize = 50
const selectedItems = ref<PurchaseItemRow[]>([])

async function loadItems() {
  itemLoading.value = true
  try {
    itemList.value = await procureApi.listItems({
      project_no: itemFilters.project_no || undefined,
      supplier_id: itemFilters.supplier_id || undefined,
      month: itemFilters.month || undefined,
      recon_status: itemFilters.recon_status || undefined,
      kw: itemFilters.kw || undefined,
      page: itemPage.value, page_size: itemPageSize,
    })
  } finally { itemLoading.value = false }
}
function resetItemFilters() {
  itemFilters.project_no = ''; itemFilters.supplier_id = undefined; itemFilters.month = ''
  itemFilters.recon_status = ''; itemFilters.kw = ''; itemPage.value = 1; loadItems()
}
function onItemSelect(rows: PurchaseItemRow[]) { selectedItems.value = rows }
const selectedIds = computed(() => selectedItems.value.map(r => r.id))

// 明细 新增/编辑
const itemDialog = ref(false)
const editingItemId = ref<number | null>(null)
const emptyItem = (): PurchaseItemForm => ({
  supplier_id: 0 as any, delivery_date: '', project_no: '', item_name: '', spec: '',
  qty: undefined, unit_price: undefined, recv_amount: undefined, contract_no: '', delivery_no: '',
  tax_rate: '', invoice_date: '', invoice_amount: undefined, pay_date: '', pay_amount: undefined,
  recon_status: '待对账', note: '',
})
const itemForm = reactive<PurchaseItemForm>(emptyItem())
function openItemCreate() {
  Object.assign(itemForm, emptyItem())
  if (itemFilters.supplier_id) itemForm.supplier_id = itemFilters.supplier_id
  editingItemId.value = null; itemDialog.value = true
}
function openItemEdit(row: PurchaseItemRow) {
  Object.assign(itemForm, {
    supplier_id: row.supplier_id, delivery_date: row.delivery_date || '', project_no: row.project_no || '',
    item_name: row.item_name || '', spec: row.spec || '', qty: row.qty ?? undefined,
    unit_price: row.unit_price ?? undefined, recv_amount: row.recv_amount,
    contract_no: row.contract_no || '', delivery_no: row.delivery_no || '', tax_rate: row.tax_rate || '',
    invoice_date: row.invoice_date || '', invoice_amount: row.invoice_amount,
    pay_date: row.pay_date || '', pay_amount: row.pay_amount, recon_status: row.recon_status, note: row.note || '',
  })
  editingItemId.value = row.id; itemDialog.value = true
}
function autoRecv() {
  if (itemForm.qty != null && itemForm.unit_price != null)
    itemForm.recv_amount = Math.round((itemForm.qty * itemForm.unit_price) * 100) / 100
}
async function saveItem() {
  if (!itemForm.supplier_id) { ElMessage.warning('请选择供应商'); return }
  try {
    if (editingItemId.value) await procureApi.updateItem(editingItemId.value, itemForm)
    else await procureApi.createItem(itemForm)
    ElMessage.success('已保存'); itemDialog.value = false; loadItems()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
}
async function removeItem(row: PurchaseItemRow) {
  await ElMessageBox.confirm(`删除这条采购明细（${row.supplier_name || ''} ${row.item_name || ''}）？`, '确认', { type: 'warning' })
  await procureApi.deleteItem(row.id); ElMessage.success('已删除'); loadItems()
}

async function batchSettle(kind: 'invoice' | 'pay' | 'reconcile') {
  if (!selectedIds.value.length) { ElMessage.warning('请先勾选明细'); return }
  const ids = selectedIds.value
  if (kind === 'reconcile') {
    await procureApi.batchReconcile(ids); ElMessage.success('已标记对账'); loadItems(); return
  }
  const label = kind === 'invoice' ? '开票' : '付款'
  try {
    const { value } = await ElMessageBox.prompt(
      `对勾选的 ${ids.length} 条按「全额${label}」登记，${label}金额=收货金额。请填${label}日期：`,
      `批量${label}`, { inputPattern: /^\d{4}-\d{2}-\d{2}$/, inputPlaceholder: 'YYYY-MM-DD', inputValue: new Date().toISOString().slice(0, 10) })
    if (kind === 'invoice') await procureApi.batchInvoice(ids, value)
    else await procureApi.batchPay(ids, value)
    ElMessage.success(`已登记${label}`); loadItems()
  } catch { /* canceled */ }
}

// ===================== Tab2 供应商账目一览 =====================
const supFilters = reactive({ category: '', status: '', kw: '' })
const suppliers = ref<SupplierRow[]>([])
const supLoading = ref(false)
async function loadSuppliers() {
  supLoading.value = true
  try {
    const r = await procureApi.listSuppliers({
      category: supFilters.category || undefined, status: supFilters.status || undefined, kw: supFilters.kw || undefined })
    suppliers.value = r.rows
  } finally { supLoading.value = false }
}
const supTotals = computed(() => suppliers.value.reduce((a, s) => {
  a.recv += s.recv_total; a.inv += s.invoiced; a.toi += s.to_invoice; a.paid += s.paid; a.owed += s.owed; return a
}, { recv: 0, inv: 0, toi: 0, paid: 0, owed: 0 }))

const supDialog = ref(false)
const editingSupId = ref<number | null>(null)
const emptySup = (): SupplierForm => ({ name: '', category: '', contact: '', phone: '', address: '', tax_no: '', bank_name: '', bank_account: '', settle_type: '月结', settle_days: undefined, note: '', status: 'active' })
const supForm = reactive<SupplierForm>(emptySup())
function openSupCreate() { Object.assign(supForm, emptySup()); editingSupId.value = null; supDialog.value = true }
function openSupEdit(row: SupplierRow) {
  Object.assign(supForm, {
    name: row.name, category: row.category || '', contact: row.contact || '', phone: row.phone || '',
    address: row.address || '', tax_no: row.tax_no || '', bank_name: row.bank_name || '', bank_account: row.bank_account || '',
    settle_type: row.settle_type || '月结', settle_days: row.settle_days ?? undefined, note: row.note || '', status: row.status,
  })
  editingSupId.value = row.id; supDialog.value = true
}
async function saveSup() {
  if (!supForm.name?.trim()) { ElMessage.warning('请填供应商名称'); return }
  try {
    if (editingSupId.value) await procureApi.updateSupplier(editingSupId.value, supForm)
    else await procureApi.createSupplier(supForm)
    ElMessage.success('已保存'); supDialog.value = false; loadSuppliers(); loadSupplierOptions()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
}
async function removeSup(row: SupplierRow) {
  await ElMessageBox.confirm(`删除供应商「${row.name}」？（有采购明细的不可删，可改为停用）`, '确认', { type: 'warning' })
  try { await procureApi.deleteSupplier(row.id); ElMessage.success('已删除'); loadSuppliers() }
  catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}
function viewSupplierItems(row: SupplierRow) {
  resetItemFilters(); itemFilters.supplier_id = row.id; activeTab.value = 'items'; loadItems()
}

// 批量导入供应商名单
const importDialog = ref(false)
const importText = ref('')
const importCategory = ref('')
function openImport() { importText.value = ''; importCategory.value = ''; importDialog.value = true }
async function doImport() {
  const names = importText.value.split('\n').map(s => s.trim()).filter(Boolean)
  if (!names.length) { ElMessage.warning('请粘贴供应商名单（每行一个）'); return }
  try {
    const r = await procureApi.importSuppliers(names, importCategory.value || undefined)
    ElMessage.success(r.message); importDialog.value = false; loadSuppliers(); loadSupplierOptions()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '导入失败') }
}

// ===================== Tab3 汇总报表 =====================
const summary = ref<ProcureSummary | null>(null)
const sumLoading = ref(false)
const summaryYear = ref(String(new Date().getFullYear()))
const yearOptions = computed(() => {
  const y = new Date().getFullYear(); return [y, y - 1, y - 2].map(String)
})
async function loadSummary() {
  sumLoading.value = true
  try { summary.value = await procureApi.summary(summaryYear.value) } finally { sumLoading.value = false }
}
const maxMonthly = computed(() => Math.max(1, ...(summary.value?.monthly || []).map(m => m.recv)))

// ===================== Tab 切换懒加载 =====================
function onTab(name: any) {
  if (name === 'items' && !itemList.value.rows.length) loadItems()
  else if (name === 'suppliers' && !suppliers.value.length) loadSuppliers()
  else if (name === 'summary' && !summary.value) loadSummary()
}
onMounted(() => { loadSupplierOptions(); loadItems() })
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>采购管理</h1>
        <div class="desc">录一次采购明细 → 供应商账目、单家报表、月季年汇总全部自动派生</div>
      </div>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTab" class="proc-tabs">
      <!-- ============ 采购明细 ============ -->
      <el-tab-pane label="采购明细" name="items">
        <div class="toolbar">
          <el-input v-model="itemFilters.project_no" placeholder="项目编号" clearable style="width: 130px" @keyup.enter="loadItems" @clear="loadItems" />
          <el-select v-model="itemFilters.supplier_id" placeholder="供应商" clearable filterable style="width: 180px" @change="loadItems">
            <el-option v-for="s in supplierOptions" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <el-date-picker v-model="itemFilters.month" type="month" placeholder="送货月份" value-format="YYYY-MM" style="width: 130px" @change="loadItems" />
          <el-select v-model="itemFilters.recon_status" placeholder="对账状态" clearable style="width: 120px" @change="loadItems">
            <el-option v-for="s in RECON_STATUSES" :key="s" :label="s" :value="s" />
          </el-select>
          <el-input v-model="itemFilters.kw" placeholder="名称/规格/送货单号" clearable style="width: 170px" @keyup.enter="loadItems" @clear="loadItems" />
          <el-button :icon="Refresh" @click="resetItemFilters">重置</el-button>
          <div class="spacer" />
          <el-button :icon="Check" :disabled="!selectedIds.length" @click="batchSettle('reconcile')">批量对账</el-button>
          <el-button :icon="Document" :disabled="!selectedIds.length" @click="batchSettle('invoice')">批量开票</el-button>
          <el-button :icon="Money" type="warning" :disabled="!selectedIds.length" @click="batchSettle('pay')">批量付款</el-button>
          <el-button :icon="Plus" type="primary" @click="openItemCreate">新增明细</el-button>
        </div>

        <el-card shadow="never" body-style="padding:0">
          <el-table :data="itemList.rows" stripe v-loading="itemLoading" size="small" @selection-change="onItemSelect"
                    max-height="calc(100vh - 330px)">
            <el-table-column type="selection" width="40" />
            <el-table-column type="index" label="#" width="44" />
            <el-table-column prop="delivery_date" label="送货时间" width="100" />
            <el-table-column prop="supplier_name" label="供应商" min-width="150" show-overflow-tooltip />
            <el-table-column prop="project_no" label="项目编号" width="100" />
            <el-table-column prop="item_name" label="名称" min-width="110" show-overflow-tooltip />
            <el-table-column prop="spec" label="规格型号" min-width="110" show-overflow-tooltip />
            <el-table-column prop="qty" label="数量" width="64" align="right" />
            <el-table-column prop="unit_price" label="单价" width="80" align="right">
              <template #default="{ row }">{{ row.unit_price != null ? fmt(row.unit_price) : '—' }}</template>
            </el-table-column>
            <el-table-column label="收货金额" width="100" align="right">
              <template #default="{ row }"><b>{{ fmt(row.recv_amount) }}</b></template>
            </el-table-column>
            <el-table-column label="已开票" width="92" align="right">
              <template #default="{ row }">{{ fmt(row.invoice_amount) }}</template>
            </el-table-column>
            <el-table-column label="待开票" width="92" align="right">
              <template #default="{ row }"><span :class="{ red: row.to_invoice > 0 }">{{ fmt(row.to_invoice) }}</span></template>
            </el-table-column>
            <el-table-column label="已付款" width="92" align="right">
              <template #default="{ row }">{{ fmt(row.pay_amount) }}</template>
            </el-table-column>
            <el-table-column label="欠款" width="92" align="right">
              <template #default="{ row }"><span :class="{ red: row.owed > 0 }">{{ fmt(row.owed) }}</span></template>
            </el-table-column>
            <el-table-column label="对账" width="76" align="center">
              <template #default="{ row }">
                <el-tag :type="row.recon_status === '已对账' ? 'success' : 'info'" size="small" effect="light">{{ row.recon_status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="isManager" prop="buyer_name" label="采购员" width="80" />
            <el-table-column label="操作" width="96" fixed="right" align="center">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openItemEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="removeItem(row)">删</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <div class="totalbar">
          <span>共 <b>{{ itemList.total }}</b> 条</span>
          <div class="spacer" />
          <span>收货合计 <b>¥{{ fmt(itemList.recv_total) }}</b></span>
          <span>已开票 ¥{{ fmt(itemList.invoiced) }}</span>
          <span>待开票 <b class="amber">¥{{ fmt(itemList.to_invoice) }}</b></span>
          <span>已付款 ¥{{ fmt(itemList.paid) }}</span>
          <span>欠款 <b class="red">¥{{ fmt(itemList.owed) }}</b></span>
          <el-pagination v-if="itemList.total > itemPageSize" small layout="prev, pager, next" :total="itemList.total"
                         :page-size="itemPageSize" v-model:current-page="itemPage" @current-change="loadItems" style="margin-left:12px" />
        </div>
      </el-tab-pane>

      <!-- ============ 供应商账目一览 ============ -->
      <el-tab-pane label="供应商账目一览" name="suppliers">
        <div class="toolbar">
          <el-select v-model="supFilters.category" placeholder="分类" clearable style="width: 120px" @change="loadSuppliers">
            <el-option v-for="c in SUPPLIER_CATEGORIES" :key="c" :label="c" :value="c" />
          </el-select>
          <el-select v-model="supFilters.status" placeholder="状态" clearable style="width: 100px" @change="loadSuppliers">
            <el-option label="启用" value="active" /><el-option label="停用" value="inactive" />
          </el-select>
          <el-input v-model="supFilters.kw" placeholder="名称/联系人" clearable style="width: 160px" @keyup.enter="loadSuppliers" @clear="loadSuppliers" />
          <el-button :icon="Refresh" @click="loadSuppliers">刷新</el-button>
          <div class="spacer" />
          <el-button :icon="Download" @click="openImport">批量导入名单</el-button>
          <el-button :icon="Plus" type="primary" @click="openSupCreate">新增供应商</el-button>
        </div>

        <el-card shadow="never" body-style="padding:0">
          <el-table :data="suppliers" stripe v-loading="supLoading" size="small" max-height="calc(100vh - 330px)">
            <el-table-column type="index" label="#" width="44" />
            <el-table-column prop="name" label="公司名称" min-width="180" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="90">
              <template #default="{ row }"><el-tag v-if="row.category" size="small" effect="plain">{{ row.category }}</el-tag><span v-else class="muted">—</span></template>
            </el-table-column>
            <el-table-column prop="contact" label="联系人" width="80" />
            <el-table-column prop="phone" label="电话" width="120" />
            <el-table-column label="结算/账期" width="110">
              <template #default="{ row }">{{ row.settle_type || '—' }}<span v-if="row.settle_type === '月结' && row.settle_days">·{{ row.settle_days }}天</span></template>
            </el-table-column>
            <el-table-column label="送货单金额" width="110" align="right">
              <template #default="{ row }"><b>{{ fmt(row.recv_total) }}</b></template>
            </el-table-column>
            <el-table-column label="已开票" width="100" align="right"><template #default="{ row }">{{ fmt(row.invoiced) }}</template></el-table-column>
            <el-table-column label="待开票" width="100" align="right"><template #default="{ row }"><span :class="{ amber: row.to_invoice > 0 }">{{ fmt(row.to_invoice) }}</span></template></el-table-column>
            <el-table-column label="已付款" width="100" align="right"><template #default="{ row }">{{ fmt(row.paid) }}</template></el-table-column>
            <el-table-column label="欠款" width="100" align="right"><template #default="{ row }"><b :class="{ red: row.owed > 0 }">{{ fmt(row.owed) }}</b></template></el-table-column>
            <el-table-column label="状态" width="64" align="center">
              <template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">{{ row.status === 'active' ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="130" fixed="right" align="center">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="viewSupplierItems(row)">明细({{ row.item_count }})</el-button>
                <el-button size="small" link @click="openSupEdit(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="removeSup(row)">删</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
        <div class="totalbar">
          <span>共 <b>{{ suppliers.length }}</b> 家</span>
          <div class="spacer" />
          <span>送货单金额 <b>¥{{ fmt(supTotals.recv) }}</b></span>
          <span>待开票 <b class="amber">¥{{ fmt(supTotals.toi) }}</b></span>
          <span>欠款合计 <b class="red">¥{{ fmt(supTotals.owed) }}</b></span>
        </div>
      </el-tab-pane>

      <!-- ============ 汇总报表 ============ -->
      <el-tab-pane label="汇总报表" name="summary">
        <div class="toolbar">
          <el-select v-model="summaryYear" style="width: 110px" @change="loadSummary">
            <el-option v-for="y in yearOptions" :key="y" :label="y + '年'" :value="y" />
          </el-select>
          <el-button :icon="Refresh" :loading="sumLoading" @click="loadSummary">刷新</el-button>
        </div>
        <div v-loading="sumLoading">
          <div class="kpis">
            <div class="kpi"><div class="l">本月采购额</div><div class="v">¥{{ fmt(summary?.month_total) }}</div></div>
            <div class="kpi"><div class="l">本季采购额</div><div class="v">¥{{ fmt(summary?.quarter_total) }}</div></div>
            <div class="kpi"><div class="l">本年采购额</div><div class="v">¥{{ fmt(summary?.year_total) }}</div></div>
            <div class="kpi"><div class="l">应付总额（欠款）</div><div class="v red">¥{{ fmt(summary?.owed_total) }}</div></div>
          </div>

          <el-row :gutter="16">
            <el-col :span="14">
              <el-card shadow="never" header="月度采购额趋势（万元）">
                <div class="bars">
                  <div v-for="m in summary?.monthly || []" :key="m.key" class="bar">
                    <div class="bv">{{ m.recv > 0 ? wan(m.recv) : '' }}</div>
                    <i :style="{ height: (m.recv / maxMonthly * 100) + '%' }" />
                    <span>{{ m.key.slice(5) }}月</span>
                  </div>
                </div>
              </el-card>
            </el-col>
            <el-col :span="10">
              <el-card shadow="never" header="Top 供应商（采购额）">
                <el-table :data="summary?.top_suppliers || []" size="small" :show-header="true" max-height="300">
                  <el-table-column type="index" label="#" width="40" />
                  <el-table-column prop="key" label="供应商" show-overflow-tooltip />
                  <el-table-column label="采购额" width="110" align="right"><template #default="{ row }"><b>{{ fmt(row.recv) }}</b></template></el-table-column>
                  <el-table-column label="欠款" width="100" align="right"><template #default="{ row }"><span :class="{ red: row.owed > 0 }">{{ fmt(row.owed) }}</span></template></el-table-column>
                </el-table>
              </el-card>
            </el-col>
          </el-row>

          <el-card shadow="never" header="按采购员" style="margin-top:16px">
            <el-table :data="summary?.by_buyer || []" size="small">
              <el-table-column type="index" label="#" width="44" />
              <el-table-column prop="key" label="采购员" min-width="120" />
              <el-table-column prop="count" label="明细数" width="90" align="right" />
              <el-table-column label="采购额" width="140" align="right"><template #default="{ row }"><b>{{ fmt(row.recv) }}</b></template></el-table-column>
              <el-table-column label="已付款" width="140" align="right"><template #default="{ row }">{{ fmt(row.paid) }}</template></el-table-column>
              <el-table-column label="欠款" width="140" align="right"><template #default="{ row }"><span :class="{ red: row.owed > 0 }">{{ fmt(row.owed) }}</span></template></el-table-column>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ======= 采购明细 新增/编辑 ======= -->
    <el-dialog v-model="itemDialog" :title="editingItemId ? '编辑采购明细' : '新增采购明细'" width="720px" destroy-on-close>
      <el-form :model="itemForm" label-width="84px">
        <el-row :gutter="14">
          <el-col :span="12"><el-form-item label="供应商" required>
            <el-select v-model="itemForm.supplier_id" filterable placeholder="选择供应商" style="width:100%">
              <el-option v-for="s in supplierOptions" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
          </el-form-item></el-col>
          <el-col :span="12"><el-form-item label="送货时间"><el-date-picker v-model="itemForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="项目编号"><el-input v-model="itemForm.project_no" placeholder="如 2026-041M" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="送货单号"><el-input v-model="itemForm.delivery_no" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="名称"><el-input v-model="itemForm.item_name" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="规格型号"><el-input v-model="itemForm.spec" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="数量"><el-input-number v-model="itemForm.qty" :controls="false" style="width:100%" @change="autoRecv" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="单价"><el-input-number v-model="itemForm.unit_price" :controls="false" :precision="2" style="width:100%" @change="autoRecv" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="收货金额"><el-input-number v-model="itemForm.recv_amount" :controls="false" :precision="2" style="width:100%" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="合同编号"><el-input v-model="itemForm.contract_no" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="税率"><el-input v-model="itemForm.tax_rate" placeholder="如 13%" /></el-form-item></el-col>
        </el-row>
        <el-divider content-position="left" style="margin:4px 0 14px">开票 / 付款（也可在列表勾选后批量登记）</el-divider>
        <el-row :gutter="14">
          <el-col :span="6"><el-form-item label="开票日期" label-width="74px"><el-date-picker v-model="itemForm.invoice_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="开票金额" label-width="74px"><el-input-number v-model="itemForm.invoice_amount" :controls="false" :precision="2" style="width:100%" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="付款日期" label-width="74px"><el-date-picker v-model="itemForm.pay_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="付款金额" label-width="74px"><el-input-number v-model="itemForm.pay_amount" :controls="false" :precision="2" style="width:100%" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="对账状态" label-width="74px">
            <el-select v-model="itemForm.recon_status" style="width:100%"><el-option v-for="s in RECON_STATUSES" :key="s" :label="s" :value="s" /></el-select>
          </el-form-item></el-col>
          <el-col :span="18"><el-form-item label="备注" label-width="74px"><el-input v-model="itemForm.note" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="itemDialog = false">取消</el-button>
        <el-button type="primary" @click="saveItem">保存</el-button>
      </template>
    </el-dialog>

    <!-- ======= 供应商 新增/编辑 ======= -->
    <el-dialog v-model="supDialog" :title="editingSupId ? '编辑供应商' : '新增供应商'" width="640px" destroy-on-close>
      <el-form :model="supForm" label-width="84px">
        <el-row :gutter="14">
          <el-col :span="14"><el-form-item label="公司名称" required><el-input v-model="supForm.name" /></el-form-item></el-col>
          <el-col :span="10"><el-form-item label="分类">
            <el-select v-model="supForm.category" clearable style="width:100%"><el-option v-for="c in SUPPLIER_CATEGORIES" :key="c" :label="c" :value="c" /></el-select>
          </el-form-item></el-col>
          <el-col :span="12"><el-form-item label="联系人"><el-input v-model="supForm.contact" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="电话"><el-input v-model="supForm.phone" /></el-form-item></el-col>
          <el-col :span="10"><el-form-item label="结算方式">
            <el-select v-model="supForm.settle_type" style="width:100%"><el-option v-for="s in SETTLE_TYPES" :key="s" :label="s" :value="s" /></el-select>
          </el-form-item></el-col>
          <el-col :span="8"><el-form-item label="账期天数"><el-input-number v-model="supForm.settle_days" :controls="false" :min="0" style="width:100%" :disabled="supForm.settle_type !== '月结'" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="状态">
            <el-select v-model="supForm.status" style="width:100%"><el-option label="启用" value="active" /><el-option label="停用" value="inactive" /></el-select>
          </el-form-item></el-col>
          <el-col :span="24"><el-form-item label="地址"><el-input v-model="supForm.address" /></el-form-item></el-col>
          <el-col :span="14"><el-form-item label="税号"><el-input v-model="supForm.tax_no" /></el-form-item></el-col>
          <el-col :span="10"><el-form-item label="开户行"><el-input v-model="supForm.bank_name" /></el-form-item></el-col>
          <el-col :span="14"><el-form-item label="银行账号"><el-input v-model="supForm.bank_account" /></el-form-item></el-col>
          <el-col :span="24"><el-form-item label="备注"><el-input v-model="supForm.note" type="textarea" :rows="2" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="supDialog = false">取消</el-button>
        <el-button type="primary" @click="saveSup">保存</el-button>
      </template>
    </el-dialog>

    <!-- ======= 批量导入供应商名单 ======= -->
    <el-dialog v-model="importDialog" title="批量导入供应商名单" width="480px" destroy-on-close>
      <el-form label-width="72px">
        <el-form-item label="默认分类">
          <el-select v-model="importCategory" clearable placeholder="可不选" style="width:100%"><el-option v-for="c in SUPPLIER_CATEGORIES" :key="c" :label="c" :value="c" /></el-select>
        </el-form-item>
        <el-form-item label="名单">
          <el-input v-model="importText" type="textarea" :rows="10" placeholder="每行一个公司名称，已存在的自动跳过" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importDialog = false">取消</el-button>
        <el-button type="primary" @click="doImport">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.page-header h1 { margin: 0; font-size: 20px; }
.page-header .desc { color: var(--el-text-color-secondary); font-size: 13px; margin-top: 2px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.spacer { flex: 1; }
.muted { color: var(--el-text-color-secondary); }
.red { color: #dc2626; }
.amber { color: #d97706; }
.totalbar { display: flex; align-items: center; gap: 16px; padding: 8px 12px; font-size: 13px;
  color: var(--el-text-color-regular); background: var(--el-fill-color-light); border-radius: 0 0 8px 8px; }
.totalbar b { color: var(--el-text-color-primary); }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
.kpi { background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 10px; padding: 14px 16px; }
.kpi .l { color: var(--el-text-color-secondary); font-size: 13px; }
.kpi .v { font-size: 24px; font-weight: 600; margin-top: 4px; }
.kpi .v.red { color: #dc2626; }
.bars { display: flex; align-items: flex-end; gap: 8px; height: 200px; padding-top: 10px; }
.bars .bar { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; gap: 3px; }
.bars .bar i { display: block; width: 62%; min-height: 2px; background: linear-gradient(180deg, #60a5fa, #2563eb); border-radius: 4px 4px 0 0; transition: height .3s; }
.bars .bar span { font-size: 11px; color: var(--el-text-color-secondary); }
.bars .bar .bv { font-size: 10px; color: var(--el-text-color-secondary); height: 14px; }
</style>
