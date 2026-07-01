<script setup lang="ts">
// 🆕 采购管理：采购明细 / 供应商账目一览 / 汇总报表
import { ref, computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasRole('buyer', 'buyer_lead', 'admin', 'manager'))
const isLeadOrAbove = computed(() => auth.hasRole('buyer_lead', 'finance', 'admin', 'manager'))

function fmtMoney(v: number | undefined | null) {
  if (v == null) return '¥0'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// ===== types =====
interface SupplierOut {
  id: number; name: string; code?: string | null; category?: string | null
  contact?: string | null; phone?: string | null; address?: string | null
  tax_no?: string | null; bank_name?: string | null; bank_account?: string | null
  settlement_type?: string | null; credit_days?: number | null
  status: string; notes?: string | null; created_at: string
}
interface PurchaseItemOut {
  id: number; supplier_id: number; supplier_name: string
  delivery_date?: string | null; contract_no?: string | null
  project_code?: string | null; delivery_note_no?: string | null
  item_name: string; spec?: string | null; qty?: number | null; unit_price?: number | null
  received_amount: number; invoice_date?: string | null; tax_rate?: string | null
  invoice_amount: number; paid_amount: number; paid_date?: string | null
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

// ===== state =====
const tab = ref('items')
const loading = ref(false)

// items tab
const items = ref<PurchaseItemOut[]>([])
const itemSummary = ref<ItemSummary | null>(null)
const selectedItems = ref<PurchaseItemOut[]>([])
const filterSupplierId = ref<number | ''>('')
const filterProjectCode = ref('')
const filterMonth = ref('')
const filterInvoiceStatus = ref('')

// suppliers & statements
const suppliers = ref<SupplierOut[]>([])
const statementData = ref<StatementList | null>(null)
const drawerVisible = ref(false)
const drawerSupplier = ref<SupplierStatementRow | null>(null)
const drawerItems = ref<PurchaseItemOut[]>([])
const drawerLoading = ref(false)

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
  invoice_status: '待对账', notes: '',
})

const supplierDialogVisible = ref(false)
const editingSupplier = ref<SupplierOut | null>(null)
const supplierForm = reactive({
  name: '', code: '', category: '', contact: '', phone: '', address: '',
  tax_no: '', bank_name: '', bank_account: '', settlement_type: '', credit_days: null as number | null,
  notes: '',
})

const batchInvoiceVisible = ref(false)
const batchInvoiceForm = reactive({ invoice_date: '', invoice_amount: null as number | null })

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
  await loadItems()
})

async function onTabChange(name: string) {
  if (name === 'statements') { await loadSuppliers(); await loadStatements() }
  else if (name === 'reports' && isLeadOrAbove.value) { await loadReports(); await loadProjectReport() }
}

