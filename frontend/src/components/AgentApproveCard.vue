<script setup lang="ts">
/**
 * 审批卡（网页版/桌面客户端）—— 结构化 JSON → Vue 组件，**绝不 v-html**。
 * 依据手册 3.4.2：让模型输出 HTML 是一次性省事，长期全是负债；
 * 工具返回的数据里带着用户自由填写的备注，里面完全可以有 <img onerror=...>。
 *
 * 🆕 2026-09-24。网页版原来连 `cards` 字段都不认，电脑上看不到
 * 「通过 / 驳回 / 确认发出」这些按钮，只能另外去业务页面找那张单子。
 *
 * ⚠️ 与 H5ApproveCard.vue 是**两套界面、同一份注册表**（shared/agentCards.ts）：
 *    界面共用不了（那边是设计稿风格、这边是 element-plus），但端点和动作定义
 *    必须共用 —— 抄两份的话后端改了路径就会有一端静默打错地址。
 *
 * facts / flags / actions 全由后端装配，本组件只负责画，不做任何业务判断 ——
 * 按钮该不该灰看后端给的 disabled_by，不在前端重算一遍（那就是双写了）。
 */
import { ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CARD_REGISTRY, runCardAction, type AgentCard } from '@/shared/agentCards'

const props = defineProps<{ card: AgentCard }>()
const emit = defineEmits<{ (e: 'done', payload: { action: string; card: AgentCard }): void }>()

const def = computed(() => CARD_REGISTRY[props.card.type])
const busy = ref('')
const errMsg = ref('')
const doneAction = ref('')

const blockFlags = computed(() => props.card.flags.filter((f) => f.level === 'block'))
const warnFlags = computed(() => props.card.flags.filter((f) => f.level === 'warn'))

async function act(key: string) {
  const a = def.value?.actions[key]
  if (!a || busy.value) return
  let reason: string | undefined
  if (a.needsReason) {
    // 驳回原因会推送给发起人，不能省
    try {
      const { value } = await ElMessageBox.prompt(
        '会原样发给提交人，说清楚怎么改', '填写原因',
        {
          inputType: 'textarea',
          inputPlaceholder: '例如：金额与合同不符，请核对后重新提交',
          inputValidator: (v: string) => (v && v.trim() ? true : '这一项必须填写'),
          confirmButtonText: '提交', cancelButtonText: '取消',
        })
      reason = value
    } catch { return }        // 点了取消
  }
  busy.value = key
  errMsg.value = ''
  try {
    await runCardAction(props.card, key, reason)
    doneAction.value = key
    ElMessage.success(a.danger ? '已驳回' : '已完成')
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
  <!-- type 不在注册表里就整张不渲染：宁可少一张卡，也不画一张点了没反应的 -->
  <div v-if="def" class="agent-card">
    <div class="ac-hd">
      <span class="ac-glyph">{{ def.glyph }}</span>
      <span class="ac-title">{{ def.title }}</span>
      <span class="ac-ref">#{{ card.ref }}</span>
    </div>

    <div class="ac-facts">
      <div v-for="(f, i) in card.facts" :key="i" class="ac-fact">
        <span class="ac-k">{{ f.k }}</span>
        <!-- 文本插值，不用 v-html：这些值里可能有用户自己填的备注 -->
        <span class="ac-v" :class="{ emph: f.emphasis }">{{ f.v }}</span>
      </div>
    </div>

    <div v-if="card.note" class="ac-note">{{ card.note }}</div>

    <!-- flags 逐条对应后端端点里的前置校验：端点有几条 raise，这里就该显示几条，
         漏一条用户就会点下去吃 400（手册 3.5.3） -->
    <div v-for="(f, i) in blockFlags" :key="'b' + i" class="ac-flag block">{{ f.msg }}</div>
    <div v-for="(f, i) in warnFlags" :key="'w' + i" class="ac-flag warn">{{ f.msg }}</div>

    <div v-if="errMsg" class="ac-flag block">{{ errMsg }}</div>

    <div v-if="doneAction" class="ac-done">已处理</div>
    <div v-else class="ac-acts">
      <el-button
        v-for="a in card.actions" :key="a.key"
        :type="a.primary ? 'primary' : 'default'"
        :plain="!a.primary"
        size="small"
        :loading="busy === a.key"
        :disabled="!!a.disabled_by || (!!busy && busy !== a.key)"
        :title="a.disabled_by ? (card.flags.find((f) => f.code === a.disabled_by)?.msg || '') : ''"
        @click="act(a.key)"
      >{{ def.actions[a.key]?.label || a.key }}</el-button>
    </div>
  </div>
</template>

<style scoped>
.agent-card {
  border: 1px solid var(--el-border-color-light); border-radius: 8px;
  padding: 10px 12px; margin: 8px 0; background: var(--el-bg-color);
  max-width: 460px;
}
.ac-hd { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.ac-glyph {
  width: 22px; height: 22px; border-radius: 6px; flex: none;
  background: var(--el-color-primary-light-8); color: var(--el-color-primary);
  display: grid; place-items: center; font-size: 12px; font-weight: 700;
}
.ac-title { font-weight: 600; font-size: 14px; }
.ac-ref { margin-left: auto; font-size: 12px; color: var(--el-text-color-secondary); }
.ac-facts { display: flex; flex-direction: column; gap: 4px; }
.ac-fact { display: flex; gap: 10px; font-size: 13px; line-height: 1.6; }
.ac-k { width: 68px; flex: none; color: var(--el-text-color-secondary); }
.ac-v { flex: 1; min-width: 0; word-break: break-all; }
.ac-v.emph { font-weight: 700; color: var(--el-color-danger); }
.ac-note {
  margin-top: 6px; font-size: 12px; color: var(--el-text-color-regular);
  background: var(--el-fill-color-lighter); border-radius: 6px; padding: 6px 8px;
}
.ac-flag {
  margin-top: 6px; font-size: 12px; border-radius: 6px; padding: 6px 8px; line-height: 1.6;
}
.ac-flag.block { color: var(--el-color-danger); background: var(--el-color-danger-light-9); }
.ac-flag.warn { color: var(--el-color-warning); background: var(--el-color-warning-light-9); }
.ac-acts { display: flex; gap: 8px; margin-top: 10px; }
.ac-done { margin-top: 10px; font-size: 13px; color: var(--el-color-success); }
</style>
