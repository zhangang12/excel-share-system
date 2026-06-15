<script setup lang="ts">
// 🆕 v3 部门工作台（设计/电工/生产共用）：
// 工人视角 = 我的待办（卡片）/ 已完成（表格）
// 负责人/管理层视角 = 待分派（卡片）/ 任务跟踪（表格，含换人/作废/改回）
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Document, Download, Close, UploadFilled, Check, RefreshLeft, Switch as SwitchIcon, Lock,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import {
  ordersApi, downloadAttachment, ORDER_STATUS_TEXT, ORDER_STATUS_TAG,
  type DeptOrder, type DeptOptions, type OrderAttachment,
} from '@/api/orders'
import FeedbackPanel from '@/components/FeedbackPanel.vue'
import StockQueryDialog from '@/components/StockQueryDialog.vue'
import EmptyHint from '@/components/EmptyHint.vue'
import { reportsApi, type DeptReport } from '@/api/reports'

const route = useRoute()
const auth = useAuthStore()

const dept = computed(() => (route.name as string) || 'design')

const WORKER_ROLES: Record<string, string> = { design: 'designer', electric: 'electrician', produce: 'assembler' }
const LEAD_ROLES: Record<string, string> = { design: 'design_lead', electric: 'electric_lead', produce: 'pm_lead' }

const isMgr = computed(() => auth.isAdmin)
const isLead = computed(() => auth.user?.role_code === LEAD_ROLES[dept.value])
const isWorker = computed(() => auth.user?.role_code === WORKER_ROLES[dept.value])

const loading = ref(false)
const orders = ref<DeptOrder[]>([])
const options = ref<DeptOptions | null>(null)
const activeTab = ref('')

// ---- 数据加载 ----
async function load() {
  loading.value = true
  try {
    const [os, opt] = await Promise.all([
      ordersApi.list(dept.value),
      ordersApi.options(dept.value),
    ])
    orders.value = os
    options.value = opt
    if (!activeTab.value) activeTab.value = isWorker.value ? 'todo' : 'assign'
  } finally {
    loading.value = false
  }
}
watch(dept, () => { activeTab.value = ''; load() })
onMounted(load)

// ---- 工人视角数据 ----
const myTodo = computed(() => orders.value.filter(o => ['assigned', 'in_progress'].includes(o.status)))
const myDone = computed(() => orders.value.filter(o => o.status === 'done'))
// ---- 负责人视角数据 ----
const pendingAssign = computed(() => orders.value.filter(o => o.status === 'pending_assign'))

// ---- 接单（填时间开始） ----
const startDates = ref<Record<number, { start: string; due: string }>>({})
function dateOf(o: DeptOrder) {
  if (!startDates.value[o.id]) {
    startDates.value[o.id] = { start: new Date().toISOString().slice(0, 10), due: '' }
  }
  return startDates.value[o.id]
}
async function doStart(o: DeptOrder) {
  const d = dateOf(o)
  if (!d.start || !d.due) { ElMessage.warning('请填写开始与预计完成日期'); return }
  await ordersApi.start(o.id, d.start, d.due)
  ElMessage.success('已开始' + (dept.value !== 'produce' ? '，已回传项目目录' : ''))
  await load()
}

// ---- 接单后上传（图纸包/采购清单） ----
async function pickStartUpload(o: DeptOrder, kind: string) {
  const input = document.createElement('input')
  input.type = 'file'
  input.multiple = true
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    await ordersApi.startUpload(o.id, kind, files)
    ElMessage.success('已上传并推送下游')
    await load()
  }
  input.click()
}
async function removeAtt(o: DeptOrder, att: OrderAttachment) {
  // #68 移除已上传资料（可能已推送下游）为破坏性操作，二次确认
  try {
    await ElMessageBox.confirm(
      `移除已上传的「${att.name}」？该资料可能已推送下游部门。`, '移除文件', { type: 'warning' })
  } catch { return }
  await ordersApi.removeAttachment(o.id, att.id)
  ElMessage.success('已移除')
  await load()
}
function startFilesOf(o: DeptOrder, kind: string) {
  return o.start_files.filter(f => f.kind === kind)
}

