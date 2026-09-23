<script setup lang="ts">
import { ref, computed, nextTick, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { MagicStick, Promotion, Refresh, Setting } from '@element-plus/icons-vue'
import { agentApi, type ChatHistoryItem, type AgentChatLogItem } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'
import { fmtDateTime } from '@/utils/format'
import PageRefresh from '@/components/PageRefresh.vue'   // 反馈#359：每个页面都有刷新

interface AskOption { label: string; send: string }
interface ChatItem {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
  fallback?: boolean
  suggestions?: string[]
  /** 🆕 提问卡：模型问「你要查哪个」，给几个可点的选项，点了直接发 send 那句 */
  ask?: { q: string; options: AskOption[] } | null
  /** 🆕 审批卡：模型只能提案（指定 ref），facts/flags 全由后端用当前用户重查后装配 */
  cards?: AgentCard[]
}

const QUICK_QUESTIONS = ['待我审批的', '今日晨报', '采购未到货', '尾款到期', '逾期任务']

/**
 * 🆕 2026-09-24 卡片通道（与 H5 同口径）。这几条是**精确文案**，别改成模糊匹配 ——
 * H5 那边改过一次，把用户自己打的「查询一下所有的待审批的待办?」也劫持成查请款单了。
 * 走这条通道时不经模型：待办是确定性的，让模型再想一遍纯属浪费，而且一线最怕等。
 */
const CARD_ENTRIES = new Set(['待我审批的', '待我审批的请款单', '待我审批', '请款审批', 'OA审批'])

// 🆕 2026-09-24 会话 id：只用于把审计日志里的多轮串起来分析，不参与鉴权。
//    刷新页面 = 新会话，与 H5 同口径（H5ChatView 也是每次进页面生成一个）。
const sessionId = newSessionId('w')

/** 「正在查 xxx…」。工具轮次后端不推正文，不给状态人会以为卡住了 */
const toolHint = ref('')

// 🆕 助手回复按 Markdown 渲染（html:false 防 XSS，原始 HTML 一律转义；用户消息保持纯文本）
//
// ⚠️ 2026-09-24：这里原来是自己 `new MarkdownIt(...)`，于是后端后来加的两套约定
//    **一套都没认**，用户截图报上来了：
//      · 语义着色 `[[danger:30 天]]` `[[muted:—]]` 全部裸露成字面量。而 render.py
//        把表格里**每个空单元格**都写成 `[[muted:—]]`，30 行的表就是几十处。
//      · ```pmschart 围栏本该画成 SVG 条形图，这里退化成代码块，
//        把整段 JSON 原样甩给用户 —— 比裸标记更难看。
//    现在与 H5/APP 共用 `shared/agentMarkdown.ts` 的同一个实例：谁也不能再单边漏。
import { renderAgentMd as renderMd } from '@/shared/agentMarkdown'
// 🆕 2026-09-24 改走流式。收流与分包和 H5 共用一份（见 shared/agentStream.ts）。
import { streamAgentChat, newSessionId } from '@/shared/agentStream'
// 🆕 2026-09-24 审批卡。注册表与 H5 共用（端点/字段名只有一份），界面各自一套。
import { setCardHttp, isKnownCard, type AgentCard } from '@/shared/agentCards'
import { http } from '@/api'
import AgentApproveCard from '@/components/AgentApproveCard.vue'

setCardHttp(http)

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

// ==================== 🆕 审计日志（仅 admin 可见）：问答全量记录，可折叠 ====================
const auditCollapsed = ref(false)
const auditLoading = ref(false)
const auditList = ref<AgentChatLogItem[]>([])
const auditTotal = ref(0)
const auditPage = ref(1)
const auditSize = ref(20)
const auditUser = ref('')

// 工具名 → 中文标签（与后端 TOOL_LABELS 一致，原始名兜底）
const AUDIT_TOOL_LABELS: Record<string, string> = {
  morning_report: '晨报聚合',
  po_arrival_overdue: '采购到期未到货',
  po_arriving: '预计到货',
  po_overdue_by_supplier: '未到货·按供应商汇总',
  balance_due: '尾款到期清单',
  overdue_orders: '部门逾期任务',
  project_status: '项目进度查询',
}
const toolLabel = (n: string) => AUDIT_TOOL_LABELS[n] || n
const fmtDuration = (ms: number | null) =>
  ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`

async function loadAuditLogs() {
  auditLoading.value = true
  try {
    const res = await agentApi.getChatLogs({
      page: auditPage.value, size: auditSize.value,
      username: auditUser.value.trim() || undefined,
    })
    auditList.value = res.items
    auditTotal.value = res.total
  } catch { /* 失败由拦截器弹 detail */ } finally {
    auditLoading.value = false
  }
}
// 过滤条件变了从第 1 页重新查
function reloadAudit() { auditPage.value = 1; loadAuditLogs() }

onMounted(() => { if (isAdmin.value) loadAuditLogs() })

// 反馈#359：这页的「刷新」= 重拉模型列表与审计日志（对话记录原样保留）
async function reloadMeta() {
  await loadModels()
  if (isAdmin.value) await loadAuditLogs()
}

async function scrollBottom() {
  await nextTick()
  if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
}

/** 待办卡通道：不经模型，直接取后端装配好的卡 */
async function loadCards() {
  sending.value = true
  try {
    const { data } = await http.get('/agent/cards/pending')
    // 白名单在前端再过一道：后端给了未登记的 type 就整张不渲染（原则三）；
    // 再把能批的排前面——批不了的压后，别让人先划过一堆灰按钮
    const cards = (data.cards as AgentCard[])
      .filter((c) => isKnownCard(c.type))
      .sort((a, b) => Number(a.flags.some((f) => f.level === 'block'))
                    - Number(b.flags.some((f) => f.level === 'block')))
    const dropped = data.cards.length - cards.length
    messages.value.push({
      role: 'assistant',
      content: cards.length === 0
        ? '现在没有待你审批的单子。想看别的可以直接问我。'
        : `共 ${cards.length} 单`
          + (data.blocked ? `，其中 ${data.blocked} 单按职责分离需他人处理` : '')
          + '。'
          // ⚠️ 别写成「无法安全展示」——那听着像出了安全事故，而实际原因几乎总是
          //   **版本差**：后端上了新卡片类型，手上的前端还不认识它（白名单挡掉）。
          + (dropped
            ? `（另有 ${dropped} 条是新类型，你这个版本还显示不了：刷新页面；`
              + `客户端的话等一次自动更新）`
            : ''),
      cards: cards.length ? cards : undefined,
    })
  } catch (e: any) {
    messages.value.push({
      role: 'assistant',
      content: e?.response?.data?.detail || '取待办失败，请稍后重试',
    })
  } finally {
    sending.value = false
    await scrollBottom()
  }
}

async function send(text?: string) {
  const q = (text ?? input.value).trim()
  if (!q || sending.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: q })
  await scrollBottom()
  if (CARD_ENTRIES.has(q)) return loadCards()
  sending.value = true
  // 只带最近 10 轮上下文（后端同样会截断）
  const history: ChatHistoryItem[] = messages.value
    .slice(0, -1)
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .slice(-20)
    .map((m) => ({ role: m.role, content: m.content }))

  // 🆕 2026-09-24 改走 /chat/stream。不只是为了「逐字出字」——
  //   **技能、别名展开、口径召回都只写在流式那条路径上**（agent_router._chat_stream），
  //   非流式的 _chat_with_llm 一条都没有。所以之前同一句话在手机上和电脑上
  //   会得到不一样的答案，而两边看起来都「正常」，这种不一致最难查。
  //
  // ⚠️ 气泡必须先 push 进数组、再用**数组里那个引用**累加文字：
  //   直接改 push 之前的原始对象改的是 raw target，不走 Vue 的 set 陷阱，
  //   表现是「只显示第一个字，后面全不动」（H5 踩过，注释留在 H5ChatView）。
  const draft: ChatItem = { role: 'assistant', content: '' }
  let bubble = draft
  let opened = false
  const open = () => {
    if (opened) return
    opened = true
    sending.value = false          // 收到第一个字就撤掉「思考中」三个点
    toolHint.value = ''
    messages.value.push(draft)
    bubble = messages.value[messages.value.length - 1]
  }

  try {
    await streamAgentChat({
      // baseURL 与 axios 同源：桌面客户端打包时 VITE_API_BASE 是绝对地址，
      // 浏览器构建为空 → '/api'（走 Vite 代理 / nginx）。
      url: (import.meta.env.VITE_API_BASE ?? '') + '/api/agent/chat/stream',
      token: localStorage.getItem('pms_token') || '',
      message: q,
      history,
      sessionId,
      model: selectedModel.value || undefined,
    }, {
      onTool: (label) => { toolHint.value = `正在查 ${label}…` },
      onDelta: (text) => { open(); bubble.content += text; void scrollBottom() },
      onDone: (d) => {
        open()
        bubble.sources = d.sources
        bubble.fallback = d.fallback
        bubble.suggestions = d.suggestions || []
        if (d.ask?.options?.length) bubble.ask = d.ask
        // type 不在注册表里的整张丢掉，别渲染一张点了没反应的卡
        if (d.cards?.length) {
          const cs = (d.cards as AgentCard[]).filter((c) => isKnownCard(c.type))
          if (cs.length) bubble.cards = cs
        }
        toolHint.value = ''
      },
      onError: (msg) => { open(); bubble.content += msg },
    })
    // done 可能一次都不来（网络断/用户关页面），兜一句别留个空气泡
    if (!opened) {
      open()
      bubble.content = '（没有收到回答，请重试）'
    }
  } catch {
    if (!opened) open()
    bubble.content = bubble.content || '（请求失败，请稍后重试）'
  } finally {
    sending.value = false
    toolHint.value = ''
    await scrollBottom()
  }
}
</script>

<template>
  <div class="agent-page">
    <div class="page-header">
      <div>
        <h1>AI 助手</h1>
        <div class="desc">只读问数：答案中的数字均来自系统实时查询，不会修改任何数据；可查询的数据域与你的菜单权限一致</div>
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
      <!-- 反馈#359 说的是「每个界面」，这页也不例外。
           ⚠️ 只重拉模型列表/审计日志，**不动对话记录**——
           聊天页上一个「刷新」很容易被当成"清空对话"，那是最不该发生的误解。 -->
      <PageRefresh :load="reloadMeta" size="small" />
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
            <!-- 🆕 2026-09-24 审批卡：点「通过/驳回/确认发出」打的是用户自己的 token，
                 跟他在业务页面上点是同一个请求（见 shared/agentCards.ts）。 -->
            <AgentApproveCard
              v-for="(c, ci) in (m.cards || [])" :key="'c' + ci"
              :card="c"
            />
            <!-- 🆕 2026-09-24 提问卡：模型不猜，把歧义摆出来让人点。
                 以前网页版拿的是 ask_to_text 降级出来的「1. 2. 3.」纯文本，
                 用户得自己把选项重打一遍。 -->
            <div v-if="m.role === 'assistant' && m.ask?.options?.length" class="ask-box">
              <div class="ask-q">{{ m.ask.q }}</div>
              <div class="ask-opts">
                <el-button
                  v-for="(o, oi) in m.ask.options" :key="oi"
                  size="small" plain :disabled="sending" @click="send(o.send)"
                >{{ o.label }}</el-button>
              </div>
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
            <!-- 🆕 工具轮次后端不推正文，不给状态人会以为卡住了 -->
            <span v-if="toolHint" class="tool-hint">{{ toolHint }}</span>
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

    <!-- 🆕 审计日志卡片（仅 admin 可见）：问答全量记录，可按用户名过滤，行展开看全文 -->
    <el-card v-if="isAdmin" shadow="never" class="audit-card">
      <template #header>
        <div class="audit-header">
          <span class="audit-title">审计日志</span>
          <span class="audit-tip">问答全量记录（含规则降级），按时间倒序</span>
          <div class="spacer"></div>
          <el-input
            v-model="auditUser"
            size="small"
            placeholder="按用户名过滤"
            clearable
            style="width: 150px"
            @keyup.enter="reloadAudit"
            @clear="reloadAudit"
          />
          <el-button size="small" @click="reloadAudit">查询</el-button>
          <el-button size="small" :icon="Refresh" :loading="auditLoading" @click="loadAuditLogs" />
          <el-button size="small" text @click="auditCollapsed = !auditCollapsed">
            {{ auditCollapsed ? '展开' : '收起' }}
          </el-button>
        </div>
      </template>
      <template v-if="!auditCollapsed">
        <el-table :data="auditList" v-loading="auditLoading" stripe size="small" max-height="360">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="audit-expand">
                <div class="audit-qa">
                  <div class="audit-qa-label">问题</div>
                  <div class="audit-text">{{ row.question }}</div>
                </div>
                <div class="audit-qa">
                  <div class="audit-qa-label">回答</div>
                  <div class="audit-text">{{ row.answer }}</div>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="140">
            <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="用户" prop="username" width="100" show-overflow-tooltip />
          <el-table-column label="问题" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">{{ row.question }}</template>
          </el-table-column>
          <el-table-column label="回答" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.answer }}</template>
          </el-table-column>
          <el-table-column label="工具" min-width="150">
            <template #default="{ row }">
              <template v-if="row.tools_used?.length">
                <el-tag
                  v-for="t in row.tools_used" :key="t"
                  size="small" effect="plain" style="margin-right: 4px"
                >{{ toolLabel(t) }}</el-tag>
              </template>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="方式 / 模型" width="190">
            <template #default="{ row }">
              <el-tag size="small" :type="row.via === 'llm' ? 'primary' : 'info'">
                {{ row.via === 'llm' ? 'LLM' : '规则' }}
              </el-tag>
              <span class="audit-model" :title="row.model">{{ row.model }}</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="80" align="right">
            <template #default="{ row }">{{ fmtDuration(row.duration_ms) }}</template>
          </el-table-column>
        </el-table>
        <div class="audit-pager">
          <el-pagination
            v-model:current-page="auditPage"
            layout="total, prev, pager, next"
            :total="auditTotal"
            :page-size="auditSize"
            @current-change="loadAuditLogs"
          />
        </div>
      </template>
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

/* 🆕 审计日志卡片（admin）：页内滚出空间，行展开看问答全文 */
.agent-page { overflow-y: auto; }
.audit-card { flex: none; margin-top: 12px; }
.audit-header { display: flex; align-items: center; gap: 8px; }
.audit-header .spacer { flex: 1; }
.audit-title { font-weight: 600; }
.audit-tip { font-size: 12px; color: var(--el-text-color-secondary); font-weight: 400; }
.audit-model { margin-left: 6px; font-size: 12px; color: var(--el-text-color-secondary); }
.audit-expand { padding: 8px 12px; display: flex; flex-direction: column; gap: 10px; }
.audit-qa-label { font-size: 12px; font-weight: 600; color: var(--el-text-color-secondary); margin-bottom: 2px; }
.audit-text {
  white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.6;
  max-height: 260px; overflow-y: auto;
}
.audit-pager { display: flex; justify-content: flex-end; padding-top: 10px; }

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

/* 🆕 2026-09-24 语义着色。后端只发档位（[[danger:…]]），颜色在这里定
   —— 渲染器见 shared/agentMarkdown.ts，两端共用一份，类名也共用。
   ⚠️ 只给真正要一眼看见的上色；muted 是空值占位（表格里每个空格都会走它），
      刻意做得很淡，不然满屏破折号比数据还抢眼。 */
.md-body :deep(.agent-tone) { font-weight: 600; }
.md-body :deep(.agent-tone--danger) { color: var(--el-color-danger); }
.md-body :deep(.agent-tone--warn)   { color: var(--el-color-warning); }
.md-body :deep(.agent-tone--good)   { color: var(--el-color-success); }
.md-body :deep(.agent-tone--muted)  { color: var(--el-text-color-placeholder); font-weight: 400; }

/* 🆕 2026-09-24 图（shared/agentMarkdown.ts 的 chartPlugin 拼出来的 SVG）。
   ⚠️ **必须封顶**：viewBox 固定 320 宽、靠 width:100% 自适应，
      网页版容器比手机宽得多，不封顶就等比放大成一张巨图、字也跟着糊。 */
.md-body :deep(.pmschart) { margin: 10px 0 2px; }
.md-body :deep(.pmschart svg) { width: 100%; max-width: 420px; height: auto; display: block; }
.md-body :deep(.pc-title) { font-size: 12px; font-weight: 600; fill: var(--el-text-color-primary); }
.md-body :deep(.pc-lab)   { font-size: 11px; fill: var(--el-text-color-regular); }
.md-body :deep(.pc-val)   { font-size: 11px; font-weight: 600; text-anchor: end; }
.md-body :deep(.pc-zero)  { font-size: 10px; fill: var(--el-text-color-placeholder); text-anchor: middle; }
.md-body :deep(.pc-axis)  { stroke: var(--el-border-color); stroke-width: 1; }

/* 🆕 2026-09-24 提问卡与「正在查…」 */
.ask-box {
  margin-top: 6px; padding: 8px 10px; border-radius: 8px;
  background: var(--el-fill-color-lighter); border: 1px solid var(--el-border-color-lighter);
}
.ask-q { font-size: 13px; color: var(--el-text-color-regular); margin-bottom: 6px; }
.ask-opts { display: flex; flex-wrap: wrap; gap: 6px; }
.tool-hint { margin-left: 8px; font-size: 12px; color: var(--el-text-color-secondary); }
</style>
