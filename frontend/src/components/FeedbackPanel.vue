<script setup lang="ts">
// 🆕 v3 M13 问题反馈面板（按角色显示：装配提交 / 生产主管审批 / 设计师接收驳回）
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Check } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { feedbackApi, FB_STATUS_TXT, FB_STATUS_TAG, type Feedback } from '@/api/feedback'
import { http } from '@/api'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'

const auth = useAuthStore()
// 多角色：按并集判断（任一角色命中即显示对应能力）
const isAssembler = computed(() => auth.hasRole('assembler'))
// 🆕 反馈#227/#228：装配反馈直达设计,取消生产主管审批环节——不再显示"问题反馈审批"面板
const isDesigner = computed(() => auth.hasRole('designer'))

const list = ref<Feedback[]>([])
const loading = ref(false)
async function load() {
  loading.value = true
  try { list.value = await feedbackApi.mine() }
  finally { loading.value = false }
}
onMounted(load)

const title = computed(() => {
  if (isAssembler.value) return '📝 我的问题反馈'
  if (isDesigner.value) return '📥 待接收的问题反馈'
  return '问题反馈'
})

// 装配提交
const submitVisible = ref(false)
const form = reactive({ project_id: undefined as number | undefined, content: '' })
const projOptions = ref<{ id: number; code: string; name: string }[]>([])
const fbImages = ref<File[]>([])   // 🆕 #193 现场照片(选填,多张)
async function openSubmit() {
  projOptions.value = await feedbackApi.myProjects()
  form.project_id = undefined; form.content = ''; fbImages.value = []
  submitVisible.value = true
}
function pickImages() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = 'image/*'; input.multiple = true
  input.onchange = () => { fbImages.value = [...fbImages.value, ...Array.from(input.files || [])] }
  input.click()
}
function removeImage(i: number) { fbImages.value.splice(i, 1) }
// 查看附图：带鉴权取 blob 后新标签打开
async function viewImage(img: { id: number; name: string }) {
  try {
    const r = await http.get(`/attachments/${img.id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data as Blob)
    window.open(url, '_blank')
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  } catch { ElMessage.error('图片打开失败') }
}
const submitting = ref(false)
async function submit() {
  if (!form.project_id) { ElMessage.warning('请选择项目'); return }
  if (!form.content.trim()) { ElMessage.warning('请填写问题内容'); return }
  submitting.value = true
  try {
    await feedbackApi.create(form.project_id, form.content, fbImages.value)
    ElMessage.success('已提交，等待生产主管审批')
    submitVisible.value = false
    await load()
  } finally { submitting.value = false }
}

const actingId = ref<number | null>(null)
async function act(fb: Feedback, fn: 'designAccept' | 'designReject') {
  if (fn === 'designReject') {
    try {
      await ElMessageBox.confirm('确认驳回该问题反馈？提交人将收到驳回通知。', '驳回反馈', { type: 'warning' })
    } catch { return }
  }
  actingId.value = fb.id
  try {
    const r: any = await feedbackApi[fn](fb.id)
    ElMessage.success(r.message || '操作成功')
    await load()
  } finally { actingId.value = null }
}
</script>

<template>
  <el-card v-if="isAssembler || isDesigner" shadow="never" class="fb-card">
    <template #header>
      <div class="fb-head">
        <span>{{ title }} <el-tag v-if="list.length" size="small" type="warning">{{ list.length }}</el-tag></span>
        <el-button v-if="isAssembler" size="small" type="primary" :icon="Plus" @click="openSubmit">提交反馈</el-button>
      </div>
    </template>

    <EmptyHint v-if="!loading && !list.length"
              :text="isAssembler ? '暂无反馈，可对在手项目提交问题' : '暂无待处理反馈'" />
    <el-table v-else :data="list" v-loading="loading" size="small" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
      <el-table-column label="项目" width="110"><template #default="{ row }"><b class="code">{{ row.code }}</b></template></el-table-column>
      <el-table-column prop="content" label="问题内容" min-width="220" show-overflow-tooltip />
      <el-table-column label="附图" width="120">
        <template #default="{ row }">
          <template v-if="row.images?.length">
            <el-tag v-for="(img, i) in row.images" :key="img.id" size="small" effect="plain"
                    style="cursor:pointer;margin-right:4px" @click="viewImage(img)">📷{{ i + 1 }}</el-tag>
          </template>
          <span v-else class="muted small">—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="!isAssembler" label="提交人" width="90"><template #default="{ row }">{{ row.created_by_name || '—' }}</template></el-table-column>
      <el-table-column label="状态" width="100" align="center">
        <template #default="{ row }"><StatusPill :text="FB_STATUS_TXT[row.status]" :variant="FB_STATUS_TAG[row.status]" /></template>
      </el-table-column>
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <template v-if="isDesigner && row.status === 'pending_design'">
            <el-button size="small" type="success" :icon="Check" :loading="actingId === row.id" @click="act(row, 'designAccept')">接收存档</el-button>
            <el-button size="small" :loading="actingId === row.id" @click="act(row, 'designReject')">驳回</el-button>
          </template>
          <span v-else class="muted small">—</span>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="submitVisible" title="📝 提交问题反馈" width="480px">
      <el-form label-position="top">
        <el-form-item label="项目（在手）" required>
          <el-select v-model="form.project_id" filterable placeholder="选择在手项目" style="width: 100%">
            <el-option v-for="p in projOptions" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="问题内容" required>
          <el-input v-model="form.content" type="textarea" :rows="3" placeholder="描述装配中发现的问题，提交后经生产主管审批回馈设计师" />
        </el-form-item>
        <!-- 🆕 #193 现场照片(选填,多张) -->
        <el-form-item label="现场照片（选填，可多张）">
          <el-button size="small" @click="pickImages">📷 选择图片</el-button>
          <div v-if="fbImages.length" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px">
            <el-tag v-for="(f, i) in fbImages" :key="i" size="small" closable @close="removeImage(i)">{{ f.name }}</el-tag>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="submitVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">提交</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.fb-card { margin-top: 14px; }
.fb-head { display: flex; align-items: center; justify-content: space-between; }
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
</style>
