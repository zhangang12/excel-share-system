<script setup lang="ts">
// 🆕 v3 M09 财务部：待开票 / 已开票 / 售后费用 / 请款审批 四 tab
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { http } from '@/api'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/api/sales'
import EmptyHint from '@/components/EmptyHint.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const isManager = computed(() => auth.hasRole('admin', 'manager'))

interface PaymentRequestOut {
  id: number; supplier_id: number; supplier_name: string
  requested_amount: number; requester_id: number; requester_name: string
  status: string; notes?: string | null
  finance_approver_id?: number | null; approver_name?: string | null; approved_at?: string | null
  paid_amount?: number | null; paid_date?: string | null; payment_method?: string | null
  pay_voucher_file_id?: number | null; pay_voucher_name?: string | null
  reject_reason?: string | null; created_at: string
  items: { item_id: number; allocated_amount: number; item_name?: string }[]
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
interface AsRow {
  id: number; code: string; name: string; problem: string; cost: number
  mat_file_id?: number | null; mat_file_name?: string | null
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
const invValue = ref<{ total_value: number; rows: any[] }>({ total_value: 0, rows: [] })
const projCost = ref<{ code: string; name: string; cost: number }[]>([])
const invLoading = ref(false)
async function loadInventory() {
  invLoading.value = true
  try {
    const [iv, pc] = await Promise.all([
      http.get<{ total_value: number; rows: any[] }>('/wh/inventory-value').then(r => r.data),
      http.get<{ rows: any[] }>('/wh/project-cost').then(r => r.data),
    ])
    invValue.value = iv; projCost.value = pc.rows || []
  } finally { invLoading.value = false }
}
function onFinTab(name: string) {
  if (name === 'payables') loadPayables()
  if (name === 'inventory') loadInventory()
}

// 请款审批
const prStatus = ref('pending')
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
    const r = await http.get<PaymentRequestOut[]>('/finance/payment-requests', { params: { status: prStatus.value } })
    payReqs.value = r.data
  } finally { prLoading.value = false }
}

async function approvePayReq(id: number) {
  try {
    await ElMessageBox.confirm('确认审批通过此请款申请？', '审批确认', { type: 'info' })
  } catch { return }
  await http.put(`/purchase-mgmt/payment-requests/${id}/approve`)
  ElMessage.success('已审批通过')
  await loadPayReqs()
}

function openReject(id: number) {
  rejectTargetId.value = id
  rejectReason.value = ''
  rejectDialogVisible.value = true
}

async function submitReject() {
  if (!rejectTargetId.value) return
  await http.put(`/purchase-mgmt/payment-requests/${rejectTargetId.value}/reject`, { reason: rejectReason.value })
  ElMessage.success('已拒绝请款申请')
  rejectDialogVisible.value = false
  await loadPayReqs()
}

