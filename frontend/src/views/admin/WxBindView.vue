<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '@/api'
import type { User } from '@/types'
import StatusPill from '@/components/StatusPill.vue'

// 🆕 v3 企微绑定：用户 ↔ 企业微信 userid（手动绑定 F1 口径）
interface BindUser extends User { wxid?: string | null }

const loading = ref(false)
const users = ref<BindUser[]>([])
const editing = ref<Record<number, string>>({})

async function load() {
  loading.value = true
  try {
    const r = await http.get<BindUser[]>('/admin/users')
    users.value = r.data
    users.value.forEach((u) => { editing.value[u.id] = (u as any).wxid || '' })
  } finally {
    loading.value = false
  }
}

async function bind(u: BindUser) {
  const wxid = (editing.value[u.id] || '').trim()
  await http.put(`/admin/users/${u.id}/wxid`, { wxid })
  ;(u as any).wxid = wxid || null
  ElMessage.success(wxid ? `已绑定 ${u.full_name || u.username}` : '已解绑')
}

// 🆕 企微推送自检：给自己发一条测试消息（凭证/可信IP/userid 有问题会直接报具体原因）
const testing = ref(false)
async function testPush() {
  testing.value = true
  try {
    const r = await http.post<{ message: string }>('/admin/wecom-test')
    ElMessage.success(r.data.message || '测试消息已发送，请查收企业微信')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '测试失败')
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>企微绑定</h1>
        <div class="desc">绑定企业微信 userid 后，站内消息将同步推送企业微信（凭证未配置时仅站内）</div>
      </div>
      <div class="spacer" style="flex:1"></div>
      <el-button type="primary" plain :loading="testing" @click="testPush">🔔 测试推送（给自己）</el-button>
    </div>

    <el-card v-loading="loading" shadow="never">
      <el-table :data="users" stripe max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="姓名" min-width="140">
          <template #default="{ row }">{{ row.full_name || row.username }}</template>
        </el-table-column>
        <el-table-column prop="role_name" label="角色" min-width="120" />
        <el-table-column label="当前绑定" min-width="140">
          <template #default="{ row }">
            <StatusPill v-if="row.wxid" :text="row.wxid" variant="success" />
            <span v-else class="muted">未绑定</span>
          </template>
        </el-table-column>
        <el-table-column label="企微 userid" min-width="260">
          <template #default="{ row }">
            <div style="display: flex; gap: 8px">
              <el-input v-model="editing[row.id]" placeholder="企业微信 userid" size="small" style="max-width: 200px" />
              <el-button size="small" type="primary" plain @click="bind(row)">
                {{ row.wxid ? '更新' : '绑定' }}
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.muted { color: var(--el-text-color-secondary); font-size: 13px; }
</style>
