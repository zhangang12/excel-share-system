<script setup lang="ts">
// 🆕 管理层待办 · 全局浮动挂件（全部人可见）
//  - 收件人：右下角挂件看「我收到的待办」，回复承诺完成时间 / 标记完成 / 申请顺延
//  - admin/manager：额外可「新建待办」下发 + 监控每人进展 + 审批顺延申请
//  图标：清单打勾（方案 A）。角标 = 需我处理的条数（待回复 + 已逾期未完成）。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { adminApi } from '@/api/admin'
import { managementTodoApi, type MyTodoRow, type MgmtTodo } from '@/api/managementTodo'
import type { User } from '@/types'
import { fmtRelative } from '@/utils/format'

const auth = useAuthStore()
const isMgr = computed(() => auth.isAdmin) // hasRole('admin','manager')

const visible = ref(false)
const activeTab = ref<'mine' | 'sent'>('mine')
const myCount = ref(0)
let timer: number | undefined

// ===== 我收到的 =====
const mineList = ref<MyTodoRow[]>([])
const mineLoading = ref(false)
async function loadMine() {
  mineLoading.value = true
  try { mineList.value = await managementTodoApi.listMine() } finally { mineLoading.value = false }
  myCount.value = mineList.value.filter(r => r.status !== 'done' && (r.status === 'pending' || r.overdue)).length
}
async function refreshCount() {
  if (!auth.isLoggedIn) return
  try { myCount.value = await managementTodoApi.myCount() } catch { /* 静默 */ }
}

function open() {
  visible.value = true
  activeTab.value = 'mine'
  loadMine()
}
function onTabChange(name: any) {
  if (name === 'sent') loadSent()
  else loadMine()
}

// ===== 回复承诺完成时间 =====
const replyDlg = ref(false)
const replyTarget = ref<MyTodoRow | null>(null)
const replyForm = ref({ committed_at: '', progress: '' })
function openReply(row: MyTodoRow) {
  replyTarget.value = row
  replyForm.value = { committed_at: row.committed_at || '', progress: row.progress || '' }
  replyDlg.value = true
}
async function submitReply() {
  if (!replyTarget.value) return
  if (!replyForm.value.committed_at) { ElMessage.warning('请选择承诺完成日期'); return }
  await managementTodoApi.reply(replyTarget.value.target_id, replyForm.value.committed_at, replyForm.value.progress || undefined)
  ElMessage.success('已回复承诺完成时间')
  replyDlg.value = false
  await loadMine()
}

// ===== 标记完成 =====
async function markDone(row: MyTodoRow) {
  try {
    const { value } = await ElMessageBox.prompt('可填写完成情况说明（选填），确认标记为已完成？', `完成待办：${row.title}`, {
      confirmButtonText: '标记完成', cancelButtonText: '取消', inputType: 'textarea', inputValue: row.progress || '',
      inputPlaceholder: '完成情况说明（选填）',
    })
    await managementTodoApi.markDone(row.target_id, (value || '').trim() || undefined)
    ElMessage.success('已标记完成')
    await loadMine()
  } catch { /* 取消 */ }
}

// ===== 申请顺延 =====
const extendDlg = ref(false)
const extendTarget = ref<MyTodoRow | null>(null)
const extendForm = ref({ extend_to: '', reason: '' })
function openExtend(row: MyTodoRow) {
  extendTarget.value = row
  extendForm.value = { extend_to: '', reason: '' }
  extendDlg.value = true
}
async function submitExtend() {
  if (!extendTarget.value) return
  if (!extendForm.value.extend_to) { ElMessage.warning('请选择顺延到的新日期'); return }
  if (!extendForm.value.reason.trim()) { ElMessage.warning('请填写顺延原因'); return }
  await managementTodoApi.requestExtend(extendTarget.value.target_id, extendForm.value.extend_to, extendForm.value.reason.trim())
  ElMessage.success('顺延申请已提交，等待管理层审批')
  extendDlg.value = false
  await loadMine()
}

