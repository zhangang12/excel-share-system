import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from './index'

// 🆕 v3 M16 带导出审批闸的下载：403 时引导申请（开关关闭时后端直接放行，行为同旧）
export async function fetchExport(url: string, fallbackName: string, scope: string) {
  const token = localStorage.getItem('pms_token') || ''
  const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
  if (res.status === 403) {
    const ok = await ElMessageBox.confirm(
      '导出数据需管理层审批。是否提交导出申请？批准后即可导出。', '需要导出审批',
      { confirmButtonText: '提交申请', cancelButtonText: '取消', type: 'warning' },
    ).catch(() => false)
    if (ok) {
      try {
        await http.post('/export-requests', { scope })
        ElMessage.success('导出申请已提交，等待管理层审批')
      } catch { /* 重复申请等由拦截器提示 */ }
    }
    return
  }
  if (!res.ok) { ElMessage.error('导出失败'); return }
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8'')?([^;\n]+)/i)
  const fname = m ? decodeURIComponent(m[1].replace(/"/g, '')) : fallbackName
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = fname
  document.body.appendChild(a); a.click(); a.remove()
  setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}
