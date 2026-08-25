<script setup lang="ts">
/**
 * H5 助手主界面：登录进来就是对话。
 * 查询走 /agent/chat（复用现有 LLM + 工具 + 降级），审批走卡片组件。
 *
 * 这里刻意不做业务页面——手机上填单子是灾难，看数和批单才合适。
 */
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { http, errText } from './http'
import { api } from './apiBase'
import { clearSession, displayName } from './session'
import { renderMd } from './markdown'
import { useViewportHeight } from './useViewport'
import { useSpeech } from './useSpeech'
import H5ApproveCard from './H5ApproveCard.vue'
import { isKnownCard, cardTitle, type AgentCard } from './cardRegistry'

const router = useRouter()
const route = useRoute()

interface CardSummary {
  count: number; total: number; oldest_days: number; shown: number
  groups: { key: string; label: string; count: number; amount: number; note: string }[]
}

/** 🆕 提问卡：模型拿不准时给几个选项，点一下就把 send 发出去，
 *  省得用户自己重新打一遍问题。 */
interface Ask { q: string; options: { label: string; send: string }[] }

type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'ai'; text: string; sources?: string[]; ask?: Ask }
  // 汇总卡：先给总账，点「逐条处理」才展开明细。一次弹 20 张没人看得下去。
  // 汇总 → 列表 → 单笔明细，三级都能退回。
  // 原来是 expanded 布尔：一点就把 20 张卡全铺开，而且**收不回去**。
  | { kind: 'summary'; summary: CardSummary; cards: AgentCard[]
      view: 'sum' | 'list' | 'one'; ref?: number }
  | { kind: 'cards'; cards: AgentCard[] }

const msgs = ref<Msg[]>([])
const input = ref('')
const thinking = ref(false)
const toolHint = ref('')   // 「正在查 xxx…」，工具轮次不流正文，给个状态别让人以为卡住了
const scroller = ref<HTMLElement>()
/** 🆕 输入法（尤其是拼音候选框）弹出时用可视视口驱动高度，
 *  否则输入框会被候选框盖住看不见。见 useViewport.ts */
const panel = ref<HTMLElement>()
const pending = ref<{ count: number; amount_total: number; blocked: number }>(
  { count: 0, amount_total: 0, blocked: 0 })
const suggestions = ref<string[]>(['这月销售额多少？', '哪些供应商老迟到？', '待我审批的'])
/** 走卡片通道的精确入口文案；只有这几条，别改成模糊匹配 */
// ⚠️ 这几条是**精确文案**，别改成模糊匹配（改过一次，把用户自己打的
//    「查询一下所有的待审批的待办?」也劫持成查请款单）。
//    「待我审批的」不带类别 —— /cards/pending 现在把请款和 OA 一起返回。
const CARD_ENTRIES = new Set(['待我审批的', '待我审批的请款单', '待我审批', '请款审批', 'OA审批'])

const greet = computed(() => {
  const h = new Date().getHours()
  return h < 6 ? '夜里好' : h < 12 ? '早上好' : h < 18 ? '下午好' : '晚上好'
})
const who = displayName
const today = computed(() => {
  const d = new Date()
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})
const amountText = computed(() =>
  '¥' + pending.value.amount_total.toLocaleString('zh-CN', { maximumFractionDigits: 0 }))
const showWelcome = computed(() => msgs.value.length === 0)

async function scrollDown() {
  await nextTick()
  scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' })
}

async function loadPending() {
  try {
    const { data } = await http.get('/agent/cards/pending')
    pending.value = { count: data.count, amount_total: data.amount_total, blocked: data.blocked }
  } catch { /* 拿不到待办不影响聊天，静默 */ }
}

