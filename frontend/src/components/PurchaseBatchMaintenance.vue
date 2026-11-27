<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/api'
import { moneyParser } from '@/utils/money'

type Mode = 'reconcile' | 'invoice'
interface Selection {
  item_ids: number[]
  orders: { supplier_id: number; po_no: string }[]
}
interface Group {
  supplier_id: number
  supplier_name: string
  po_no: string | null
  whole_order: boolean
  item_ids: number[]
  item_names: string[]
  received_amount: number
  pending_count: number
  invoice_no: string | null
  invoice_date: string | null
  invoice_amount: number
  revision: string
}
interface Draft extends Group {
  numberInput: string
  dateInput: string
  amountInput: number | null
}

const emit = defineEmits<{ saved: [] }>()
const visible = ref(false)
const loading = ref(false)
const saving = ref(false)
const mode = ref<Mode>('reconcile')
const rows = ref<Draft[]>([])
const error = ref('')
const stale = ref(false)
let selection: Selection
let lastRequest = 0
const invoiceMode = computed(() => mode.value === 'invoice')
const title = computed(() => invoiceMode.value ? '批量维护发票' : '批量对账')
const pendingCount = computed(() => rows.value.reduce((sum, row) => sum + row.pending_count, 0))
const detailCount = computed(() => rows.value.reduce((sum, row) => sum + row.item_ids.length, 0))
const orderCount = computed(() => new Set(rows.value.map(row =>
  row.po_no ? JSON.stringify([row.supplier_id, row.po_no]) : 'item-' + row.item_ids[0])).size)
const money = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })

function startingAmount(row: Group) {
  return row.invoice_no || row.invoice_amount ? row.invoice_amount : row.received_amount
}
function isChanged(row: Draft) {
  return row.numberInput.trim() !== (row.invoice_no ?? '') || (row.dateInput || '') !== (row.invoice_date ?? '') ||
    row.amountInput !== startingAmount(row)
}
const edited = computed(() => rows.value.filter(isChanged))
const recordsDifference = (row: Draft) => row.item_ids.length > 1 && row.amountInput != null &&
  row.amountInput !== row.received_amount && row.amountInput !== row.invoice_amount
const complete = (row: Draft) => !!row.numberInput.trim() && !!row.dateInput &&
  row.amountInput != null && Number.isFinite(row.amountInput) && row.amountInput >= 0
const canSave = computed(() => !loading.value && !saving.value && !stale.value &&
  (invoiceMode.value ? edited.value.length > 0 && edited.value.every(complete) : pendingCount.value > 0))

async function preview() {
  const request = ++lastRequest
  loading.value = true
  error.value = ''
  stale.value = false
  rows.value = []
  try {
    const { data } = await http.post<{ groups: Group[] }>('/purchase-mgmt/items/batch-maintenance-preview', selection)
    if (request !== lastRequest) return
    rows.value = data.groups.map(group => ({
      ...group, numberInput: group.invoice_no ?? '', dateInput: group.invoice_date ?? '',
      amountInput: startingAmount(group),
    }))
  } catch {
    if (request === lastRequest) error.value = '未能读取所选采购单，请重试。'
  } finally {
    if (request === lastRequest) loading.value = false
  }
}
function open(action: Mode, selected: Selection) {
  mode.value = action
  selection = selected
  visible.value = true
  void preview()
}
defineExpose({ open })

