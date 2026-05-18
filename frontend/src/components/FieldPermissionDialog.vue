<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { permApi, type FieldPermItem } from '@/api/permissions'
import { adminApi } from '@/api/admin'
import type { Role } from '@/types'

const props = defineProps<{
  modelValue: boolean
  fieldId: number
  fieldName: string
  scope: 'datasheet' | 'overview'
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
}>()

const roles = ref<Role[]>([])
const perms = ref<FieldPermItem[]>([])
const loading = ref(false)
const saving = ref(false)

async function load() {
  loading.value = true
  try {
    if (!roles.value.length) {
      // admin / manager 角色由系统自动全权，不在这里配置
      const all = await adminApi.listRoles()
      roles.value = all.filter(r => r.code !== 'admin' && r.code !== 'manager')
    }
    const existing = props.scope === 'datasheet'
      ? await permApi.listFieldPerms(props.fieldId)
      : await permApi.listOverviewPerms(props.fieldId)
    const existingMap = new Map(existing.map(p => [p.role_id, p]))
    perms.value = roles.value.map(r => {
      const e = existingMap.get(r.id)
      return {
        role_id: r.id, role_name: r.name,
        can_view: e?.can_view ?? true,
        can_edit: e?.can_edit ?? true,
      }
    })
  } finally { loading.value = false }
}

// 首次挂载时，如果 dialog 已经是 open 状态，需要立刻 load（watch 默认不会触发）
onMounted(() => {
  if (props.modelValue && props.fieldId) load()
})

watch(() => props.modelValue, (v) => {
  if (v && props.fieldId) load()
})

// 字段切换时（同一对话框换不同字段）也要重新加载
watch(() => props.fieldId, (fid) => {
  if (fid && props.modelValue) load()
})

async function save() {
  saving.value = true
  try {
    if (props.scope === 'datasheet') {
      await permApi.setFieldPerms(props.fieldId, perms.value)
    } else {
      await permApi.setOverviewPerms(props.fieldId, perms.value)
    }
    ElMessage.success('权限已保存')
    emit('update:modelValue', false)
  } finally { saving.value = false }
}

function onViewChange(p: FieldPermItem) {
  // 不可见自动取消可编辑
  if (!p.can_view) p.can_edit = false
}
</script>

<template>
  <el-dialog :model-value="modelValue" @update:model-value="emit('update:modelValue', $event)"
             :title="`字段权限：${fieldName}`" width="540px" :close-on-click-modal="false">
    <div v-loading="loading">
      <div class="hint">
        超级管理员（admin）和管理层（manager）始终有全部权限。下面的设置只对其他角色生效。<br />
        不勾选"可见"时，"可编辑"自动失效。
      </div>
      <el-table :data="perms" size="default" stripe>
        <el-table-column label="角色" min-width="120">
          <template #default="{ row }">
            <el-tag effect="plain">{{ row.role_name }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可见" width="100" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.can_view" @change="onViewChange(row)" />
          </template>
        </el-table-column>
        <el-table-column label="可编辑" width="100" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.can_edit" :disabled="!row.can_view" />
          </template>
        </el-table-column>
      </el-table>
    </div>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" @click="save" :loading="saving">保存</el-button>
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
.muted { color: var(--text-3); }
.small { font-size: 12px; }
</style>