// ===== 管理层：新建 / 监控 =====
const sentList = ref<MgmtTodo[]>([])
const sentLoading = ref(false)
async function loadSent() {
  sentLoading.value = true
  try { sentList.value = await managementTodoApi.listSent() } finally { sentLoading.value = false }
}

const createDlg = ref(false)
const users = ref<User[]>([])
const createForm = ref<{ title: string; content: string; priority: string; recipient_ids: number[] }>(
  { title: '', content: '', priority: 'normal', recipient_ids: [] })
async function openCreate() {
  createForm.value = { title: '', content: '', priority: 'normal', recipient_ids: [] }
  createDlg.value = true
  if (!users.value.length) {
    try { users.value = await adminApi.listUsers() } catch { /* 静默 */ }
  }
}
const creating = ref(false)
async function submitCreate() {
  if (!createForm.value.title.trim()) { ElMessage.warning('请填写待办标题'); return }
  if (!createForm.value.recipient_ids.length) { ElMessage.warning('请至少勾选一个收件人'); return }
  creating.value = true
  try {
    await managementTodoApi.create({
      title: createForm.value.title.trim(),
      content: createForm.value.content.trim() || undefined,
      priority: createForm.value.priority,
      recipient_ids: createForm.value.recipient_ids,
    })
    ElMessage.success('待办已下发')
    createDlg.value = false
    await loadSent()
    refreshCount()
  } finally { creating.value = false }
}

async function removeTodo(t: MgmtTodo) {
  try {
    await ElMessageBox.confirm(`确认撤销待办「${t.title}」？收件人将不再收到该待办的提醒。`, '撤销待办', { type: 'warning' })
    await managementTodoApi.remove(t.id)
    ElMessage.success('已撤销')
    await loadSent()
  } catch { /* 取消 */ }
}

async function decideExtend(t: MgmtTodo, targetId: number, uname: string, extendTo: string, approve: boolean) {
  try {
    let note: string | undefined
    if (!approve) {
      const r = await ElMessageBox.prompt(`驳回 ${uname} 顺延到 ${extendTo} 的申请，可填写理由：`, '驳回顺延', {
        confirmButtonText: '驳回', cancelButtonText: '取消', inputPlaceholder: '理由（选填）',
      })
      note = (r.value || '').trim() || undefined
    } else {
      await ElMessageBox.confirm(`同意 ${uname} 把承诺日顺延到 ${extendTo}？`, '同意顺延', { type: 'warning' })
    }
    await managementTodoApi.decideExtend(targetId, approve, note)
    ElMessage.success(approve ? '已同意顺延' : '已驳回')
    await loadSent()
  } catch { /* 取消 */ }
}

const STATUS_TXT: Record<string, string> = { pending: '待回复', committed: '进行中', done: '已完成' }
function statusTagType(row: MyTodoRow): string {
  if (row.status === 'done') return 'success'
  if (row.overdue) return 'danger'
  if (row.status === 'pending') return 'warning'
  return 'primary'
}
function statusLabel(row: MyTodoRow): string {
  if (row.status === 'done') return '已完成'
  if (row.overdue) return '已逾期'
  return STATUS_TXT[row.status] || row.status
}

