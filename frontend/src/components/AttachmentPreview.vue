<script setup lang="ts">
// 🆕 统一附件预览：图片 / PDF 内嵌；xls·xlsx 渲成表格(SheetJS)；docx 渲成文档(docx-preview)；
//    doc(旧版二进制)·dwg(CAD) 无法网页渲染 → 下载。库按需动态导入，不拖累首屏。
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { attExt, isImageAtt, isPdfAtt, attachmentBlob, attachmentBlobUrl } from '@/api/attachments'
import { downloadAttachment } from '@/api/orders'

interface Att { id: number; name: string }

const visible = ref(false)
const title = ref('')
const loading = ref(false)
const mode = ref<'' | 'image' | 'pdf' | 'xlsx' | 'docx'>('')
const imgUrl = ref('')
const pdfUrl = ref('')
const xlsxHtml = ref('')
const xlsxSheets = ref<string[]>([])
const xlsxActive = ref('')
let xlsxWb: any = null
const docxHost = ref<HTMLElement | null>(null)
let curBlobUrl = ''

function cleanup() {
  if (curBlobUrl) { URL.revokeObjectURL(curBlobUrl); curBlobUrl = '' }
  imgUrl.value = ''; pdfUrl.value = ''; xlsxHtml.value = ''
  xlsxSheets.value = []; xlsxActive.value = ''; xlsxWb = null
  if (docxHost.value) docxHost.value.innerHTML = ''
}

async function open(att: Att) {
  title.value = att.name
  cleanup()
  mode.value = ''
  const ext = attExt(att.name)

  if (isImageAtt(att.name)) {
    visible.value = true; loading.value = true; mode.value = 'image'
    try { curBlobUrl = await attachmentBlobUrl(att.id); imgUrl.value = curBlobUrl }
    catch { ElMessage.error('图片加载失败') } finally { loading.value = false }
    return
  }
  if (isPdfAtt(att.name)) {
    visible.value = true; loading.value = true; mode.value = 'pdf'
    try { curBlobUrl = await attachmentBlobUrl(att.id); pdfUrl.value = curBlobUrl }
    catch { ElMessage.error('PDF 加载失败') } finally { loading.value = false }
    return
  }
  if (ext === 'xlsx' || ext === 'xls') {
    visible.value = true; loading.value = true; mode.value = 'xlsx'
    try {
      const buf = await (await attachmentBlob(att.id)).arrayBuffer()
      const XLSX = await import('xlsx')
      xlsxWb = XLSX.read(buf, { type: 'array' })
      xlsxSheets.value = xlsxWb.SheetNames || []
      await selectSheet(xlsxSheets.value[0] || '')
    } catch { ElMessage.error('表格预览失败，请下载查看'); visible.value = false } finally { loading.value = false }
    return
  }
  if (ext === 'docx') {
    visible.value = true; loading.value = true; mode.value = 'docx'
    try {
      const blob = await attachmentBlob(att.id)
      const { renderAsync } = await import('docx-preview')
      await nextTick()
      if (docxHost.value) { docxHost.value.innerHTML = ''; await renderAsync(blob, docxHost.value) }
    } catch { ElMessage.error('文档预览失败，请下载查看'); visible.value = false } finally { loading.value = false }
    return
  }
  // doc(旧版) / dwg(CAD) / 其它：浏览器无法渲染 → 下载
  ElMessage.info('该格式暂不支持在线预览，已为你下载')
  downloadAttachment(att)
}

async function selectSheet(name: string) {
  if (!xlsxWb || !name) return
  const XLSX = await import('xlsx')
  xlsxActive.value = name
  xlsxHtml.value = XLSX.utils.sheet_to_html(xlsxWb.Sheets[name])
}

function onClose() { cleanup(); mode.value = '' }
defineExpose({ open })
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="88vw" top="4vh" append-to-body destroy-on-close @close="onClose">
    <div v-loading="loading" style="min-height:200px">
      <div v-if="mode === 'image'" style="text-align:center">
        <img v-if="imgUrl" :src="imgUrl" :alt="title" style="max-width:100%;max-height:82vh" />
      </div>
      <iframe v-else-if="mode === 'pdf'" :src="pdfUrl" style="width:100%;height:82vh;border:none"></iframe>
      <div v-else-if="mode === 'xlsx'">
        <div v-if="xlsxSheets.length > 1" style="margin-bottom:8px">
          <el-radio-group v-model="xlsxActive" size="small" @change="selectSheet">
            <el-radio-button v-for="s in xlsxSheets" :key="s" :value="s">{{ s }}</el-radio-button>
          </el-radio-group>
        </div>
        <div class="xlsx-host" v-html="xlsxHtml"></div>
      </div>
      <div v-else-if="mode === 'docx'" ref="docxHost" class="docx-host"></div>
    </div>
  </el-dialog>
</template>

<style scoped>
.xlsx-host { overflow: auto; max-height: 80vh; }
.xlsx-host :deep(table) { border-collapse: collapse; font-size: 13px; }
.xlsx-host :deep(td), .xlsx-host :deep(th) { border: 1px solid #dcdfe6; padding: 4px 9px; white-space: nowrap; }
.docx-host { overflow: auto; max-height: 82vh; background: #f3f5f9; padding: 10px; }
</style>