const payVoucherFile = ref<File | null>(null)
function openPay(pr: PaymentRequestOut) {
  payTargetId.value = pr.id
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
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-tabs v-model="tab" @tab-change="onFinTab">
        <el-tab-pane :label="`📥 待开票 (${pendingView.length})`" name="pending">
          <el-table :data="pendingView" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
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
            <el-table-column label="操作" width="200">
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

        <el-tab-pane :label="`✅ 已开票 (${invoicedView.length})`" name="invoiced">
          <el-table :data="invoicedView" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
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
            <el-table-column label="操作" width="110">
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

        <el-tab-pane :label="`🛎️ 售后费用 (${aftersales.length})`" name="aftersales">
          <el-table :data="aftersales" stripe show-summary :summary-method="() => ['合计', '', '', fmtMoney(asTotal), '']" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column type="index" label="#" width="50" />
            <el-table-column label="项目编号" width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="项目名称" min-width="140" />
            <el-table-column prop="problem" label="售后问题" min-width="200" show-overflow-tooltip />
            <el-table-column label="售后费用" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.cost) }}</template></el-table-column>
            <el-table-column label="售后物料清单" min-width="140">
              <template #default="{ row }">
                <el-button v-if="row.mat_file_id" size="small" link type="primary"
                           @click="downloadAttachment({ id: row.mat_file_id, name: row.mat_file_name || '物料清单' })">
                  {{ row.mat_file_name }}
                </el-button>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="isManager" label="操作" width="80">
              <template #default="{ row }">
                <el-button size="small" link type="danger" @click="voidAfterSales(row)">作废</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!aftersales.length" text="暂无已审批售后费用（售后部审批后自动同步）" />
        </el-tab-pane>

        <el-tab-pane label="💰 请款审批" name="pay_requests">
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px">
            <el-select v-model="prStatus" style="width:130px" @change="loadPayReqs">
              <el-option value="pending" label="待审批" />
              <el-option value="approved" label="已审批" />
              <el-option value="paid" label="已付款" />
              <el-option value="rejected" label="已拒绝" />
              <el-option value="all" label="全部" />
            </el-select>
            <el-button @click="loadPayReqs" :loading="prLoading">刷新</el-button>
          </div>
          <el-table :data="payReqs" stripe v-loading="prLoading" max-height="calc(100vh - 280px)" :scrollbar-always-on="true">
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
            <el-table-column prop="notes" label="备注" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.notes || '—' }}</template>
            </el-table-column>
            <el-table-column label="付款信息" min-width="170">
              <template #default="{ row }">
                <template v-if="row.status === 'paid'">
                  <div>{{ fmtMoney(row.paid_amount) }} · {{ row.paid_date }} · {{ row.payment_method }}</div>
                  <el-button v-if="row.pay_voucher_file_id" size="small" link type="primary"
                             @click="downloadAttachment({ id: row.pay_voucher_file_id!, name: row.pay_voucher_name || '付款凭证' })">
                    📎 付款凭证
                  </el-button>
                </template>
                <span v-else-if="row.reject_reason" class="muted">拒绝：{{ row.reject_reason }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="申请时间" width="110">
              <template #default="{ row }">{{ row.created_at?.slice(0, 10) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="180">
              <template #default="{ row }">
                <template v-if="row.status === 'pending'">
                  <el-button size="small" type="primary" @click="approvePayReq(row.id)">审批通过</el-button>
                  <el-button size="small" type="danger" link @click="openReject(row.id)">拒绝</el-button>
                </template>
                <template v-else-if="row.status === 'approved'">
                  <el-button size="small" type="success" @click="openPay(row)">记录付款</el-button>
                </template>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!payReqs.length" text="暂无请款申请" />
        </el-tab-pane>

        <!-- 🆕 采购应付 -->
        <el-tab-pane label="📄 采购应付" name="payables">
          <div class="summary-bar" style="margin-bottom:10px">
            <span>应付合计 <b class="danger">{{ fmtMoney(payablesTotal) }}</b></span>
            <span class="muted small">已收货未付款 = 对供应商的应付;从「请款审批」页付款</span>
          </div>
          <el-table :data="payables" v-loading="payablesLoading" stripe size="small"
                    max-height="calc(100vh - 300px)" :scrollbar-always-on="true" class="wrap-cells">
            <el-table-column prop="supplier_name" label="供应商" min-width="180" />
            <el-table-column prop="category" label="分类" width="90"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
            <el-table-column label="收货合计" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.received_total) }}</template></el-table-column>
            <el-table-column label="开票合计" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.invoice_total) }}</template></el-table-column>
            <el-table-column label="已付款" width="120" align="right"><template #default="{ row }">{{ fmtMoney(row.paid_total) }}</template></el-table-column>
            <el-table-column label="应付余额" width="120" align="right"><template #default="{ row }"><b class="danger">{{ fmtMoney(row.outstanding) }}</b></template></el-table-column>
            <el-table-column prop="item_count" label="明细数" width="80" align="center" />
          </el-table>
          <EmptyHint v-if="!payablesLoading && !payables.length" text="暂无采购应付" />
        </el-tab-pane>

        <!-- 🆕 库存 / 成本 -->
        <el-tab-pane label="📦 库存 / 成本" name="inventory">
          <div class="summary-bar" style="margin-bottom:10px">
            <span>库存总金额 <b class="amt">{{ fmtMoney(invValue.total_value) }}</b></span>
            <span class="muted small">库存金额 = 现存 × 入库加权平均单价;项目成本 = 领料出库 × 单价</span>
          </div>
          <el-row :gutter="16">
            <el-col :span="14">
              <div class="section-title">库存金额（按物料）</div>
              <el-table :data="invValue.rows" v-loading="invLoading" stripe size="small" max-height="calc(100vh - 340px)" class="wrap-cells">
                <el-table-column prop="name" label="物料" min-width="140" />
                <el-table-column prop="spec" label="规格" min-width="110"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
                <el-table-column label="现存" width="80" align="right"><template #default="{ row }">{{ row.stock }}</template></el-table-column>
                <el-table-column label="均价" width="100" align="right"><template #default="{ row }">{{ row.avg_price != null ? fmtMoney(row.avg_price) : '—' }}</template></el-table-column>
                <el-table-column label="金额" width="120" align="right"><template #default="{ row }"><b>{{ row.value != null ? fmtMoney(row.value) : '—' }}</b></template></el-table-column>
              </el-table>
            </el-col>
            <el-col :span="10">
              <div class="section-title">项目材料成本</div>
              <el-table :data="projCost" v-loading="invLoading" stripe size="small" max-height="calc(100vh - 340px)" class="wrap-cells">
                <el-table-column label="项目" min-width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b> {{ row.name }}</template></el-table-column>
                <el-table-column label="材料成本" width="130" align="right"><template #default="{ row }"><b>{{ fmtMoney(row.cost) }}</b></template></el-table-column>
              </el-table>
              <EmptyHint v-if="!invLoading && !projCost.length" text="暂无项目领料成本" size="sm" />
            </el-col>
          </el-row>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 拒绝原因弹窗 -->
    <el-dialog v-model="rejectDialogVisible" title="拒绝请款申请" width="420px">
      <el-form label-width="80px">
        <el-form-item label="拒绝原因">
          <el-input v-model="rejectReason" type="textarea" :rows="3" placeholder="请填写拒绝原因（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rejectDialogVisible = false">取消</el-button>
        <el-button type="danger" @click="submitReject">确认拒绝</el-button>
      </template>
    </el-dialog>

    <!-- 记录付款弹窗 -->
    <el-dialog v-model="payDialogVisible" title="记录付款" width="480px">
      <el-form :model="payForm" label-width="90px">
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
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.small { font-size: 12px; }
.summary-bar { display: flex; gap: 24px; align-items: center; padding: 10px 16px; background: var(--el-fill-color-light); border-radius: 6px; font-size: 14px; }
.section-title { font-weight: 600; font-size: 14px; margin: 4px 0 8px; color: var(--el-text-color-primary); }
.danger { color: var(--el-color-danger); }
.amt { color: var(--el-color-primary); }
</style>