onMounted(() => {
  refreshCount()
  timer = window.setInterval(refreshCount, 60_000)
})
onUnmounted(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <!-- 右下角浮动挂件（在「反馈」按钮上方，避免重叠） -->
  <button class="mt-fab" title="管理层待办" @click="open">
    <svg class="mt-ico" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
         stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="4" y="3.5" width="16" height="17" rx="2.2" />
      <path d="M9 3.5V2.6h6v.9" />
      <path d="M7.5 8.6l1.3 1.3 2.2-2.4" />
      <path d="M7.5 13.4l1.3 1.3 2.2-2.4" />
      <line x1="13.2" y1="8" x2="16.5" y2="8" />
      <line x1="13.2" y1="12.8" x2="16.5" y2="12.8" />
      <line x1="7.6" y1="17.4" x2="16.5" y2="17.4" />
    </svg>
    <span class="mt-lbl">待办</span>
    <span v-if="myCount > 0" class="mt-badge">{{ myCount > 99 ? '99+' : myCount }}</span>
  </button>

  <el-dialog v-model="visible" title="🗒️ 管理层待办" width="720px"
             :close-on-click-modal="false" append-to-body class="v3-scroll-dialog">
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 我收到的 ============ -->
      <el-tab-pane name="mine">
        <template #label>
          <span>我收到的<el-badge v-if="myCount > 0" :value="myCount" class="tab-badge" /></span>
        </template>
        <div v-loading="mineLoading" class="mine-wrap">
          <div v-if="!mineLoading && !mineList.length" class="empty">暂无收到的待办 🎉</div>
          <div v-for="row in mineList" :key="row.target_id" class="todo-card" :class="{ done: row.status === 'done' }">
            <div class="tc-head">
              <el-tag v-if="row.priority === 'urgent'" type="danger" size="small" effect="dark">紧急</el-tag>
              <span class="tc-title">{{ row.title }}</span>
              <el-tag :type="statusTagType(row)" size="small" class="tc-status">{{ statusLabel(row) }}</el-tag>
              <span class="tc-from">来自 {{ row.creator_name || '管理层' }} · {{ fmtRelative(row.created_at) }}</span>
            </div>
            <div v-if="row.content" class="tc-content">{{ row.content }}</div>
            <div class="tc-meta">
              <span v-if="row.committed_at">承诺完成：<b :class="{ over: row.overdue }">{{ row.committed_at }}</b></span>
              <span v-if="row.done_at" class="ok">已于 {{ fmtRelative(row.done_at) }} 完成</span>
              <span v-if="row.extend_status === 'pending'" class="pend">顺延申请审批中（申请到 {{ row.extend_to }}）</span>
              <span v-if="row.extend_status === 'rejected'" class="rej">顺延申请被驳回</span>
            </div>
            <div v-if="row.progress" class="tc-progress">进展：{{ row.progress }}</div>
            <div v-if="row.status !== 'done'" class="tc-actions">
              <el-button v-if="row.status === 'pending'" type="primary" size="small" @click="openReply(row)">回复承诺完成时间</el-button>
              <template v-else>
                <el-button type="success" size="small" @click="markDone(row)">标记完成</el-button>
                <el-button size="small" @click="openReply(row)">更新承诺/进展</el-button>
                <el-button v-if="row.extend_status !== 'pending'" size="small" @click="openExtend(row)">申请顺延</el-button>
              </template>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ 下发 / 监控（仅管理层）============ -->
      <el-tab-pane v-if="isMgr" name="sent" label="下发 / 监控">
        <div class="sent-bar">
          <el-button type="primary" @click="openCreate">＋ 新建待办</el-button>
          <span class="sent-tip">下发给勾选的收件人；到承诺日仍未完成，系统每日推送逾期提醒。</span>
        </div>
        <div v-loading="sentLoading">
          <div v-if="!sentLoading && !sentList.length" class="empty">还没有下发过待办</div>
          <div v-for="t in sentList" :key="t.id" class="sent-card">
            <div class="tc-head">
              <el-tag v-if="t.priority === 'urgent'" type="danger" size="small" effect="dark">紧急</el-tag>
              <span class="tc-title">{{ t.title }}</span>
              <span class="sent-sum">
                完成 {{ t.done_count }}/{{ t.total }}
                <span v-if="t.overdue_count" class="over"> · 逾期 {{ t.overdue_count }}</span>
                <span v-if="t.pending_reply_count" class="pend"> · 待回复 {{ t.pending_reply_count }}</span>
              </span>
              <span class="tc-from">{{ t.creator_name }} · {{ fmtRelative(t.created_at) }}</span>
              <el-button type="danger" link size="small" @click="removeTodo(t)">撤销</el-button>
            </div>
            <div v-if="t.content" class="tc-content">{{ t.content }}</div>
            <table class="tg-table">
              <thead><tr><th>收件人</th><th>状态</th><th>承诺完成</th><th>进展</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="g in t.targets" :key="g.id">
                  <td>{{ g.user_name }}</td>
                  <td>
                    <span v-if="g.status === 'done'" class="ok">已完成</span>
                    <span v-else-if="g.overdue" class="over">已逾期</span>
                    <span v-else-if="g.status === 'pending'" class="pend">待回复</span>
                    <span v-else>进行中</span>
                  </td>
                  <td>{{ g.committed_at || '—' }}</td>
                  <td class="tg-prog">{{ g.progress || '—' }}</td>
                  <td>
                    <template v-if="g.extend_status === 'pending'">
                      <span class="pend">申顺延→{{ g.extend_to }}</span>
                      <el-button type="success" link size="small" @click="decideExtend(t, g.id, g.user_name || '', g.extend_to || '', true)">同意</el-button>
                      <el-button type="danger" link size="small" @click="decideExtend(t, g.id, g.user_name || '', g.extend_to || '', false)">驳回</el-button>
                    </template>
                    <span v-else class="muted">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-dialog>

  <!-- 回复承诺完成时间 -->
  <el-dialog v-model="replyDlg" :title="`回复承诺完成时间：${replyTarget?.title || ''}`" width="440px" append-to-body>
    <el-form label-position="top">
      <el-form-item label="承诺完成日期" required>
        <el-date-picker v-model="replyForm.committed_at" type="date" value-format="YYYY-MM-DD"
                        placeholder="选择日期" style="width:100%" />
      </el-form-item>
      <el-form-item label="进展说明（选填）">
        <el-input v-model="replyForm.progress" type="textarea" :rows="3" maxlength="500" show-word-limit
                  placeholder="当前进展 / 计划安排" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="replyDlg = false">取消</el-button>
      <el-button type="primary" @click="submitReply">提交</el-button>
    </template>
  </el-dialog>

  <!-- 申请顺延 -->
  <el-dialog v-model="extendDlg" :title="`申请顺延：${extendTarget?.title || ''}`" width="440px" append-to-body>
    <el-form label-position="top">
      <el-form-item label="顺延到" required>
        <el-date-picker v-model="extendForm.extend_to" type="date" value-format="YYYY-MM-DD"
                        placeholder="新的承诺完成日期" style="width:100%" />
      </el-form-item>
      <el-form-item label="顺延原因" required>
        <el-input v-model="extendForm.reason" type="textarea" :rows="3" maxlength="500" show-word-limit
                  placeholder="说明为什么需要顺延（管理层审批依据）" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="extendDlg = false">取消</el-button>
      <el-button type="warning" @click="submitExtend">提交申请</el-button>
    </template>
  </el-dialog>

  <!-- 新建待办（管理层） -->
  <el-dialog v-model="createDlg" title="新建管理层待办" width="560px" append-to-body>
    <el-form label-position="top">
      <el-form-item label="待办标题" required>
        <el-input v-model="createForm.title" maxlength="200" show-word-limit placeholder="要办的事" />
      </el-form-item>
      <el-form-item label="详情说明（选填）">
        <el-input v-model="createForm.content" type="textarea" :rows="3" maxlength="1000" show-word-limit
                  placeholder="具体要求 / 背景" />
      </el-form-item>
      <el-form-item label="优先级">
        <el-radio-group v-model="createForm.priority">
          <el-radio-button value="normal">普通</el-radio-button>
          <el-radio-button value="urgent">紧急</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="收件人（可多选）" required>
        <el-select v-model="createForm.recipient_ids" multiple filterable collapse-tags collapse-tags-tooltip
                   placeholder="勾选要下发的人" style="width:100%">
          <el-option v-for="u in users" :key="u.id" :value="u.id"
                     :label="(u.full_name || u.username) + (u.role_names?.length ? ` (${u.role_names.join('/')})` : '')" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createDlg = false">取消</el-button>
      <el-button type="primary" :loading="creating" @click="submitCreate">下发</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 浮动挂件：置于「反馈」按钮上方 */
.mt-fab {
  position: fixed; right: 22px; bottom: 84px; z-index: 2000;
  display: flex; align-items: center; gap: 6px;
  background: #0f766e; color: #fff;
  border: none; border-radius: 999px; padding: 11px 18px;
  box-shadow: 0 6px 20px rgba(15, 118, 110, .35), 0 2px 6px rgba(0,0,0,.08);
  cursor: pointer; font-size: 13.5px; font-weight: 500;
  transition: transform .15s, box-shadow .15s;
}
.mt-fab:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(15, 118, 110, .45); }
.mt-fab:active { transform: translateY(0); }
.mt-ico { flex-shrink: 0; }
.mt-lbl { line-height: 1; }
.mt-badge {
  position: absolute; top: -6px; right: -4px;
  min-width: 18px; height: 18px; padding: 0 5px;
  background: #ef4444; color: #fff; font-size: 11px; font-weight: 700;
  line-height: 18px; text-align: center; border-radius: 9px;
  box-shadow: 0 0 0 2px #fff;
}
@media (max-width: 640px) { .mt-fab { padding: 10px 14px; } .mt-lbl { display: none; } }

