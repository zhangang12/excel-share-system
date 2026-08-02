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
import { clearSession, displayName } from './session'
import { renderMd } from './markdown'
import { useSpeech } from './useSpeech'
import H5ApproveCard from './H5ApproveCard.vue'
import { isKnownCard, type AgentCard } from './cardRegistry'

const router = useRouter()
const route = useRoute()

type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'ai'; text: string; sources?: string[] }
  | { kind: 'cards'; cards: AgentCard[] }

const msgs = ref<Msg[]>([])
const input = ref('')
const thinking = ref(false)
const scroller = ref<HTMLElement>()
const pending = ref<{ count: number; amount_total: number; blocked: number }>(
  { count: 0, amount_total: 0, blocked: 0 })
const suggestions = ref<string[]>(['这月销售额多少？', '哪些供应商老迟到？', '待我审批的请款单'])
/** 走卡片通道的精确入口文案；只有这几条，别改成模糊匹配 */
const CARD_ENTRIES = new Set(['待我审批的请款单', '待我审批', '请款审批'])

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

async function openApprovals() {
  thinking.value = true
  msgs.value.push({ kind: 'user', text: '待我审批的请款单' })
  await scrollDown()
  try {
    const { data } = await http.get('/agent/cards/pending')
    // 白名单在前端再过一道：后端给了未登记的 type 就整张不渲染（原则三）
    // 白名单在前端再过一道；再把能批的排前面——批不了的压后，别让人先划过一堆灰按钮
    const cards = (data.cards as AgentCard[])
      .filter((c) => isKnownCard(c.type))
      .sort((a, b) => Number(a.flags.some((f) => f.level === 'block'))
                    - Number(b.flags.some((f) => f.level === 'block')))
    const dropped = data.cards.length - cards.length
    const n = cards.length
    msgs.value.push({
      kind: 'ai',
      text: n === 0
        ? '你名下没有待审的请款单。想看别的可以直接问我，比如「采购未到货」「尾款到期」。'
        : `共 ${n} 单${data.blocked ? `，其中 ${data.blocked} 单按职责分离需他人处理` : ''}。`
          + (dropped ? `（另有 ${dropped} 条无法安全展示，请到电脑端查看）` : ''),
    })
    if (n) msgs.value.push({ kind: 'cards', cards })
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

  msgs.value.push({ kind: 'user', text: q })
  thinking.value = true
  await scrollDown()
  try {
    const history = msgs.value
      .filter((m): m is Extract<Msg, { kind: 'user' | 'ai' }> => m.kind === 'user' || m.kind === 'ai')
      .slice(-10)
      .map((m) => ({ role: m.kind === 'user' ? 'user' : 'assistant', content: m.text }))
    const { data } = await http.post('/agent/chat', { message: q, history: history.slice(0, -1) })
    msgs.value.push({ kind: 'ai', text: data.reply, sources: data.sources })
    if (Array.isArray(data.suggestions) && data.suggestions.length) {
      suggestions.value = data.suggestions
    }
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

function onCardDone() {
  loadPending()
}

function logout() {
  clearSession()
  router.replace('/login')
}

onMounted(() => {
  loadPending()
  // 从门户点卡片进来：带着问题直接发，用户不用打字
  const q = route.query.q
  if (typeof q === 'string' && q.trim()) send(q.trim())
})
</script>

<template>
  <div class="wrap">
    <div class="panel">
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

          <button v-if="pending.count" class="sumcard" @click="openApprovals">
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
              <div v-if="m.sources?.length" class="src">来源：{{ m.sources.join('、') }}</div>
            </div>
          </div>
          <div v-else class="cards">
            <H5ApproveCard v-for="(c, k) in m.cards" :key="c.ref" :card="c"
                           :index="k + 1" :total="m.cards.length" @done="onCardDone" />
          </div>
        </template>

        <div v-if="thinking" class="row">
          <div class="bubble theirs dots"><i></i><i></i><i></i></div>
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
.md :deep(table) { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 6px 0 }
.md :deep(th), .md :deep(td) {
  border-bottom: 1px solid rgba(24, 32, 50, .08); padding: 6px 8px; text-align: left;
}
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
.cards { display: flex; flex-direction: column; gap: 12px; margin-bottom: 12px }

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