// ---- 完成弹窗 ----
const completeVisible = ref(false)
const completeOrder = ref<DeptOrder | null>(null)
const notifyUserId = ref<number | null>(null)
const completing = ref(false)
function openComplete(o: DeptOrder) {
  completeOrder.value = o
  notifyUserId.value = null
  completeVisible.value = true
}
function outputFilesOf(kind: string) {
  return completeOrder.value?.output_files.filter(f => f.kind === kind) || []
}
async function pickOutputUpload(kind: string) {
  const o = completeOrder.value
  if (!o) return
  const input = document.createElement('input')
  input.type = 'file'
  input.onchange = async () => {
    const files = Array.from(input.files || [])
    if (!files.length) return
    await ordersApi.outputUpload(o.id, kind, files)
    const fresh = await ordersApi.list(dept.value)
    orders.value = fresh
    completeOrder.value = fresh.find(x => x.id === o.id) || o
    ElMessage.success('产物已上传')
  }
  input.click()
}
async function doComplete() {
  const o = completeOrder.value
  if (!o) return
  if (!notifyUserId.value) { ElMessage.warning(options.value?.notify_label || '请选择通知人'); return }
  // #67 必传产物前端校验：完成前确认 required 产物都已上传，内联提示缺哪项
  const miss = (options.value?.outputs || []).filter((ot: any) => ot.required && !outputFilesOf(ot.k).length)
  if (miss.length) { ElMessage.warning('请先上传必传产物：' + miss.map((m: any) => m.label).join('、')); return }
  completing.value = true
  try {
    const r: any = await ordersApi.complete(o.id, notifyUserId.value)
    ElMessage.success(r.message || '已完成')
    completeVisible.value = false
    await load()
  } finally {
    completing.value = false
  }
}

// ---- 负责人：分派 / 作废 / 换人 / 改回 ----
const assignSel = ref<Record<number, number | null>>({})
const assigning = ref<number | null>(null)
async function doAssign(o: DeptOrder) {
  const wid = assignSel.value[o.id]
  if (!wid) { ElMessage.warning('请选择分派对象'); return }
  assigning.value = o.id
  try {
    const r: any = await ordersApi.assign(o.id, wid)
    ElMessage.success(r.message || '已分派')
    await load()
  } finally { assigning.value = null }
}
async function doVoid(o: DeptOrder) {
  await ElMessageBox.confirm(`确认作废 ${o.project_code} 的任务单？作废后留痕，管理层将收到通知。`, '作废单号', { type: 'warning' })
  await ordersApi.void(o.id)
  ElMessage.success('已作废并通知管理层')
  await load()
}
async function doReopen(o: DeptOrder) {
  await ElMessageBox.confirm('改回进行中将清除完成日期（已推送下游的资料联动撤回）', '改回进行中', { type: 'warning' })
  await ordersApi.reopen(o.id)
  ElMessage.success('已改回进行中')
  await load()
}

const reassignVisible = ref(false)
const reassignOrder = ref<DeptOrder | null>(null)
const reassignWid = ref<number | null>(null)
function openReassign(o: DeptOrder) {
  reassignOrder.value = o
  reassignWid.value = null
  reassignVisible.value = true
}
const reassigning = ref(false)
async function doReassign() {
  const o = reassignOrder.value
  if (!o || !reassignWid.value) { ElMessage.warning('请选择转交对象'); return }
  reassigning.value = true
  try {
    const r: any = await ordersApi.reassign(o.id, reassignWid.value)
    ElMessage.success(r.message || '已转交')
    reassignVisible.value = false
    await load()
  } finally { reassigning.value = false }
}
const reassignCandidates = computed(() =>
  (options.value?.workers || []).filter(w => w.id !== reassignOrder.value?.worker_id))

// ---- 展示工具 ----
function effClass(o: DeptOrder) {
  if (o.eff_pct == null) return ''
  return o.eff_pct <= 100 ? 'eff-good' : 'eff-bad'
}
const deptName = computed(() => options.value?.dept_name || '')

// 🆕 M14 部门报表（负责人/管理层）
const reportVisible = ref(false)
const report = ref<DeptReport | null>(null)
async function openReport() {
  report.value = await reportsApi.dept(dept.value)
  reportVisible.value = true
}