async function openApprovals(cardType = 'pay_req_approve', label?: string) {
  thinking.value = true
  // ⚠️ 别给 label 写死默认值。曾经默认成「待我审批的请款单」，
  //    而首页「今天该管的」只传 cardType 不传 label，结果点回款登记、
  //    点销售订单，气泡统统显示「待我审批的请款单」——三类卡片一个名字。
  msgs.value.push({ kind: 'user', text: label || cardTitle(cardType) })
  await scrollDown()
  try {
    const url = cardType === 'pay_req_approve'
      ? '/agent/cards/pending' : `/agent/cards/${cardType}`
    const { data } = await http.get(url)
    // 白名单在前端再过一道：后端给了未登记的 type 就整张不渲染（原则三）
    // 白名单在前端再过一道；再把能批的排前面——批不了的压后，别让人先划过一堆灰按钮
    const cards = (data.cards as AgentCard[])
      .filter((c) => isKnownCard(c.type))
      .sort((a, b) => Number(a.flags.some((f) => f.level === 'block'))
                    - Number(b.flags.some((f) => f.level === 'block')))
    const dropped = data.cards.length - cards.length
    const n = cards.length
    const hasSummary = !!data.summary && n > 3
    if (!hasSummary) {
      msgs.value.push({
        kind: 'ai',
        text: n === 0
          ? '这一类现在没有待办。想看别的可以直接问我。'
          : `共 ${n} 单${data.blocked ? `，其中 ${data.blocked} 单按职责分离需他人处理` : ''}。`
            + (dropped
              // ⚠️ 别写成「无法安全展示」——那听着像出了安全事故，而实际原因几乎总是
              //   **版本差**：后端上了新卡片类型，手上的前端还不认识它（白名单挡掉）。
              //   2026-08-26 加 OA 审批卡时用户就被这句话问住了。说清楚怎么办才有用。
              ? `（另有 ${dropped} 条是新类型，你这个版本还显示不了：`
                + `重开一次 APP 或刷新页面；还不行就到电脑端看）`
              : ''),
      })
    }
    if (n) {
      if (data.summary && n > 3) {
        msgs.value.push({ kind: 'summary', summary: data.summary, cards, view: 'sum' })
      } else {
        msgs.value.push({ kind: 'cards', cards })
      }
    }
  } catch (e: any) {
    msgs.value.push({ kind: 'ai', text: errText(e, '取待办失败，请稍后重试') })
  } finally {
    thinking.value = false
    await scrollDown()
  }
}

async function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q || thinking.value) return
  input.value = ''
  // ⚠️ 只对「门户/建议按钮」这几条精确文案走卡片通道。
  //   曾经用模糊正则(/待审|审批/)拦截，结果把用户自己打的
  //   「查询一下所有的待审批的待办?」也劫持成查请款单——用户打的字必须原样送模型，
  //   宁可模型答不好，也不能把问题偷换掉。
  if (CARD_ENTRIES.has(q)) return openApprovals()
  // 门户带 card= 进来的，走对应类型的卡片通道
  const ct = route.query.card
  if (typeof ct === 'string' && ct && msgs.value.length === 0) return openApprovals(ct, q)

  msgs.value.push({ kind: 'user', text: q })
  thinking.value = true
  await scrollDown()
  try {
    const history = msgs.value
      .filter((m): m is Extract<Msg, { kind: 'user' | 'ai' }> => m.kind === 'user' || m.kind === 'ai')
      .slice(-10)
      .map((m) => ({ role: m.kind === 'user' ? 'user' : 'assistant', content: m.text }))
    await streamChat(q, history.slice(0, -1))
  } catch (e: any) {
    msgs.value.push({ kind: 'ai', text: errText(e, '暂时问不通，请稍后再试') })
  } finally {
    thinking.value = false
    await scrollDown()
  }
}

// 语音：能力探测不通过就整个不显示按钮，别摆一个点了没反应的
const speech = useSpeech((text, final) => {
  input.value = text
  if (final && text.trim()) send()
})

/**
 * SSE 流式问答。用 fetch + ReadableStream 而不是 EventSource——
 * EventSource 只能 GET，带不了 Authorization 头，也发不了 body。
 *
 * 总时长压不下去（模型出字就那么快），但第一个字通常 1-2s 内就到，
 * 人不再对着白屏干等 17 秒。
 */
/**
 * 本次会话 id。进这一页时生成一次，整段对话共用。
 *
 * ⚠️ 用 `crypto.randomUUID` 要**留兜底**：它需要安全上下文，
 *   在 http 的网页版（非 localhost）里根本取不到，直接调会抛异常把发消息整条带崩。
 */
const sessionId = (() => {
  try {
    const c = (window as any).crypto
    if (c?.randomUUID) return c.randomUUID()
  } catch { /* 取不到就用下面的兜底 */ }
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
})()

