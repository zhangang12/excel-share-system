<script setup lang="ts">
/**
 * 智能体门户。
 *
 * 为什么要有这一页：只给一个对话框，等于逼所有人打字。手机上打字本来就烦，
 * 管理层更不会为了看个数去敲一行问题。门户把「他常问的那几件事」摆成可点的入口，
 * 点一下直接出答案；真有别的问题再进对话框。
 *
 * 入口只列后端真有工具支撑的（agent_router 的 7 个只读工具 + 请款审批卡）。
 * 没有工具的别摆——摆了点进去只会得到「查不到」，比不摆更伤信任。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { http } from './http'
import { clearSession, displayName } from './session'

const router = useRouter()
const pending = ref({ count: 0, amount_total: 0, blocked: 0 })
const loading = ref(true)

const greet = computed(() => {
  const h = new Date().getHours()
  return h < 6 ? '夜里好' : h < 12 ? '早上好' : h < 18 ? '下午好' : '晚上好'
})
const amountText = computed(() =>
  '¥' + pending.value.amount_total.toLocaleString('zh-CN', { maximumFractionDigits: 0 }))

/** 每一项都对应 agent_router 里一个真实工具，别加没有后端支撑的条目 */
const groups = [
  {
    title: '每天看一眼',
    items: [
      { q: '今日晨报', label: '今日晨报', desc: '一条消息看完全部要紧事', glyph: '报', tone: 'blue' },
      { q: '采购未到货', label: '采购超期', desc: '到期未到货的料和供应商', glyph: '箱', tone: 'danger' },
    ],
  },
  {
    title: '钱在哪儿',
    items: [
      { q: '尾款到期', label: '尾款到期', desc: '14 天内到期与已逾期的应收', glyph: '收', tone: 'warn' },
      { q: '按供应商汇总未到货', label: '按供应商汇总', desc: '哪家供应商拖得最狠', glyph: '供', tone: 'blue' },
    ],
  },
  {
    title: '事推到哪儿了',
    items: [
      { q: '部门逾期任务', label: '部门逾期', desc: '各部门超期未完成的任务', glyph: '逾', tone: 'danger' },
      { q: '未来 7 天到货', label: '近期到货', desc: '接下来一周能到的料', glyph: '期', tone: 'good' },
    ],
  },
]

async function load() {
  try {
    const { data } = await http.get('/agent/cards/pending')
    pending.value = { count: data.count, amount_total: data.amount_total, blocked: data.blocked }
  } catch { /* 拿不到不影响门户其它入口 */ } finally { loading.value = false }
}

const ask = (q: string) => router.push({ name: 'chat', query: { q } })
const openApprovals = () => router.push({ name: 'chat', query: { q: '待我审批的请款单' } })
const openChat = () => router.push({ name: 'chat' })

function logout() { clearSession(); router.replace('/login') }
onMounted(load)
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <header class="hd">
        <div class="ttl">
          <div class="t1">同辉项目管理智能体</div>
          <div class="t2"><i class="dot"></i>在线 · {{ displayName }}</div>
        </div>
        <button class="more" @click="logout" aria-label="退出">···</button>
      </header>

      <main class="scroll">
        <div class="hero">
          <div class="orb-wrap">
            <div class="orb-glow"></div>
            <div class="orb">
              <svg width="26" height="26" viewBox="0 0 20 20" fill="none">
                <path d="M10 2.5c.9 4.2 3.3 6.6 7.5 7.5-4.2.9-6.6 3.3-7.5 7.5-.9-4.2-3.3-6.6-7.5-7.5 4.2-.9 6.6-3.3 7.5-7.5Z" fill="#fff"/>
              </svg>
            </div>
          </div>
          <div class="ht">{{ greet }}，{{ displayName }}</div>
          <div class="hs">点一下就看，不用打字</div>
        </div>

        <!-- 等你签字：唯一一个「要动手」的入口，所以单独做大 -->
        <button v-if="pending.count" class="sign" @click="openApprovals">
          <div class="sk">等你签字</div>
          <div class="sv">{{ amountText }}</div>
          <div class="chips">
            <span class="h5-pill h5-pill--blue">{{ pending.count }} 件待办</span>
            <span v-if="pending.blocked" class="h5-pill h5-pill--warn">
              {{ pending.blocked }} 件需他人处理
            </span>
          </div>
          <span class="go">去处理 ›</span>
        </button>
        <div v-else-if="!loading" class="clear">
          <span class="tick">✓</span>没有待你签字的单子
        </div>

        <section v-for="g in groups" :key="g.title" class="grp">
          <div class="gh">{{ g.title }}</div>
          <div class="grid">
            <button v-for="it in g.items" :key="it.q" class="tile" @click="ask(it.q)">
              <span class="tg" :class="it.tone">{{ it.glyph }}</span>
              <span class="tl">{{ it.label }}</span>
              <span class="td">{{ it.desc }}</span>
            </button>
          </div>
        </section>
      </main>

      <footer class="ft">
        <button class="askbar" @click="openChat">
          <span>问点别的…</span>
          <span class="send">↑</span>
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.wrap { min-height: 100vh; min-height: 100dvh; background: var(--h5-bg); display: flex; justify-content: center }
.panel {
  width: 100%; max-width: 440px; height: 100vh; height: 100dvh;
  display: flex; flex-direction: column;
  background: var(--h5-panel); background-image: var(--h5-screen-wash), var(--h5-panel);
}
.hd {
  flex: none; display: flex; align-items: center; gap: 10px;
  padding: calc(env(safe-area-inset-top, 0px) + 16px) 18px 10px;
}
.ttl { flex: 1; min-width: 0 }
.t1 { font-size: 15px; font-weight: 700; color: var(--h5-ink); letter-spacing: .2px }
.t2 { font-size: 11.5px; color: var(--h5-ink-3); margin-top: 3px; display: flex; align-items: center; gap: 5px }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--h5-good); display: block }
.more {
  width: 34px; height: 34px; border-radius: 50%; flex: none; cursor: pointer;
  border: 1px solid rgba(255,255,255,.8); background: rgba(255,255,255,.55);
  color: var(--h5-ink-3); font-size: 15px; line-height: 1;
}

