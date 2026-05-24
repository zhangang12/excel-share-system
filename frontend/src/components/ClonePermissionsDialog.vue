<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Warning } from '@element-plus/icons-vue'
import { projectsApi } from '@/api/projects'
import { permApi, type ClonePermsResult } from '@/api/permissions'
import type { Project } from '@/types'

const props = defineProps<{
  modelValue: boolean
  targetProjectId: number
  targetProjectName: string
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  'cloned': []
}>()

const loading = ref(false)
const submitting = ref(false)
const projects = ref<Project[]>([])
const sourceId = ref<number | null>(null)
const keyword = ref('')
const result = ref<ClonePermsResult | null>(null)

const selectableProjects = computed(() =>
  projects.value.filter(p => p.id !== props.targetProjectId)
)

async function loadProjects() {
  loading.value = true
  try {
    projects.value = await projectsApi.list()
  } finally { loading.value = false }
}

watch(() => props.modelValue, (v) => {
  if (v) {
    sourceId.value = null
    keyword.value = ''
    result.value = null
    loadProjects()
  }
})

async function submit() {
  if (!sourceId.value) { ElMessage.warning('请选择源项目'); return }
  const src = projects.value.find(p => p.id === sourceId.value)
  if (!src) return

  const ok = await ElMessageBox.confirm(
    `确定从「${src.code} · ${src.name}」克隆字段权限到「${props.targetProjectName}」吗？\n\n` +
    `• 仅覆盖目标项目"数据表名 + 字段名"都能匹配上的字段权限\n` +
    `• 不会修改源项目，不会增删字段、行、成员等数据\n` +
    `• 未匹配的字段权限保持原样不变`,
    '权限克隆确认',
    { type: 'warning', confirmButtonText: '克隆', cancelButtonText: '取消' }
  ).catch(() => false)
  if (!ok) return

  submitting.value = true
  try {
    result.value = await permApi.cloneProjectPerms(props.targetProjectId, sourceId.value)
    if (result.value.cloned_field_count > 0) {
      ElMessage.success(result.value.message)
    } else {
      ElMessage.warning(result.value.message)
    }
    emit('cloned')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || e.message || '克隆失败')
  } finally {
    submitting.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)"
             title="克隆其他项目的字段权限" width="560px" :close-on-click-modal="false">
    <div v-loading="loading">
      <div class="hint">
        <el-icon style="vertical-align: -2px"><Warning /></el-icon>
        权限克隆将以"<b>数据表名 + 字段名</b>"为匹配键，把源项目的字段级权限配置覆盖到当前项目的对应字段。
        <br />存量数据安全：不会增删字段/数据行/成员，仅覆盖匹配字段的权限记录。
      </div>

      <el-form label-position="top" v-if="!result">
        <el-form-item label="目标项目（当前项目）">
          <el-input :model-value="targetProjectName" disabled size="large" />
        </el-form-item>
        <el-form-item label="选择源项目">
          <el-select v-model="sourceId" filterable size="large" style="width: 100%"
                     placeholder="搜索 项目编号 / 名称">
            <el-option v-for="p in selectableProjects" :key="p.id"
                       :value="p.id" :label="`${p.code} · ${p.name}`">
              <span style="float:left;font-weight:600">{{ p.code }}</span>
              <span style="float:right;color:var(--text-3);font-size:12px;margin-left:12px">
                {{ p.name }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>

      <!-- 克隆结果 -->
      <div v-else class="result">
        <div class="result-head" :class="result.cloned_field_count > 0 ? 'ok' : 'warn'">
          <el-icon><CopyDocument /></el-icon>
          {{ result.message }}
        </div>
        <div class="result-stat">
          <span>已克隆字段：<b>{{ result.cloned_field_count }}</b></span>
          <span>匹配数据表：<b>{{ result.matched_datasheets.length }}</b></span>
        </div>
        <div v-if="result.matched_datasheets.length" class="result-block">
          <div class="result-label">✓ 已应用的数据表</div>
          <el-tag v-for="d in result.matched_datasheets" :key="d" type="success" effect="light"
                  style="margin: 2px 4px 2px 0">{{ d }}</el-tag>
        </div>
        <div v-if="result.unmatched_target_datasheets.length" class="result-block">
          <div class="result-label">⚠ 源项目中找不到的数据表（保持原权限）</div>
          <el-tag v-for="d in result.unmatched_target_datasheets" :key="d" type="warning" effect="light"
                  style="margin: 2px 4px 2px 0">{{ d }}</el-tag>
        </div>
        <div v-if="result.skipped_target_fields.length" class="result-block">
          <div class="result-label">
            ⚠ 源项目中找不到的字段（保持原权限） · 共 {{ result.skipped_target_fields.length }} 个
          </div>
          <div class="skipped-list">
            <span v-for="(f, i) in result.skipped_target_fields" :key="i" class="skipped-item">{{ f }}</span>
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">{{ result ? '关闭' : '取消' }}</el-button>
      <el-button v-if="!result" type="primary" :icon="CopyDocument"
                 @click="submit" :loading="submitting"
                 :disabled="!sourceId">
        开始克隆
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  padding: 12px 16px;
  background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
  color: #92400e;
  border-left: 3px solid #f59e0b;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 14px;
  line-height: 1.7;
}

.result {
  padding: 4px 0;
}
.result-head {
  font-size: 14px;
  font-weight: 600;
  padding: 10px 14px;
  border-radius: 6px;
  margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}
.result-head.ok { background: #ecfdf5; color: #065f46; }
.result-head.warn { background: #fffbeb; color: #92400e; }

.result-stat {
  display: flex; gap: 18px;
  font-size: 13px; color: var(--text-2);
  padding: 0 4px 12px;
}
.result-stat b { color: var(--primary); font-size: 15px; margin-left: 4px; }

.result-block {
  margin-top: 10px;
  padding: 10px 12px;
  background: #f9fafb;
  border-radius: 6px;
}
.result-label {
  font-size: 12px; color: var(--text-2);
  margin-bottom: 6px;
  font-weight: 500;
}
.skipped-list {
  max-height: 120px;
  overflow-y: auto;
  font-size: 12px;
  color: var(--text-3);
}
.skipped-item {
  display: inline-block;
  padding: 2px 6px;
  margin: 2px 4px 2px 0;
  background: white;
  border: 1px solid var(--border-light);
  border-radius: 3px;
}
</style>
