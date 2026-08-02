import { http } from './index'

// 🆕 外网登录闸门配置（admin/manager 专属）：开关 + 内网 IP/网段名单，存后端 app_settings
// device_gate：设备闸开关，默认关；打开后桌面端还要设备 ID 在 device_ids 里才免验证码
export interface GateConfig {
  enabled: boolean
  cidrs: string[]
  device_gate: boolean
  device_ids: string[]
}

export const gateApi = {
  get: () => http.get<GateConfig>('/admin/gate-config').then((r) => r.data),
  save: (cfg: GateConfig) => http.put<GateConfig>('/admin/gate-config', cfg).then((r) => r.data),
}