async function save() {
  if (!canSave.value) return
  saving.value = true
  error.value = ''
  const issuing = invoiceMode.value
  const groups = issuing
    ? edited.value.map(row => ({
      item_ids: row.item_ids, revision: row.revision,
      invoice_no: row.numberInput.trim(), invoice_date: row.dateInput, invoice_amount: row.amountInput,
    }))
    : rows.value.filter(row => row.pending_count > 0).map(row => ({ item_ids: row.item_ids, revision: row.revision }))
  try {
    const path = issuing ? 'batch-maintain-invoices' : 'batch-reconcile'
    const { data } = await http.post<{ updated: number; skipped: number }>('/purchase-mgmt/items/' + path, { groups })
    ElMessage.success(issuing ? `已保存 ${data.updated} 条明细的发票信息` : `已完成 ${data.updated} 条明细的对账`)
    visible.value = false
    emit('saved')
  } catch (e: any) {
    stale.value = e.response?.status === 409
    const detail = e.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : '保存失败，请核对输入后重试。'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" :width="invoiceMode ? 'min(1100px, 95vw)' : 'min(850px, 95vw)'"
    top="8vh" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
    <div v-loading="loading" class="maintenance-body">
      <template v-if="rows.length">
        <div class="summary">
          <strong>{{ orderCount }} 张采购单 · {{ detailCount }} 条明细</strong>
          <span v-if="invoiceMode">逐行填写票号、日期和金额，保存已编辑的行。</span>
          <span v-else>待对账 <b>{{ pendingCount }}</b> 条，已对账 {{ detailCount - pendingCount }} 条</span>
        </div>
        <p v-if="rows.length > orderCount" class="group-note">同一采购单已有不同票号或开票日期，已分行列出。</p>
        <el-table :data="rows" row-key="revision" stripe border max-height="55vh">
          <el-table-column label="采购单 / 供应商" :min-width="invoiceMode ? 210 : 220">
            <template #default="{ row }">
              <div class="order-no">{{ row.po_no || ('散单明细 #' + row.item_ids[0]) }}</div>
              <div class="secondary">{{ row.supplier_name }}</div>
            </template>
          </el-table-column>
          <el-table-column label="本次范围" width="112">
            <template #default="{ row }">
              <div>{{ row.whole_order ? '勾选整单' : '勾选明细' }}</div>
              <el-popover trigger="click" placement="bottom" :width="300">
                <template #reference><el-button link type="primary">{{ row.item_ids.length }} 条明细</el-button></template>
                <div class="detail-list">
                  <div v-for="(id, i) in row.item_ids" :key="id">{{ row.item_names[i] || ('明细 #' + id) }}</div>
                </div>
              </el-popover>
            </template>
          </el-table-column>
          <el-table-column label="收货金额" align="right" min-width="112">
            <template #default="{ row }">{{ money.format(row.received_amount) }}</template>
          </el-table-column>
          <template v-if="invoiceMode">
            <el-table-column label="开票号" min-width="195">
              <template #default="{ row }">
                <el-input v-model="row.numberInput" maxlength="64" placeholder="填写发票号码" aria-label="开票号" />
                <div v-if="row.invoice_no && row.numberInput.trim() !== row.invoice_no" class="previous">
                  原票号 {{ row.invoice_no }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="开票日期" width="162">
              <template #default="{ row }">
                <el-date-picker v-model="row.dateInput" type="date" value-format="YYYY-MM-DD"
                  placeholder="选择日期" aria-label="开票日期" style="width:100%" />
                <div v-if="row.invoice_date && row.dateInput !== row.invoice_date" class="previous">原日期 {{ row.invoice_date }}</div>
              </template>
            </el-table-column>
            <el-table-column label="开票金额" width="140">
              <template #default="{ row }">
                <el-input-number v-model="row.amountInput" :min="0" :precision="2" :controls="false" :parser="moneyParser"
                  aria-label="开票金额" placeholder="填写金额" style="width:100%" />
                <div v-if="row.invoice_amount && row.amountInput !== row.invoice_amount" class="previous">原金额 {{ money.format(row.invoice_amount) }}</div>
                <div v-if="recordsDifference(row)" class="previous">金额与收货有差额，本组发票总额记入首条明细</div>
              </template>
            </el-table-column>
          </template>
          <template v-else>
            <el-table-column label="待对账" width="105" align="center">
              <template #default="{ row }">
                <el-tag :type="row.pending_count ? 'warning' : 'info'" size="small">
                  {{ row.pending_count ? row.pending_count + ' 条' : '已对账' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="已对账" width="100" align="center">
              <template #default="{ row }">{{ row.item_ids.length - row.pending_count }} 条</template>
            </el-table-column>
          </template>
        </el-table>
      </template>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-button v-if="error && (!rows.length || stale)" @click="preview">重新读取所选采购单</el-button>
    </div>
    <template #footer>
      <div class="actions">
        <span v-if="invoiceMode && rows.length" class="save-count">
          {{ edited.length && !edited.every(complete) ? '请补全已编辑行的票号、日期和金额' : '本次保存 ' + edited.length + ' 行' }}
        </span>
        <span v-else-if="rows.length" class="save-count">本次确认 {{ pendingCount }} 条待对账明细</span>
        <el-button :disabled="saving" @click="visible = false">取消</el-button>
        <el-button type="primary" :disabled="!canSave" :loading="saving" @click="save">
          {{ invoiceMode ? '保存发票' : '确认对账' }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.maintenance-body { min-height: 90px; }
.summary, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; }
.summary { margin: 0 0 15px; font-size: 14px; }
.summary strong, .order-no { color: var(--el-text-color-primary); font-weight: 600; }
.summary span, .secondary, .save-count { color: var(--el-text-color-secondary); }
.secondary, .previous { font-size: 12px; }
.group-note { margin: -6px 0 12px; font-size: 13px; color: var(--el-text-color-secondary); }
.previous { color: var(--el-color-warning-dark-2); overflow-wrap: anywhere; }
.detail-list { max-height: 200px; overflow: auto; line-height: 1.7; }
.actions { justify-content: flex-end; }
.actions .el-button + .el-button { margin-left: 0; }
.save-count { margin-right: auto; }
.maintenance-body .el-alert { margin: 12px 0; }
</style>