async function streamChat(q: string, history: { role: string; content: string }[]) {
  // ⚠️ 关键：push 进去之后必须用**数组里的那个引用**（Vue 的响应式代理）来累加文字。
  //   直接改 push 之前的原始对象，改的是 raw target，不走代理的 set 陷阱，
  //   Vue 收不到通知——表现就是「只显示第一个字，后面全不动」。
  //   第一个字之所以能出来，是因为同一批里 thinking.value=false 触发了一次渲染。
  const draft: Extract<Msg, { kind: 'ai' }> = { kind: 'ai', text: '' }
  let bubble = draft
  let opened = false
  // 走 api()：APP 里是绝对地址（页面在 localhost，API 在服务器）
  const res = await fetch(api('/agent/chat/stream'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${localStorage.getItem('pms_token') || ''}`,
    },
    // session_id 只用于把日志里的多轮串起来分析（问了几遍才问明白），
    // 不参与鉴权、不影响任何数据可见性
    body: JSON.stringify({ message: q, history, session_id: sessionId }),
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    // SSE 以空行分隔事件；最后一段可能不完整，留在缓冲里等下一片
    const parts = buf.split('\n\n')
    buf = parts.pop() || ''
    for (const raw of parts) {
      let ev = 'message', data = ''
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      let d: any
      try { d = JSON.parse(data) } catch { continue }
      if (ev === 'tool') {
        toolHint.value = `正在查 ${d.label}…`
      } else if (ev === 'delta') {
        if (!opened) {
          opened = true; thinking.value = false; toolHint.value = ''
          msgs.value.push(draft)
          bubble = msgs.value[msgs.value.length - 1] as Extract<Msg, { kind: 'ai' }>
        }
        bubble.text += d.text
        await scrollDown()
      } else if (ev === 'done') {
        if (!opened) {
          msgs.value.push(draft)
          bubble = msgs.value[msgs.value.length - 1] as Extract<Msg, { kind: 'ai' }>
        }
        bubble.sources = d.sources
        if (d.ask?.options?.length) bubble.ask = d.ask
        // 🆕 本轮模型拟了待办草稿 → 渲染成卡片，点「确认发出」才真发。
        //    模型自己发不出去（见 agent_router.send_draft），这张卡是唯一的出口。
        if (d.cards?.length) {
          const cs = (d.cards as AgentCard[]).filter((c) => isKnownCard(c.type))
          if (cs.length) msgs.value.push({ kind: 'cards', cards: cs })
        }
        if (d.suggestions?.length) suggestions.value = d.suggestions
        toolHint.value = ''
      } else if (ev === 'error') {
        if (!opened) {
          opened = true
          msgs.value.push(draft)
          bubble = msgs.value[msgs.value.length - 1] as Extract<Msg, { kind: 'ai' }>
        }
        bubble.text += d.message || '出错了'
      }
    }
  }
}

/** 列表行的主副文案。卡片的 facts 是后端排好序的，第一条是客户/项目，
 *  emphasis 的那条是金额——直接复用，不为了列表再开一个接口。 */
function rowOf(c: AgentCard) {
  const money = c.facts.find((f) => f.emphasis)
  const warn = c.flags.find((f) => f.level === 'warn')
  return {
    title: c.facts[0]?.v || `#${c.ref}`,
    money: money ? `${money.k} ${money.v}` : '',
    warn: warn?.msg || '',
    blocked: c.flags.some((f) => f.level === 'block'),
  }
}

function onCardDone() {
  loadPending()
}

/** 明细页里处理完一笔：从列表去掉并退回列表。
 *  留在原地会让人以为没生效，还得自己想办法退出去。 */
function onSummaryCardDone(m: Extract<Msg, { kind: 'summary' }>, ref: number) {
  loadPending()
  m.cards = m.cards.filter((c) => c.ref !== ref)
  m.view = m.cards.length ? 'list' : 'sum'
}

function logout() {
  clearSession()
  router.replace('/login')
}

// 🆕 输入法弹出（含拼音候选框）时把可视区高度同步到面板上，
// 并把消息列表顶到底 —— 否则最后一条消息会被顶出可视区，
// 用户一边打字一边看不见自己刚发的那句。
useViewportHeight(panel, () => {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
})

onMounted(() => {
  loadPending()
  // 从门户点卡片进来：带着问题直接发，用户不用打字
  const q = route.query.q
  if (typeof q === 'string' && q.trim()) { send(q.trim()); return }
  // 只带 card 不带 q（首页「今天该管的」就是这样跳过来的）也要直接开卡片。
  // 原来 card 只在 send() 里读，光带 card 进来会停在空白对话页，等于点了没反应。
  const ct = route.query.card
  if (typeof ct === 'string' && ct.trim()) openApprovals(ct.trim())
})
</script>

<template>
  <div class="wrap">
    <div class="panel" ref="panel">
      <!-- 顶栏 -->
      <header class="hd">
        <button class="back" @click="router.push({ name: 'home' })" aria-label="返回">‹</button>
        <div class="ttl">
          <div class="t1">同辉项目管理智能体</div>
          <div class="t2"><i class="dot"></i>在线 · {{ who }} · {{ today }}</div>
        </div>
        <button class="more" @click="logout" aria-label="退出">···</button>
      </header>

      <!-- 消息流 -->
      <main ref="scroller" class="scroll">
        <div v-if="showWelcome" class="welcome">
          <div class="orb-wrap">
            <div class="orb-glow"></div>
            <div class="orb">
              <svg width="30" height="30" viewBox="0 0 20 20" fill="none">
                <path d="M10 2.5c.9 4.2 3.3 6.6 7.5 7.5-4.2.9-6.6 3.3-7.5 7.5-.9-4.2-3.3-6.6-7.5-7.5 4.2-.9 6.6-3.3 7.5-7.5Z" fill="#fff"/>
              </svg>
            </div>
          </div>
          <div class="wt">{{ greet }}，{{ who }}</div>
          <div class="ws">查数据、批单子，一句话交给我</div>

          <button v-if="pending.count" class="sumcard" @click="openApprovals()">
            <div class="sk">等你签字</div>
            <div class="sv">{{ amountText }}</div>
            <div class="chips">
              <span class="h5-pill h5-pill--blue">{{ pending.count }} 件待办</span>
              <span v-if="pending.blocked" class="h5-pill h5-pill--warn">
                {{ pending.blocked }} 件需他人处理
              </span>
            </div>
          </button>
        </div>

        <template v-for="(m, i) in msgs" :key="i">
          <div v-if="m.kind === 'user'" class="row me"><div class="bubble mine">{{ m.text }}</div></div>
          <div v-else-if="m.kind === 'ai'" class="row">
            <div class="bubble theirs">
              <!-- renderMd 里 html:false，模型输出的原始 HTML 会被转义成文本；
                   这两处是成对的，改任一处都会打穿 XSS 防线（手册 3.4.2） -->
              <div class="md" v-html="renderMd(m.text)"></div>
              <!-- 🆕 提问卡：点一下直接把 send 发出去，不用自己再打一遍 -->
              <div v-if="m.ask" class="ask">
                <div v-if="m.ask.q" class="ask-q">{{ m.ask.q }}</div>
                <button v-for="(o, oi) in m.ask.options" :key="oi" class="ask-opt"
                        :disabled="thinking" @click="send(o.send)">{{ o.label }}</button>
              </div>
              <div v-if="m.sources?.length" class="src">来源：{{ m.sources.join('、') }}</div>
            </div>
          </div>
          <div v-else-if="m.kind === 'summary'" class="cards">
            <!-- ① 总账 -->
            <div v-if="m.view === 'sum'" class="sumcard">
              <div class="sk">合计待处理</div>
              <div class="sv">¥{{ m.summary.total.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}</div>
              <div class="ssub">{{ m.summary.count }} 笔 · 最久的挂了 {{ m.summary.oldest_days }} 天</div>
              <div class="sgrp">
                <div v-for="g in m.summary.groups" :key="g.key" class="sg">
                  <div class="sgl">{{ g.label }}</div>
                  <div class="sgv">¥{{ g.amount.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) }}
                    <span class="sgn">{{ g.count }} 笔</span></div>
                  <div class="sgd">{{ g.note }}</div>
                </div>
              </div>
              <button class="sbtn" @click="m.view = 'list'">
                逐条处理（{{ m.cards.length }} 笔）
              </button>
            </div>

            <!-- ② 列表：一行一笔，点进去才看明细 -->
            <div v-else-if="m.view === 'list'" class="lst">
              <button class="lhd" @click="m.view = 'sum'">
                <span class="lback">‹</span>
                <span>共 {{ m.cards.length }} 笔 · 点一笔处理</span>
              </button>
              <button v-for="(c, k) in m.cards" :key="c.ref" class="lrow"
                      @click="m.view = 'one'; m.ref = c.ref">
                <span class="lno">{{ k + 1 }}</span>
                <span class="lmain">
                  <span class="lt">{{ rowOf(c).title }}</span>
                  <span v-if="rowOf(c).warn" class="lw">{{ rowOf(c).warn }}</span>
                </span>
                <span class="lright">
                  <span class="lm">{{ rowOf(c).money }}</span>
                  <span v-if="rowOf(c).blocked" class="lb">需他人处理</span>
                </span>
                <span class="lgo">›</span>
              </button>
            </div>

            <!-- ③ 单笔明细 -->
            <template v-else>
              <button class="lhd" @click="m.view = 'list'">
                <span class="lback">‹</span><span>返回列表</span>
              </button>
              <H5ApproveCard v-for="c in m.cards.filter((x) => x.ref === m.ref)" :key="c.ref"
                             :card="c" :index="m.cards.findIndex((x) => x.ref === m.ref) + 1"
                             :total="m.cards.length" @done="onSummaryCardDone(m, c.ref)" />
            </template>
          </div>
          <div v-else class="cards">
            <H5ApproveCard v-for="(c, k) in m.cards" :key="c.ref" :card="c"
                           :index="k + 1" :total="m.cards.length" @done="onCardDone" />
          </div>
        </template>

        <div v-if="thinking" class="row">
          <div class="bubble theirs dots">
            <i></i><i></i><i></i>
            <span v-if="toolHint" class="thint">{{ toolHint }}</span>
          </div>
        </div>
      </main>

      <!-- 底部：快捷问答 + 输入 -->
      <footer class="ft">
        <div v-if="showWelcome" class="sugg">
          <button v-for="s in suggestions" :key="s" class="chip" @click="send(s)">{{ s }}</button>
        </div>
        <div v-if="speech.error.value" class="serr">{{ speech.error.value }}</div>
        <div class="composer">
          <input v-model="input" :placeholder="speech.listening.value ? '正在听…' : '问点什么…'"
                 @keyup.enter="send()" />
          <button v-if="speech.supported" class="mic" :class="{ on: speech.listening.value }"
                  @click="speech.toggle()" :aria-label="speech.listening.value ? '停止' : '语音输入'">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
              <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor"/>
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3" stroke="currentColor" stroke-width="2"
                    stroke-linecap="round"/>
            </svg>
          </button>
          <button class="send" :disabled="thinking || !input.trim()" @click="send()">↑</button>
        </div>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh; min-height: 100dvh; background: var(--h5-bg);
  font-family: var(--h5-font); display: flex; justify-content: center;
}
.panel {
  width: 100%; max-width: 440px; display: flex; flex-direction: column;
  height: 100vh; height: 100dvh;
  background: var(--h5-panel); background-image: var(--h5-screen-wash), var(--h5-panel);
}
.hd {
  flex: none; display: flex; align-items: center; gap: 10px;
  padding: calc(env(safe-area-inset-top, 0px) + 16px) 18px 12px;
}
.ttl { flex: 1; min-width: 0 }
.t1 { font-size: 15px; font-weight: 700; color: var(--h5-ink); letter-spacing: .2px }
.t2 { font-size: 11.5px; color: var(--h5-ink-3); margin-top: 3px; display: flex; align-items: center; gap: 5px }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--h5-good); display: block }
.more {
  width: 34px; height: 34px; border-radius: 50%; flex: none; cursor: pointer;
  border: 1px solid rgba(255, 255, 255, .8); background: rgba(255, 255, 255, .55);
  color: var(--h5-ink-3); font-size: 15px; line-height: 1;
}

