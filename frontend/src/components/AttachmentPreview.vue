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
  <el-dialog v-model="visible" width="88vw" append-to-body destroy-on-close
             class="att-preview-dialog" @close="onClose">
    <!-- 🆕 反馈 2026-08-07（周瑞第二次报）：下载按钮**在屏幕外**。
         原来 top=4vh + iframe 写死 82vh + footer，1366×768 上实测弹窗底部
         到 803px（视口 768），按钮被切掉且弹窗整体溢出、滚不到——
         人能看见 PDF，就是够不着下载。上次只 grep 确认按钮存在，没量位置，所以漏了。
         现在：① 弹窗高度受控，body 弹性填充；② 标题栏也放一个下载按钮，
         无论布局再出什么意外，顶部这个永远够得着。 -->
    <template #header>
      <div class="att-head">
        <span class="att-title" :title="title">{{ title }}</span>
        <el-button type="primary" :icon="Download" size="small" @click="doDownload">下载</el-button>
      </div>
    </template>
    <div v-loading="loading" class="att-body">
      <div v-if="mode === 'image'" style="text-align:center">
        <img v-if="imgUrl" :src="imgUrl" :alt="title" style="max-width:100%;max-height:100%" />
      </div>
      <!-- 反馈#345（周瑞）：「技术文档打开后，无法下载」。
           PDF 是用 blob URL 内嵌的，Chrome 内置阅读器工具栏上那个下载按钮
           下的是 blob，文件名会变成一串 UUID，桌面客户端里干脆没反应。
           `#toolbar=0` 把内置工具栏隐掉，只留我们自己那个能正确命名的下载。 -->
      <iframe v-else-if="mode === 'pdf'" :src="pdfUrl + '#toolbar=0&navpanes=0'"
              style="width:100%;height:100%;border:none"></iframe>
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
      <span class="dl-tip">要保存到本地就点右边「下载」</span>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" size="large" :icon="Download" @click="doDownload">
        下载{{ curAtt?.name ? `「${curAtt.name}」` : '' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<!-- ⚠️ 这段不能加 scoped：dialog 是 append-to-body 的，
     scoped 的属性选择器不会挂到 body 下的那个节点上，样式压根不生效。
     靠 .att-preview-dialog 这个类名限定作用域。 -->
<style>
.att-preview-dialog {
  /* 整个弹窗高度受控，footer 永远在视口内 */
  display: flex; flex-direction: column;
  max-height: 92vh; margin-top: 4vh !important; margin-bottom: 4vh !important;
}
.att-preview-dialog .el-dialog__body {
  /* flex-basis 直接给足高度：只写 flex:1 的话，内容（iframe height:100%）
     撑不起父级，弹窗会缩成一小条——预览区被压扁，等于没法看。
     92vh 减去 标题栏+footer 的大致高度(约 130px)。 */
  flex: 1 1 calc(92vh - 130px);
  min-height: 0;               /* min-height:0 不能省，否则 flex 子项不会收缩 */
  overflow: auto; padding-top: 10px;
}
.att-preview-dialog .el-dialog__footer { flex: none; }
.att-preview-dialog .att-body { height: 100%; min-height: 200px; }
.att-preview-dialog .att-head {
  display: flex; align-items: center; gap: 12px; padding-right: 28px;
}
.att-preview-dialog .att-title {
  flex: 1; min-width: 0; font-size: 16px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>

<style scoped>
.dl-tip { float: left; line-height: 40px; font-size: 12.5px; color: var(--el-text-color-secondary); }
.xlsx-host { overflow: auto; max-height: 80vh; }
.xlsx-host :deep(table) { border-collapse: collapse; font-size: 13px; }
.xlsx-host :deep(td), .xlsx-host :deep(th) { border: 1px solid #dcdfe6; padding: 4px 9px; white-space: nowrap; }
.docx-host { overflow: auto; max-height: 82vh; background: #f3f5f9; padding: 10px; }
</style>
