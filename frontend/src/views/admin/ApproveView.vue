<script setup lang="ts">
// 🆕 v3 M16 导出审批（管理层）
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check } from '@element-plus/icons-vue'
import { http } from '@/api'
import EmptyHint from '@/components/EmptyHint.vue'

interface Req {
  id: number; user_id: number; user_name?: string | null; user_role?: string | null
  scope: string; status: string; created_at: string
}
const loading = ref(false)
const list = ref<Req[]>([])
const enabled = ref(false)

async function load() {
  loading.value = true
  try {
    const [reqs, cfg] = await Promise.all([
      http.get<Req[]>('/export-requests').then(r => r.data),
      http.get<{ enabled: boolean }>('/export-requests/config').then(r => r.data),
    ])
    list.value = reqs; enabled.value = cfg.enabled
  } finally { loading.value = false }
}
onMounted(load)

const STATUS_TXT: Record<string, string> = { pending: '待审批', approved: '已批准', rejected: '已驳回' }
const STATUS_TAG: Record<string, any> = { pending: 'warning', approved: 'success', rejected: 'danger' }

const actingId = ref<number | null>(null)
async function act(r: Req, ok: boolean) {
  if (!ok) {
    try {
      await ElMessageBox.confirm('驳回后该用户不会获得导出权限，确认驳回？', '驳回导出申请', { type: 'warning' })
    } catch { return }
  }
  actingId.value = r.id
  try {
    await http.post(`/export-requests/${r.id}/${ok ? 'approve' : 'reject'}`)
    ElMessage.success(ok ? '已批准，该用户获得导出权限' : '已驳回')
    await load()
  } finally { actingId.value = null }
}

function fmt(s: string) {
  const d = new Date(s)
  return `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>导出审批</h1>
        <div class="desc">非管理层导出数据需申请，批准后永久获得导出权限</div>
      </div>
    </div>
    <el-alert v-if="!enabled" type="info" :closable="false" style="margin-bottom:12px"
              title="导出审批当前为「关闭」状态：所有角色可直接导出（与上线前一致）。开启需在后端 export_approval_enabled 配置。" />
    <el-card shadow="never" v-loading="loading">
      <el-table :data="list" stripe>
        <el-table-column label="申请人" min-width="120">
          <template #default="{ row }">{{ row.user_name }} <span class="muted small">{{ row.user_role }}</span></template>
        </el-table-column>
        <el-table-column prop="scope" label="导出范围" min-width="160" />
        <el-table-column label="申请时间" width="120"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><el-tag size="small" :type="STATUS_TAG[row.status]">{{ STATUS_TXT[row.status] }}</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <template v-if="row.status === 'pending'">
              <el-button size="small" type="success" :icon="Check" :loading="actingId === row.id" @click="act(row, true)">批准</el-button>
              <el-button size="small" :loading="actingId === row.id" @click="act(row, false)">驳回</el-button>
            </template>
            <span v-else class="muted small">—</span>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!list.length" text="暂无导出申请" />
    </el-card>
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
</style>