.scroll { flex: 1; overflow-y: auto; padding: 8px 16px 12px; -webkit-overflow-scrolling: touch }

.welcome { text-align: center; padding: 26px 4px 8px }
.orb-wrap { position: relative; width: 56px; height: 56px; margin: 0 auto 16px }
.orb {
  position: relative; z-index: 1; width: 56px; height: 56px; border-radius: 50%;
  background: var(--h5-grad-orb); box-shadow: var(--h5-sh-orb);
  display: grid; place-items: center; animation: h5OrbFloat 4s ease-in-out infinite;
}
.orb-glow {
  position: absolute; inset: -13px; border-radius: 50%;
  background: radial-gradient(circle, rgba(76, 141, 255, .42), rgba(76, 141, 255, 0) 70%);
  animation: h5GlowPulse 3.2s ease-in-out infinite;
}
.wt { font-size: 19px; font-weight: 700; color: var(--h5-ink); letter-spacing: .3px }
.ws { font-size: 12.5px; color: var(--h5-ink-3); line-height: 1.7; margin-top: 5px }

.sumcard {
  display: block; width: 100%; max-width: 300px; margin: 18px auto 0; text-align: left;
  background: rgba(255, 255, 255, .6); backdrop-filter: blur(20px) saturate(1.4);
  border: 1px solid rgba(255, 255, 255, .85); border-radius: var(--h5-r-panel);
  box-shadow: var(--h5-sh-raised); padding: 18px 20px; cursor: pointer;
  font-family: inherit;
}
.sk { font-size: 12px; color: var(--h5-ink-3) }
.sv {
  font-size: 28px; font-weight: 700; color: var(--h5-ink);
  letter-spacing: -.5px; line-height: 1.2; margin-top: 2px; font-variant-numeric: tabular-nums;
}
.chips { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap }
.h5-pill--warn { color: var(--h5-warn); background: rgba(169, 106, 8, .13) }

