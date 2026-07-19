<script setup lang="ts">
import { ref, computed, nextTick, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { MagicStick, Promotion, Setting } from '@element-plus/icons-vue'
import { agentApi, type ChatHistoryItem } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'

interface ChatItem {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
  fallback?: boolean
}

const QUICK_QUESTIONS = ['今日晨报', '采购未到货', '尾款到期', '逾期任务']

const auth = useAuthStore()
// 🆕 LLM 配置入口仅 admin 可见（manager 能用助手/选模型，但看不到配置按钮）
const isAdmin = computed(() => auth.hasRole('admin'))

const messages = ref<ChatItem[]>([
  {
    role: 'assistant',
    content: '你好，我是 ERP 数据助手（只读）。可以问我：今日晨报 / 采购未到货 / 尾款到期 / 逾期任务，'
      + '或带上项目编号问进度（如「TH-2501 进度」）。所有数字都来自系统实时查询。',
  },
])
const input = ref('')
const sending = ref(false)
const listRef = ref<HTMLElement | null>(null)

// 🆕 模型选择：选项来自 GET /api/agent/models，选择持久化到 localStorage
const MODEL_LS_KEY = 'pms_agent_model'
const modelOptions = ref<string[]>([])
const selectedModel = ref(localStorage.getItem(MODEL_LS_KEY) || '')
const llmEnabled = ref(true)
watch(selectedModel, (v) => localStorage.setItem(MODEL_LS_KEY, v || ''))

async function loadModels() {
  try {
    const resp = await agentApi.getModels()
    modelOptions.value = resp.models
    llmEnabled.value = resp.llm_enabled
    // localStorage 里的值已不在白名单（或没选过）→ 回落到后端默认模型
    if (!selectedModel.value || !resp.models.includes(selectedModel.value)) {
      selectedModel.value = resp.default
    }
  } catch { /* 拉不到列表就不显示下拉，聊天不受影响 */ }
}

onMounted(loadModels)

// 🆕 LLM 配置弹窗（admin）：保存后刷新模型下拉与 llm_enabled 状态
const cfgVisible = ref(false)
const cfgSaving = ref(false)
const cfgHasKey = ref(false)
const cfgMasked = ref('')
const cfgForm = ref({ base_url: '', api_key: '', model: '', models: '' })

async function openConfig() {
  cfgVisible.value = true
  cfgForm.value = { base_url: '', api_key: '', model: '', models: '' }
  try {
    const cfg = await agentApi.getConfig()
    cfgHasKey.value = cfg.has_key
    cfgMasked.value = cfg.api_key_masked
    cfgForm.value = { base_url: cfg.base_url, api_key: '', model: cfg.model, models: cfg.models }
  } catch { /* 失败由拦截器弹 detail */ }
}

async function saveConfig() {
  cfgSaving.value = true
  try {
    const cfg = await agentApi.saveConfig({ ...cfgForm.value })
    cfgHasKey.value = cfg.has_key
    cfgMasked.value = cfg.api_key_masked
    ElMessage.success('配置已保存，全局生效')
    cfgVisible.value = false
    await loadModels()   // 配置变了 → 模型下拉/规则模式状态立即刷新
  } catch { /* 失败由拦截器弹后端 detail，弹窗保持打开 */ } finally {
    cfgSaving.value = false
  }
}

async function scrollBottom() {
  await nextTick()
  if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
}

async function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q || sending.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: q })
  sending.value = true
  await scrollBottom()
  try {
    // 只带最近 10 轮上下文（后端同样会截断）
    const history: ChatHistoryItem[] = messages.value
      .slice(0, -1)
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .slice(-20)
      .map((m) => ({ role: m.role, content: m.content }))
    const resp = await agentApi.chat(q, history, selectedModel.value || undefined)
    messages.value.push({
      role: 'assistant', content: resp.reply, sources: resp.sources, fallback: resp.fallback,
    })
  } catch {
    messages.value.push({ role: 'assistant', content: '（请求失败，请稍后重试）' })
  } finally {
    sending.value = false
    await scrollBottom()
  }
}
</script>

