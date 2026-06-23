<script setup lang="ts">
// 🆕 通用「资料预览 + 打包下载」抽屉：任务跟踪/物流/仓库等处复用
import { ref, computed, watch } from 'vue'
import { Download, View } from '@element-plus/icons-vue'
import { packageAttachments, canInlinePreview, isImageAtt, isPdfAtt, attachmentBlobUrl } from '@/api/attachments'
import { downloadAttachment } from '@/api/orders'
import { ElMessage } from 'element-plus'

interface Item { id: number; name: string }
interface Group { label: string; items: Item[] }

const props = defineProps<{
  modelValue: boolean
  title?: string
  zipname?: string
  groups: Group[]
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const allItems = computed(() => props.groups.flatMap(g => g.items))
const sel = ref<number[]>([])
watch(() => props.modelValue, (v) => { if (v) sel.value = allItems.value.map(i => i.id) })

function toggleAll(v: any) { sel.value = v ? allItems.value.map(i => i.id) : [] }

async function doPack() {
  if (!sel.value.length) { ElMessage.info('请至少勾选一项'); return }
  await packageAttachments(sel.value, props.zipname || '资料打包')
}

// 内联预览：图片 → 弹窗看；pdf → 新标签打开；其它 → 直接下载
const imgVisible = ref(false)
const imgSrc = ref('')
const imgName = ref('')
async function preview(it: Item) {
  if (isImageAtt(it.name)) {
    if (imgSrc.value) URL.revokeObjectURL(imgSrc.value)
    imgSrc.value = await attachmentBlobUrl(it.id)
    imgName.value = it.name
    imgVisible.value = true
  } else if (isPdfAtt(it.name)) {
    const url = await attachmentBlobUrl(it.id)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } else {
    downloadAttachment(it)  // 非图片/PDF 无法网页预览，直接下载
  }
}
function closeImg() { if (imgSrc.value) URL.revokeObjectURL(imgSrc.value); imgSrc.value = '' }
</script>

<template>
  <el-drawer v-model="visible" :title="title || '资料预览 / 打包下载'" direction="rtl" size="440px" destroy-on-close>
    <template v-if="allItems.length">
      <div class="apd-tip">勾选需要的资料，点「打包下载」打成 zip；可点「预览」在线查看（图片/PDF）。</div>
      <div class="apd-head">
        <el-checkbox :model-value="sel.length === allItems.length"
                     :indeterminate="sel.length > 0 && sel.length < allItems.length"
                     @change="toggleAll">全选</el-checkbox>
        <span class="apd-count">已选 {{ sel.length }} / {{ allItems.length }}</span>
      </div>
      <el-checkbox-group v-model="sel">
        <div v-for="g in groups" :key="g.label" class="apd-group" v-show="g.items.length">
          <div class="apd-group-h">{{ g.label }}</div>
          <div v-for="it in g.items" :key="it.id" class="apd-item">
            <el-checkbox :value="it.id" class="apd-cb"><span class="apd-name">{{ it.name }}</span></el-checkbox>
            <span class="apd-actions">
              <el-button v-if="canInlinePreview(it.name)" size="small" link type="primary" :icon="View" @click="preview(it)">预览</el-button>
              <el-button size="small" link :icon="Download" @click="downloadAttachment(it)" />
            </span>
          </div>
        </div>
      </el-checkbox-group>
    </template>
    <div v-else class="apd-empty">暂无可下载的资料</div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :icon="Download" :disabled="!sel.length" @click="doPack">打包下载（{{ sel.length }}）</el-button>
    </template>

    <!-- 图片预览 -->
    <el-dialog v-model="imgVisible" :title="imgName" width="80%" top="6vh" append-to-body @close="closeImg">
      <div style="text-align:center"><img v-if="imgSrc" :src="imgSrc" :alt="imgName" style="max-width:100%;max-height:78vh" /></div>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.apd-tip { font-size: 12.5px; color: var(--el-text-color-secondary); margin-bottom: 12px; line-height: 1.6; }
.apd-head { display: flex; align-items: center; justify-content: space-between; padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--el-border-color-lighter); }
.apd-count { font-size: 12.5px; color: var(--el-text-color-secondary); }
.apd-group { margin-bottom: 14px; }
.apd-group-h { font-size: 12.5px; font-weight: 600; color: #0f172a; margin: 6px 0; }
.apd-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 3px 0; }
.apd-cb { flex: 1; min-width: 0; margin-right: 0; }
.apd-cb :deep(.el-checkbox__label) { white-space: normal; word-break: break-all; line-height: 1.5; }
.apd-actions { flex: none; white-space: nowrap; }
.apd-empty { text-align: center; color: var(--el-text-color-secondary); padding: 40px 0; }
</style>