.row { display: flex; margin-bottom: 12px }
.row.me { justify-content: flex-end }
.bubble { max-width: 82%; font-size: 13.5px; line-height: 1.7; padding: 11px 15px }
.mine {
  background: var(--h5-grad-btn); color: #fff; font-weight: 500;
  border-radius: 22px 22px 6px 22px; box-shadow: var(--h5-sh-btn-sm);
}
.theirs {
  background: rgba(255, 255, 255, .7); color: var(--h5-ink-2);
  border: 1px solid rgba(255, 255, 255, .8); border-radius: 22px 22px 22px 6px;
  box-shadow: var(--h5-sh-card);
}
.src { margin-top: 6px; font-size: 11px; color: var(--h5-ink-4) }

/* 🆕 提问卡。做成竖排整行按钮而不是横排 chip：选项是项目名/供应商名，
   动辄十几个字，横排必然换行错位，竖排在手机上一屏点得完也不会截断。 */
.ask { margin-top: 10px; display: flex; flex-direction: column; gap: 7px }
.ask-q { font-size: 12px; color: var(--h5-ink-3); margin-bottom: 1px }
.ask-opt {
  width: 100%; text-align: left; font-family: inherit; cursor: pointer;
  /* 43,110,246 = --h5-blue #2B6EF6。这里要的是它的低透明度版本，
     而 CSS 变量存的是 hex，没法直接塞进 rgba()，所以写通道值。 */
  background: rgba(43, 110, 246, .07); border: 1px solid rgba(43, 110, 246, .22);
  border-radius: var(--h5-r-chip); padding: 9px 12px; font-size: 13px;
  color: var(--h5-blue);
}
.ask-opt:active { background: rgba(43, 110, 246, .15) }
.ask-opt:disabled { opacity: .5; cursor: default }

