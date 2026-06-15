<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { adminApi } from '@/api/admin'
import type { AuditLog } from '@/types'
import StatusPill from '@/components/StatusPill.vue'
import { fmtRelative } from '@/utils/format'

const list = ref<AuditLog[]>([])
const loading = ref(false)
const keyword = ref('')

const ACTION_LABEL: Record<string, string> = {
  login: '登录',
  change_password: '改密',
  create_project: '建项目',
  delete_project: '删项目',
  update_project: '改项目',
  create_user: '建用户',
  delete_user: '删用户',
}

const ACTION_COLOR: Record<string, string> = {
  login: 'info',
  change_password: 'warning',
  create_project: 'success',
  delete_project: 'danger',
  create_user: 'success',
  delete_user: 'danger',
}

const ACTION_VARIANT: Record<string, 'success' | 'warn' | 'info' | 'danger' | 'primary' | 'muted'> = {
  success: 'success',
  warning: 'warn',
  info: 'info',
  danger: 'danger',
  primary: 'primary',
}

async function load() {
  loading.value = true
  try { list.value = await adminApi.listAudit(500) } finally { loading.value = false }
}

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return list.value
  return list.value.filter(a =>
    (a.username || '').toLowerCase().includes(k) ||
    a.action.toLowerCase().includes(k) ||
    (a.detail || '').toLowerCase().includes(k) ||
    (a.target_type || '').toLowerCase().includes(k)
  )
})

function fmtTime(s: string) {
  const d = new Date(s)
  return d.toLocaleString('zh-CN')
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>操作审计</h1>
        <div class="desc">最近 500 条系统操作记录</div>
      </div>
      <div class="spacer"></div>
      <el-input v-model="keyword" placeholder="搜索用户/动作/详情"
                style="width: 280px" size="large" clearable :prefix-icon="Search" />
      <el-button :icon="Refresh" size="large" @click="load">刷新</el-button>
    </div>

    <el-card v-loading="loading">
      <el-table :data="filtered" stripe size="large" :empty-text="loading ? '加载中' : '暂无记录'" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            <span class="muted small">{{ fmtRelative(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="用户" width="140">
          <template #default="{ row }">
            <span v-if="row.username">{{ row.username }}</span>
            <span v-else class="muted">系统</span>
          </template>
        </el-table-column>
        <el-table-column label="动作" width="120">
          <template #default="{ row }">
            <StatusPill
              :text="ACTION_LABEL[row.action] || row.action"
              :variant="ACTION_VARIANT[ACTION_COLOR[row.action] || 'info'] || 'muted'"
            />
          </template>
        </el-table-column>
        <el-table-column label="对象" width="160">
          <template #default="{ row }">
            <span v-if="row.target_type">{{ row.target_type }} #{{ row.target_id }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="240">
          <template #default="{ row }">
            <span v-if="row.detail">{{ row.detail }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.muted { color: var(--text-3); }
.small { font-size: 12px; }
</style>
