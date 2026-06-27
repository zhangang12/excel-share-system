<script setup lang="ts">
// 🆕 统一附件预览：图片 / PDF 内嵌；xls·xlsx 渲成表格(SheetJS)；docx 渲成文档(docx-preview)；
//    doc(旧版二进制)·dwg(CAD) 无法网页渲染 → 下载。库按需动态导入，不拖累首屏。
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
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
const curAtt = ref<Att | null>(null)   // 🆕 当前预览的附件，供弹窗「下载」按钮使用

function cleanup() {
  if (curBlobUrl) { URL.revokeObjectURL(curBlobUrl); curBlobUrl = '' }
  imgUrl.value = ''; pdfUrl.value = ''; xlsxHtml.value = ''
  xlsxSheets.value = []; xlsxActive.value = ''; xlsxWb = null
  if (docxHost.value) docxHost.value.innerHTML = ''
}

async function open(att: Att) {
  title.value = att.name
  curAtt.value = att
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
      // cellDates/cellNF：让日期格式单元格变 Date 并带上数字格式(.z)，便于识别并修复「常规」格式的日期序列号
      xlsxWb = XLSX.read(buf, { type: 'array', cellDates: true, cellNF: true })
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

// 🆕 修复在线预览日期列：源表「发出/到货日期」等列若以「常规(General)」格式存储，
// sheet_to_html 会原样输出 Excel 日期序列号(如 46174)。这里把日期列里的序列号补成 yyyy-mm-dd 再渲染，
// 已是日期型(t==='d')的单元格 SheetJS 已带 .w，无需处理；不动单元格结构，故合并单元格(公司抬头行)仍正常。
function fixDateCells(XLSX: any, ws: any) {
  if (!ws || !ws['!ref']) return
  const range = XLSX.utils.decode_range(ws['!ref'])
  const enc = XLSX.utils.encode_cell
  const isDateFmt = (z: any) => !!(z && XLSX.SSF && XLSX.SSF.is_date && XLSX.SSF.is_date(z))
  // 1) 识别「日期列」：列中存在真正日期单元格(t==='d')、日期数字格式(.z)、或表头含「日期/date」
  const dateCols = new Set<number>()
  for (let C = range.s.c; C <= range.e.c; C++) {
    for (let R = range.s.r; R <= range.e.r; R++) {
      const c = ws[enc({ r: R, c: C })]
      if (!c) continue
      if (c.t === 'd' || isDateFmt(c.z) ||
          (c.t === 's' && typeof c.v === 'string' && /日期|date/i.test(c.v))) {
        dateCols.add(C); break
      }
    }
  }
  if (!dateCols.size) return
  // 2) 把日期列里仍是「序列号数字」(合理日期序列号区间)的单元格补成 yyyy-mm-dd 显示
  for (let R = range.s.r; R <= range.e.r; R++) {
    for (const C of dateCols) {
      const c = ws[enc({ r: R, c: C })]
      if (!c) continue
      if (c.t === 'n' && typeof c.v === 'number' && c.v > 20000 && c.v < 90000) {
        try { c.w = XLSX.SSF.format('yyyy-mm-dd', c.v) } catch { /* 保底不动 */ }
      }
    }
  }
}

async function selectSheet(name: string) {
  if (!xlsxWb || !name) return
  const XLSX = await import('xlsx')
  xlsxActive.value = name
  const ws = xlsxWb.Sheets[name]
  fixDateCells(XLSX, ws)
  xlsxHtml.value = XLSX.utils.sheet_to_html(ws)
}

function onClose() { cleanup(); mode.value = '' }
function doDownload() { if (curAtt.value) downloadAttachment(curAtt.value) }
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
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :icon="Download" @click="doDownload">下载</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.xlsx-host { overflow: auto; max-height: 80vh; }
.xlsx-host :deep(table) { border-collapse: collapse; font-size: 13px; }
.xlsx-host :deep(td), .xlsx-host :deep(th) { border: 1px solid #dcdfe6; padding: 4px 9px; white-space: nowrap; }
.docx-host { overflow: auto; max-height: 82vh; background: #f3f5f9; padding: 10px; }
</style>