/* Markdown 排版：模型爱用加粗小标题和列表，不排一下就是一坨字 */
.md :deep(p) { margin: 0 0 8px }
.md :deep(p:last-child) { margin-bottom: 0 }
.md :deep(strong) { font-weight: 700; color: var(--h5-ink) }
.md :deep(ul), .md :deep(ol) { margin: 6px 0 8px; padding-left: 20px }
.md :deep(li) { margin-bottom: 4px }
.md :deep(li:last-child) { margin-bottom: 0 }
.md :deep(h1), .md :deep(h2), .md :deep(h3), .md :deep(h4) {
  margin: 10px 0 6px; font-size: 13.5px; font-weight: 700; color: var(--h5-ink);
}
.md :deep(h1:first-child), .md :deep(h2:first-child),
.md :deep(h3:first-child), .md :deep(p:first-child) { margin-top: 0 }
.md :deep(code) {
  background: rgba(24, 32, 50, .06); border-radius: 5px;
  padding: 1px 5px; font-size: 12px;
}
.md :deep(pre) { overflow-x: auto; background: rgba(24,32,50,.05); padding: 10px 12px; border-radius: 10px }
/* 明细表。助手回答里的明细一律走表格（见 backend/app/agent/render.py 顶部说明）——
   手机上 `- A · B · C` 会折成多行，十几条就是一屏文字墙，人只会划过去。 */
.md :deep(table) {
  width: 100%; border-collapse: separate; border-spacing: 0;
  font-size: 12.5px; margin: 8px 0 10px;
  /* ⚠️ fixed 布局 + 下面的 word-break：内容再长也在格子里换行，
     **不撑出横向滚动**。横向滚动的表格在手机上等于没有表格（没人去滑）。 */
  table-layout: fixed;
  background: #fff; border-radius: 10px; overflow: hidden;
  box-shadow: 0 0 0 1px rgba(24, 32, 50, .07);
}
.md :deep(th), .md :deep(td) {
  padding: 7px 9px; text-align: left; word-break: break-word; line-height: 1.45;
  /* ⚠️ 表格单元格默认是 content-box，百分比宽度**不含 padding** —— 写 42% 实际占
     42%+18px，三列一挤第三列就被压到放不下「电工未完成」（实测 80px）。
     改成 border-box，百分比才名副其实。 */
  box-sizing: border-box;
}
/* 表头压暗一档：它是标签，值才是主角（与设计稿「标签一律比值小」一致） */
.md :deep(th) {
  font-weight: 600; font-size: 11.5px; color: var(--h5-ink-3);
  background: rgba(24, 32, 50, .035); white-space: nowrap;
  border-bottom: 1px solid rgba(24, 32, 50, .08);
}
.md :deep(td) { border-top: 1px solid rgba(24, 32, 50, .05) }
.md :deep(tbody tr:first-child td) { border-top: none }
/* 斑马纹：行多时靠它对齐视线，比加边框轻 */
.md :deep(tbody tr:nth-child(even)) { background: rgba(24, 32, 50, .018) }
/* 列宽按内容长度分：第一列「是哪一单」最长，第二列是「已过 55 天」这种定长短值，
   第三列「卡在哪」是句子。⚠️ 三列均分时第三列会折成四行、把行高撑到 60px 以上
   （375px 宽实测），所以把宽度从第二列匀给第三列。 */
