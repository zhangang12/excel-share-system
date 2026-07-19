<script setup lang="ts">
import { ref, computed, nextTick, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { MagicStick, Promotion, Setting } from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import { agentApi, type ChatHistoryItem } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'

interface ChatItem {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
  fallback?: boolean
  suggestions?: string[]
}

const QUICK_QUESTIONS = ['今日晨报', '采购未到货', '尾款到期', '逾期任务']

// 🆕 助手回复按 Markdown 渲染（html:false 防 XSS，原始 HTML 一律转义；用户消息保持纯文本）
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const renderMd = (text: string) => md.render(text || '')

const auth = useAuthStore()
// 🆕 LLM 配置入口仅 admin 可见（manager 能用助手/选模型，但看不到配置按钮）
const isAdmin = computed(() => auth.hasRole('admin'))
// 用户头像首字（姓名优先，其次用户名）
const userInitial = computed(() => {
  const name = auth.user?.full_name || auth.user?.username || '我'
  return name.trim().charAt(0) || '我'
})

const messages = ref<ChatItem[]>([
  {
    role: 'assistant',
    content: '你好，我是 ERP 数据助手（只读），所有数字都来自系统实时查询。可以问我：\n'
      + '- **今日晨报**：采购未到货 / 逾期任务 / 尾款 / 人事到期一览\n'
      + '- **采购未到货**、**哪个供应商拖期**、**未来一周到货**\n'
      + '- **尾款到期**、**逾期任务**\n'
      + '- 单项目进度：带上项目编号，如「TH-2501 进度」',
    suggestions: QUICK_QUESTIONS.slice(0, 3),
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
      role: 'assistant', content: resp.reply, sources: resp.sources,
      fallback: resp.fallback, suggestions: resp.suggestions || [],
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
          <div v-if="m.role === 'assistant'" class="avatar assistant">
            <el-icon><MagicStick /></el-icon>
          </div>
          <div class="bubble-col">
            <div class="bubble" :class="m.role">
              <!-- 助手：Markdown 渲染；用户：纯文本 -->
              <div v-if="m.role === 'assistant'" class="md-body" v-html="renderMd(m.content)"></div>
              <div v-else class="bubble-text">{{ m.content }}</div>
            </div>
            <div v-if="m.role === 'assistant' && (m.sources?.length || m.fallback)" class="bubble-meta">
              <el-tag v-if="m.fallback" size="small" type="info" effect="plain">规则模式</el-tag>
              <span v-if="m.sources?.length">数据来源：{{ m.sources.join('、') }}</span>
            </div>
            <!-- 🆕 追问建议 chips：点击直接发送 -->
            <div v-if="m.role === 'assistant' && m.suggestions?.length" class="sugg-row">
              <span
                v-for="s in m.suggestions" :key="s"
                class="sugg-chip" :class="{ disabled: sending }"
                @click="!sending && send(s)"
              >{{ s }}</span>
            </div>
          </div>
          <div v-if="m.role === 'user'" class="avatar user">{{ userInitial }}</div>
        </div>
        <div v-if="sending" class="msg-row assistant">
          <div class="avatar assistant">
            <el-icon><MagicStick /></el-icon>
          </div>
          <div class="bubble assistant thinking">
            <span class="dot"></span><span class="dot"></span><span class="dot"></span>
          </div>
        </div>
      </div>

      <!-- 输入区：Enter 发送，Shift+Enter 换行 -->
      <div class="input-row">
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 5 }"
          placeholder="输入问题，Enter 发送、Shift+Enter 换行（如：采购未到货吗 / TH-2501 进度）"
          :disabled="sending"
          @keydown.enter.exact.prevent="send()"
        />
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
  padding: 16px 6px;
  display: flex; flex-direction: column; gap: 16px;
}
.msg-row { display: flex; align-items: flex-start; gap: 8px; }
.msg-row.user { flex-direction: row-reverse; }

.avatar {
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 600; margin-top: 2px;
}
.avatar.assistant {
  background: linear-gradient(135deg, var(--el-color-primary), #7aa5f8);
  color: #fff; font-size: 15px;
}
.avatar.user { background: var(--el-color-primary-light-7); color: var(--el-color-primary); }

.bubble-col { max-width: 78%; display: flex; flex-direction: column; }
.msg-row.user .bubble-col { align-items: flex-end; }

.bubble {
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 14px; line-height: 1.7;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06);
}
.bubble.user {
  background: var(--el-color-primary);
  color: #fff;
  border-bottom-right-radius: 2px;
}
.bubble.assistant {
  background: #fff;
  border: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-primary);
  border-bottom-left-radius: 2px;
}
.bubble-text { white-space: pre-wrap; word-break: break-word; }

.bubble-meta {
  margin-top: 5px; font-size: 12px; color: var(--el-text-color-secondary);
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}

/* 🆕 追问建议 chips */
.sugg-row { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.sugg-chip {
  font-size: 12px; color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-5);
  border-radius: 14px; padding: 2px 10px; cursor: pointer;
  transition: background .15s;
}
.sugg-chip:hover { background: var(--el-color-primary-light-7); }
.sugg-chip.disabled { opacity: .5; cursor: not-allowed; }

/* 「正在思考…」三点动画 */
.bubble.thinking { display: flex; gap: 5px; align-items: center; padding: 14px 16px; }
.dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--el-text-color-secondary);
  animation: blink 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: .2s; }
.dot:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }

.input-row { display: flex; gap: 10px; padding-top: 12px; align-items: flex-end; }
.input-row .el-input { flex: 1; }

/* 🆕 助手气泡内的 Markdown 排版（scoped 需 :deep 穿透 v-html） */
.md-body { word-break: break-word; }
.md-body :deep(p) { margin: 4px 0; }
.md-body :deep(p:first-child) { margin-top: 0; }
.md-body :deep(p:last-child) { margin-bottom: 0; }
.md-body :deep(h2), .md-body :deep(h3) {
  font-size: 15px; font-weight: 700; margin: 8px 0 4px; color: var(--el-text-color-primary);
}
.md-body :deep(ul), .md-body :deep(ol) { margin: 4px 0; padding-left: 20px; }
.md-body :deep(li) { margin: 2px 0; }
.md-body :deep(strong) { color: var(--el-color-danger-dark-2); font-weight: 700; }
.md-body :deep(table) {
  border-collapse: collapse; margin: 8px 0; font-size: 13px;
  display: block; overflow-x: auto; max-width: 100%;
}
.md-body :deep(th), .md-body :deep(td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 5px 10px; text-align: left; white-space: nowrap;
}
.md-body :deep(th) { background: var(--el-fill-color-light); font-weight: 600; }
.md-body :deep(tbody tr:nth-child(even)) { background: var(--el-fill-color-lighter); }
.md-body :deep(code) {
  font-family: Menlo, Consolas, monospace; font-size: .9em;
  background: var(--el-fill-color); border-radius: 4px; padding: 1px 5px;
}
.md-body :deep(a) { color: var(--el-color-primary); }
</style>