// ===== item CRUD =====
function openNewItem() {
  editingItem.value = null
  Object.assign(itemForm, {
    supplier_id: '', delivery_date: '', contract_no: '', project_code: '',
    delivery_note_no: '', item_name: '', spec: '', qty: null, unit_price: null,
    received_amount: 0, invoice_date: '', tax_rate: '', invoice_amount: 0,
    invoice_status: '待对账', notes: '',
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

// ===== batch invoice =====
function openBatchInvoice() {
  if (!selectedItems.value.length) { ElMessage.warning('请先勾选明细行'); return }
  batchInvoiceForm.invoice_date = ''
  batchInvoiceForm.invoice_amount = null
  batchInvoiceVisible.value = true
}

async function doBatchInvoice() {
  try {
    await http.post('/purchase-mgmt/items/batch-invoice', {
      item_ids: selectedItems.value.map(i => i.id),
      invoice_date: batchInvoiceForm.invoice_date || null,
      invoice_amount: batchInvoiceForm.invoice_amount,
    })
    ElMessage.success(`已标记 ${selectedItems.value.length} 条为已开票`)
    batchInvoiceVisible.value = false
    selectedItems.value = []
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

// ===== opening balance =====
function openOpeningBalance(row: SupplierStatementRow) {
  openingBalanceSupplierId.value = row.supplier_id
  openingBalanceSupplierName.value = row.supplier_name
  openingBalanceForm.balance_date = ''
  openingBalanceForm.outstanding_amount = row.opening_balance
  openingBalanceForm.notes = ''
  openingBalanceVisible.value = true
  // try to load existing
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

// ===== invoice status label =====
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
        <div class="desc">采购明细录入 · 供应商账目一览 · 请款流程 · 汇总报表</div>
      </div>
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-tabs v-model="tab" @tab-change="onTabChange">

        <!-- ==================== Tab 1: 采购明细 ==================== -->
        <el-tab-pane label="📦 采购明细" name="items">
          <!-- 筛选栏 -->
          <div class="filter-bar">
            <el-select v-model="filterSupplierId" placeholder="全部供应商" clearable style="width:160px" @change="loadItems">
              <el-option v-for="s in suppliers" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
            <el-input v-model="filterProjectCode" placeholder="项目编号" clearable style="width:130px" @change="loadItems" />
            <el-input v-model="filterMonth" placeholder="月份 YYYY-MM" clearable style="width:140px" @change="loadItems" />
            <el-select v-model="filterInvoiceStatus" placeholder="对账状态" clearable style="width:120px" @change="loadItems">
              <el-option label="待对账" value="待对账" />
              <el-option label="已对账" value="已对账" />
              <el-option label="已开票" value="已开票" />
            </el-select>
            <el-button @click="loadItems">刷新</el-button>
            <template v-if="canWrite">
              <el-button type="primary" @click="openNewItem">+ 新增明细</el-button>
              <el-button :disabled="!selectedItems.length" @click="openBatchInvoice">批量开票</el-button>
              <el-button :disabled="!selectedItems.length" type="warning" @click="openPaymentRequest">发起请款</el-button>
            </template>
          </div>

          <!-- 明细表 -->
          <el-table
            :data="items" stripe
            @selection-change="(v: PurchaseItemOut[]) => selectedItems = v"
            max-height="calc(100vh - 340px)"
            :scrollbar-always-on="true"
            show-overflow-tooltip
          >
            <el-table-column v-if="canWrite" type="selection" width="40" />
            <el-table-column label="供应商" min-width="110">
              <template #default="{ row }"><span class="sup-name">{{ row.supplier_name }}</span></template>
            </el-table-column>
            <el-table-column prop="delivery_date" label="送货日期" width="95" />
            <el-table-column prop="project_code" label="项目编号" width="100">
              <template #default="{ row }"><b class="code">{{ row.project_code || '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="delivery_note_no" label="送货单号" width="100">
              <template #default="{ row }">{{ row.delivery_note_no || '—' }}</template>
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
            <el-table-column v-if="canWrite" label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link @click="openEditItem(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="deleteItem(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <!-- 汇总栏 -->
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
          <div class="filter-bar" v-if="canWrite">
            <el-button type="primary" @click="openNewSupplier">+ 新增供应商</el-button>
          </div>
          <el-table
            :data="statementData?.rows || []"
            stripe show-summary
            :summary-method="() => {
              const d = statementData
              return ['合计', '', '',
                fmtMoney(d?.total_opening || 0),
                fmtMoney(d?.total_received || 0), '','',
                fmtMoney(d?.total_paid || 0),
                fmtMoney(d?.total_outstanding || 0), '', '', '']
            }"
            max-height="calc(100vh - 280px)"
            :scrollbar-always-on="true"
          >
            <el-table-column prop="supplier_name" label="供应商" min-width="130" />
            <el-table-column prop="category" label="分类" width="90">
              <template #default="{ row }">{{ row.category || '—' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="75">
              <template #default="{ row }">
                <el-tag :type="suppliers.find(s=>s.id===row.supplier_id)?.status==='active'?'success':'info'" size="small">
                  {{ suppliers.find(s=>s.id===row.supplier_id)?.status==='active' ? '启用' : '停用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="期初欠款" width="110" align="right">
              <template #default="{ row }">{{ fmtMoney(row.opening_balance) }}</template>
            </el-table-column>
            <el-table-column label="收货合计" width="110" align="right">
              <template #default="{ row }"><b>{{ fmtMoney(row.received_total) }}</b></template>
            </el-table-column>
            <el-table-column label="开票合计" width="110" align="right">
              <template #default="{ row }">{{ fmtMoney(row.invoice_total) }}</template>
            </el-table-column>
            <el-table-column label="待开票" width="105" align="right">
              <template #default="{ row }"><span class="warn">{{ fmtMoney(row.uninvoiced) }}</span></template>
            </el-table-column>
            <el-table-column label="已付款" width="110" align="right">
              <template #default="{ row }">{{ fmtMoney(row.paid_total) }}</template>
            </el-table-column>
            <el-table-column label="欠款余额" width="110" align="right">
              <template #default="{ row }"><b class="danger">{{ fmtMoney(row.outstanding) }}</b></template>
            </el-table-column>
            <el-table-column label="明细数" width="70" align="center">
              <template #default="{ row }">{{ row.item_count }}</template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openDrawer(row)">查看明细</el-button>
                <template v-if="canWrite">
                  <el-button size="small" link @click="openEditSupplier(suppliers.find(s=>s.id===row.supplier_id)!)">编辑</el-button>
                  <el-button size="small" link @click="openOpeningBalance(row)">期初</el-button>
                  <el-button size="small" link type="warning" @click="toggleSupplier(row)">
                    {{ suppliers.find(s=>s.id===row.supplier_id)?.status==='active' ? '停用' : '启用' }}
                  </el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Tab 3: 汇总报表 ==================== -->
        <el-tab-pane v-if="isLeadOrAbove" label="📈 汇总报表" name="reports">
          <!-- KPI 卡片 -->
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
            <!-- 月度趋势 -->
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

            <!-- Top 供应商 -->
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
            <!-- 按采购员 -->
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

            <!-- 按项目 -->
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

    <!-- ==================== 采购明细弹窗 ==================== -->
    <el-dialog v-model="itemDialogVisible" :title="editingItem ? '编辑采购明细' : '新增采购明细'" width="680px">
      <el-form :model="itemForm" label-width="90px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="供应商" required>
              <el-select v-model="itemForm.supplier_id" placeholder="选择供应商" style="width:100%">
                <el-option v-for="s in suppliers.filter(x=>x.status==='active')" :key="s.id" :label="s.name" :value="s.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="送货日期">
              <el-date-picker v-model="itemForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="名称" required>
              <el-input v-model="itemForm.item_name" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="规格型号">
              <el-input v-model="itemForm.spec" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="项目编号">
              <el-input v-model="itemForm.project_code" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="送货单号">
              <el-input v-model="itemForm.delivery_note_no" />
            </el-form-item>
          </el-col>
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
            <el-form-item label="收货金额">
              <el-input-number v-model="itemForm.received_amount" :precision="2" :min="0" style="width:100%" />
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
              <el-select v-model="itemForm.tax_rate" style="width:100%" clearable>
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
          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="itemForm.notes" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="itemDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveItem">保存</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 批量开票弹窗 ==================== -->
    <el-dialog v-model="batchInvoiceVisible" title="批量标记开票" width="400px">
      <el-alert :title="`已选 ${selectedItems.length} 条明细`" type="info" :closable="false" style="margin-bottom:16px" />
      <el-form :model="batchInvoiceForm" label-width="90px">
        <el-form-item label="开票日期">
          <el-date-picker v-model="batchInvoiceForm.invoice_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
        </el-form-item>
        <el-form-item v-if="selectedItems.length === 1" label="开票金额">
          <el-input-number v-model="batchInvoiceForm.invoice_amount" :precision="2" :min="0" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="batchInvoiceVisible = false">取消</el-button>
        <el-button type="primary" @click="doBatchInvoice">确认开票</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 请款弹窗 ==================== -->
    <el-dialog v-model="payReqVisible" title="发起请款" width="500px">
      <el-form :model="payReqForm" label-width="90px">
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
    <el-dialog v-model="supplierDialogVisible" :title="editingSupplier ? '编辑供应商' : '新增供应商'" width="600px">
      <el-form :model="supplierForm" label-width="90px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="供应商名称" required>
              <el-input v-model="supplierForm.name" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="编码">
              <el-input v-model="supplierForm.code" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分类">
              <el-select v-model="supplierForm.category" style="width:100%" clearable allow-create filterable>
                <el-option label="外协" value="外协" />
                <el-option label="标准件" value="标准件" />
                <el-option label="不锈钢" value="不锈钢" />
                <el-option label="激光" value="激光" />
                <el-option label="电气" value="电气" />
                <el-option label="运输" value="运输" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="结算方式">
              <el-select v-model="supplierForm.settlement_type" style="width:100%" clearable>
                <el-option label="现金" value="现金" />
                <el-option label="月结" value="月结" />
                <el-option label="无账期" value="无账期" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12" v-if="supplierForm.settlement_type === '月结'">
            <el-form-item label="账期天数">
              <el-input-number v-model="supplierForm.credit_days" :min="0" :max="365" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="联系人">
              <el-input v-model="supplierForm.contact" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="电话">
              <el-input v-model="supplierForm.phone" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="地址">
              <el-input v-model="supplierForm.address" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="税号">
              <el-input v-model="supplierForm.tax_no" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
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
        <el-button @click="supplierDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSupplier">保存</el-button>
      </template>
    </el-dialog>

    <!-- ==================== 期初余额弹窗 ==================== -->
    <el-dialog v-model="openingBalanceVisible" :title="`录入期初余额 — ${openingBalanceSupplierName}`" width="400px">
      <el-form :model="openingBalanceForm" label-width="90px">
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
      <el-table v-loading="drawerLoading" :data="drawerItems" stripe size="small" max-height="calc(100vh - 180px)">
        <el-table-column prop="delivery_date" label="送货日期" width="95" />
        <el-table-column prop="project_code" label="项目" width="90">
          <template #default="{ row }"><b class="code">{{ row.project_code || '—' }}</b></template>
        </el-table-column>
        <el-table-column prop="item_name" label="名称" min-width="120" />
        <el-table-column prop="spec" label="规格" min-width="90">
          <template #default="{ row }">{{ row.spec || '—' }}</template>
        </el-table-column>
        <el-table-column label="收货金额" width="105" align="right">
          <template #default="{ row }"><b>{{ fmtMoney(row.received_amount) }}</b></template>
        </el-table-column>
        <el-table-column label="开票金额" width="105" align="right">
          <template #default="{ row }">{{ row.invoice_amount ? fmtMoney(row.invoice_amount) : '—' }}</template>
        </el-table-column>
        <el-table-column label="已付款" width="105" align="right">
          <template #default="{ row }">{{ row.paid_amount ? fmtMoney(row.paid_amount) : '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.invoice_status)" size="small">{{ row.invoice_status }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
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
</style>