.md :deep(th:first-child), .md :deep(td:first-child) { width: 38% }
.md :deep(th:nth-child(2)), .md :deep(td:nth-child(2)) { width: 26%; white-space: nowrap }
.md :deep(td:first-child) { color: var(--h5-ink) }

/* 语义着色。后端只发档位（[[danger:…]]），颜色在这里定 —— 见 markdown.ts。
   ⚠️ **只给真正要一眼看见的上色**：全都上色等于都没上色。
   色值用既有语义 token，不另造一套。 */
.md :deep(.h5-tone) { font-weight: 600 }
.md :deep(.h5-tone--danger) { color: var(--h5-danger) }
.md :deep(.h5-tone--warn)   { color: var(--h5-warn) }
.md :deep(.h5-tone--good)   { color: var(--h5-good) }
.md :deep(.h5-tone--muted)  { color: var(--h5-ink-4); font-weight: 400 }

/* 🆕 图（markdown.ts 的 chartPlugin 拼出来的 SVG）。
   宽度跟着气泡走 —— viewBox 固定 320，靠 width:100% 自适应，
   手机横竖屏都不会溢出。 */
.md :deep(.pmschart) { margin: 10px 0 2px }
/* ⚠️ **必须封顶**。viewBox 是 320 宽，靠 width:100% 自适应 —— 手机上正好铺满，
   但容器一宽（网页版 /h5/ 在电脑上开、平板、企微 iPad 端）SVG 就等比放大：
   实测 981px 视口下放大 2.55 倍，图里 11px 的字变成 28px，
   旁边表格还是 12.5px，一大一小很难看。封到 340 就不会再撑。 */
