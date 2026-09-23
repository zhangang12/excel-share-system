/**
 * 智能体流式问答（SSE）的收流与分包 —— **网页版/桌面客户端与 H5/APP 共用这一份**。
 *
 * 🆕 2026-09-24。抽这一份的理由和 `agentMarkdown.ts` 完全一样，而且是同一天、
 * 同一次排查发现的：`/chat/stream` 当初只接在 H5 上，网页版一直在打非流式的
 * `/chat`。后果不只是「网页端问一句要白屏等十几秒」——
 * **技能（skills）、别名展开、口径召回都只写在流式那条路径上**
 * （agent_router 的 `_chat_stream`，非流式的 `_chat_with_llm` 一条都没有），
 * 于是同一句话在手机上和电脑上会得到不一样的答案。这种不一致最难查，
 * 因为两边看起来都「正常」。
 *
 * 这里只做**收流和分包**，一行界面代码都没有：拿到 tool/delta/done/error 之后
 * 各自往自己的状态里写。两个前端一个用 Vue ref、一个用 element-plus，
 * 共用不了界面，但**协议解析必须共用** —— 分两份写就是下一个 bug。
 *
 * ⚠️ 为什么不用 `EventSource`：它**带不了 Authorization 头**（只能靠 cookie 或
 *    把 token 放进 URL，后者会进 nginx 访问日志）。所以用 fetch + ReadableStream
 *    自己解 SSE。这条别改回去。
 *
 * ⚠️ src/shared/ 的规矩：只许依赖零负担的东西，不许引任何 UI 框架。本文件零依赖。
 */

/** 后端 `sse()` 发出来的四种事件。见 agent_router.chat_stream。 */
export interface AgentStreamHandlers {
  /** 正在调某个工具。后端在工具轮次不推正文，给个「正在查 xxx…」别让人以为卡住了 */
  onTool?: (label: string) => void
  /** 正文片段，逐字来 */
  onDelta?: (text: string) => void
  /** 收尾：sources / suggestions / ask（提问卡）/ cards（审批卡）/ fallback / duration_ms */
  onDone?: (d: any) => void
  /** 后端明确报的错（比如无效模型）。网络断开不走这里，走 streamAgentChat 的 reject */
  onError?: (message: string) => void
}

export interface AgentStreamOptions {
  /** 完整 URL。APP 里页面在 localhost、API 在服务器上，所以由调用方拼绝对地址 */
  url: string
  token: string
  message: string
  history: { role: string; content: string }[]
  /** 只进审计日志，用来把多轮串起来分析；不参与鉴权、不影响任何数据可见性 */
  sessionId?: string
  model?: string
  signal?: AbortSignal
}

/**
 * 收一条流式问答。抛异常 = 连不上或 HTTP 非 2xx，调用方自己降级提示。
 *
 * 注意 `done` 事件**可能一次都不来**（用户中途关页面、网络断），
 * 所以别把「必须收到 done」写进调用方的状态机 —— 收到就补充，收不到也要能收场。
 */
export async function streamAgentChat(
  opts: AgentStreamOptions,
  h: AgentStreamHandlers,
): Promise<void> {
  const res = await fetch(opts.url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${opts.token}`,
    },
    body: JSON.stringify({
      message: opts.message,
      history: opts.history,
      ...(opts.sessionId ? { session_id: opts.sessionId } : {}),
      ...(opts.model ? { model: opts.model } : {}),
    }),
    signal: opts.signal,
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

  // 🆕 令牌续期（#425）：后端在剩余不足一半时用响应头下发新令牌，用着就不掉线。
  // ⚠️ 这条**必须在这里也做一遍** —— 两个前端的续期原来都写在 axios 响应拦截器里，
  //    而流式走的是 fetch，绕过了拦截器。一个长期泡在助手里、很少点别的页面的人
  //    （推广后的一线正是这样用）就会莫名其妙掉线，还查不出原因。
  const fresh = res.headers.get('x-pms-refresh-token')
  if (fresh) {
    try { localStorage.setItem('pms_token', fresh) } catch { /* 存储不可用就沿用旧令牌 */ }
  }

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    // SSE 以空行分隔事件；最后一段可能不完整，留在缓冲里等下一片。
    // ⚠️ 别改成按行直接处理：一个 data 可能被 TCP 切在中间，
    //    那样 JSON.parse 每隔几片就失败一次，表现是「偶尔掉字」。
    const parts = buf.split('\n\n')
    buf = parts.pop() || ''
    for (const raw of parts) {
      let ev = 'message'
      let data = ''
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      let d: any
      try { d = JSON.parse(data) } catch { continue }   // 坏包跳过，不要中断整条流
      if (ev === 'tool') h.onTool?.(d.label)
      else if (ev === 'delta') h.onDelta?.(d.text || '')
      else if (ev === 'done') h.onDone?.(d)
      else if (ev === 'error') h.onError?.(d.message || '出错了')
    }
  }
}

/**
 * 会话 id。刷新页面 = 新会话。
 *
 * ⚠️ `crypto.randomUUID` 只在**安全上下文**（https / localhost）里有，
 *    在 http 的网页版（非 localhost）里根本取不到，直接调会抛异常，
 *    把发消息整条带崩。所以必须兜底。
 */
export function newSessionId(prefix = 's'): string {
  try {
    const c = (globalThis as any).crypto
    if (c?.randomUUID) return c.randomUUID()
  } catch { /* 取不到就用下面的兜底 */ }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
