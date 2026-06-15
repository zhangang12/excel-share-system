<script setup lang="ts">
// 🆕 v3 M09 财务部：待开票 / 已开票 / 售后费用 三 tab
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { http } from '@/api'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/api/sales'
import EmptyHint from '@/components/EmptyHint.vue'

interface InvoiceRow {
  ledger_id: number; code: string; name: string; customer?: string | null
  sales_name?: string | null; amount: number; tax_rate?: string | null
  apply_file_id?: number | null; apply_file_name?: string | null
  invoice_file_id?: number | null; invoice_file_name?: string | null
}
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

async function uploadInvoice(row: InvoiceRow) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.pdf,.jpg,.jpeg,.png,.ofd'
  input.onchange = async () => {
    const f = input.files?.[0]
    if (!f) return
    const fd = new FormData(); fd.append('file', f)
    await http.post(`/sales/ledger/${row.ledger_id}/invoice-upload`, fd)
    ElMessage.success('发票已上传，已回传销售订单')
    await load()
    tab.value = 'invoiced'
  }
  input.click()
}

// #2 财务开票纠错：作废原发票退回待开票，可重新上传正确发票
async function revokeInvoice(row: InvoiceRow) {
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
        <el-tab-pane :label="`📥 待开票 (${pending.length})`" name="pending">
          <el-table :data="pending" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="设备名称" min-width="150" />
            <el-table-column prop="customer" label="客户单位" min-width="120"><template #default="{ row }">{{ row.customer || '—' }}</template></el-table-column>
            <el-table-column prop="sales_name" label="销售" width="90"><template #default="{ row }">{{ row.sales_name || '—' }}</template></el-table-column>
            <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column prop="tax_rate" label="税票" width="70"><template #default="{ row }">{{ row.tax_rate || '—' }}</template></el-table-column>
            <el-table-column label="开票申请表" min-width="130">
              <template #default="{ row }">
                <el-button v-if="row.apply_file_id" size="small" link type="primary"
                           @click="downloadAttachment({ id: row.apply_file_id, name: row.apply_file_name || '申请表' })">
                  {{ row.apply_file_name }}
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button size="small" type="primary" :icon="UploadFilled" @click="uploadInvoice(row)">上传发票</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!pending.length" text="暂无待开票" />
        </el-tab-pane>

        <el-tab-pane :label="`✅ 已开票 (${invoiced.length})`" name="invoiced">
          <el-table :data="invoiced" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目编号" width="120"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
            <el-table-column prop="name" label="设备名称" min-width="150" />
            <el-table-column prop="sales_name" label="销售" width="90"><template #default="{ row }">{{ row.sales_name || '—' }}</template></el-table-column>
            <el-table-column label="金额" width="110" align="right"><template #default="{ row }">{{ fmtMoney(row.amount) }}</template></el-table-column>
            <el-table-column label="发票" min-width="150">
              <template #default="{ row }">
                <el-button v-if="row.invoice_file_id" size="small" link type="success"
                           @click="downloadAttachment({ id: row.invoice_file_id, name: row.invoice_file_name || '发票' })">
                  📎 {{ row.invoice_file_name }}
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }">
                <el-button size="small" link type="warning" @click="revokeInvoice(row)">作废重开</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!invoiced.length" text="暂无已开票" />
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
          </el-table>
          <EmptyHint v-if="!aftersales.length" text="暂无已审批售后费用（售后部审批后自动同步）" />
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
</style>
