<script setup lang="ts">
// 🆕 反馈#445（杨坛）售后/安装费用清单编辑：登记弹窗和重传发票弹窗共用。
//   每行：费用项 / 金额 / 备注；发票可以传好几张，也可以「用已传的发票」和别的行共用一张；
//   没有发票的行必须传付款记录（转账或支付截图）——提交时由 costRowsProblem 拦。
import { ref, computed } from 'vue'
import { Plus, Delete } from '@element-plus/icons-vue'
import { http } from '@/api'
import { downloadAttachment } from '@/api/orders'
import { fmtMoney } from '@/utils/format'
import { moneyParser } from '@/utils/money'
import { newCostItem, type CostItem, type AsFile } from '@/types/aftersales'

const items = defineModel<CostItem[]>({ required: true })

const total = computed(() => items.value.reduce((s, it) => s + (Number(it.amount) || 0), 0))

// 同一张发票在哪几行出现（共用提示、「用已传的发票」下拉都靠它）
function rowsOf(id: number): number[] {
  return items.value.flatMap((it, i) => it.invoice_files.some(f => f.id === id) ? [i + 1] : [])
}
function sharedHint(i: number, id: number): string {
  const others = rowsOf(id).filter(n => n !== i + 1)
  return others.length ? `与第 ${others.join('、')} 行共用` : ''
}
/** 别的行传过、这一行还没有的发票 */
function otherInvoices(i: number): (AsFile & { rows: string })[] {
  const mine = new Set(items.value[i].invoice_files.map(f => f.id))
  const seen = new Map<number, AsFile>()
  items.value.forEach((it, k) => { if (k !== i) it.invoice_files.forEach(f => { if (!mine.has(f.id)) seen.set(f.id, f) }) })
  return [...seen.values()].map(f => ({ ...f, rows: rowsOf(f.id).join('、') }))
}
function useShared(i: number, id: number) {
  const f = items.value.flatMap(it => it.invoice_files).find(x => x.id === id)
  if (f && !items.value[i].invoice_files.some(x => x.id === id)) items.value[i].invoice_files.push({ ...f })
}
function removeFile(list: AsFile[], id: number) {
  const k = list.findIndex(f => f.id === id)
  if (k >= 0) list.splice(k, 1)
}
const needProof = (it: CostItem) =>
  !!(it.name.trim() || Number(it.amount)) && !it.invoice_files.length && !it.pay_proof_files.length

// 上传：一个隐藏的多选 input，点哪行的哪个按钮就记下目标
const fileInput = ref<HTMLInputElement | null>(null)
const target = ref<{ i: number; kind: 'inv' | 'pay' } | null>(null)
const busy = ref('')
function pick(i: number, kind: 'inv' | 'pay') {
  target.value = { i, kind }
  if (fileInput.value) { fileInput.value.value = ''; fileInput.value.click() }
}
async function onFiles(e: Event) {
  const files = [...((e.target as HTMLInputElement).files || [])]
  const t = target.value
  if (!t || !files.length) return
  busy.value = `${t.i}-${t.kind}`
  try {
    for (const f of files) {
      const fd = new FormData()
      fd.append('file', f)
      fd.append('biz_type', t.kind === 'inv' ? 'aftersales_invoice' : 'aftersales_payproof')
      const { data } = await http.post<AsFile>('/attachments', fd)
      const row = items.value[t.i]
      if (row) (t.kind === 'inv' ? row.invoice_files : row.pay_proof_files).push({ id: data.id, name: data.name })
    }
  } catch { /* 全局拦截器已提示 */ } finally { busy.value = '' }
}
</script>

