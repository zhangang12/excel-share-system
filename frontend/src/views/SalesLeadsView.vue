<script setup lang="ts">
// 🆕 销售线索跟踪：线索池录入 → 分配销售员 → 跟进/补全/改状态 → 成交率报表
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, TrendCharts, Clock, Delete } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { leadsApi, LEAD_SOURCES, LEAD_STATUSES, type SalesLeadRow, type SalesLeadReport } from '@/api/leads'
import { salesApi } from '@/api/sales'
import { fmtDate, fmtDateTime } from '@/utils/format'
import EmptyHint from '@/components/EmptyHint.vue'

const auth = useAuthStore()
// 主管/管理层：录入、分配/改派、改来源、删除、报表、看全部；销售员：仅跟进本人线索
const allView = computed(() => auth.hasRole('admin', 'manager', 'sales_lead'))

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'
function statusType(s: string): TagType {
  const m: Record<string, TagType> = { '潜在需求': 'warning', '报价': 'primary', '成交': 'success', '丢单': 'danger' }
  return m[s] || 'info'
}
function leadTitle(r: SalesLeadRow): string {
  return r.customer || r.contact || r.phone || r.wechat || '(待补全)'
}
function firstLine(s?: string | null): string {
  if (!s) return ''
  const t = s.trim().split('\n')[0]
  return t.length > 16 ? t.slice(0, 16) + '…' : t
}

const loading = ref(false)
const rows = ref<SalesLeadRow[]>([])
const total = ref(0)
const filters = reactive({ source: '', owner_uid: undefined as number | undefined, status: '', kw: '' })
const page = ref(1)
const pageSize = ref(50)

// 销售员名单（分配下拉 + 负责人筛选；主管/管理层才需要）
const salesStaff = ref<{ id: number; name: string }[]>([])
async function loadStaff() {
  if (!allView.value) return
  try { salesStaff.value = await salesApi.salespeople() } catch { /* 静默 */ }
}

