<script setup lang="ts">
/**
 * H5 助手主界面：登录进来就是对话。
 * 查询走 /agent/chat（复用现有 LLM + 工具 + 降级），审批走卡片组件。
 *
 * 这里刻意不做业务页面——手机上填单子是灾难，看数和批单才合适。
 */
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { http, errText } from './http'
import { clearSession, displayName } from './session'
import H5ApproveCard from './H5ApproveCard.vue'
import { isKnownCard, type AgentCard } from './cardRegistry'

const router = useRouter()

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
      text: n === 0 ? '你名下没有待审的请款单。'
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
  // 审批类走卡片通道，不进 LLM——审批不能靠模型转述
  if (/待我审批|待审|请款单|审批/.test(q) && /请款|审批/.test(q)) return openApprovals()

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

function onCardDone() {
  loadPending()
}

function logout() {
  clearSession()
  router.replace('/login')
}

onMounted(loadPending)
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <!-- 顶栏 -->
      <header class="hd">
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
              {{ m.text }}
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
        <div class="composer">
          <input v-model="input" placeholder="问点什么…" @keyup.enter="send()" />
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