<template>
  <div class="cost-rows">
    <div v-for="(it, i) in items" :key="i" class="cr" :class="{ 'cr-warn': needProof(it) }">
      <div class="cr-top">
        <span class="cr-no">{{ i + 1 }}</span>
        <el-input v-model="it.name" placeholder="费用项，如 配件费/差旅/住宿" class="c-name" />
        <!-- 金额审计：费用行不能为负；parser 防千分位粘贴截断 -->
        <el-input-number v-model="it.amount" :controls="false" :precision="2" :min="0" :parser="moneyParser"
                         placeholder="金额" class="c-amt" />
        <el-input v-model="it.note" placeholder="备注（选填）" maxlength="200" class="c-note" />
        <el-button link type="danger" :icon="Delete" class="c-del" :aria-label="`删除第 ${i + 1} 行`"
                   :disabled="items.length <= 1" @click="items.splice(i, 1)" />
      </div>
      <div class="cr-files">
        <span class="cr-lbl">发票</span>
        <el-tag v-for="f in it.invoice_files" :key="f.id" closable size="small" effect="plain" class="cr-file"
                @close="removeFile(it.invoice_files, f.id)">
          <a class="cr-fname" @click="downloadAttachment({ id: f.id, name: f.name || '发票' })">{{ f.name || '发票' }}</a>
          <span v-if="sharedHint(i, f.id)" class="cr-share">{{ sharedHint(i, f.id) }}</span>
        </el-tag>
        <el-button size="small" link type="primary" :loading="busy === `${i}-inv`" @click="pick(i, 'inv')">＋ 上传发票</el-button>
        <el-dropdown v-if="otherInvoices(i).length" trigger="click" @command="(id: number) => useShared(i, id)">
          <el-button size="small" link type="primary">用已传的发票 ▾</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="o in otherInvoices(i)" :key="o.id" :command="o.id">
                {{ o.name || '发票' }}（第 {{ o.rows }} 行）
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <div class="cr-files">
        <span class="cr-lbl">付款记录</span>
        <el-tag v-for="f in it.pay_proof_files" :key="f.id" closable size="small" effect="plain" type="info" class="cr-file"
                @close="removeFile(it.pay_proof_files, f.id)">
          <a class="cr-fname" @click="downloadAttachment({ id: f.id, name: f.name || '付款记录' })">{{ f.name || '付款记录' }}</a>
        </el-tag>
        <el-button size="small" link type="primary" :loading="busy === `${i}-pay`" @click="pick(i, 'pay')">＋ 上传付款记录</el-button>
        <span v-if="needProof(it)" class="cr-need">没有发票就要传付款记录（转账或支付截图）</span>
        <span v-else-if="!it.pay_proof_files.length" class="cr-tip">没发票时必传</span>
      </div>
    </div>
    <div class="cost-foot">
      <el-button link type="primary" :icon="Plus" @click="items.push(newCostItem())">添加一行</el-button>
      <span class="cost-total">合计 <b>{{ fmtMoney(total) }}</b></span>
    </div>
    <input ref="fileInput" type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.ofd" hidden @change="onFiles">
  </div>
</template>

<style scoped>
.cost-rows { width: 100%; }
.cr { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.cr-warn { border-color: var(--el-color-danger-light-5); background: var(--el-color-danger-light-9); }
.cr-top { display: flex; gap: 8px; align-items: center; }
.cr-no { width: 18px; flex: none; color: var(--el-text-color-secondary); font-size: 12px; text-align: center; }
.cr-top .c-name { flex: 2 1 0; min-width: 110px; }
.cr-top .c-amt { flex: 1 1 0; min-width: 90px; }
.cr-top .c-note { flex: 2 1 0; min-width: 100px; }
.cr-top .c-del { flex: none; }
.cr-files { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0 0 26px; font-size: 13px; }
.cr-lbl { width: 56px; flex: none; color: var(--el-text-color-secondary); font-size: 12px; }
.cr-file { max-width: 260px; }
.cr-fname { cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; display: inline-block; vertical-align: bottom; }
.cr-share { margin-left: 6px; color: var(--el-color-warning-dark-2, #b88230); font-size: 12px; }
.cr-need { color: var(--el-color-danger); font-size: 12px; }
.cr-tip { color: var(--el-text-color-secondary); font-size: 12px; }
.cost-foot { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.cost-total b { font-size: 15px; }
@media (max-width: 640px) {
  .cr-top { flex-wrap: wrap; }
  .cr-top .c-name, .cr-top .c-note { flex-basis: 100%; }
  .cr-files { margin-left: 0; }
}
</style>
