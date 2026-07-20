import { onUnmounted, ref } from 'vue'

export interface CellChangedEvent {
  type: 'cell_changed'
  record_id?: number | null
  project_id?: number | null
  field_id: number
  value: unknown
  by_user_id: number
}

export interface PresenceEvent {
  type: 'presence'
  action: 'join' | 'leave'
  user_id: number
}

type Event = CellChangedEvent | PresenceEvent | { type: 'pong' }

export function useRealtime(
  path: string,
  onCellChanged?: (ev: CellChangedEvent) => void,
) {
  const connected = ref(false)
  const onlineCount = ref(0)
  const _users = new Set<number>()
  let ws: WebSocket | null = null
  let pingTimer: number | null = null
  let reconnectTimer: number | null = null
  let destroyed = false

  function connect() {
    if (destroyed) return
    // 调试时如想关掉实时：localStorage.setItem('pms_disable_ws', '1')
    if (localStorage.getItem('pms_disable_ws') === '1') return
    const token = localStorage.getItem('pms_token') || ''
    if (!token) return
    try {
      // 🆕 桌面客户端以 VITE_API_BASE 构建：ws 地址从它推导（http→ws、https→wss）；
      //   浏览器构建未设该变量，保持从 location 拼的老逻辑。
      const apiBase = import.meta.env.VITE_API_BASE as string | undefined
      let url: string
      if (apiBase) {
        url = `${apiBase.replace(/^http/, 'ws')}${path}?token=${encodeURIComponent(token)}`
      } else {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws'
        url = `${proto}://${location.host}${path}?token=${encodeURIComponent(token)}`
      }
      ws = new WebSocket(url)
      ws.onopen = () => {
        connected.value = true
        pingTimer = window.setInterval(
          () => { try { ws?.send(JSON.stringify({ type: 'ping' })) } catch {} },
          30000,
        )
      }
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as Event
          if (data.type === 'cell_changed') {
            onCellChanged?.(data)
          } else if (data.type === 'presence') {
            if (data.action === 'join') _users.add(data.user_id)
            else _users.delete(data.user_id)
            onlineCount.value = _users.size
          }
        } catch { /* */ }
      }
      ws.onclose = () => {
        connected.value = false
        if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
        if (!destroyed) {
          reconnectTimer = window.setTimeout(connect, 5000)
        }
      }
      ws.onerror = () => {
        try { ws?.close() } catch {}
      }
    } catch {
      // WebSocket 构造失败也别让组件卡住
      if (!destroyed) {
        reconnectTimer = window.setTimeout(connect, 10000)
      }
    }
  }

  function disconnect() {
    destroyed = true
    if (pingTimer) clearInterval(pingTimer)
    if (reconnectTimer) clearTimeout(reconnectTimer)
    try { ws?.close() } catch {}
    ws = null
  }

  connect()
  onUnmounted(disconnect)

  return { connected, onlineCount, disconnect }
}
