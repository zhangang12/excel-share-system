<script setup lang="ts">
// 🆕 v3 M09 财务部：待开票 / 已开票 / 售后费用 三 tab
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
onMounted(load)

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
      <el-tabs v-model="tab">
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
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
</style>