// 🆕 M07 设计部「查库存」只读
const stockVisible = ref(false)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>{{ deptName }}工作台</h1>
        <div class="desc">
          销售/管理层下单 → 负责人分派 → {{ isWorker ? '接单填时间 → 完成（' + (options?.sheet_check ? '四表校验/' : '') + '通知/产物）→ 推送下游' : '跟踪进度与完成效率' }}
        </div>
      </div>
      <div class="spacer"></div>
      <el-button v-if="dept === 'design'" @click="stockVisible = true">🔎 查库存(只读)</el-button>
      <el-button v-if="isLead || isMgr" type="primary" plain @click="openReport">📊 {{ deptName }}报表</el-button>
    </div>

    <!-- ===== 工人视角 ===== -->
    <template v-if="isWorker">
      <el-tabs v-model="activeTab">
        <el-tab-pane :label="`📥 我的待办 (${myTodo.length})`" name="todo">
          <EmptyHint v-if="!loading && myTodo.length === 0" text="暂无待办任务" />
          <div v-else class="todo-grid" v-loading="loading">
            <el-card v-for="o in myTodo" :key="o.id" shadow="hover"
                     class="todo-card" :class="{ urgent: o.overdue }">
              <div class="tc-head">
                <span class="tc-code">{{ o.project_code }}</span>
                <el-tag :type="ORDER_STATUS_TAG[o.status]" size="small">{{ ORDER_STATUS_TEXT[o.status] }}</el-tag>
                <el-tag v-if="o.overdue" type="danger" size="small" effect="dark">已超预计</el-tag>
              </div>
              <div class="tc-name">{{ o.project_name }}</div>
              <div v-if="o.req_text" class="tc-req">📌 {{ o.req_text }}</div>

              <div v-if="o.input_files.length" class="tc-files">
                <el-tag v-for="f in o.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" @click="downloadAttachment(f)">
                  <el-icon><Document /></el-icon>{{ f.name }}<el-icon class="dl"><Download /></el-icon>
                </el-tag>
              </div>

              <!-- 待接单：填时间开始 -->
              <template v-if="o.status === 'assigned'">
                <div class="tc-dates">
                  <div class="fd">
                    <label>{{ options?.start_label }}</label>
                    <el-date-picker v-model="dateOf(o).start" type="date" value-format="YYYY-MM-DD" size="small" style="width: 100%" />
                  </div>
                  <div class="fd">
                    <label>{{ options?.end_label }}</label>
                    <el-date-picker v-model="dateOf(o).due" type="date" value-format="YYYY-MM-DD" size="small" style="width: 100%" />
                  </div>
                </div>
                <div class="tc-hint">时间一经填写本人不可改（仅管理层可改）</div>
                <el-button type="primary" size="small" @click="doStart(o)">开始</el-button>
              </template>

              <!-- 进行中：起始上传 + 完成 -->
              <template v-else-if="o.status === 'in_progress'">
                <div class="tc-kv">
                  {{ options?.start_label }}：<b>{{ o.start_date }}</b>
                  ｜ {{ options?.end_label }}：<b>{{ o.due_date }}</b>
                </div>

                <div v-for="so in options?.start_outputs || []" :key="so.k" class="up-sec">
                  <div class="up-h">
                    <el-icon><UploadFilled /></el-icon> {{ so.label }}
                    <el-tag size="small" :type="startFilesOf(o, so.k).length ? 'success' : 'warning'" style="margin-left: auto">
                      {{ startFilesOf(o, so.k).length ? `已推送 ${startFilesOf(o, so.k).length} 个` : '待上传 → 推送下游' }}
                    </el-tag>
                  </div>
                  <div class="up-b">
                    <div v-if="startFilesOf(o, so.k).length" class="tc-files">
                      <el-tag v-for="f in startFilesOf(o, so.k)" :key="f.id" size="small" effect="plain" class="file-chip">
                        <span @click="downloadAttachment(f)" style="cursor: pointer">{{ f.name }}</span>
                        <el-icon class="rm" @click="removeAtt(o, f)"><Close /></el-icon>
                      </el-tag>
                    </div>
                    <el-button size="small" plain type="primary" :icon="UploadFilled" @click="pickStartUpload(o, so.k)">
                      {{ startFilesOf(o, so.k).length ? '继续添加' : '上传' }}{{ so.label }}
                    </el-button>
                  </div>
                </div>

                <el-button type="success" size="small" :icon="Check" @click="openComplete(o)">完成…</el-button>
              </template>
            </el-card>
          </div>
        </el-tab-pane>

        <el-tab-pane :label="`✅ 已完成 (${myDone.length})`" name="done">
          <el-table :data="myDone" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目" min-width="120">
              <template #default="{ row }"><b>{{ row.project_code }}</b> {{ row.project_name }}</template>
            </el-table-column>
            <el-table-column prop="start_date" :label="options?.start_label" width="105" />
            <el-table-column prop="due_date" :label="options?.end_label" width="105" />
            <el-table-column prop="done_date" label="完成" width="105" />
            <el-table-column label="完成效率" width="90">
              <template #default="{ row }">
                <span v-if="row.eff_pct != null" :class="effClass(row)">{{ row.eff_pct }}%</span>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="上传资料/产物" min-width="200">
              <template #default="{ row }">
                <el-tag v-for="f in [...row.start_files, ...row.output_files]" :key="f.id"
                        size="small" effect="plain" class="file-chip" @click="downloadAttachment(f)">
                  {{ f.name }}
                </el-tag>
                <span v-if="!row.start_files.length && !row.output_files.length">—</span>
              </template>
            </el-table-column>
            <el-table-column label="通知" width="110">
              <template #default="{ row }">{{ row.notify_user_name ? '📲 ' + row.notify_user_name : '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="130">
              <template #default="{ row }">
                <el-button size="small" :icon="RefreshLeft" @click="doReopen(row)">改回进行中</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!loading && !myDone.length" text="还没有已完成的任务" size="sm" />
        </el-tab-pane>
      </el-tabs>
    </template>

    <!-- ===== 负责人 / 管理层视角 ===== -->
    <template v-else-if="isLead || isMgr">
      <el-tabs v-model="activeTab">
        <el-tab-pane :label="`📥 待分派 (${pendingAssign.length})`" name="assign">
          <EmptyHint v-if="!loading && pendingAssign.length === 0" text="暂无待分派任务" />
          <div v-else class="todo-grid" v-loading="loading">
            <el-card v-for="o in pendingAssign" :key="o.id" shadow="hover" class="todo-card assign">
              <div class="tc-head">
                <span class="tc-code">{{ o.project_code }}</span>
                <el-tag type="warning" size="small">待分派</el-tag>
              </div>
              <div class="tc-name">{{ o.project_name }}</div>
              <div v-if="o.req_text" class="tc-req">📌 {{ o.req_text }}</div>
              <div v-if="o.input_files.length" class="tc-files">
                <el-tag v-for="f in o.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
              </div>
              <div class="assign-bar">
                <el-select v-model="assignSel[o.id]" placeholder="分派给…" size="small" style="flex: 1">
                  <el-option v-for="w in options?.workers || []" :key="w.id" :label="w.name" :value="w.id" />
                </el-select>
                <el-button type="primary" size="small" :icon="Check" :loading="assigning === o.id" @click="doAssign(o)">分派</el-button>
                <el-button size="small" @click="doVoid(o)">作废单号</el-button>
              </div>
            </el-card>
          </div>
        </el-tab-pane>

        <el-tab-pane label="📋 任务跟踪" name="track">
          <el-table :data="orders" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
            <el-table-column label="项目" min-width="130">
              <template #default="{ row }"><b>{{ row.project_code }}</b> {{ row.project_name }}</template>
            </el-table-column>
            <el-table-column label="负责人" width="90">
              <template #default="{ row }">{{ row.worker_name || '待分派' }}</template>
            </el-table-column>
            <el-table-column label="下发资料" min-width="140">
              <template #default="{ row }">
                <el-tag v-for="f in row.input_files" :key="f.id" size="small" effect="plain"
                        class="file-chip" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
                <span v-if="!row.input_files.length">—</span>
              </template>
            </el-table-column>
            <el-table-column label="上传产物" min-width="140">
              <template #default="{ row }">
                <template v-if="row.start_files.length || row.output_files.length">
                  <el-tag v-for="f in [...row.start_files, ...row.output_files]" :key="f.id"
                          size="small" effect="plain" class="file-chip" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
                </template>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="ORDER_STATUS_TAG[row.status]" size="small">{{ ORDER_STATUS_TEXT[row.status] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="start_date" :label="options?.start_label" width="100">
              <template #default="{ row }">{{ row.start_date || '—' }}</template>
            </el-table-column>
            <el-table-column prop="due_date" :label="options?.end_label" width="100">
              <template #default="{ row }">{{ row.due_date || '—' }}</template>
            </el-table-column>
            <el-table-column label="完成" width="100">
              <template #default="{ row }">{{ row.done_date || '—' }}</template>
            </el-table-column>
            <el-table-column label="完成效率" width="85">
              <template #default="{ row }">
                <span v-if="row.eff_pct != null" :class="effClass(row)">{{ row.eff_pct }}%</span>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="190" fixed="right">
              <template #default="{ row }">
                <el-button v-if="['assigned', 'in_progress'].includes(row.status)"
                           size="small" :icon="SwitchIcon" @click="openReassign(row)">换人</el-button>
                <el-button v-if="row.status === 'done'" size="small" :icon="RefreshLeft" @click="doReopen(row)">改回</el-button>
                <el-button v-if="!['done', 'voided'].includes(row.status)" size="small" type="danger" plain
                           @click="doVoid(row)">作废</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </template>

    <EmptyHint v-else text="你没有本工作台权限" :icon="Lock" />

    <!-- 🆕 v3 M13 问题反馈面板（生产部=装配提交/主管审批；设计部=设计师接收） -->
    <FeedbackPanel v-if="dept === 'produce' || dept === 'design'" :key="dept" />

    <!-- 🆕 M07 设计师查库存 -->
    <StockQueryDialog v-if="dept === 'design'" v-model="stockVisible" />

    <!-- 🆕 M14 部门报表弹窗 -->
    <el-dialog v-model="reportVisible" :title="`📊 ${report?.dept_name || ''}报表（仅本部门数据）`" width="720px" class="v3-scroll-dialog">
      <div v-if="report">
        <div class="kpi-grid">
          <div class="kpi is-primary"><div class="kpi-v">{{ report.total }}</div><div class="kpi-l">任务总数</div></div>
          <div class="kpi is-good"><div class="kpi-v">{{ report.done }}</div><div class="kpi-l">已完成</div></div>
          <div class="kpi" :class="report.overdue ? 'is-bad' : ''"><div class="kpi-v">{{ report.overdue }}</div><div class="kpi-l">逾期</div></div>
          <div class="kpi"><div class="kpi-v">{{ report.ontime_rate ?? '—' }}%</div><div class="kpi-l">按时率 · 均效率 {{ report.avg_eff ?? '—' }}%</div></div>
        </div>
        <el-table :data="report.workers" size="small" stripe style="margin-top:10px" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="worker_name" label="人员" min-width="100" />
          <el-table-column prop="total" label="任务数" width="80" />
          <el-table-column prop="done" label="完成" width="70" />
          <el-table-column prop="ontime" label="按时" width="70" />
          <el-table-column label="逾期" width="70"><template #default="{ row }">{{ row.over }}</template></el-table-column>
          <el-table-column label="按时率" width="80"><template #default="{ row }">{{ row.rate ?? '—' }}%</template></el-table-column>
          <el-table-column label="平均效率" width="90"><template #default="{ row }"><span :class="row.avg_eff != null && row.avg_eff <= 100 ? 'eff-good' : 'eff-bad'">{{ row.avg_eff ?? '—' }}%</span></template></el-table-column>
        </el-table>
        <div class="sec-title" style="margin-top:16px">逾期任务（{{ report.overdue_items.length }}）</div>
        <el-table v-if="report.overdue_items.length" :data="report.overdue_items" size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
          <el-table-column prop="worker_name" label="人员" width="100" />
          <el-table-column prop="code" label="项目" width="120" />
          <el-table-column prop="due_date" label="预计" width="110" />
          <el-table-column prop="done_date" label="实际" width="110" />
          <el-table-column label="逾期" width="90"><template #default="{ row }">超 {{ row.over_days }} 天</template></el-table-column>
        </el-table>
        <EmptyHint v-else text="本月无逾期任务" size="sm" />
      </div>
    </el-dialog>

    <!-- ===== 完成弹窗 ===== -->
    <el-dialog v-model="completeVisible" :title="`✓ 完成任务 · ${completeOrder?.project_code || ''}（${deptName}）`" width="560px">
      <el-alert v-if="options?.sheet_check" type="info" :closable="false" style="margin-bottom: 14px"
                title="完成前将校验四个数据表均已通过 Excel 导入（项目详情页头「导入 Excel」）" />
      <el-form label-position="top">
        <el-form-item :label="`${options?.notify_label}（必选，企业微信/站内通知）`" required>
          <el-select v-model="notifyUserId" placeholder="选择通知人" size="large" style="width: 100%">
            <el-option v-for="u in options?.notify_pool || []" :key="u.id" :label="u.name" :value="u.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="options?.outputs.length" label="上传产物（完成后按目标推送下游）">
          <div class="out-rows">
            <div v-for="ot in options?.outputs || []" :key="ot.k" class="out-row">
              <span class="ol">{{ ot.label }}<span v-if="ot.required" class="req-star">*必传</span></span>
              <el-tag v-for="f in outputFilesOf(ot.k)" :key="f.id" size="small" type="success" effect="plain"><el-icon><Check /></el-icon> {{ f.name }}</el-tag>
              <el-button size="small" plain type="primary" :icon="UploadFilled" @click="pickOutputUpload(ot.k)">上传</el-button>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="completeVisible = false">取消</el-button>
        <el-button type="success" :loading="completing" @click="doComplete">确认完成</el-button>
      </template>
    </el-dialog>

    <!-- ===== 换人弹窗（M17 防离职） ===== -->
    <el-dialog v-model="reassignVisible" :title="`🔄 更换负责人 · ${reassignOrder?.project_code || ''}`" width="480px">
      <el-alert type="warning" :closable="false" style="margin-bottom: 14px"
                title="用于人员离职/请假：任务转交同部门他人，时间与已传产物保留，新负责人接续；设计/电工换人将回传项目目录" />
      <el-form label-position="top">
        <el-form-item label="当前负责人">
          <el-input :model-value="reassignOrder?.worker_name || '—'" disabled />
        </el-form-item>
        <el-form-item label="转交给" required>
          <el-select v-model="reassignWid" placeholder="选择同部门人员" style="width: 100%">
            <el-option v-for="w in reassignCandidates" :key="w.id" :label="w.name" :value="w.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassignVisible = false">取消</el-button>
        <el-button type="primary" :loading="reassigning" @click="doReassign">确认转交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.todo-grid {
  display: grid;
  /* #71 窄屏(<412px)不再横向溢出：min(380px,100%) 让单列可收缩 */
  grid-template-columns: repeat(auto-fill, minmax(min(380px, 100%), 1fr));
  gap: 14px;
}
.todo-card { border-left: 4px solid var(--primary, #2563eb); border-radius: 10px; }
.todo-card.urgent { border-left-color: var(--danger); }
.todo-card.assign { border-left-color: var(--warning); }
.tc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.tc-code { font-size: 16px; font-weight: 700; color: var(--primary, #2563eb); }
.tc-name { color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 8px; }
.tc-req {
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--primary, #2563eb);
  padding: 7px 10px; font-size: 12.5px; border-radius: 0 6px 6px 0; margin: 8px 0;
}
.tc-files { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.file-chip { cursor: pointer; }
.file-chip .dl, .file-chip .rm { margin-left: 4px; }
.file-chip .rm { color: var(--danger); }
.tc-dates { display: flex; gap: 10px; margin: 10px 0 4px; }
.tc-dates .fd { flex: 1; }
.tc-dates label { font-size: 12px; color: var(--el-text-color-secondary); display: block; margin-bottom: 4px; }
.tc-hint { font-size: 11.5px; color: var(--el-text-color-placeholder); margin: 2px 0 8px; }
.tc-kv { font-size: 12.5px; color: var(--el-text-color-secondary); margin: 6px 0 10px; }
.tc-kv b { color: var(--el-text-color-primary); }
.up-sec {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px; margin-bottom: 10px; overflow: hidden;
}
.up-h {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 12px; background: var(--el-fill-color-light);
  font-size: 12.5px; font-weight: 600;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.up-b { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.assign-bar { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
.eff-good { color: var(--success); font-weight: 700; }
.eff-bad { color: var(--danger); font-weight: 700; }
.out-rows { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.out-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.out-row .ol { font-size: 13px; min-width: 130px; }
.req-star { color: var(--danger); font-size: 12px; margin-left: 4px; }
</style>