.scroll { flex: 1; overflow-y: auto; padding: 4px 16px 12px; -webkit-overflow-scrolling: touch }

.hero { text-align: center; padding: 14px 0 18px }
.orb-wrap { position: relative; width: 50px; height: 50px; margin: 0 auto 12px }
.orb {
  position: relative; z-index: 1; width: 50px; height: 50px; border-radius: 50%;
  background: var(--h5-grad-orb); box-shadow: var(--h5-sh-orb);
  display: grid; place-items: center; animation: h5OrbFloat 4s ease-in-out infinite;
}
.orb-glow {
  position: absolute; inset: -12px; border-radius: 50%;
  background: radial-gradient(circle, rgba(76,141,255,.42), rgba(76,141,255,0) 70%);
  animation: h5GlowPulse 3.2s ease-in-out infinite;
}
.ht { font-size: 19px; font-weight: 700; color: var(--h5-ink); letter-spacing: .3px }
.hs { font-size: 12.5px; color: var(--h5-ink-3); margin-top: 4px }

.sign {
  display: block; width: 100%; text-align: left; position: relative; cursor: pointer;
  background: rgba(255,255,255,.62); backdrop-filter: blur(20px) saturate(1.4);
  border: 1px solid rgba(255,255,255,.85); border-radius: var(--h5-r-panel);
  box-shadow: var(--h5-sh-raised); padding: 18px 20px; font-family: inherit;
}
.sk { font-size: 12px; color: var(--h5-ink-3) }
.sv {
  font-size: 28px; font-weight: 700; color: var(--h5-ink); letter-spacing: -.5px;
  line-height: 1.2; margin-top: 2px; font-variant-numeric: tabular-nums;
}
.chips { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap }
.h5-pill--warn { color: var(--h5-warn); background: rgba(169,106,8,.13) }
.go { position: absolute; right: 18px; bottom: 18px; font-size: 12px; color: var(--h5-blue); font-weight: 600 }

.clear {
  display: flex; align-items: center; gap: 8px; border-radius: var(--h5-r-card);
  background: rgba(42,122,82,.10); color: var(--h5-good);
  padding: 13px 16px; font-size: 13px; font-weight: 500;
}
.tick {
  width: 18px; height: 18px; border-radius: 50%; background: var(--h5-good);
  color: #fff; display: grid; place-items: center; font-size: 11px; flex: none;
}

.grp { margin-top: 20px }
.gh { font-size: 12px; color: var(--h5-ink-3); padding: 0 4px 8px; font-weight: 500 }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px }
.tile {
  display: flex; flex-direction: column; align-items: flex-start; gap: 3px;
  text-align: left; cursor: pointer; font-family: inherit;
  background: rgba(255,255,255,.58); border: 1px solid rgba(255,255,255,.8);
  border-radius: var(--h5-r-card); box-shadow: var(--h5-sh-card);
  padding: 13px 14px; min-height: 92px;
  transition: transform .14s, box-shadow .14s;
}
.tile:active { transform: translateY(1px); box-shadow: none }
.tg {
  width: 30px; height: 30px; border-radius: var(--h5-r-chip); display: grid; place-items: center;
  font-size: 13px; font-weight: 700; margin-bottom: 4px;
  background: rgba(76,141,255,.15); color: var(--h5-blue);
}
.tg.danger { background: rgba(196,54,47,.12); color: var(--h5-danger) }
.tg.warn { background: rgba(169,106,8,.13); color: var(--h5-warn) }
.tg.good { background: rgba(42,122,82,.12); color: var(--h5-good) }
.tl { font-size: 13.5px; font-weight: 600; color: var(--h5-ink) }
.td { font-size: 11px; color: var(--h5-ink-3); line-height: 1.5 }

.ft { flex: none; padding: 8px 16px calc(env(safe-area-inset-bottom, 0px) + 14px) }
.askbar {
  display: flex; align-items: center; justify-content: space-between; width: 100%;
  cursor: pointer; font-family: inherit;
  background: rgba(255,255,255,.7); border: 1px solid rgba(255,255,255,.8);
  border-radius: var(--h5-r-pill); padding: 7px 7px 7px 18px;
  font-size: 14px; color: var(--h5-ink-4);
}
.send {
  width: 40px; height: 40px; border-radius: 50%; flex: none; display: grid; place-items: center;
  background: var(--h5-grad-btn); color: #fff; font-size: 16px; font-weight: 700;
  box-shadow: var(--h5-sh-btn-sm);
}
</style>
