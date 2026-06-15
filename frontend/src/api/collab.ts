import { http } from './index'

// 🆕 v3 M12 部门协作：工作流聚合 / 装配前置三表 / 数据表完成标记
export interface WfDept {
  dept: string
  name: string
  status: string
  worker_name?: string | null
  due_date?: string | null
  done_date?: string | null
  eff_pct?: number | null
}

export interface Workflow {
  project_id: number
  code: string
  name: string
  status: string
  sales_name?: string | null
  sign_date?: string | null
  deliver_date?: string | null
  depts: WfDept[]
  sheetpkg_count: number
  purchase_list_count: number
  ship_list_count: number
  ship_status: string
  can_ship: boolean
  gate_missing: string[]
}

export interface SheetStatusRow {
  project_id: number
  code: string
  name: string
  sheets: Record<string, boolean>
}

export const collabApi = {
  workflow: (pid: number) => http.get<Workflow>(`/projects/${pid}/workflow`).then((r) => r.data),
  assemblySheetStatus: () => http.get<SheetStatusRow[]>('/assembly/sheet-status').then((r) => r.data),
  setDoneFlag: (did: number, done: boolean) =>
    http.put(`/datasheets/${did}/done-flag`, { done }).then((r) => r.data),
}

export const ASSEMBLY_SHEETS = ['钣金装配', '标准件清单', '外协外购']
