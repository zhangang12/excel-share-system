<script setup lang="ts">
/**
 * 智能体门户（可定制）。
 *
 * 为什么要有这一页：只给一个对话框，等于逼所有人打字。手机上打字本来就烦，
 * 管理层更不会为了看个数去敲一行问题。门户把他常做的事摆成可点入口。
 *
 * 定制化：卡片配置按人存在服务端（user_settings.portal_tiles）。
 * 能摆什么由服务端目录说了算（原则三 能力可枚举）——用户只能挑选、排序，
 * 以及把自己常问的话沉淀成一张自定义卡；自定义卡本质只是一句预置提问，
 * 点下去仍走 /agent/chat，不会凭配置多出任何数据访问路径。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { http, errText } from './http'
import { clearSession, displayName } from './session'

interface Tile {
  key: string; label: string; desc?: string; glyph?: string
  tone?: string; q: string; custom?: boolean; kind?: string | null
}

const router = useRouter()
const pending = ref({ count: 0, amount_total: 0, blocked: 0 })
const tiles = ref<Tile[]>([])
const catalog = ref<Tile[]>([])
const limits = ref({ max_tiles: 12, max_label: 10, max_question: 120 })
const loading = ref(true)
const editing = ref(false)
const saving = ref(false)
const err = ref('')

const greet = computed(() => {
  const h = new Date().getHours()
  return h < 6 ? '夜里好' : h < 12 ? '早上好' : h < 18 ? '下午好' : '晚上好'
})
const amountText = computed(() =>
  '¥' + pending.value.amount_total.toLocaleString('zh-CN', { maximumFractionDigits: 0 }))

/** 审批卡单独渲染成大卡，不混在网格里 */
const approveTile = computed(() => tiles.value.find((t) => t.kind === 'approve'))
const gridTiles = computed(() => tiles.value.filter((t) => t.kind !== 'approve'))
/** 目录里还没摆上门户的 */
const addable = computed(() => {
  const on = new Set(tiles.value.map((t) => t.key))
  return catalog.value.filter((c) => !on.has(c.key))
})

async function load() {
  try {
    const [p, c] = await Promise.all([
      http.get('/agent/cards/pending').catch(() => ({ data: null })),
      http.get('/agent/portal'),
    ])
    if (p.data) pending.value = { count: p.data.count, amount_total: p.data.amount_total, blocked: p.data.blocked }
    tiles.value = c.data.tiles
    catalog.value = c.data.catalog
    limits.value = c.data.limits
  } catch (e: any) {
    err.value = errText(e, '门户加载失败')
  } finally { loading.value = false }
}

async function save() {
  saving.value = true; err.value = ''
  try {
    // 只回传 key 与自定义卡的 label/q，其余字段服务端会重新填，不用带
    const payload = tiles.value.map((t) =>
      t.custom ? { key: t.key, label: t.label, q: t.q, custom: true } : { key: t.key })
    const { data } = await http.put('/agent/portal', { tiles: payload })
    tiles.value = data.tiles
    editing.value = false
  } catch (e: any) { err.value = errText(e, '保存失败') } finally { saving.value = false }
}

async function reset() {
  if (!confirm('恢复成系统默认门户？你的自定义卡会被清掉。')) return
  saving.value = true
  try {
    const { data } = await http.delete('/agent/portal')
    tiles.value = data.tiles
  } catch (e: any) { err.value = errText(e, '恢复失败') } finally { saving.value = false }
}

function addTile(c: Tile) {
  if (tiles.value.length >= limits.value.max_tiles) { err.value = `最多摆 ${limits.value.max_tiles} 张`; return }
  tiles.value.push({ ...c })
}
function removeTile(i: number) { tiles.value.splice(i, 1) }
function moveTile(i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= tiles.value.length) return
  const [x] = tiles.value.splice(i, 1)
  tiles.value.splice(j, 0, x)
}
function addCustom() {
  const label = window.prompt(`卡片标题（${limits.value.max_label} 字以内）`)?.trim()
  if (!label) return
  const q = window.prompt('点这张卡时要问的话')?.trim()
  if (!q) return
  if (tiles.value.length >= limits.value.max_tiles) { err.value = `最多摆 ${limits.value.max_tiles} 张`; return }
  tiles.value.push({ key: `custom:new${Date.now()}`, label, q, custom: true, glyph: '问', tone: 'blue', desc: q })
}

