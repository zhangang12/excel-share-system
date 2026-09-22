<script setup lang="ts">
/**
 * 🆕 待办（手机版）——「做成跟网页版一样」（用户 2026-09-19）。
 *
 * 网页版是 ManagementTodoFloating 浮层，三个页签全量对齐到这里：
 *   ① 我收到的：回复承诺完成时间 / 更新承诺·进展 / 标记完成 / 申请顺延 / 附图预览
 *   ② 我自己的：回车即建 + 行内编辑（日期/项目/紧急/备注）——
 *      原 H5 版只能打勾和删，「日期、项目、紧急在电脑上补」这句提示从此作废
 *   ③ 下发/监控（仅 admin/manager）：新建(带附图)/编辑/撤销 + 每人进展 + 批顺延 + 完成筛选(#403)
 *
 * 接口与网页版完全同一套，后端一行没改；差异只在交互形态：
 *   · 弹窗 → 底部抽屉（拇指够得着），日期用原生 <input type=date>（WebView 自带滚轮）
 *   · 收件人勾选 → 大号可点行（网页版的小 checkbox 手机上点不中）
 *   · 附图预览：fetch blob + objectURL 的图片浮层——**不能直接 <img src=/api/...>**，
 *     附件下载端点要 Bearer 头，裸 img 标签带不上
 *
 * ⚠️ 谁能看「下发/监控」页签：H5 的 session 里没有角色（刻意的，见 session.ts），
 *    这里进页面时问一次 /auth/me 的 role_codes。装配和端点侧本来就各有权限闸，
 *    这个判断只决定页签显不显示，猜错了也越不了权。
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { http, errText } from './http'
import { toast, uiConfirm, uiPrompt } from './ui'

// ───────────────────────── 类型（与 api/managementTodo.ts 同构）─────────────────────────
interface Att { id: number; name: string; ext?: string | null; size: number }
interface MineRow {
  target_id: number; todo_id: number; title: string; content?: string | null
  priority: 'normal' | 'urgent'; due_date?: string | null
  creator_name?: string | null; created_at: string
  status: 'pending' | 'committed' | 'done'
  committed_at?: string | null; progress?: string | null; done_at?: string | null
  overdue: boolean
  extend_status?: 'pending' | 'approved' | 'rejected' | null
  extend_to?: string | null; extend_reason?: string | null
  attachments?: Att[]
}
interface SentTarget {
  id: number; user_id: number; user_name?: string | null
  status: 'pending' | 'committed' | 'done'
  committed_at?: string | null; progress?: string | null; overdue: boolean
  extend_status?: 'pending' | 'approved' | 'rejected' | null
  extend_to?: string | null; extend_reason?: string | null
}
interface SentTodo {
  id: number; title: string; content?: string | null
  priority: 'normal' | 'urgent'; due_date?: string | null
  creator_name?: string | null; created_at: string
  targets: SentTarget[]; total: number; done_count: number
  overdue_count: number; pending_reply_count: number
  attachments?: Att[]
}
interface PTodo {
  id: number; title: string; note?: string | null; due_date?: string | null
  priority: 'normal' | 'urgent'; project_id?: number | null; project_code?: string | null
  done: boolean; overdue: boolean
}
interface UserRow { id: number; full_name?: string | null; username: string; is_active: boolean }

const router = useRouter()
const err = ref('')
const tab = ref<'mine' | 'personal' | 'sent'>('mine')
const isMgr = ref(false)

function relTime(iso: string): string {
  const t = new Date(iso).getTime()
  if (!t) return ''
  const s = (Date.now() - t) / 1000
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`
  return iso.slice(0, 10)
}

// ───────────────────────── ① 我收到的 ─────────────────────────
const mine = ref<MineRow[]>([])
const mineLoading = ref(false)
// 与网页版同口径：需处理 = 待回复 + 已逾期未完成
const mineCount = computed(() =>
  mine.value.filter(r => r.status !== 'done' && (r.status === 'pending' || r.overdue)).length)

async function loadMine() {
  mineLoading.value = true
  try { mine.value = (await http.get<MineRow[]>('/management-todos/mine')).data }
  catch (e) { err.value = errText(e, '加载失败') }
  finally { mineLoading.value = false }
}

function statusLabel(r: MineRow): string {
  if (r.status === 'done') return '已完成'
  if (r.overdue) return '已逾期'
  return r.status === 'pending' ? '待回复' : '进行中'
}
function statusCls(r: MineRow): string {
  if (r.status === 'done') return 'ok'
  if (r.overdue) return 'over'
  return r.status === 'pending' ? 'pend' : 'run'
}

// 回复承诺时间 / 更新承诺·进展（同一张抽屉，网页版也是复用）
const replyOpen = ref(false)
const replyRow = ref<MineRow | null>(null)
const replyForm = ref({ committed_at: '', progress: '' })
function openReply(r: MineRow) {
  replyRow.value = r
  replyForm.value = { committed_at: r.committed_at || '', progress: r.progress || '' }
  replyOpen.value = true
}
const replyBusy = ref(false)
async function submitReply() {
  if (!replyRow.value) return
  if (!replyForm.value.committed_at) { toast('请选择承诺完成日期', 'err'); return }
  replyBusy.value = true
  try {
    await http.post(`/management-todos/${replyRow.value.target_id}/reply`,
      { committed_at: replyForm.value.committed_at, progress: replyForm.value.progress || undefined })
    replyOpen.value = false
    toast('已回复承诺时间')
    await loadMine()
  } catch (e) { toast(errText(e, '提交失败'), 'err') }
  finally { replyBusy.value = false }
}

// 标记完成（多行抽屉：完成说明可以好好写）
async function markDone(r: MineRow) {
  const note = await uiPrompt({
    title: `完成「${r.title}」`, message: '确认标记为已完成',
    textarea: true, initial: r.progress || '', placeholder: '完成情况说明（选填）', confirmText: '标记完成',
  })
  if (note === null) return
  try {
    await http.post(`/management-todos/${r.target_id}/done`, { progress: note.trim() || undefined })
    toast('已标记完成')
    await loadMine()
  } catch (e) { toast(errText(e, '操作失败'), 'err') }
}

// 申请顺延
const extOpen = ref(false)
const extRow = ref<MineRow | null>(null)
const extForm = ref({ extend_to: '', reason: '' })
function openExtend(r: MineRow) {
  extRow.value = r
  extForm.value = { extend_to: '', reason: '' }
  extOpen.value = true
}
const extBusy = ref(false)
async function submitExtend() {
  if (!extRow.value) return
  if (!extForm.value.extend_to) { toast('请选择顺延到的新日期', 'err'); return }
  if (!extForm.value.reason.trim()) { toast('请填写顺延原因', 'err'); return }
  extBusy.value = true
  try {
    await http.post(`/management-todos/${extRow.value.target_id}/extend`,
      { extend_to: extForm.value.extend_to, reason: extForm.value.reason.trim() })
    extOpen.value = false
    toast('顺延申请已提交，等待管理层审批')
    await loadMine()
  } catch (e) { toast(errText(e, '提交失败'), 'err') }
  finally { extBusy.value = false }
}

// ───────────────────────── 附图预览（blob + objectURL）─────────────────────────
const previewUrl = ref('')
const previewName = ref('')
const previewFail = ref('')
function isImg(name: string): boolean {
  return /\.(jpe?g|png|gif|bmp|webp)$/i.test(name)
}
async function previewAtt(a: Att) {
  previewName.value = a.name
  previewFail.value = ''
  previewUrl.value = ''
  if (!isImg(a.name)) { previewFail.value = '这个格式手机上暂不支持预览，请到电脑端查看'; return }
  try {
    const { data } = await http.get(`/attachments/${a.id}/download`, { responseType: 'blob' })
    previewUrl.value = URL.createObjectURL(data as Blob)
  } catch { previewFail.value = '加载失败，稍后再试' }
}
function closePreview() {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
  previewName.value = ''
  previewFail.value = ''
}
onUnmounted(closePreview)

// ───────────────────────── ② 我自己的（个人待办）─────────────────────────
const ps = ref<PTodo[]>([])
const psLoading = ref(false)
const psInput = ref('')
const psShowDone = ref(false)
const psBusy = ref<number | null>(null)
const psCount = computed(() => ps.value.filter(t => !t.done).length)
const psVisible = computed(() => psShowDone.value ? ps.value : ps.value.filter(t => !t.done))

async function loadPersonal() {
  psLoading.value = true
  try { ps.value = (await http.get<PTodo[]>('/personal-todos')).data }
  catch (e) { err.value = errText(e, '加载失败') }
  finally { psLoading.value = false }
}
async function psAdd() {
  const title = psInput.value.trim()
  if (!title) return
  psInput.value = ''
  try { await http.post('/personal-todos', { title }); await loadPersonal() }
  catch (e) { toast(errText(e, '添加失败'), 'err'); psInput.value = title }
}
async function psToggle(t: PTodo) {
  if (psBusy.value) return
  psBusy.value = t.id
  t.done = !t.done   // 先翻转再请求，失败 load 纠正（手机上等一个来回像没点上）
  try { await http.post(`/personal-todos/${t.id}/toggle`); await loadPersonal() }
  catch (e) { err.value = errText(e, '操作失败'); await loadPersonal() }
  finally { psBusy.value = null }
}
async function psDel(t: PTodo) {
  if (!(await uiConfirm({ title: `删除「${t.title}」？`, confirmText: '删除', danger: true }))) return
  try { await http.delete(`/personal-todos/${t.id}`); toast('已删除'); await loadPersonal() }
  catch (e) { toast(errText(e, '删除失败'), 'err') }
}

// 编辑（网页版行内编辑的抽屉版）：补日期/项目/紧急/备注
const psEditOpen = ref(false)
const psEditing = ref<PTodo | null>(null)
const psForm = ref({ title: '', note: '', due_date: '', priority: 'normal', project_id: '' })
const projects = ref<{ id: number; code: string; name: string }[]>([])
async function psOpenEdit(t: PTodo) {
  psEditing.value = t
  psForm.value = {
    title: t.title, note: t.note || '', due_date: t.due_date || '',
    priority: t.priority || 'normal',
    project_id: t.project_id == null ? '' : String(t.project_id),
  }
  psEditOpen.value = true
  if (!projects.value.length) {
    try {
      projects.value = ((await http.get<any[]>('/projects')).data || [])
        .map(p => ({ id: p.id, code: p.code, name: p.name }))
    } catch { projects.value = [] }
  }
}
const psSaveBusy = ref(false)
async function psSaveEdit() {
  const t = psEditing.value
  if (!t) return
  if (!psForm.value.title.trim()) { toast('待办内容不能为空', 'err'); return }
  psSaveBusy.value = true
  try {
    await http.put(`/personal-todos/${t.id}`, {
      title: psForm.value.title.trim(),
      note: psForm.value.note,
      due_date: psForm.value.due_date || null,
      priority: psForm.value.priority,
      project_id: psForm.value.project_id === '' ? null : Number(psForm.value.project_id),
    })
    psEditOpen.value = false
    toast('已保存')
    await loadPersonal()
  } catch (e) { toast(errText(e, '保存失败'), 'err') }
  finally { psSaveBusy.value = false }
}

// ───────────────────────── ③ 下发 / 监控（仅管理层）─────────────────────────
const sent = ref<SentTodo[]>([])
const sentLoading = ref(false)
type SentFilter = 'open' | 'done' | 'all'
const sentFilter = ref<SentFilter>('open')   // #403：默认只看未完成
function sentIsDone(t: SentTodo) { return (t.total || 0) > 0 && (t.done_count || 0) >= (t.total || 0) }
const sentOpen = computed(() => sent.value.filter(t => !sentIsDone(t)))
const sentDone = computed(() => sent.value.filter(t => sentIsDone(t)))
const sentShown = computed(() =>
  sentFilter.value === 'open' ? sentOpen.value
    : sentFilter.value === 'done' ? sentDone.value : sent.value)

async function loadSent() {
  sentLoading.value = true
  try { sent.value = (await http.get<SentTodo[]>('/management-todos/sent')).data }
  catch (e) { err.value = errText(e, '加载失败') }
  finally { sentLoading.value = false }
}

function tgLabel(g: SentTarget): string {
  if (g.status === 'done') return '已完成'
  if (g.overdue) return '已逾期'
  return g.status === 'pending' ? '待回复' : '进行中'
}
function tgCls(g: SentTarget): string {
  if (g.status === 'done') return 'ok'
  if (g.overdue) return 'over'
  return g.status === 'pending' ? 'pend' : 'run'
}

async function decideExtend(g: SentTarget, approve: boolean) {
  const who = g.user_name || '对方'
  let note: string | undefined
  if (approve) {
    const ok = await uiConfirm({
      title: '同意顺延？',
      message: `${who} 申请把承诺日顺延到 ${g.extend_to}` + (g.extend_reason ? `\n理由：${g.extend_reason}` : ''),
      confirmText: '同意',
    })
    if (!ok) return
  } else {
    const r = await uiPrompt({
      title: `驳回 ${who} 的顺延申请`, message: `申请顺延到 ${g.extend_to}`,
      textarea: true, placeholder: '理由（选填，会发给对方）', confirmText: '驳回',
    })
    if (r === null) return
    note = r.trim() || undefined
  }
  try {
    await http.post(`/management-todos/${g.id}/extend/decide`, { approve, note })
    toast(approve ? '已同意顺延' : '已驳回')
    await loadSent()
  } catch (e) { toast(errText(e, '操作失败'), 'err') }
}

async function removeTodo(t: SentTodo) {
  const ok = await uiConfirm({
    title: `撤销「${t.title}」？`, message: '收件人将不再收到该待办的提醒。',
    confirmText: '撤销', danger: true,
  })
  if (!ok) return
  try { await http.delete(`/management-todos/${t.id}`); toast('已撤销'); await loadSent() }
  catch (e) { toast(errText(e, '撤销失败'), 'err') }
}

// 新建 / 编辑（同一张抽屉，editingId 非空即编辑——与网页版 #366/#380 同构）
const createOpen = ref(false)
const editingId = ref<number | null>(null)
const users = ref<UserRow[]>([])
const cForm = ref<{ title: string; content: string; priority: string; due_date: string; recipient_ids: number[] }>(
  { title: '', content: '', priority: 'normal', due_date: '', recipient_ids: [] })
const cFiles = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

async function ensureUsers() {
  if (users.value.length) return
  try {
    users.value = ((await http.get<UserRow[]>('/admin/users')).data || []).filter(u => u.is_active)
  } catch (e) { err.value = errText(e, '加载人员失败') }
}
function openCreate() {
  editingId.value = null
  cForm.value = { title: '', content: '', priority: 'normal', due_date: '', recipient_ids: [] }
  cFiles.value = []
  createOpen.value = true
  void ensureUsers()
}
function openEdit(t: SentTodo) {
  editingId.value = t.id
  cForm.value = {
    title: t.title || '', content: t.content || '',
    priority: t.priority || 'normal', due_date: t.due_date || '',
    recipient_ids: (t.targets || []).map(x => x.user_id),
  }
  cFiles.value = []   // 编辑不动附图（网页版同款：附图按待办 id 挂，这里只改文字与收件人）
  createOpen.value = true
  void ensureUsers()
}
function toggleRecipient(id: number) {
  const i = cForm.value.recipient_ids.indexOf(id)
  if (i >= 0) cForm.value.recipient_ids.splice(i, 1)
  else cForm.value.recipient_ids.push(id)
}
function pickFiles() { fileInput.value?.click() }
function onFiles(e: Event) {
  const list = (e.target as HTMLInputElement).files
  for (const f of Array.from(list || [])) {
    if (!cFiles.value.some(x => x.name === f.name && x.size === f.size)) cFiles.value.push(f)
  }
  ;(e.target as HTMLInputElement).value = ''
}
const creating = ref(false)
async function submitCreate() {
  if (!cForm.value.title.trim()) { toast('请填写待办标题', 'err'); return }
  if (!cForm.value.recipient_ids.length) { toast('请至少勾选一个收件人', 'err'); return }
  creating.value = true
  try {
    if (editingId.value) {
      await http.put(`/management-todos/${editingId.value}`, {
        title: cForm.value.title.trim(), content: cForm.value.content.trim(),
        priority: cForm.value.priority, due_date: cForm.value.due_date || '',
        recipient_ids: cForm.value.recipient_ids,
      })
    } else {
      const todo = (await http.post<SentTodo>('/management-todos', {
        title: cForm.value.title.trim(), content: cForm.value.content.trim() || undefined,
        priority: cForm.value.priority, due_date: cForm.value.due_date || undefined,
        recipient_ids: cForm.value.recipient_ids,
      })).data
      // 附图逐张传，失败不阻塞（网页版同款链路）
      let fail = 0
      for (const f of cFiles.value) {
        const fd = new FormData()
        fd.append('file', f)
        fd.append('biz_type', 'management_todo')
        fd.append('biz_id', String(todo.id))
        try { await http.post('/attachments', fd) } catch { fail++ }
      }
      if (fail) toast(`${fail} 张图片上传失败，其余已随待办发出`, 'err')
      cFiles.value = []
    }
    createOpen.value = false
    toast(editingId.value ? '已修改，收件人会收到变更通知' : '待办已下发')
    await loadSent()
  } catch (e) { toast(errText(e, editingId.value ? '修改失败' : '下发失败'), 'err') }
  finally { creating.value = false }
}

// ───────────────────────── 进入 ─────────────────────────
function switchTab(t: 'mine' | 'personal' | 'sent') {
  // 🆕 再点当前页签 = 刷新（H5 没有刷新按钮，这是手机上最顺手的刷新入口）
  const isRefresh = tab.value === t
  tab.value = t
  err.value = ''
  if (t === 'mine') void loadMine()
  else if (t === 'personal') void loadPersonal()
  else void loadSent()
  if (isRefresh) toast('已刷新', 'info')
}
onMounted(async () => {
  void loadMine()
  void loadPersonal()
  try {
    const me = (await http.get<{ role_codes?: string[] }>('/auth/me')).data
    isMgr.value = !!me.role_codes?.some(c => c === 'admin' || c === 'manager')
  } catch { isMgr.value = false }
})
</script>

<template>
  <div class="wrap">
    <header class="hd">
      <button class="back" @click="router.push({ name: 'home' })" aria-label="返回">‹</button>
      <div class="ttl">待办</div>
    </header>

    <nav class="tabs">
      <button :class="{ on: tab === 'mine' }" @click="switchTab('mine')">
        我收到的<i v-if="mineCount" class="badge">{{ mineCount }}</i>
      </button>
      <button :class="{ on: tab === 'personal' }" @click="switchTab('personal')">
        我自己的<i v-if="psCount" class="badge">{{ psCount }}</i>
      </button>
      <button v-if="isMgr" :class="{ on: tab === 'sent' }" @click="switchTab('sent')">
        下发监控<i v-if="sentOpen.length" class="badge dim">{{ sentOpen.length }}</i>
      </button>
    </nav>

    <p v-if="err" class="err" @click="err = ''">{{ err }} ✕</p>

    <main class="scroll">
      <!-- ═════════ ① 我收到的 ═════════ -->
      <template v-if="tab === 'mine'">
        <div v-if="mineLoading" class="skel-list">
          <div v-for="i in 3" :key="i" class="skel-card"><i class="l1"></i><i class="l2"></i><i class="l3"></i></div>
        </div>
        <div v-else-if="!mine.length" class="hint">暂无收到的待办 🎉</div>
        <div v-for="r in mine" :key="r.target_id" class="card" :class="{ dim: r.status === 'done' }">
          <div class="c-head">
            <b v-if="r.priority === 'urgent'" class="urg">紧急</b>
            <span class="c-title">{{ r.title }}</span>
            <i class="st" :class="statusCls(r)">{{ statusLabel(r) }}</i>
          </div>
          <div class="c-from">来自 {{ r.creator_name || '管理层' }} · {{ relTime(r.created_at) }}</div>
          <div v-if="r.content" class="c-body">{{ r.content }}</div>
          <div v-if="r.attachments?.length" class="atts">
            <span v-for="a in r.attachments" :key="a.id" class="att" @click="previewAtt(a)">📎 {{ a.name }}</span>
          </div>
          <div class="c-meta">
            <span v-if="r.due_date">截止 <b :class="{ over: r.overdue }">{{ r.due_date }}</b></span>
            <span v-if="r.committed_at">承诺 <b :class="{ over: r.overdue }">{{ r.committed_at }}</b></span>
            <span v-if="r.done_at" class="ok">已于 {{ relTime(r.done_at) }} 完成</span>
            <span v-if="r.extend_status === 'pending'" class="pend">顺延审批中（→{{ r.extend_to }}）</span>
            <span v-if="r.extend_status === 'rejected'" class="over">顺延被驳回</span>
          </div>
          <div v-if="r.progress" class="c-prog">进展：{{ r.progress }}</div>
          <div v-if="r.status !== 'done'" class="c-acts">
            <button v-if="r.status === 'pending'" class="b b-pri" @click="openReply(r)">回复承诺完成时间</button>
            <template v-else>
              <button class="b b-ok" @click="markDone(r)">标记完成</button>
              <button class="b" @click="openReply(r)">更新承诺/进展</button>
              <button v-if="r.extend_status !== 'pending'" class="b" @click="openExtend(r)">申请顺延</button>
            </template>
          </div>
        </div>
      </template>

      <!-- ═════════ ② 我自己的 ═════════ -->
      <template v-else-if="tab === 'personal'">
        <div class="addbar">
          <input v-model="psInput" class="inp" placeholder="记一件事，回车添加" @keyup.enter="psAdd" />
          <button class="b b-pri" :disabled="!psInput.trim()" @click="psAdd">添加</button>
        </div>
        <div class="bar2">
          <button class="link" @click="psShowDone = !psShowDone">{{ psShowDone ? '隐藏已完成' : '显示已完成' }}</button>
          <span class="tip2">只有自己看得见 · 到期当天推一次企业微信</span>
        </div>
        <div v-if="psLoading" class="skel-list">
          <div v-for="i in 3" :key="i" class="skel-card slim"><i class="l1"></i><i class="l3"></i></div>
        </div>
        <div v-else-if="!psVisible.length" class="hint">还没有个人待办 ✍️</div>
        <div v-for="t in psVisible" :key="t.id" class="ps-row" :class="{ dim: t.done }">
          <span class="tick" :class="{ on: t.done }" @click="psToggle(t)">{{ t.done ? '✓' : '' }}</span>
          <div class="ps-main" @click="psToggle(t)">
            <div class="ps-t" :class="{ strike: t.done }">
              <b v-if="t.priority === 'urgent' && !t.done" class="urg">紧急</b>{{ t.title }}
            </div>
            <div v-if="t.due_date || t.project_code || t.note" class="ps-m">
              <i v-if="t.due_date" :class="{ over: t.overdue }">{{ t.overdue ? '已逾期 ' : '' }}{{ t.due_date }}</i>
              <i v-if="t.project_code" class="proj">{{ t.project_code }}</i>
              <i v-if="t.note">{{ t.note }}</i>
            </div>
          </div>
          <button class="link" @click="psOpenEdit(t)">编辑</button>
          <button class="link danger" @click="psDel(t)">删除</button>
        </div>
      </template>

      <!-- ═════════ ③ 下发 / 监控 ═════════ -->
      <template v-else>
        <div class="bar2">
          <button class="b b-pri" @click="openCreate">＋ 新建待办</button>
          <span class="seg">
            <button :class="{ on: sentFilter === 'open' }" @click="sentFilter = 'open'">未完成 {{ sentOpen.length }}</button>
            <button :class="{ on: sentFilter === 'done' }" @click="sentFilter = 'done'">已完成 {{ sentDone.length }}</button>
            <button :class="{ on: sentFilter === 'all' }" @click="sentFilter = 'all'">全部 {{ sent.length }}</button>
          </span>
        </div>
        <div v-if="sentLoading" class="skel-list">
          <div v-for="i in 2" :key="i" class="skel-card"><i class="l1"></i><i class="l2"></i><i class="l3"></i></div>
        </div>
        <div v-else-if="!sent.length" class="hint">还没有下发过待办</div>
        <div v-else-if="!sentShown.length" class="hint">
          {{ sentFilter === 'open' ? '下发的待办都办完了 🎉' : '还没有已完成的待办' }}
        </div>
        <div v-for="t in sentShown" :key="t.id" class="card" :class="{ dim: sentIsDone(t) }">
          <div class="c-head">
            <b v-if="t.priority === 'urgent'" class="urg">紧急</b>
            <span class="c-title">{{ t.title }}</span>
            <i v-if="sentIsDone(t)" class="st ok">已完成</i>
            <i v-else class="st run">{{ t.done_count }}/{{ t.total }}</i>
          </div>
          <div class="c-from">
            <span v-if="t.due_date">截止 {{ t.due_date }} · </span>{{ relTime(t.created_at) }}
            <span v-if="t.overdue_count" class="over"> · 逾期 {{ t.overdue_count }}</span>
            <span v-if="t.pending_reply_count" class="pend"> · 待回复 {{ t.pending_reply_count }}</span>
          </div>
          <div v-if="t.content" class="c-body">{{ t.content }}</div>
          <div v-if="t.attachments?.length" class="atts">
            <span v-for="a in t.attachments" :key="a.id" class="att" @click="previewAtt(a)">📎 {{ a.name }}</span>
          </div>
          <div class="tg" v-for="g in t.targets" :key="g.id">
            <span class="tg-name">{{ g.user_name }}</span>
            <i class="st" :class="tgCls(g)">{{ tgLabel(g) }}</i>
            <span class="tg-info">
              <template v-if="g.committed_at">承诺 {{ g.committed_at }}</template>
              <template v-if="g.progress"> · {{ g.progress }}</template>
            </span>
            <template v-if="g.extend_status === 'pending'">
              <span class="pend">申顺延→{{ g.extend_to }}</span>
              <button class="link ok" @click="decideExtend(g, true)">同意</button>
              <button class="link danger" @click="decideExtend(g, false)">驳回</button>
            </template>
          </div>
          <div class="c-acts">
            <button class="b" @click="openEdit(t)">编辑</button>
            <button class="b b-dg" @click="removeTodo(t)">撤销</button>
          </div>
        </div>
      </template>
    </main>

    <!-- ═════════ 抽屉：回复承诺 / 更新进展 ═════════ -->
    <div v-if="replyOpen" class="mask" @click.self="replyOpen = false">
      <div class="sheet">
        <div class="s-t">{{ replyRow?.status === 'pending' ? '回复承诺完成时间' : '更新承诺/进展' }}</div>
        <div class="s-sub">{{ replyRow?.title }}</div>
        <label class="f">承诺完成日期 *<input v-model="replyForm.committed_at" type="date" class="inp" /></label>
        <label class="f">进展说明（选填）<textarea v-model="replyForm.progress" class="inp" rows="3"
                          maxlength="500" placeholder="当前进展 / 计划安排"></textarea></label>
        <div class="s-acts">
          <button class="b" @click="replyOpen = false">取消</button>
          <button class="b b-pri" :disabled="replyBusy" @click="submitReply">提交</button>
        </div>
      </div>
    </div>

    <!-- ═════════ 抽屉：申请顺延 ═════════ -->
    <div v-if="extOpen" class="mask" @click.self="extOpen = false">
      <div class="sheet">
        <div class="s-t">申请顺延</div>
        <div class="s-sub">{{ extRow?.title }}</div>
        <label class="f">顺延到 *<input v-model="extForm.extend_to" type="date" class="inp" /></label>
        <label class="f">顺延原因 *<textarea v-model="extForm.reason" class="inp" rows="3"
                          maxlength="500" placeholder="说明为什么需要顺延（管理层审批依据）"></textarea></label>
        <div class="s-acts">
          <button class="b" @click="extOpen = false">取消</button>
          <button class="b b-warn" :disabled="extBusy" @click="submitExtend">提交申请</button>
        </div>
      </div>
    </div>

    <!-- ═════════ 抽屉：个人待办编辑 ═════════ -->
    <div v-if="psEditOpen" class="mask" @click.self="psEditOpen = false">
      <div class="sheet">
        <div class="s-t">编辑个人待办</div>
        <label class="f">内容 *<input v-model="psForm.title" class="inp" maxlength="200" /></label>
        <label class="f">备注<input v-model="psForm.note" class="inp" maxlength="500" placeholder="选填" /></label>
        <label class="f">截止日期<input v-model="psForm.due_date" type="date" class="inp" /></label>
        <label class="f">优先级
          <span class="seg wide">
            <button :class="{ on: psForm.priority === 'normal' }" @click="psForm.priority = 'normal'">普通</button>
            <button :class="{ on: psForm.priority === 'urgent' }" @click="psForm.priority = 'urgent'">紧急</button>
          </span>
        </label>
        <label class="f">关联项目
          <select v-model="psForm.project_id" class="inp">
            <option value="">不关联</option>
            <option v-for="p in projects" :key="p.id" :value="String(p.id)">{{ p.code }} {{ p.name }}</option>
          </select>
        </label>
        <div class="s-acts">
          <button class="b" @click="psEditOpen = false">取消</button>
          <button class="b b-pri" :disabled="psSaveBusy" @click="psSaveEdit">保存</button>
        </div>
      </div>
    </div>

    <!-- ═════════ 抽屉：新建 / 编辑下发待办 ═════════ -->
    <div v-if="createOpen" class="mask" @click.self="createOpen = false">
      <div class="sheet tall">
        <div class="s-t">{{ editingId ? '修改待办' : '新建待办' }}</div>
        <label class="f">待办标题 *<input v-model="cForm.title" class="inp" maxlength="200" placeholder="要办的事" /></label>
        <label class="f">详情说明<textarea v-model="cForm.content" class="inp" rows="2" maxlength="1000"
                          placeholder="具体要求 / 背景（选填）"></textarea></label>
        <div class="frow">
          <label class="f half">优先级
            <span class="seg wide">
              <button :class="{ on: cForm.priority === 'normal' }" @click="cForm.priority = 'normal'">普通</button>
              <button :class="{ on: cForm.priority === 'urgent' }" @click="cForm.priority = 'urgent'">紧急</button>
            </span>
          </label>
          <label class="f half">截止日期<input v-model="cForm.due_date" type="date" class="inp" /></label>
        </div>
        <div class="f">收件人 *（已选 {{ cForm.recipient_ids.length }} 人）
          <div class="people">
            <button v-for="u in users" :key="u.id" class="person"
                    :class="{ on: cForm.recipient_ids.includes(u.id) }" @click="toggleRecipient(u.id)">
              {{ u.full_name || u.username }}
            </button>
          </div>
        </div>
        <div v-if="!editingId" class="f">附图（选填）
          <div class="atts">
            <span v-for="(f, i) in cFiles" :key="i" class="att">🖼 {{ f.name }}
              <b class="rm" @click="cFiles.splice(i, 1)">✕</b></span>
            <button class="link" @click="pickFiles">＋ 选图片</button>
          </div>
          <input ref="fileInput" type="file" multiple accept=".jpg,.jpeg,.png,.gif,.bmp,.webp"
                 style="display:none" @change="onFiles" />
        </div>
        <div v-else class="tip2">编辑不改附图；收件人改动只增删差集，留下的人已回复的承诺和进展不会丢</div>
        <div class="s-acts">
          <button class="b" @click="createOpen = false">取消</button>
          <button class="b b-pri" :disabled="creating" @click="submitCreate">
            {{ editingId ? '保存修改' : '下发' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ═════════ 附图预览 ═════════ -->
    <div v-if="previewName" class="mask dark" @click.self="closePreview">
      <div class="pv">
        <div class="pv-t">{{ previewName }}<b class="rm" @click="closePreview">✕</b></div>
        <img v-if="previewUrl" :src="previewUrl" class="pv-img" />
        <div v-else-if="previewFail" class="hint" style="color:#fff">{{ previewFail }}</div>
        <div v-else class="hint" style="color:#fff">加载中…</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  height: 100%; display: flex; flex-direction: column;
  background: var(--h5-screen-wash), var(--h5-bg);
}
/* 🆕 2026-09-22 原来顶部只留 12px、没算状态栏：返回键压在状态栏底下点不到 */
.hd { display: flex; align-items: center; gap: 6px; padding: calc(var(--h5-safe-top) + 12px) 14px 6px; }
.back { border: 0; background: transparent; font-size: 26px; line-height: 1; color: var(--h5-ink, #111); padding: 0 4px; }
.ttl { font-size: 17px; font-weight: 700; color: var(--h5-ink, #111); }

.tabs { display: flex; gap: 6px; padding: 4px 14px 8px; }
.tabs > button {
  flex: 1; border: 1px solid var(--h5-line, #e5e7eb); background: rgba(255,255,255,.7);
  border-radius: 999px; padding: 8px 0; font-size: 13.5px; color: var(--h5-ink-3, #6b7280);
  position: relative;
}
.tabs > button { transition: background .18s ease, color .18s ease, transform .12s ease }
.tabs > button:active { transform: scale(.96) }
.tabs > button.on {
  background: var(--h5-grad-btn, #2B6EF6); color: #fff; border-color: transparent;
  font-weight: 600; box-shadow: var(--h5-sh-btn-sm);
}
.badge {
  position: absolute; top: -4px; right: 4px; min-width: 17px; height: 17px; border-radius: 9px;
  background: #dc2626; color: #fff; font-size: 10.5px; font-style: normal;
  display: inline-flex; align-items: center; justify-content: center; padding: 0 4px;
}
.badge.dim { background: #94a3b8; }

.err { margin: 0 14px 6px; color: #dc2626; font-size: 13px; }
.hint { padding: 28px 14px; text-align: center; color: var(--h5-ink-3, #6b7280); font-size: 14px; }
.scroll { flex: 1; overflow-y: auto; padding: 0 14px 28px; -webkit-overflow-scrolling: touch; }

.card {
  background: var(--h5-glass-strong); border: var(--h5-glass-border);
  border-radius: var(--h5-r-card); padding: 13px 14px; margin-bottom: 10px;
  box-shadow: var(--h5-sh-card);
  animation: h5FadeUp .3s ease both;
}
/* 前几张卡错峰入场——列表「铺开」而不是「砸下来」 */
.card:nth-child(2) { animation-delay: .04s }
.card:nth-child(3) { animation-delay: .08s }
.card:nth-child(4) { animation-delay: .12s }
.card.dim { opacity: .6; }
.c-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.c-title { font-size: 15px; font-weight: 600; color: var(--h5-ink, #111); flex: 1; min-width: 0; word-break: break-word; }
.urg { background: #dc2626; color: #fff; font-size: 11px; border-radius: 4px; padding: 1px 5px; font-weight: 600; flex: none; }
.st { font-style: normal; font-size: 11.5px; border-radius: 999px; padding: 2px 8px; flex: none; }
.st.ok { background: #dcfce7; color: #15803d; }
.st.over { background: #fee2e2; color: #b91c1c; }
.st.pend { background: #fef3c7; color: #a16207; }
.st.run { background: #dbeafe; color: #1d4ed8; }
.c-from { font-size: 12px; color: var(--h5-ink-3, #6b7280); margin-top: 3px; }
.c-body { font-size: 13.5px; color: var(--h5-ink-2, #374151); margin-top: 6px; white-space: pre-wrap; word-break: break-word; }
.c-meta { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12.5px; color: var(--h5-ink-3, #6b7280); margin-top: 6px; }
.c-meta b { font-weight: 600; color: var(--h5-ink-2, #374151); }
.c-meta .over, .over { color: #dc2626 !important; }
.c-meta .ok, .ok { color: #15803d; }
.c-meta .pend, .pend { color: #a16207; }
.c-prog { font-size: 12.5px; color: var(--h5-ink-2, #374151); background: rgba(0,0,0,.035); border-radius: 8px; padding: 6px 9px; margin-top: 6px; }
.c-acts { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }

.b {
  border: 1px solid var(--h5-line, #e5e7eb); background: #fff; border-radius: 999px;
  padding: 7px 14px; font-size: 13px; color: var(--h5-ink-2, #374151); font-weight: 500;
}
.b { transition: transform .12s ease, opacity .12s ease }
.b:active { transform: scale(.96) }
.b:disabled { opacity: .5; }
.b-pri { background: var(--h5-grad-btn, #2B6EF6); color: #fff; border-color: transparent; font-weight: 600; }
.b-ok { background: #16a34a; color: #fff; border-color: transparent; font-weight: 600; }
.b-warn { background: #d97706; color: #fff; border-color: transparent; font-weight: 600; }
.b-dg { color: #dc2626; border-color: #fecaca; }

.atts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; align-items: center; }
.att {
  font-size: 12px; color: var(--h5-blue, #2B6EF6); background: rgba(43,110,246,.08);
  border-radius: 8px; padding: 4px 8px; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.att .rm { margin-left: 4px; color: #94a3b8; font-weight: 600; }

.addbar { display: flex; gap: 8px; margin-bottom: 8px; }
.inp {
  width: 100%; box-sizing: border-box; border: 1px solid var(--h5-line, #e5e7eb); border-radius: 10px;
  padding: 10px 12px; font-size: 15px; background: #fff; color: var(--h5-ink, #111);
}
.addbar .inp { flex: 1; min-width: 0; }
.addbar .b { flex: none; }
.bar2 { display: flex; align-items: center; gap: 10px; margin: 2px 0 10px; flex-wrap: wrap; }
.tip2 { font-size: 11.5px; color: var(--h5-ink-4, #9ca3af); }
.link { border: 0; background: transparent; color: var(--h5-blue, #2B6EF6); font-size: 13px; padding: 4px 2px; flex: none; }
.link.danger { color: #dc2626; }
.link.ok { color: #15803d; }

.ps-row {
  display: flex; gap: 10px; align-items: flex-start;
  background: var(--h5-glass-strong); border: var(--h5-glass-border);
  border-radius: 12px; padding: 11px 12px; margin-bottom: 8px;
  box-shadow: var(--h5-sh-card2);
  animation: h5FadeUp .28s ease both;
  transition: transform .12s ease;
}
.ps-row:active { transform: scale(.985) }
.ps-row.dim { opacity: .55; }
.tick {
  flex: none; width: 22px; height: 22px; border-radius: 6px; margin-top: 1px;
  border: 1.5px solid #cbd5e1; color: #fff; font-size: 14px;
  display: flex; align-items: center; justify-content: center;
}
.tick.on { background: #16a34a; border-color: #16a34a; }
.ps-main { flex: 1; min-width: 0; }
.ps-t { font-size: 15px; color: var(--h5-ink, #111); word-break: break-word; }
.ps-t.strike { text-decoration: line-through; color: var(--h5-ink-3, #6b7280); }
.ps-t .urg { margin-right: 6px; vertical-align: 1px; }
.ps-m { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; font-size: 12px; color: var(--h5-ink-3, #6b7280); }
.ps-m i { font-style: normal; }
.ps-m .proj { color: var(--h5-blue, #2B6EF6); }

.seg { display: inline-flex; border: 1px solid var(--h5-line, #e5e7eb); border-radius: 999px; overflow: hidden; background: #fff; }
.seg.wide { width: 100%; margin-top: 4px; }
.seg > button { border: 0; background: transparent; padding: 6px 12px; font-size: 12.5px; color: var(--h5-ink-3, #6b7280); flex: 1; }
.seg > button.on { background: var(--h5-grad-btn, #2B6EF6); color: #fff; font-weight: 600; }

.tg { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12.5px;
  border-top: 1px dashed rgba(0,0,0,.07); padding: 7px 0 2px; margin-top: 7px; }
.tg-name { font-weight: 600; color: var(--h5-ink, #111); flex: none; }
.tg-info { color: var(--h5-ink-3, #6b7280); min-width: 0; }

.mask {
  position: fixed; inset: 0; background: rgba(15,23,42,.45); z-index: 60;
  display: flex; align-items: flex-end; justify-content: center;
}
.mask.dark { background: rgba(0,0,0,.82); align-items: center; }
.sheet {
  width: 100%; max-width: 560px; background: var(--h5-bg, #f4f6f9);
  border-radius: 18px 18px 0 0; padding: 16px 16px calc(16px + env(safe-area-inset-bottom));
  max-height: 86vh; overflow-y: auto;
}
.s-t { font-size: 16px; font-weight: 700; color: var(--h5-ink, #111); }
.s-sub { font-size: 12.5px; color: var(--h5-ink-3, #6b7280); margin-top: 2px; }
.f { display: block; font-size: 12.5px; color: var(--h5-ink-3, #6b7280); margin-top: 12px; }
.f .inp, .f textarea.inp, .f select.inp { margin-top: 4px; }
.frow { display: flex; gap: 10px; }
.f.half { flex: 1; min-width: 0; }
.s-acts { display: flex; gap: 10px; margin-top: 16px; }
.s-acts .b { flex: 1; padding: 11px 0; font-size: 14.5px; text-align: center; }

.people { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 6px; }
.person {
  border: 1px solid var(--h5-line, #e5e7eb); background: #fff; border-radius: 999px;
  padding: 7px 13px; font-size: 13px; color: var(--h5-ink-2, #374151);
}
.person.on { background: var(--h5-grad-btn, #2B6EF6); color: #fff; border-color: transparent; font-weight: 600; }

.pv { max-width: 94vw; max-height: 90vh; display: flex; flex-direction: column; gap: 8px; }
.pv-t { color: #fff; font-size: 13px; display: flex; align-items: center; gap: 10px; }
.pv-t .rm { margin-left: auto; font-size: 18px; padding: 4px 8px; }
.pv-img { max-width: 94vw; max-height: 80vh; object-fit: contain; border-radius: 8px; background: #fff; }

/* ── 骨架屏：加载时给出内容轮廓，比一行「加载中…」踏实 ── */
.skel-list { display: flex; flex-direction: column; gap: 10px; }
.skel-card {
  background: var(--h5-glass-strong); border: var(--h5-glass-border);
  border-radius: var(--h5-r-card); padding: 14px;
  display: flex; flex-direction: column; gap: 9px;
}
.skel-card i {
  display: block; height: 13px; border-radius: 6px;
  background: linear-gradient(90deg, rgba(0,0,0,.05) 25%, rgba(0,0,0,.10) 45%, rgba(0,0,0,.05) 65%);
  background-size: 220% 100%;
  animation: skelWave 1.3s ease-in-out infinite;
}
.skel-card .l1 { width: 62% }
.skel-card .l2 { width: 88%; height: 11px }
.skel-card .l3 { width: 38%; height: 11px }
.skel-card.slim { flex-direction: row; align-items: center }
.skel-card.slim .l1 { flex: 1 }
.skel-card.slim .l3 { width: 52px }
@keyframes skelWave { 0% { background-position: 180% 0 } 100% { background-position: -60% 0 } }
@media (prefers-reduced-motion: reduce) {
  .card, .ps-row { animation: none !important }
  .skel-card i { animation: none }
}
</style>
