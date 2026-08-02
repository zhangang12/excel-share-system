<script setup lang="ts">
/**
 * 审批卡 —— 结构化 JSON → Vue 组件，**绝不 v-html**。
 * 依据手册 3.4.2：让模型输出 HTML 是一次性省事，长期全是负债；
 * 工具返回的数据里带着用户自由填写的备注，里面完全可以有 <img onerror=...>。
 *
 * facts / flags / actions 全由后端装配，本组件只负责画，不做任何业务判断——
 * 按钮该不该灰，看后端给的 disabled_by，不在前端重算一遍（那就是双写了）。
 */
import { ref, computed } from 'vue'
import { CARD_REGISTRY, runCardAction, type AgentCard } from './cardRegistry'

const props = defineProps<{ card: AgentCard; index?: number; total?: number }>()
const emit = defineEmits<{ (e: 'done', payload: { action: string; card: AgentCard }): void }>()

const def = computed(() => CARD_REGISTRY[props.card.type])
const busy = ref('')
const errMsg = ref('')
const doneAction = ref('')

const amountFact = computed(() => props.card.facts.find((f) => f.emphasis))
/** 头部已经写了「#单号 · 提交人 X」，下面不再重复列——一个事实只出现一次 */
const HEADER_KEYS = new Set(['供应商', '单号', '提交人'])
const restFacts = computed(() =>
  props.card.facts.filter((f) => !f.emphasis && !HEADER_KEYS.has(f.k)))
const supplier = computed(() => props.card.facts.find((f) => f.k === '供应商')?.v || '')
const blockFlags = computed(() => props.card.flags.filter((f) => f.level === 'block'))
const warnFlags = computed(() => props.card.flags.filter((f) => f.level === 'warn'))

/** 金额拆整数位与小数位：设计稿里 29px + 16px，是这套视觉最有辨识度的一处 */
const amountParts = computed(() => {
  const v = amountFact.value?.v || ''
  const i = v.lastIndexOf('.')
  return i < 0 ? { int: v, dec: '' } : { int: v.slice(0, i), dec: v.slice(i) }
})