const ask = (q: string) => { if (!editing.value) router.push({ name: 'chat', query: { q } }) }
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
        <template v-if="editing">
          <button class="tbtn" :disabled="saving" @click="reset">恢复默认</button>
          <button class="tbtn primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '完成' }}
          </button>
        </template>
        <template v-else>
          <button class="tbtn" @click="editing = true">定制</button>
          <button class="more" @click="logout" aria-label="退出">···</button>
        </template>
      </header>

      <main class="scroll">
        <div v-if="!editing" class="hero">
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
        <div v-else class="edithint">拖不了就用箭头调顺序；「+ 常问的话」把你自己的问题存成卡片</div>

        <p v-if="err" class="err">{{ err }}</p>

        <!-- 等你签字：唯一「要动手」的入口，单独做大 -->
        <template v-if="approveTile">
          <button v-if="pending.count && !editing" class="sign" @click="ask(approveTile.q)">
            <div class="sk">{{ approveTile.label }}</div>
            <div class="sv">{{ amountText }}</div>
            <div class="chips">
              <span class="h5-pill h5-pill--blue">{{ pending.count }} 件待办</span>
              <span v-if="pending.blocked" class="h5-pill h5-pill--warn">
                {{ pending.blocked }} 件需他人处理
              </span>
            </div>
            <span class="go">去处理 ›</span>
          </button>
          <div v-else-if="!editing && !loading" class="clear">
            <span class="tick">✓</span>没有待你签字的单子
          </div>
        </template>

        <div class="grid" :class="{ edit: editing }">
          <div v-for="(t, i) in gridTiles" :key="t.key" class="cell">
            <button class="tile" :class="{ dim: editing }" @click="ask(t.q)">
              <span class="tg" :class="t.tone">{{ t.glyph }}</span>
              <span class="tl">{{ t.label }}</span>
              <span class="td">{{ t.desc }}</span>
            </button>
            <div v-if="editing" class="ops">
              <button @click="moveTile(tiles.indexOf(t), -1)" aria-label="上移">‹</button>
              <button @click="moveTile(tiles.indexOf(t), 1)" aria-label="下移">›</button>
              <button class="del" @click="removeTile(tiles.indexOf(t))" aria-label="移除">×</button>
            </div>
          </div>
        </div>

        <template v-if="editing">
          <div class="gh">还能加这些</div>
          <div class="addrow">
            <button v-for="c in addable" :key="c.key" class="addchip" @click="addTile(c)">
              + {{ c.label }}
            </button>
            <button class="addchip custom" @click="addCustom">+ 常问的话</button>
          </div>
        </template>
      </main>

      <footer v-if="!editing" class="ft">
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

.gh { font-size: 12px; color: var(--h5-ink-3); padding: 0 4px 8px; font-weight: 500 }
.grid { margin-top: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 10px }
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

.tbtn {
  flex: none; border: 1px solid rgba(255,255,255,.8); background: rgba(255,255,255,.6);
  color: var(--h5-ink-2); border-radius: var(--h5-r-pill); padding: 7px 14px;
  font: 600 12.5px var(--h5-font); cursor: pointer;
}
.tbtn.primary { background: var(--h5-grad-btn); color: #fff; border: 0; box-shadow: var(--h5-sh-btn-sm) }
.tbtn:disabled { opacity: .5 }
.edithint {
  font-size: 12px; color: var(--h5-ink-3); line-height: 1.6;
  background: rgba(255,255,255,.5); border-radius: var(--h5-r-card); padding: 10px 14px; margin: 6px 0 14px;
}
.err {
  margin: 0 0 12px; font-size: 12px; color: var(--h5-danger);
  background: rgba(196,54,47,.09); border-radius: 10px; padding: 9px 12px;
}
.cell { position: relative }
.grid.edit .tile { pointer-events: none }
.tile.dim { opacity: .82 }
.ops {
  position: absolute; top: 6px; right: 6px; display: flex; gap: 4px;
}
.ops button {
  width: 24px; height: 24px; border-radius: 50%; border: 1px solid rgba(255,255,255,.9);
  background: rgba(255,255,255,.92); color: var(--h5-ink-2); font-size: 13px;
  line-height: 1; cursor: pointer; padding: 0;
}
.ops .del { color: var(--h5-danger); font-size: 15px }
.addrow { display: flex; flex-wrap: wrap; gap: 8px; padding-bottom: 8px }
.addchip {
  border: 1px dashed rgba(43,110,246,.4); background: rgba(76,141,255,.08);
  color: var(--h5-blue); border-radius: var(--h5-r-pill); padding: 9px 14px;
  font: 600 12.5px var(--h5-font); cursor: pointer;
}
.addchip.custom { border-style: solid; background: var(--h5-grad-btn); color: #fff; border-color: transparent }

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