.md :deep(.pmschart svg) {
  width: 100%; max-width: 340px; height: auto; display: block; overflow: visible;
}
.md :deep(.pc-title) { font-size: 11px; fill: var(--h5-ink-3) }
.md :deep(.pc-lab)   { font-size: 11px; fill: var(--h5-ink-2) }
/* 数值贴右边缘，所以右对齐 */
.md :deep(.pc-val)   { font-size: 11px; text-anchor: end; font-weight: 600 }
.md :deep(.pc-zero)  { font-size: 9px; fill: var(--h5-ink-4); text-anchor: middle }
.md :deep(.pc-axis)  { stroke: var(--h5-ink-5); stroke-width: 1; stroke-dasharray: 2 2 }
.md :deep(blockquote) {
  margin: 6px 0; padding-left: 10px; border-left: 3px solid rgba(43,110,246,.3);
  color: var(--h5-ink-3);
}
.dots { display: flex; gap: 5px; align-items: center; padding: 14px 16px }
.dots i {
  width: 6px; height: 6px; border-radius: 50%; background: var(--h5-blue); display: block;
  animation: h5DotJump 1.2s infinite;
}
.dots i:nth-child(2) { animation-delay: .15s }
.dots i:nth-child(3) { animation-delay: .3s }
.thint { font-size: 11.5px; color: var(--h5-ink-3); margin-left: 6px }
.cards { display: flex; flex-direction: column; gap: 12px; margin-bottom: 12px }
.sumcard {
  background: rgba(255,255,255,.7); border: 1px solid rgba(255,255,255,.85);
  border-radius: var(--h5-r-panel); box-shadow: var(--h5-sh-raised); padding: 18px 20px;
}
.sk { font-size: 12px; color: var(--h5-ink-3) }
.sv {
  font-size: 28px; font-weight: 700; color: var(--h5-ink); letter-spacing: -.5px;
  line-height: 1.2; margin-top: 2px; font-variant-numeric: tabular-nums;
}
.ssub { font-size: 11.5px; color: var(--h5-ink-3); margin-top: 4px }
.sgrp { margin-top: 14px; display: flex; flex-direction: column; gap: 10px }
.sg { border-top: 1px solid rgba(24,32,50,.07); padding-top: 10px }
.sgl { font-size: 12.5px; color: var(--h5-ink-2); font-weight: 500 }
.sgv {
  font-size: 17px; font-weight: 700; color: var(--h5-ink); margin-top: 2px;
  font-variant-numeric: tabular-nums;
}
.sgn { font-size: 11.5px; font-weight: 400; color: var(--h5-ink-3); margin-left: 6px }
.sgd { font-size: 11px; color: var(--h5-ink-4); margin-top: 3px; line-height: 1.45 }
.lst {
  background: rgba(255,255,255,.7); border: 1px solid rgba(255,255,255,.85);
  border-radius: var(--h5-r-panel); box-shadow: var(--h5-sh-raised); overflow: hidden;
}
.lhd {
  width: 100%; display: flex; align-items: center; gap: 6px; border: 0; cursor: pointer;
  background: rgba(24,32,50,.03); padding: 11px 14px; text-align: left;
  font: 500 12.5px/1 var(--h5-font); color: var(--h5-ink-3);
}
.lback { font-size: 17px; line-height: 1; color: var(--h5-blue) }
.lrow {
  width: 100%; display: flex; align-items: center; gap: 10px; border: 0; cursor: pointer;
  background: none; padding: 12px 14px; text-align: left;
  border-top: 1px solid rgba(24,32,50,.06);
}
.lno {
  flex: none; width: 18px; height: 18px; border-radius: 50%; background: rgba(24,32,50,.06);
  font: 600 11px/18px var(--h5-font); color: var(--h5-ink-3); text-align: center;
}
.lmain { flex: 1; min-width: 0 }
.lt {
  display: block; font-size: 13.5px; font-weight: 600; color: var(--h5-ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.lw { display: block; font-size: 11px; color: var(--h5-warn, #b26a00); margin-top: 2px }
.lright { flex: none; text-align: right }
.lm { display: block; font-size: 12.5px; font-weight: 600; color: var(--h5-ink-2);
      font-variant-numeric: tabular-nums }
.lb { display: block; font-size: 10.5px; color: var(--h5-ink-4); margin-top: 2px }
.lgo { flex: none; color: var(--h5-ink-4); font-size: 15px }

.sbtn {
  width: 100%; margin-top: 16px; border: 0; border-radius: var(--h5-r-card);
  background: var(--h5-grad-btn); color: #fff; box-shadow: var(--h5-sh-btn-sm);
  font: 600 15px/1 var(--h5-font); padding: 14px; cursor: pointer;
}

.ft { flex: none; padding: 8px 16px calc(env(safe-area-inset-bottom, 0px) + 14px) }
.sugg { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px }
.chip {
  background: rgba(255, 255, 255, .55); border: 1px solid rgba(255, 255, 255, .75);
  backdrop-filter: blur(10px); box-shadow: var(--h5-sh-card);
  border-radius: var(--h5-r-pill); padding: 11px 18px; font-size: 12.5px;
  color: var(--h5-ink-2); cursor: pointer; font-family: inherit;
}
.back {
  width: 30px; height: 34px; flex: none; border: 0; background: none; cursor: pointer;
  color: var(--h5-blue); font-size: 24px; line-height: 1; padding: 0; margin-left: -6px;
}
.serr { font-size: 11.5px; color: var(--h5-warn); padding: 0 6px 8px }
.mic {
  width: 46px; height: 46px; flex: none; border-radius: 50%; cursor: pointer;
  border: 1px solid rgba(255,255,255,.8); background: rgba(255,255,255,.7);
  color: var(--h5-ink-3); display: grid; place-items: center;
}
.mic.on {
  background: var(--h5-danger); color: #fff; border-color: transparent;
  animation: h5GlowPulse 1.4s ease-in-out infinite;
}
.composer { display: flex; gap: 10px; align-items: center }
.composer input {
  flex: 1; min-width: 0; border: 1px solid rgba(255, 255, 255, .8);
  background: rgba(255, 255, 255, .7); border-radius: var(--h5-r-pill);
  padding: 13px 18px; font: 400 14px/1 var(--h5-font); color: var(--h5-ink); outline: none;
}
.composer input::placeholder { color: var(--h5-ink-4) }
.send {
  width: 46px; height: 46px; flex: none; border: 0; border-radius: 50%; cursor: pointer;
  background: var(--h5-grad-btn); color: #fff; font-size: 17px; font-weight: 700;
  box-shadow: var(--h5-sh-btn-sm);
}
.send:disabled { opacity: .45; box-shadow: none }
</style>
