import { http } from './index'

export interface FieldPermItem {
  role_id: number
  role_name?: string
  can_view: boolean
  can_edit: boolean
}

export interface PermMatrixCell {
  role_name: string
  can_view: boolean
  can_edit: boolean
  customized: boolean
}
export interface MatrixField {
  field_id: number
  field_name: string
  field_type: string
  perms: Record<string, PermMatrixCell>
}
export interface MatrixDatasheet {
  datasheet_id: number
  datasheet_name: string
  project_id: number
  project_code: string
  project_name: string
  fields: MatrixField[]
}
export interface PermMatrix {
  roles: Array<{ code: string; name: string }>
  overview: MatrixField[]
  datasheets: MatrixDatasheet[]
}

// 🆕 异步分片加载
export type PermRole = { code: string; name: string }
export interface OverviewMatrix {
  roles: PermRole[]
  overview: MatrixField[]
}
export interface DatasheetProject {
  project_id: number
  project_code: string
  project_name: string
  datasheet_count: number
}
export interface DatasheetMatrix {
  roles: PermRole[]
  datasheets: MatrixDatasheet[]
}

export interface ClonePermsResult {
  cloned_field_count: number
  matched_datasheets: string[]
  unmatched_target_datasheets: string[]
  skipped_target_fields: string[]
  message: string
}

export const permApi = {
  getMatrix: () => http.get<PermMatrix>('/permissions/matrix').then(r => r.data),
  // 🆕 异步分片：概览矩阵(轻量) / 有数据表的项目列表 / 单项目数据表矩阵(按需)
  getOverviewMatrix: () => http.get<OverviewMatrix>('/permissions/matrix/overview').then(r => r.data),
  getDatasheetProjects: () => http.get<DatasheetProject[]>('/permissions/matrix/datasheet-projects').then(r => r.data),
  getDatasheetMatrix: (projectId: number) =>
    http.get<DatasheetMatrix>('/permissions/matrix/datasheets', { params: { project_id: projectId } }).then(r => r.data),
  // 数据表字段
  listFieldPerms: (fid: number) =>
    http.get<FieldPermItem[]>(`/permissions/fields/${fid}`).then(r => r.data),
  setFieldPerms: (fid: number, permissions: FieldPermItem[]) =>
    http.put<FieldPermItem[]>(`/permissions/fields/${fid}`, { permissions }).then(r => r.data),

  // 一览字段
  listOverviewPerms: (fid: number) =>
    http.get<FieldPermItem[]>(`/permissions/overview-fields/${fid}`).then(r => r.data),
  setOverviewPerms: (fid: number, permissions: FieldPermItem[]) =>
    http.put<FieldPermItem[]>(`/permissions/overview-fields/${fid}`, { permissions }).then(r => r.data),

  // 我能看/能改哪些字段（数据表）
  myDatasheetPerms: (did: number) =>
    http.get<Record<string, { can_view: boolean; can_edit: boolean }>>(`/permissions/me/datasheet/${did}`).then(r => r.data),
  myOverviewPerms: () =>
    http.get<Record<string, { can_view: boolean; can_edit: boolean }>>('/permissions/me/overview').then(r => r.data),

  // 项目权限克隆（仅 manager）
  cloneProjectPerms: (targetPid: number, sourcePid: number) =>
    http.post<ClonePermsResult>(`/permissions/clone-project/${targetPid}`,
      { source_project_id: sourcePid }).then(r => r.data),
}
