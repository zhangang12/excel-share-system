import { ElMessage } from 'element-plus'
import { http } from './index'

// 🆕 通用附件打包下载：传一组附件 ID → 后端打 zip → 浏览器下载
export async function packageAttachments(ids: number[], zipname: string) {
  if (!ids.length) { ElMessage.info('请至少勾选一项'); return }
  try {
    const res = await http.post('/attachments/package',
      { attachment_ids: ids, zipname }, { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url; a.download = `${zipname}.zip`
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success('已打包下载')
  } catch {
    ElMessage.error('打包下载失败')
  }
}

const IMG_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']
// 可在网页内渲染预览的格式：图片 / PDF / Excel(xls,xlsx) / Word(docx)
// 注：旧版 .doc(二进制) 与 .dwg(CAD) 无法在浏览器内渲染，只能下载查看
const PREVIEW_EXTS = [...IMG_EXTS, 'pdf', 'xlsx', 'xls', 'docx']
export function attExt(name: string): string {
  return (name.split('.').pop() || '').toLowerCase()
}
export function isImageAtt(name: string): boolean { return IMG_EXTS.includes(attExt(name)) }
export function isPdfAtt(name: string): boolean { return attExt(name) === 'pdf' }
export function canInlinePreview(name: string): boolean { return PREVIEW_EXTS.includes(attExt(name)) }

/** 取附件 blob 的临时 URL（调用方负责 revoke）。 */
export async function attachmentBlobUrl(id: number): Promise<string> {
  const r = await http.get(`/attachments/${id}/download`, { responseType: 'blob' })
  return URL.createObjectURL(r.data as Blob)
}

/** 取附件原始 Blob（用于 SheetJS / docx-preview 解析）。 */
export async function attachmentBlob(id: number): Promise<Blob> {
  const r = await http.get(`/attachments/${id}/download`, { responseType: 'blob' })
  return r.data as Blob
}