<template>
  <div class="agent-page">
    <div class="page-header">
      <div>
        <h1>Agent 助手（测试）</h1>
        <div class="desc">只读问数：答案中的数字均来自系统实时查询，不会修改任何数据</div>
      </div>
      <div class="spacer"></div>
      <!-- 🆕 模型选择：未配置 LLM Key 时禁用并提示规则模式 -->
      <div v-if="modelOptions.length" class="model-bar">
        <span v-if="!llmEnabled" class="model-hint">未配置模型 Key，当前为规则模式</span>
        <el-select
          v-model="selectedModel"
          :disabled="!llmEnabled"
          size="small"
          style="width: 200px"
          title="选择大模型"
        >
          <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
        </el-select>
      </div>
      <!-- 🆕 LLM 配置入口（仅 admin 可见） -->
      <el-button
        v-if="isAdmin"
        size="small"
        :icon="Setting"
        style="margin-left: 10px"
        @click="openConfig"
      >配置</el-button>
    </div>

    <el-card shadow="never" class="chat-card">
      <!-- 快捷问题 -->
      <div class="quick-row">
        <el-button
          v-for="q in QUICK_QUESTIONS" :key="q"
          size="small" round :disabled="sending" @click="send(q)"
        >{{ q }}</el-button>
      </div>

      <!-- 消息列表 -->
      <div ref="listRef" class="msg-list">
        <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
          <div class="bubble" :class="m.role">
            <div class="bubble-text">{{ m.content }}</div>
            <div v-if="m.role === 'assistant' && (m.sources?.length || m.fallback)" class="bubble-meta">
              <el-tag v-if="m.fallback" size="small" type="info" effect="plain">规则模式</el-tag>
              <span v-if="m.sources?.length">数据来源：{{ m.sources.join('、') }}</span>
            </div>
          </div>
        </div>
        <div v-if="sending" class="msg-row assistant">
          <div class="bubble assistant">
            <div class="bubble-text thinking">正在查询…</div>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="input-row">
        <el-input
          v-model="input"
          placeholder="输入问题，回车发送（如：采购未到货吗 / AGT-2501 进度）"
          :disabled="sending"
          clearable
          @keyup.enter="send()"
        >
          <template #prefix>
            <el-icon><MagicStick /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" :loading="sending" :icon="Promotion" @click="send()">发送</el-button>
      </div>
    </el-card>

    <!-- 🆕 LLM 配置弹窗（仅 admin；保存后全局生效） -->
    <el-dialog v-model="cfgVisible" title="LLM 配置（全局生效）" width="480px">
      <el-form label-position="top">
        <el-form-item label="Base URL">
          <el-input v-model="cfgForm.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="cfgForm.api_key"
            type="password"
            show-password
            :placeholder="cfgHasKey ? `已配置：${cfgMasked}，留空则不修改` : '未配置，留空则不修改'"
          />
        </el-form-item>
        <el-form-item label="默认模型">
          <el-input v-model="cfgForm.model" placeholder="deepseek-chat" />
        </el-form-item>
        <el-form-item label="可选模型列表（逗号分隔）">
          <el-input v-model="cfgForm.models" placeholder="deepseek-chat,deepseek-reasoner" />
        </el-form-item>
        <div class="cfg-tip">API Key 留空 = 保持不变；任一字段填「-」= 清除页面配置，回退 .env 默认值。</div>
      </el-form>
      <template #footer>
        <el-button @click="cfgVisible = false">取消</el-button>
        <el-button type="primary" :loading="cfgSaving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.agent-page { display: flex; flex-direction: column; height: calc(100vh - 56px); }
.chat-card { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.chat-card :deep(.el-card__body) { display: flex; flex-direction: column; flex: 1; min-height: 0; }

/* 🆕 模型选择（页头右侧） */
.model-bar { display: flex; align-items: center; gap: 8px; }
.model-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.cfg-tip { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }

.quick-row { display: flex; flex-wrap: wrap; gap: 8px; padding-bottom: 12px; }
.quick-row .el-button + .el-button { margin-left: 0; }

.msg-list {
  flex: 1; min-height: 320px; overflow-y: auto;
  border-top: 1px solid var(--el-border-color-lighter);
  border-bottom: 1px solid var(--el-border-color-lighter);
  padding: 16px 4px;
  display: flex; flex-direction: column; gap: 14px;
}
.msg-row { display: flex; }
.msg-row.user { justify-content: flex-end; }
.msg-row.assistant { justify-content: flex-start; }

.bubble {
  max-width: 72%;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 14px; line-height: 1.7;
}
.bubble.user {
  background: var(--el-color-primary);
  color: #fff;
  border-bottom-right-radius: 2px;
}
.bubble.assistant {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
  border-bottom-left-radius: 2px;
}
.bubble-text { white-space: pre-wrap; word-break: break-word; }
.bubble-text.thinking { color: var(--el-text-color-secondary); }
.bubble-meta {
  margin-top: 6px; font-size: 12px; color: var(--el-text-color-secondary);
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}

.input-row { display: flex; gap: 10px; padding-top: 12px; }
.input-row .el-input { flex: 1; }
</style>
