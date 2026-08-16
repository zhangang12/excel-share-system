<script setup lang="ts">
/**
 * 🆕 反馈#382「个人待办的功能同步做到 APP 上」（配套 #363/#381）。
 *
 * ⚠️ 设计稿里写的「放底部『我的』页里」落不了地：**H5 根本没有底部导航栏、也没有「我的」页**
 *    （H5App.vue 只有一个 router-view，全部页面 = 登录 / 首页 / 聊天）。
 *    业务选「底部我的」而不是「首页」，意思是**别挤占首页**，所以这里做成独立一页，
 *    入口放首页顶栏 —— 意图落到了，又不用为一个功能凭空造一条底部导航。
 *
 * 手机上的交互按手机来（设计稿二期）：
 *   · 整行点一下 = 打勾 / 取消打勾（不用瞄准小方框）
 *   · 左滑露出「删除」（不做拖动排序：手机上拖动很难用）
 *   · 顶部一个输入框回车即建，日期/项目/紧急去网页版补
 *
 * 接口与网页版**完全同一套** `/personal-todos`，后端一行没为 H5 改过。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { http, errText } from './http'

interface Todo {
  id: number
  title: string
  note?: string | null
  due_date?: string | null
  priority: 'normal' | 'urgent'
  project_code?: string | null
  done: boolean
  overdue: boolean
}

const router = useRouter()
const rows = ref<Todo[]>([])
const loading = ref(false)
const err = ref('')
const input = ref('')
const showDone = ref(false)
const busy = ref<number | null>(null)

const visible = computed(() => showDone.value ? rows.value : rows.value.filter(t => !t.done))
const undoneCount = computed(() => rows.value.filter(t => !t.done).length)

async function load() {
  loading.value = true
  err.value = ''
  try { rows.value = (await http.get<Todo[]>('/personal-todos')).data }
  catch (e) { err.value = errText(e, '加载失败') }
  finally { loading.value = false }
}
onMounted(load)

async function add() {
  const title = input.value.trim()
  if (!title) return
  input.value = ''
  try { await http.post('/personal-todos', { title }); await load() }
  catch (e) { err.value = errText(e, '添加失败'); input.value = title }
}

async function toggle(t: Todo) {
  if (busy.value) return
  busy.value = t.id
  // 先本地翻转再发请求：手机上等一个来回会让人觉得没点上，失败了 load() 会纠正回来
  t.done = !t.done
  try { await http.post(`/personal-todos/${t.id}/toggle`); await load() }
  catch (e) { err.value = errText(e, '操作失败'); await load() }
  finally { busy.value = null }
}

async function del(t: Todo) {
  if (!confirm(`删除「${t.title}」？`)) { swipedId.value = null; return }
  try { await http.delete(`/personal-todos/${t.id}`); swipedId.value = null; await load() }
  catch (e) { err.value = errText(e, '删除失败') }
}

// ---- 左滑露出删除 ----
// 只认「横向位移明显大于纵向」的滑动，否则会把上下滚动误判成左滑，列表就滚不动了。
const swipedId = ref<number | null>(null)
let sx = 0, sy = 0, tracking = false
function onStart(e: TouchEvent, t: Todo) {
  sx = e.touches[0].clientX; sy = e.touches[0].clientY; tracking = true
  if (swipedId.value && swipedId.value !== t.id) swipedId.value = null
}
function onMove(e: TouchEvent, t: Todo) {
  if (!tracking) return
  const dx = e.touches[0].clientX - sx
  const dy = e.touches[0].clientY - sy
  if (Math.abs(dy) > Math.abs(dx)) { tracking = false; return }   // 竖向滚动，放行
  if (dx < -36) { swipedId.value = t.id; tracking = false }
  else if (dx > 24 && swipedId.value === t.id) { swipedId.value = null; tracking = false }
}
function onEnd() { tracking = false }
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <header class="hd">
        <button class="back" @click="router.push({ name: 'home' })" aria-label="返回">‹</button>
        <div class="ttl">
          <div class="t1">我的待办</div>
          <div class="t2">只有自己看得见 · 未完成 {{ undoneCount }} 件</div>
        </div>
        <button class="tbtn" @click="showDone = !showDone">{{ showDone ? '隐藏已完成' : '显示已完成' }}</button>
      </header>

      <div class="addbar">
        <input v-model="input" class="addinp" placeholder="记一件事，回车添加" @keyup.enter="add" />
        <button class="addbtn" :disabled="!input.trim()" @click="add">添加</button>
      </div>

      <p v-if="err" class="err">{{ err }}</p>

      <main class="scroll">
        <div v-if="loading" class="hint">加载中…</div>
        <div v-else-if="!visible.length" class="hint">还没有待办 ✍️</div>

        <div v-for="t in visible" :key="t.id" class="rowwrap">
          <div class="row" :class="{ done: t.done, swiped: swipedId === t.id }"
               @touchstart="onStart($event, t)" @touchmove="onMove($event, t)"
               @touchend="onEnd" @click="toggle(t)">
            <span class="tick" :class="{ on: t.done }">{{ t.done ? '✓' : '' }}</span>
            <span class="main">
              <span class="title" :class="{ strike: t.done }">
                <b v-if="t.priority === 'urgent' && !t.done" class="urg">紧急</b>{{ t.title }}
              </span>
              <span v-if="t.due_date || t.project_code || t.note" class="meta">
                <i v-if="t.due_date" :class="{ over: t.overdue }">
                  {{ t.overdue ? '已逾期 ' : '' }}{{ t.due_date }}
                </i>
                <i v-if="t.project_code" class="proj">{{ t.project_code }}</i>
                <i v-if="t.note" class="note">{{ t.note }}</i>
              </span>
            </span>
          </div>
          <button class="delbtn" :class="{ show: swipedId === t.id }" @click.stop="del(t)">删除</button>
        </div>

        <div class="tip">左滑一行可以删除；日期、项目、紧急在电脑上补</div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.wrap { height: 100%; display: flex; flex-direction: column; background: var(--h5-bg); }
.panel { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.hd {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 14px 10px; border-bottom: 1px solid var(--h5-line, #e5e7eb);
}
.back {
  border: 0; background: transparent; font-size: 26px; line-height: 1;
  color: var(--h5-fg, #111); padding: 0 4px; cursor: pointer;
}
.ttl { flex: 1; min-width: 0; }
.t1 { font-size: 17px; font-weight: 700; color: var(--h5-fg, #111); }
.t2 { font-size: 12px; color: var(--h5-fg-2, #6b7280); margin-top: 2px; }
.tbtn {
  border: 1px solid var(--h5-line, #e5e7eb); background: transparent; border-radius: 999px;
  padding: 5px 10px; font-size: 12px; color: var(--h5-fg-2, #6b7280);
}
.addbar { display: flex; gap: 8px; padding: 10px 14px; }
.addinp {
  flex: 1; min-width: 0; border: 1px solid var(--h5-line, #e5e7eb); border-radius: 10px;
  padding: 10px 12px; font-size: 15px; background: var(--h5-card, #fff); color: var(--h5-fg, #111);
}
.addbtn {
  border: 0; border-radius: 10px; padding: 0 16px; font-size: 15px; font-weight: 600;
  background: var(--h5-accent, #d4a05a); color: #fff;
}
.addbtn:disabled { opacity: .45; }
.err { margin: 0 14px 8px; color: #dc2626; font-size: 13px; }
.hint { padding: 30px 14px; text-align: center; color: var(--h5-fg-2, #6b7280); font-size: 14px; }
.scroll { flex: 1; overflow-y: auto; padding: 0 14px 24px; -webkit-overflow-scrolling: touch; }

.rowwrap { position: relative; overflow: hidden; border-radius: 12px; margin-bottom: 8px; }
.row {
  display: flex; gap: 10px; align-items: flex-start;
  background: var(--h5-card, #fff); border: 1px solid var(--h5-line, #e5e7eb);
  border-radius: 12px; padding: 12px; transition: transform .16s ease;
}
.row.swiped { transform: translateX(-76px); }
.row.done { opacity: .55; }
.tick {
  flex: none; width: 22px; height: 22px; border-radius: 6px; margin-top: 1px;
  border: 1.5px solid var(--h5-line, #cbd5e1); color: #fff; font-size: 14px;
  display: flex; align-items: center; justify-content: center;
}
.tick.on { background: #16a34a; border-color: #16a34a; }
.main { flex: 1; min-width: 0; }
.title { font-size: 15px; color: var(--h5-fg, #111); word-break: break-word; }
.title.strike { text-decoration: line-through; color: var(--h5-fg-2, #6b7280); }
.urg {
  display: inline-block; background: #dc2626; color: #fff; font-size: 11px;
  border-radius: 4px; padding: 1px 5px; margin-right: 6px; vertical-align: 1px;
}
.meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; font-size: 12px; color: var(--h5-fg-2, #6b7280); }
.meta i { font-style: normal; }
.meta .over { color: #dc2626; font-weight: 600; }
.meta .proj { color: var(--h5-accent, #d4a05a); }
.delbtn {
  position: absolute; right: 0; top: 0; bottom: 0; width: 72px;
  border: 0; background: #dc2626; color: #fff; font-size: 14px; font-weight: 600;
  opacity: 0; pointer-events: none; transition: opacity .16s ease;
}
.delbtn.show { opacity: 1; pointer-events: auto; }
.tip { text-align: center; font-size: 12px; color: var(--h5-fg-2, #6b7280); padding: 14px 0 4px; }
</style>
