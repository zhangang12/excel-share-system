<script setup lang="ts">
// 🆕 用户反馈小助手：右下角悬浮按钮 + 提交弹窗 +「我的反馈」(查看系统回信)
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatLineRound, Picture, Close } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import { userFeedbackApi, type UserFeedbackRow } from '@/api/userFeedback'
import { fmtRelative } from '@/utils/format'

const route = useRoute()

const visible = ref(false)
const activeTab = ref<'submit' | 'mine'>('submit')
const submitting = ref(false)
const form = ref({ kind: 'bug', content: '' })
const file = ref<File | null>(null)
const preview = ref<string>('')

const KIND_TXT: Record<string, string> = { bug: '问题反馈', suggest: '意见建议', other: '其它' }

function open() {
  activeTab.value = 'submit'
  form.value = { kind: 'bug', content: '' }
  file.value = null
  preview.value = ''
  visible.value = true
}

// ===== 我的反馈（查看处理意见回复）=====
const mineList = ref<UserFeedbackRow[]>([])
const mineLoading = ref(false)

async function loadMine() {
  mineLoading.value = true
  try {
    mineList.value = (await userFeedbackApi.list({ mine: true, page_size: 100 })).items
    // 打开即视为已读所有回复 → 消除登录右下角弹窗提醒
    try {
      await userFeedbackApi.markRepliesRead()
      window.dispatchEvent(new Event('pms:feedback-replies-read'))
    } catch { /* 静默 */ }
  } finally { mineLoading.value = false }
}
async function openMine() {
  activeTab.value = 'mine'
  visible.value = true
  await loadMine()
}
function onTabChange(name: any) { if (name === 'mine') loadMine() }

// 登录后右下角弹窗点击「查看」→ 打开「我的反馈」
onMounted(() => window.addEventListener('pms:open-my-feedback', openMine))
onUnmounted(() => window.removeEventListener('pms:open-my-feedback', openMine))

function onFileChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) bindFile(f)
}

function bindFile(f: File) {
  if (!/^image\//.test(f.type)) { ElMessage.warning('请选择图片文件'); return }
  if (f.size > 4 * 1024 * 1024) { ElMessage.warning('截图不能超过 4MB'); return }
  file.value = f
  const r = new FileReader()
  r.onload = () => (preview.value = String(r.result || ''))
  r.readAsDataURL(f)
}

// 支持 Ctrl/Cmd+V 粘贴截图（最常用的 bug 反馈方式）
function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items || []
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      const f = it.getAsFile()
      if (f) { bindFile(f); e.preventDefault(); break }
    }
  }
}

function clearShot() { file.value = null; preview.value = '' }

async function doSubmit() {
  const c = form.value.content.trim()
  if (!c) { ElMessage.warning('请填写问题/建议描述'); return }
  if (c.length > 2000) { ElMessage.warning('描述请控制在 2000 字以内'); return }
  submitting.value = true
  try {
    await userFeedbackApi.submit(form.value.kind, c, route.fullPath || '', file.value)
    ElMessage.success('已提交，感谢反馈！管理员处理后会在「我的反馈」回复你')
    visible.value = false
  } finally { submitting.value = false }
}

const kindOptions = [
  { value: 'bug', label: '问题反馈' },
  { value: 'suggest', label: '意见建议' },
  { value: 'other', label: '其它' },
]

const dragOver = ref(false)
function onDrop(e: DragEvent) {
  dragOver.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) bindFile(f)
}

const tip = computed(() => `当前页面：${route.fullPath || '/'}（提交时会一并记录，便于复现）`)
</script>

