import { http } from './index'

export interface QualityTask {
  task_id: number
  code: string
  name: string
  assembly_done_at: string | null
  quality_done_at: string | null
  quality_done_by: string | null
}

export const qualityApi = {
  list: () => http.get<QualityTask[]>('/quality/tasks').then(r => r.data),
  setDone: (taskId: number, done: boolean) =>
    http.post<{ message: string }>(`/quality/tasks/${taskId}/done`, { done }).then(r => r.data),
}
