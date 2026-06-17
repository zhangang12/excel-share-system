export interface Role {
  id: number; code: string; name: string; description?: string | null
}

export interface User {
  id: number; username: string
  full_name?: string | null; email?: string | null
  // 锚点角色（兼容旧字段）
  role_id: number; role_code?: string | null; role_name?: string | null
  // 🆕 全部角色（平等多角色）
  role_ids?: number[]; role_codes?: string[]; role_names?: string[]
  is_active: boolean; password_must_change: boolean
  wxid?: string | null
  created_at: string; last_login?: string | null
}

export interface LoginResp { access_token: string; token_type: string; user: User }

export interface Project {
  id: number; code: string; name: string
  description?: string | null
  status: string
  manager_id?: number | null
  manager_name?: string | null
  member_count: number
  created_at: string; updated_at: string
  // 项目头表数据：{数量, 销售, 设计师, 电器, 下单日期, 交货日期, 制表日期, ...}
  header_meta?: Record<string, string>
  // 一览字段数据（项目头表「镜像一览」时读这里，与一览同源）：
  // {签订日期, 交货日期, 销售, 设计师, 制图开始, 制图结束, 制图用时, 电工, ...}
  overview_meta?: Record<string, string>
}

export interface ProjectMember {
  id: number; user_id: number; username: string
  full_name?: string | null
  role_name?: string | null
  permission: 'edit' | 'view'
  added_at: string
}

export type FieldType = 'text' | 'number' | 'date' | 'select' | 'multi_select' | 'person'

export interface Datasheet {
  id: number; project_id: number; name: string
  sort_order: number
  field_count: number; record_count: number
  header_lines?: string[][] | null
  imported?: boolean      // 🆕 v3 四表校验：是否已导入 Excel
  done_flag?: boolean     // 🆕 v3 装配前置完成标记
  created_at: string; updated_at: string
}

export interface FieldConfig {
  options?: string[]  // for select / multi_select
}

export interface DataField {
  id: number; datasheet_id: number
  name: string; type: FieldType
  sort_order: number
  config?: FieldConfig | null
  created_at: string
}

export interface DataRecord {
  id: number; datasheet_id: number
  sort_order: number
  values: Record<string, unknown>
  created_at: string; updated_at: string
}

export interface OverviewField {
  id: number
  name: string
  type: FieldType
  sort_order: number
  config?: FieldConfig | null
}

export interface OverviewRow {
  id: number
  code: string
  name: string
  status: string
  description?: string | null
  manager_id?: number | null
  manager_name?: string | null
  extra: Record<string, unknown>
  updated_at: string
}

export interface OverviewBundle {
  fields: OverviewField[]
  rows: OverviewRow[]
}

export interface AuditLog {
  id: number
  user_id?: number | null
  username?: string | null
  action: string
  target_type?: string | null
  target_id?: number | null
  detail?: string | null
  ip?: string | null
  created_at: string
}
