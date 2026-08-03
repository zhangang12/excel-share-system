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
  tool?: string | null; card?: string | null
}

const router = useRouter()
const pending = ref({ count: 0, amount_total: 0, blocked: 0 })
const tiles = ref<Tile[]>([])
const catalog = ref<Tile[]>([])
const limits = ref({ max_tiles: 12, max_label: 10, max_question: 120 })
const loading = ref(true)
// 今天该管的 3 件事：进来就看见，不用先想「该问什么」。
// 后端 /agent/briefing/me 已经排过序、带了「为什么是它」。
interface BriefItem { title: string; why: string; action: string; card: string; ref: number }
const brief = ref<{ items: BriefItem[]; rest: number } | null>(null)
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
function openBrief(it: BriefItem) {
  router.push({ path: '/chat', query: { card: it.card } })
}

const addable = computed(() => {
  const on = new Set(tiles.value.map((t) => t.key))
  return catalog.value.filter((c) => !on.has(c.key))
})

async function load() {
  try {
    const [p, c, b] = await Promise.all([
      http.get('/agent/cards/pending').catch(() => ({ data: null })),
      http.get('/agent/portal'),
      // 简报挂了不能把整个首页拖垮，单独兜住
      http.get('/agent/briefing/me').catch(() => ({ data: null })),
    ])
    if (p.data) pending.value = { count: p.data.count, amount_total: p.data.amount_total, blocked: p.data.blocked }
    if (b.data && b.data.items?.length) brief.value = { items: b.data.items, rest: b.data.rest }
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

/**
 * 有 card 的走卡片通道（能查也能动手）；其余走对话（LLM 流式）。
 * 直答那条通道保留但门户不用——绕过模型的话它就是个普通报表页。
 */
const ask = (t: Tile) => {
  if (editing.value) return
  const query: Record<string, string> = { q: t.q }
  if (t.card) query.card = t.card
  router.push({ name: 'chat', query })
}
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
          <div class="ht">{{ greet }}，{{ displayName }}</div>
          <div class="hs">点一下就看，不用打字</div>
        </div>
        <div v-else class="edithint">拖不了就用箭头调顺序；「+ 常问的话」把你自己的问题存成卡片</div>

        <p v-if="err" class="err">{{ err }}</p>

        <!-- 今天该管的：排过序、带理由，比「有多少件待办」有用得多 -->
        <section v-if="brief && !editing" class="brief">
          <div class="bh">今天该管的 {{ brief.items.length }} 件</div>
          <button v-for="(it, i) in brief.items" :key="i" class="brow" @click="openBrief(it)">
            <span class="bno">{{ i + 1 }}</span>
            <span class="bmain">
              <span class="bt">{{ it.title }}</span>
              <span class="bw">{{ it.why }}</span>
            </span>
            <span class="bact">{{ it.action }} ›</span>
          </button>
          <div v-if="brief.rest" class="brest">另有 {{ brief.rest }} 项，问我「还有什么」</div>
        </section>

        <!-- 等你签字：唯一「要动手」的入口，单独做大 -->
        <template v-if="approveTile">
          <button v-if="pending.count && !editing" class="sign" @click="ask(approveTile)">
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

        <div class="list" :class="{ edit: editing }">
          <div v-for="t in gridTiles" :key="t.key" class="cell">
            <button class="row" :class="{ dim: editing }" @click="ask(t)">
              <span class="rtx">
                <span class="rl">{{ t.label }}</span>
                <span class="rd">{{ t.desc }}</span>
              </span>
              <span class="rgo">›</span>
            </button>
            <div v-if="editing" class="ops">
              <button @click="moveTile(tiles.indexOf(t), -1)" aria-label="上移">↑</button>
              <button @click="moveTile(tiles.indexOf(t), 1)" aria-label="下移">↓</button>
              <button class="del" @click="removeTile(tiles.indexOf(t))" aria-label="移除">×</button>
            </div>
          </div>
        </div>

        <template v-if="editing">
          <div class="gh">还能加这些</div>
          <div class="addlist">
            <button v-for="c in addable" :key="c.key" class="additem" @click="addTile(c)">
              <span class="atx">
                <span class="al">{{ c.label }}</span>
                <span class="ad">{{ c.desc }}</span>
              </span>
              <span class="aplus">+</span>
            </button>
            <button class="additem custom" @click="addCustom">
              <span class="atx">
                <span class="al">常问的话</span>
                <span class="ad">把你自己的问题存成一张卡</span>
              </span>
              <span class="aplus">+</span>
            </button>
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
.brief {
  background: rgba(255,255,255,.7); border: 1px solid rgba(255,255,255,.85);
  border-radius: var(--h5-r-panel); box-shadow: var(--h5-sh-raised);
  padding: 14px 4px 6px; margin-bottom: 14px;
}
.bh { font-size: 12.5px; color: var(--h5-ink-3); padding: 0 14px 8px; font-weight: 500 }
.brow {
  width: 100%; display: flex; align-items: flex-start; gap: 10px; text-align: left;
  border: 0; background: none; padding: 11px 14px; cursor: pointer;
}
.brow + .brow { border-top: 1px solid rgba(24,32,50,.06) }
.bno {
  flex: none; width: 18px; height: 18px; border-radius: 50%; margin-top: 1px;
  background: var(--h5-grad-btn); color: #fff; font: 600 11px/18px var(--h5-font);
  text-align: center;
}
.bmain { flex: 1; min-width: 0 }
.bt { display: block; font-size: 14px; font-weight: 600; color: var(--h5-ink); line-height: 1.4 }
/* 理由是这张卡的价值所在，别截断成一行 */
.bw { display: block; font-size: 11.5px; color: var(--h5-ink-3); line-height: 1.5; margin-top: 3px }
.bact { flex: none; font-size: 12px; color: var(--h5-blue); font-weight: 500; margin-top: 1px }
.brest { font-size: 11.5px; color: var(--h5-ink-4); padding: 8px 14px 6px }
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

.hero { padding: 6px 4px 16px }
.ht { font-size: 20px; font-weight: 700; color: var(--h5-ink); letter-spacing: .2px }
.hs { font-size: 12.5px; color: var(--h5-ink-3); margin-top: 3px }

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
.list { margin-top: 14px; display: flex; flex-direction: column; gap: 8px }
.row {
  display: flex; align-items: center; gap: 12px; width: 100%; text-align: left;
  cursor: pointer; font-family: inherit;
  background: rgba(255,255,255,.66); border: 1px solid rgba(255,255,255,.85);
  border-radius: var(--h5-r-card); box-shadow: var(--h5-sh-card);
  padding: 14px 16px; min-height: 56px;
  transition: transform .14s, box-shadow .14s;
}
.row:active { transform: translateY(1px); box-shadow: none }
.rtx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px }
.rl { font-size: 14.5px; font-weight: 600; color: var(--h5-ink) }
.rd { font-size: 11.5px; color: var(--h5-ink-3); line-height: 1.45 }
.rgo { flex: none; color: var(--h5-ink-4); font-size: 17px }

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
.list.edit .row { pointer-events: none }
.row.dim { opacity: .82 }
.ops {
  position: absolute; top: 50%; right: 10px; transform: translateY(-50%);
  display: flex; gap: 5px;
}
.ops button {
  width: 24px; height: 24px; border-radius: 50%; border: 1px solid rgba(255,255,255,.9);
  background: rgba(255,255,255,.92); color: var(--h5-ink-2); font-size: 13px;
  line-height: 1; cursor: pointer; padding: 0;
}
.ops .del { color: var(--h5-danger); font-size: 15px }
.addlist { display: flex; flex-direction: column; gap: 8px; padding-bottom: 8px }
.additem {
  display: flex; align-items: center; gap: 11px; width: 100%; text-align: left;
  border: 1px dashed rgba(43,110,246,.35); background: rgba(255,255,255,.5);
  border-radius: var(--h5-r-card); padding: 11px 13px; cursor: pointer; font-family: inherit;
}
.additem.custom { border-style: solid; border-color: rgba(43,110,246,.45) }
.atx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px }
.al { font-size: 13px; font-weight: 600; color: var(--h5-ink) }
.ad { font-size: 11px; color: var(--h5-ink-3); line-height: 1.5 }
.aplus { flex: none; color: var(--h5-blue); font-size: 17px; font-weight: 700 }

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