async function act(key: string) {
  const a = def.value?.actions[key]
  if (!a || busy.value) return
  let reason: string | undefined
  if (a.needsReason) {
    // 手机上打字麻烦，但驳回原因会推送给发起人，不能省
    reason = window.prompt('驳回原因（会发给提交人）') || ''
    if (!reason.trim()) return
  }
  busy.value = key
  errMsg.value = ''
  try {
    await runCardAction(props.card, key, reason)
    doneAction.value = key
    emit('done', { action: key, card: props.card })
  } catch (e: any) {
    // 后端 400 的原文照抄出来，绝不吞掉只说「操作失败」（手册 3.5.3）
    errMsg.value = e?.response?.data?.detail || e?.message || '操作失败'
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div v-if="def" class="h5card">
    <!-- 头：图标 + 标题 + 右侧状态药丸 -->
    <div class="hd">
      <div class="glyph">{{ def.glyph }}</div>
      <div class="ttl">
        <div class="t1">{{ def.title }}</div>
        <div class="t2">
          #{{ card.ref }}<template v-if="card.facts.find((f) => f.k === '提交人')">
            · 提交人 {{ card.facts.find((f) => f.k === '提交人')!.v }}</template>
        </div>
      </div>
      <div v-if="doneAction" class="h5-pill h5-pill--good">
        {{ doneAction === 'approve' ? '已通过' : '已驳回' }}
      </div>
      <div v-else-if="total && total > 1" class="h5-pill h5-pill--blue">{{ index }} / {{ total }}</div>
    </div>

    <!-- 金额：整数大、小数小 -->
    <div v-if="amountFact" class="amt-wrap">
      <div class="sup">{{ supplier }}</div>
      <div class="h5-amount">{{ amountParts.int }}<span class="dec">{{ amountParts.dec }}</span></div>
    </div>

    <!-- 其余事实行 -->
    <div v-for="f in restFacts" :key="f.k" class="h5-field">
      <span class="fk">{{ f.k }}</span>
      <span class="fv">{{ f.v }}</span>
    </div>

    <!-- 异常：警示在前，拇指移向按钮的路上必然扫过 -->
    <div v-for="f in warnFlags" :key="f.code" class="alarm warn">
      <span class="ico">!</span><span>{{ f.msg }}</span>
    </div>
    <div v-for="f in blockFlags" :key="f.code" class="alarm block">
      <span class="ico">!</span><span>{{ f.msg }}</span>
    </div>

    <!-- AI 观点：独立区块，不与 facts 混排 -->
    <div v-if="card.note" class="note">
      <div class="note-h">AI 建议</div>{{ card.note }}
    </div>

    <div v-if="errMsg" class="alarm block"><span class="ico">!</span><span>{{ errMsg }}</span></div>

    <!-- 完成态 -->
    <div v-if="doneAction" class="okbar">
      <span class="tick">✓</span>
      {{ doneAction === 'approve' ? '已通过，财务会收到付款通知' : '已驳回，提交人会收到通知' }}
    </div>

    <!-- 操作条 -->
    <div v-else class="acts">
      <button
        v-for="a in card.actions" :key="a.key"
        class="btn" :class="{ primary: a.primary, danger: def.actions[a.key]?.danger }"
        :disabled="!!a.disabled_by || !!busy"
        @click="act(a.key)"
      >{{ busy === a.key ? '处理中…' : def.actions[a.key]?.label || a.key }}</button>
    </div>
  </div>

  <!-- type 不在白名单 → 整张卡不渲染，降级成一行纯文本并留痕 -->
  <div v-else class="fallback">这条内容无法安全展示（未登记的卡片类型），请到电脑端查看</div>
</template>

<style scoped>
.h5card {
  background: rgba(255, 255, 255, .6);
  backdrop-filter: blur(20px) saturate(1.4);
  border: 1px solid rgba(255, 255, 255, .85);
  border-radius: var(--h5-r-panel);
  box-shadow: var(--h5-sh-raised);
  padding: 16px 18px;
  font-family: var(--h5-font);
}
.hd { display: flex; align-items: center; gap: 10px }
.glyph {
  width: 34px; height: 34px; border-radius: var(--h5-r-chip); flex: none;
  display: grid; place-items: center; font-size: 15px; font-weight: 600;
  color: var(--h5-blue); background: rgba(76, 141, 255, .15);
  border: 1px solid rgba(255, 255, 255, .6);
}
.ttl { flex: 1; min-width: 0 }
.t1 { font-size: 14px; font-weight: 600; color: var(--h5-ink) }
.t2 { font-size: 11.5px; color: var(--h5-ink-3); margin-top: 2px }

.amt-wrap { margin-top: 14px }
.sup { font-size: 12px; color: var(--h5-ink-3) }

.h5-field { margin-top: 12px; justify-content: space-between }
.fk { font-size: 12px; color: var(--h5-ink-3) }
.fv { font-size: 13px; font-weight: 500; color: var(--h5-ink-2) }

.alarm {
  display: flex; gap: 9px; align-items: flex-start; margin-top: 12px;
  border-radius: var(--h5-r-card); padding: 11px 13px;
  font-size: 12px; line-height: 1.65;
}
.alarm .ico {
  flex: none; width: 16px; height: 16px; border-radius: 50%;
  display: grid; place-items: center; color: #fff; font-size: 11px; font-weight: 700;
}
.alarm.warn { background: rgba(169, 106, 8, .10); color: #7A4D05 }
.alarm.warn .ico { background: var(--h5-warn) }
.alarm.block { background: rgba(196, 54, 47, .10); color: #96271F }
.alarm.block .ico { background: var(--h5-danger) }

.note {
  margin-top: 12px; border-radius: var(--h5-r-card); padding: 11px 13px;
  background: rgba(107, 82, 168, .09); color: #4C1D95; font-size: 12px; line-height: 1.65;
}
.note-h { font-size: 11px; font-weight: 700; color: #6B52A8; margin-bottom: 3px }

.okbar {
  margin-top: 14px; border-radius: var(--h5-r-card); padding: 12px 14px;
  background: rgba(42, 122, 82, .12); color: var(--h5-good);
  font-size: 12.5px; font-weight: 500; display: flex; align-items: center; gap: 8px;
}
.tick {
  width: 18px; height: 18px; border-radius: 50%; background: var(--h5-good);
  color: #fff; display: grid; place-items: center; font-size: 11px; flex: none;
}

.acts { display: flex; gap: 10px; margin-top: 16px }
.btn {
  flex: 1; min-height: 46px; border: 0; border-radius: var(--h5-r-card);
  font: 600 15px/1 var(--h5-font); cursor: pointer;
  background: rgba(255, 255, 255, .7); color: var(--h5-ink-2);
  border: 1px solid rgba(255, 255, 255, .8);
  transition: transform .14s, box-shadow .14s, opacity .14s;
}
.btn.primary {
  flex: 1.7; background: var(--h5-grad-btn); color: #fff;
  border: 0; box-shadow: var(--h5-sh-btn-sm);
}
.btn.danger { color: var(--h5-danger) }
.btn:active:not(:disabled) { transform: translateY(1px) }
.btn:disabled { opacity: .45; cursor: not-allowed; box-shadow: none }

.fallback {
  border-radius: var(--h5-r-card); padding: 12px 14px; font-size: 12.5px;
  background: rgba(255, 255, 255, .55); color: var(--h5-ink-3);
}
</style>