.tab-badge { margin-left: 6px; }

.mine-wrap { max-height: 58vh; overflow-y: auto; min-height: 120px; }
.empty { text-align: center; color: var(--text-3, #9ca3af); padding: 40px 0; font-size: 13px; }

.todo-card, .sent-card {
  border: 1px solid var(--border, #e5e7eb); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 12px; background: #fff;
}
.todo-card.done { opacity: .72; }
.tc-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tc-title { font-weight: 600; font-size: 14px; color: #1f2937; }
.tc-status { margin-left: 2px; }
.tc-from { color: var(--text-3, #9ca3af); font-size: 12px; margin-left: auto; }
.tc-content { font-size: 13px; line-height: 1.6; color: #4b5563; margin: 8px 0 4px; white-space: pre-wrap; word-break: break-word; }
.tc-meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12.5px; color: #6b7280; margin-top: 6px; }
.tc-meta b { color: #1f2937; }
.tc-meta b.over { color: #dc2626; }
.tc-meta .ok { color: #059669; }
.tc-meta .pend { color: #d97706; }
.tc-meta .rej { color: #dc2626; }
.tc-progress { font-size: 12.5px; color: #4b5563; margin-top: 6px; background: #f9fafb; border-radius: 6px; padding: 6px 10px; white-space: pre-wrap; word-break: break-word; }
.tc-actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }

.sent-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.sent-tip { color: var(--text-3, #9ca3af); font-size: 12.5px; }
.sent-sum { font-size: 12.5px; color: #374151; }
.sent-sum .over { color: #dc2626; }
.sent-sum .pend { color: #d97706; }

.tg-table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 8px; }
.tg-table th, .tg-table td { border-bottom: 1px solid #f0f1f3; padding: 6px 8px; text-align: left; vertical-align: top; }
.tg-table th { color: #6b7280; font-weight: 600; background: #f9fafb; }
.tg-prog { max-width: 180px; white-space: pre-wrap; word-break: break-word; }
.tg-table .ok { color: #059669; }
.tg-table .over { color: #dc2626; font-weight: 600; }
.tg-table .pend { color: #d97706; }
.tg-table .muted { color: #cbd5e1; }
</style>