<template>
  <!-- 右下角悬浮按钮 -->
  <button class="helper-fab" :title="'问题反馈/意见建议·我的反馈'" @click="open">
    <el-icon class="ico"><ChatLineRound /></el-icon>
    <span class="lbl">反馈</span>
  </button>

  <el-dialog v-model="visible" title="💬 反馈小助手" width="560px"
             :close-on-click-modal="false" append-to-body class="v3-scroll-dialog">
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- 提交反馈 -->
      <el-tab-pane label="提交反馈" name="submit">
        <el-form label-position="top" @paste="onPaste">
          <el-form-item label="类型">
            <el-radio-group v-model="form.kind">
              <el-radio-button v-for="k in kindOptions" :key="k.value" :value="k.value">{{ k.label }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="描述（可粘贴截图 Ctrl/⌘+V）" required>
            <el-input v-model="form.content" type="textarea" :rows="5"
                      placeholder="请描述遇到的问题或建议，越详细越好（可粘贴截图）" maxlength="2000" show-word-limit />
          </el-form-item>
          <el-form-item label="截图（可选）">
            <div v-if="preview" class="shot-box">
              <img :src="preview" alt="截图预览" />
              <el-button type="danger" link :icon="Close" class="rm" @click="clearShot">移除</el-button>
            </div>
            <label v-else class="drop"
                   :class="{ over: dragOver }"
                   @dragover.prevent="dragOver = true"
                   @dragleave.prevent="dragOver = false"
                   @drop.prevent="onDrop">
              <el-icon><Picture /></el-icon>
              <span>点击选择 · 拖拽 · 或 Ctrl/⌘+V 粘贴截图（≤4MB）</span>
              <input type="file" accept="image/*" hidden @change="onFileChange" />
            </label>
          </el-form-item>
          <div class="ctx-tip">{{ tip }}</div>
        </el-form>
      </el-tab-pane>

      <!-- 我的反馈：查看处理意见回复 -->
      <el-tab-pane label="我的反馈" name="mine">
        <div v-loading="mineLoading" class="mine-wrap">
          <div v-if="!mineLoading && !mineList.length" class="mine-empty">你还没有提交过反馈</div>
          <div v-for="fb in mineList" :key="fb.id" class="fb-card">
            <div class="fb-head">
              <span class="fb-kind" :class="'k-' + fb.kind">{{ KIND_TXT[fb.kind] || fb.kind }}</span>
              <span class="fb-status" :class="fb.status">{{ fb.status === 'done' ? '已处理' : '待处理' }}</span>
              <span class="fb-time">{{ fmtRelative(fb.created_at) }}</span>
            </div>
            <div class="fb-content">{{ fb.content }}</div>
            <div v-if="fb.reply" class="fb-reply">
              <div class="fb-reply-h">📩 处理意见回复{{ fb.replier_name ? ' · ' + fb.replier_name : '' }}</div>
              <div class="fb-reply-c">{{ fb.reply }}</div>
            </div>
            <div v-else class="fb-noreply">管理员暂未回复</div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <template v-if="activeTab === 'submit'">
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="doSubmit">提交反馈</el-button>
      </template>
      <template v-else>
        <el-button @click="visible = false">关闭</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<style scoped>
.helper-fab {
  position: fixed; right: 22px; bottom: 28px; z-index: 2000;
  display: flex; align-items: center; gap: 6px;
  background: var(--primary, #2563eb); color: #fff;
  border: none; border-radius: 999px; padding: 11px 18px;
  box-shadow: 0 6px 20px rgba(37, 99, 235, .35), 0 2px 6px rgba(0,0,0,.08);
  cursor: pointer; font-size: 13.5px; font-weight: 500;
  transition: transform .15s, box-shadow .15s;
}
.helper-fab:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(37, 99, 235, .45); }
.helper-fab:active { transform: translateY(0); }
.helper-fab .ico { font-size: 18px; }
.helper-fab .lbl { line-height: 1; }
@media (max-width: 640px) { .helper-fab { padding: 10px 14px; } .helper-fab .lbl { display: none; } }

.drop {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; height: 92px; width: 100%;
  border: 1.5px dashed var(--border, #e5e7eb); border-radius: 8px;
  color: var(--text-3, #9ca3af); cursor: pointer; transition: all .15s;
  background: var(--bg-page, #f3f5f9);
}
.drop:hover, .drop.over { border-color: var(--primary, #2563eb); color: var(--primary, #2563eb); background: var(--primary-light, #eff6ff); }
.drop .el-icon { font-size: 22px; }

.shot-box { position: relative; display: inline-block; max-width: 100%; }
.shot-box img { max-width: 100%; max-height: 220px; border: 1px solid var(--border); border-radius: 6px; display: block; }
.shot-box .rm { position: absolute; top: 6px; right: 8px; background: rgba(255,255,255,.85); padding: 2px 8px !important; border-radius: 4px; }

.ctx-tip { font-size: 12px; color: var(--text-3, #9ca3af); margin-top: -6px; }

/* 我的反馈 */
.mine-wrap { max-height: 56vh; overflow-y: auto; min-height: 120px; }
.mine-empty { text-align: center; color: var(--text-3, #9ca3af); padding: 40px 0; font-size: 13px; }
.fb-card { border: 1px solid var(--border, #e5e7eb); border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; background: #fff; }
.fb-head { display: flex; align-items: center; gap: 8px; font-size: 12.5px; margin-bottom: 8px; }
.fb-kind { padding: 1px 8px; border-radius: 10px; font-weight: 600; }
.fb-kind.k-bug { background: #fee2e2; color: #991b1b; }
.fb-kind.k-suggest { background: #dbeafe; color: #1e40af; }
.fb-kind.k-other { background: #f3f4f6; color: #4b5563; }
.fb-status { padding: 1px 8px; border-radius: 10px; background: #fef3c7; color: #92400e; }
.fb-status.done { background: #d1fae5; color: #065f46; }
.fb-time { color: var(--text-3, #9ca3af); margin-left: auto; }
.fb-content { font-size: 13.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; color: #1f2937; }
.fb-reply { margin-top: 10px; border-left: 3px solid #10b981; background: #ecfdf5; border-radius: 0 8px 8px 0; padding: 8px 12px; }
.fb-reply-h { font-size: 12.5px; font-weight: 600; color: #065f46; margin-bottom: 4px; }
.fb-reply-c { font-size: 13.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; color: #064e3b; }
.fb-noreply { margin-top: 8px; font-size: 12.5px; color: var(--text-3, #9ca3af); }
</style>
