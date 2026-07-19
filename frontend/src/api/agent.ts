import { http } from './index'

// 🆕 Agent 助手（只读问数 POC，admin/manager）
export interface ChatHistoryItem {
  role: 'user' | 'assistant'
  content: string
}

export interface AgentChatReply {
  reply: string
  fallback: boolean // true=规则降级模式（未配置 LLM 或 LLM 异常）
  sources: string[] // 本轮实际调用的数据工具（「数据来源」小字展示）
  suggestions?: string[] // 追问建议（渲染为可点击 chips，点击直接发送）
}

export interface AgentModelsReply {
  models: string[]      // 可选模型白名单（含默认模型）
  default: string       // 默认模型
  llm_enabled: boolean  // 后端是否已配置 LLM Key（false=纯规则模式）
}

// 🆕 LLM 配置（仅 admin 可见/可改；api_key 永不回传明文）
export interface AgentConfig {
  base_url: string
  model: string
  models: string        // 逗号分隔白名单（原文）
  api_key_masked: string // 打码值，形如 ****abcd；空=未配置
  has_key: boolean       // 是否已配 key（含 .env 里配的）
}

export interface AgentConfigPatch {
  base_url?: string
  api_key?: string   // 空字符串=保持不变；"-"=清除回退 .env 默认
  model?: string
  models?: string
}

export const agentApi = {
  chat: (message: string, history: ChatHistoryItem[] = [], model?: string) =>
    http.post<AgentChatReply>('/agent/chat', { message, history, ...(model ? { model } : {}) })
      .then((r) => r.data),

  getModels: () => http.get<AgentModelsReply>('/agent/models').then((r) => r.data),

  getConfig: () => http.get<AgentConfig>('/agent/config').then((r) => r.data),

  saveConfig: (body: AgentConfigPatch) =>
    http.put<AgentConfig>('/agent/config', body).then((r) => r.data),
}