async function load() {
  loading.value = true
  try {
    const j = await leadsApi.list({
      source: filters.source || undefined,
      owner_uid: filters.owner_uid,
      status: filters.status || undefined,
      kw: filters.kw || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = j.rows
    total.value = j.total
  } finally {
    loading.value = false
  }
}
function reload() { page.value = 1; load() }
function onPage(p: number) { page.value = p; load() }
function onSize(s: number) { pageSize.value = s; page.value = 1; load() }
onMounted(() => { load(); loadStaff() })

// ===== 录入线索（主管/管理层） =====
const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive({
  source: '1688', customer: '', contact: '', phone: '', wechat: '',
  requirement: '', owner_uid: undefined as number | undefined, status: '潜在需求', follow_log: '',
})
function openCreate() {
  Object.assign(createForm, {
    source: '1688', customer: '', contact: '', phone: '', wechat: '',
    requirement: '', owner_uid: undefined, status: '潜在需求', follow_log: '',
  })
  createVisible.value = true
}
async function submitCreate() {
  if (!createForm.source) { ElMessage.warning('请选择询盘来源'); return }
  creating.value = true
  try {
    await leadsApi.create({ ...createForm })
    ElMessage.success('线索已录入' + (createForm.owner_uid ? '并分配' : '（进线索池，待分配）'))
    createVisible.value = false
    reload()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '录入失败')
  } finally {
    creating.value = false
  }
}

// ===== 跟进 / 编辑（销售员补全本人线索；主管全量 + 改来源/改派） =====
const editVisible = ref(false)
const editRow = ref<SalesLeadRow | null>(null)
const saving = ref(false)
const editForm = reactive({
  source: '', customer: '', contact: '', phone: '', wechat: '',
  requirement: '', owner_uid: undefined as number | undefined, status: '', follow_log: '', lost_reason: '',
})
const followRef = ref<any>(null)
function openEdit(r: SalesLeadRow) {
  editRow.value = r
  Object.assign(editForm, {
    source: r.source, customer: r.customer || '', contact: r.contact || '', phone: r.phone || '', wechat: r.wechat || '',
    requirement: r.requirement || '', owner_uid: r.owner_uid ?? undefined, status: r.status,
    follow_log: r.follow_log || '', lost_reason: r.lost_reason || '',
  })
  editVisible.value = true
}
function nowStamp(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function insertNow() {
  const stamp = `【${nowStamp()}】`
  const ta: HTMLTextAreaElement | undefined = followRef.value?.textarea
  if (ta && typeof ta.selectionStart === 'number') {
    const s = ta.selectionStart, e = ta.selectionEnd
    editForm.follow_log = editForm.follow_log.slice(0, s) + stamp + editForm.follow_log.slice(e)
    nextTick(() => { ta.focus(); const pos = s + stamp.length; ta.setSelectionRange(pos, pos) })
  } else {
    editForm.follow_log = (editForm.follow_log ? editForm.follow_log.replace(/\s*$/, '') + '\n' : '') + stamp
  }
}
async function submitEdit() {
  if (!editRow.value) return
  saving.value = true
  try {
    const payload: any = { ...editForm }
    if (!allView.value) { delete payload.source; delete payload.owner_uid }  // 销售员不可改来源/改派（后端亦拦）
    if (payload.status !== '丢单') payload.lost_reason = ''                   // 非丢单清空丢单原因
    await leadsApi.update(editRow.value.id, payload)
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function removeLead(r: SalesLeadRow) {
  try {
    await ElMessageBox.confirm(`确认删除线索「${leadTitle(r)}」？删除后不可恢复。`, '删除线索', { type: 'warning' })
  } catch { return }
  try {
    await leadsApi.remove(r.id)
    ElMessage.success('线索已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

// ===== 线索报表（主管/管理层） =====
const reportVisible = ref(false)
const report = ref<SalesLeadReport | null>(null)
const reportMonth = ref('')   // YYYY-MM；空=全部
function pct(r: number): string { return Math.round((r || 0) * 100) + '%' }
async function loadReport() {
  report.value = await leadsApi.report(reportMonth.value ? { month: reportMonth.value } : {})
}
async function openReport() {
  reportMonth.value = ''
  await loadReport()
  reportVisible.value = true
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>销售线索</h1>
        <div class="desc">线索池 · {{ allView ? '全部' : '我的' }} · 集中录入分配 → 销售跟进 → 成交率统计</div>
      </div>
      <div class="spacer"></div>
      <el-button v-if="allView" type="primary" plain :icon="TrendCharts" @click="openReport">线索报表</el-button>
      <el-button v-if="allView" type="primary" :icon="Plus" @click="openCreate">录入线索</el-button>
    </div>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="filter-bar">
        <el-select v-model="filters.source" placeholder="来源(全部)" clearable style="width: 130px" @change="reload">
          <el-option v-for="s in LEAD_SOURCES" :key="s" :label="s" :value="s" />
        </el-select>
        <el-select v-if="allView" v-model="filters.owner_uid" placeholder="负责人(全部)" clearable filterable style="width: 150px" @change="reload">
          <el-option v-for="u in salesStaff" :key="u.id" :label="u.name" :value="u.id" />
        </el-select>
        <el-select v-model="filters.status" placeholder="状态(全部)" clearable style="width: 130px" @change="reload">
          <el-option v-for="s in LEAD_STATUSES" :key="s" :label="s" :value="s" />
        </el-select>
        <el-input v-model="filters.kw" placeholder="搜索 客户/联系人/电话/微信" clearable style="width: 240px" @change="reload" />
        <span class="muted">共 {{ total }} 条</span>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table show-overflow-tooltip :data="rows" stripe v-loading="loading" max-height="calc(100vh - 290px)">
        <el-table-column type="index" label="#" :width="48" />
        <el-table-column label="来源" :width="92">
          <template #default="{ row }"><el-tag size="small" effect="plain" type="info">{{ row.source }}</el-tag></template>
        </el-table-column>
        <el-table-column label="客户 / 联系人" min-width="150">
          <template #default="{ row }">
            <div>{{ row.customer || '—' }}</div>
            <div class="muted">{{ row.contact || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="电话 / 微信" min-width="140">
          <template #default="{ row }">
            <div>{{ row.phone || '—' }}</div>
            <div class="muted">{{ row.wechat || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="设备需求" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.requirement || '—' }}</template>
        </el-table-column>
        <el-table-column label="负责人" :width="86">
          <template #default="{ row }">
            <span v-if="row.owner_name">{{ row.owner_name }}</span>
            <span v-else class="muted">未分配</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" :width="96">
          <template #default="{ row }">
            <el-tooltip v-if="row.status === '丢单' && row.lost_reason" :content="'丢单原因：' + row.lost_reason" placement="top">
              <el-tag size="small" :type="statusType(row.status)" effect="plain">{{ row.status }}</el-tag>
            </el-tooltip>
            <el-tag v-else size="small" :type="statusType(row.status)" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近跟进" min-width="150">
          <template #default="{ row }">
            <el-tooltip v-if="row.follow_log" placement="top">
              <template #content><div style="max-width: 320px; white-space: pre-wrap">{{ row.follow_log }}</div></template>
              <span>{{ firstLine(row.follow_log) }}</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="录入时间" :width="108">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" :width="allView ? 120 : 70" fixed="right" align="center" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openEdit(row)">{{ allView ? '编辑' : '跟进' }}</el-button>
            <el-button v-if="allView" size="small" link type="danger" @click="removeLead(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length"
                 :text="filters.kw || filters.source || filters.status || filters.owner_uid ? '没有符合筛选条件的线索' : (allView ? '暂无线索，点右上「录入线索」开始' : '你还没有分配到线索')" />

      <div v-if="total > 20" class="pager">
        <el-pagination background layout="prev, pager, next, sizes, jumper, ->, total"
                       :total="total" :page-size="pageSize" :page-sizes="[20, 50, 100, 200]"
                       :current-page="page" @current-change="onPage" @size-change="onSize" />
      </div>
    </el-card>

    <!-- ===== 录入线索 ===== -->
    <el-dialog v-model="createVisible" title="📥 录入线索" width="760px" :close-on-click-modal="false">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="询盘来源" required style="flex: 1">
            <el-select v-model="createForm.source" style="width: 100%">
              <el-option v-for="s in LEAD_SOURCES" :key="s" :label="s" :value="s" />
            </el-select>
          </el-form-item>
          <el-form-item label="分配给（可留空）" style="flex: 1">
            <el-select v-model="createForm.owner_uid" placeholder="待分配" clearable filterable style="width: 100%">
              <el-option v-for="u in salesStaff" :key="u.id" :label="u.name" :value="u.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="初始状态" style="flex: 1">
            <el-select v-model="createForm.status" style="width: 100%">
              <el-option v-for="s in LEAD_STATUSES" :key="s" :label="s" :value="s" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="客户名称" style="flex: 1"><el-input v-model="createForm.customer" placeholder="可后补" /></el-form-item>
          <el-form-item label="联系人" style="flex: 1"><el-input v-model="createForm.contact" placeholder="可后补" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="联系电话" style="flex: 1"><el-input v-model="createForm.phone" placeholder="可后补" /></el-form-item>
          <el-form-item label="微信号" style="flex: 1"><el-input v-model="createForm.wechat" placeholder="可后补" /></el-form-item>
        </div>
        <el-form-item label="设备需求"><el-input v-model="createForm.requirement" type="textarea" :rows="2" placeholder="客户的设备/型号/规格需求" /></el-form-item>
        <el-form-item label="跟进记录（选填）"><el-input v-model="createForm.follow_log" type="textarea" :rows="2" placeholder="初步沟通情况等" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">录入</el-button>
      </template>
    </el-dialog>

    <!-- ===== 跟进 / 编辑 ===== -->
    <el-dialog v-model="editVisible" :title="(allView ? '✏️ 编辑线索 · ' : '📞 跟进线索 · ') + (editRow ? leadTitle(editRow) : '')" width="760px" :close-on-click-modal="false">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item v-if="allView" label="询盘来源" style="flex: 1">
            <el-select v-model="editForm.source" style="width: 100%">
              <el-option v-for="s in LEAD_SOURCES" :key="s" :label="s" :value="s" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="allView" label="跟进负责人（改派）" style="flex: 1">
            <el-select v-model="editForm.owner_uid" placeholder="待分配" clearable filterable style="width: 100%">
              <el-option v-for="u in salesStaff" :key="u.id" :label="u.name" :value="u.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="当前状态" style="flex: 1">
            <el-select v-model="editForm.status" style="width: 100%">
              <el-option v-for="s in LEAD_STATUSES" :key="s" :label="s" :value="s" />
            </el-select>
          </el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="客户名称" style="flex: 1"><el-input v-model="editForm.customer" /></el-form-item>
          <el-form-item label="联系人" style="flex: 1"><el-input v-model="editForm.contact" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="联系电话" style="flex: 1"><el-input v-model="editForm.phone" /></el-form-item>
          <el-form-item label="微信号" style="flex: 1"><el-input v-model="editForm.wechat" /></el-form-item>
        </div>
        <el-form-item label="设备需求"><el-input v-model="editForm.requirement" type="textarea" :rows="2" /></el-form-item>
        <el-form-item v-if="editForm.status === '丢单'" label="丢单原因">
          <el-input v-model="editForm.lost_reason" placeholder="便于复盘，如 价格 / 工期 / 选了对手 等" />
        </el-form-item>
        <el-form-item>
          <template #label>
            <div class="follow-label">
              <span>跟进记录</span>
              <el-button size="small" text :icon="Clock" @click="insertNow">插入当前时间</el-button>
            </div>
          </template>
          <el-input ref="followRef" v-model="editForm.follow_log" type="textarea" :rows="4"
                    placeholder="记录每次跟进的时间、沟通内容、客户反馈等（可点上方「插入当前时间」）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ===== 线索报表 ===== -->
    <el-dialog v-model="reportVisible" title="📊 线索报表" width="900px">
      <div class="filter-bar" style="margin-bottom: 12px">
        <el-date-picker v-model="reportMonth" type="month" value-format="YYYY-MM" placeholder="按月份（空=全部）" clearable style="width: 180px" @change="loadReport" />
        <span class="muted" v-if="report">共 {{ report.total_leads }} 条线索 · 成交 {{ report.total_deal }} · 总成交率 <b>{{ pct(report.total_rate) }}</b></span>
      </div>
      <div v-if="report" class="report-grid">
        <div>
          <div class="rep-title">按询盘来源</div>
          <el-table show-overflow-tooltip :data="report.by_source" size="small" stripe>
            <el-table-column prop="key" label="来源" min-width="90" />
            <el-table-column prop="leads" label="线索" align="right" :width="64" />
            <el-table-column prop="quote" label="报价" align="right" :width="64" />
            <el-table-column prop="deal" label="成交" align="right" :width="64" />
            <el-table-column label="成交率" align="right" :width="80">
              <template #default="{ row }"><b style="color: var(--el-color-success)">{{ pct(row.rate) }}</b></template>
            </el-table-column>
          </el-table>
        </div>
        <div>
          <div class="rep-title">按跟进销售</div>
          <el-table show-overflow-tooltip :data="report.by_owner" size="small" stripe>
            <el-table-column prop="key" label="销售" min-width="90" />
            <el-table-column prop="leads" label="分配" align="right" :width="64" />
            <el-table-column prop="quote" label="报价" align="right" :width="64" />
            <el-table-column prop="deal" label="成交" align="right" :width="64" />
            <el-table-column label="成交率" align="right" :width="80">
              <template #default="{ row }"><b style="color: var(--el-color-success)">{{ pct(row.rate) }}</b></template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!report.by_owner.length" text="该范围内暂无已分配线索" />
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
.pager { display: flex; justify-content: flex-end; margin-top: 12px; }
.frow { display: flex; gap: 12px; flex-wrap: wrap; }
.follow-label { display: flex; align-items: center; justify-content: space-between; width: 100%; }
.report-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.rep-title { font-weight: 600; margin-bottom: 8px; font-size: 14px; }
</style>
