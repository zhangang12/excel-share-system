<script setup lang="ts">
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, User as UserIcon } from '@element-plus/icons-vue'
import { adminApi } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import type { User, Role } from '@/types'

const auth = useAuthStore()
const isAdmin = computed(() => ['admin', 'manager'].includes(auth.user?.role_code || ''))
const users = ref<User[]>([])
const roles = ref<Role[]>([])
const loading = ref(false)
const keyword = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const form = reactive({
  id: 0,
  username: '',
  password: '',
  full_name: '',
  email: '',
  role_id: undefined as number | undefined,
  is_active: true,
})

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return users.value
  return users.value.filter(u =>
    u.username.toLowerCase().includes(k) ||
    (u.full_name || '').toLowerCase().includes(k) ||
    (u.email || '').toLowerCase().includes(k)
  )
})

function initialOf(u: User) {
  return (u.full_name || u.username).charAt(0).toUpperCase()
}

const avatarColors = ['#6366f1','#8b5cf6','#ec4899','#f97316','#10b981','#0ea5e9','#f59e0b','#ef4444']
function colorOf(u: User) {
  return avatarColors[u.id % avatarColors.length]
}

async function load() {
  loading.value = true
  try {
    [users.value, roles.value] = await Promise.all([
      adminApi.listUsers(),
      adminApi.listRoles(),
    ])
  } finally { loading.value = false }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, {
    id: 0, username: '', password: '', full_name: '', email: '',
    role_id: roles.value[0]?.id, is_active: true,
  })
  dialogVisible.value = true
}

function openEdit(u: User) {
  isEdit.value = true
  Object.assign(form, {
    id: u.id, username: u.username, password: '',
    full_name: u.full_name || '', email: u.email || '',
    role_id: u.role_id,
    is_active: u.is_active,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!isEdit.value) {
    if (!form.username || form.username.length < 2) {
      ElMessage.warning('用户名至少 2 个字符'); return
    }
    if (!form.password || form.password.length < 6) {
      ElMessage.warning('密码至少 6 位'); return
    }
  } else if (form.password && form.password.length < 6) {
    ElMessage.warning('新密码至少 6 位（不改请留空）'); return
  }
  if (!form.role_id) { ElMessage.warning('请选择角色'); return }
  try {
    if (isEdit.value) {
      const body: Record<string, unknown> = {
        full_name: form.full_name, email: form.email,
        role_id: form.role_id,
        is_active: form.is_active,
      }
      if (form.password) body.password = form.password
      await adminApi.updateUser(form.id, body as Partial<User> & { password?: string })
    } else {
      await adminApi.createUser({
        username: form.username, password: form.password,
        full_name: form.full_name || undefined,
        email: form.email || undefined,
        role_id: form.role_id,
        is_active: form.is_active,
      })
    }
    dialogVisible.value = false
    ElMessage.success('已保存')
    load()
  } catch { /* */ }
}

async function remove(u: User) {
  await ElMessageBox.confirm(`确认删除用户「${u.username}」？`, '删除确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    confirmButtonClass: 'el-button--danger',
  }).catch(() => 'cancel')
    .then(async (r) => {
      if (r === 'cancel') return
      await adminApi.deleteUser(u.id)
      ElMessage.success('已删除')
      load()
    })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>用户管理</h1>
        <div class="desc">管理系统账号、分配角色与部门</div>
      </div>
      <div class="spacer"></div>
      <el-input v-model="keyword" placeholder="搜索用户名/姓名/邮箱"
                style="width: 280px" clearable size="large" />
      <el-button v-if="isAdmin" type="primary" size="large" :icon="Plus" @click="openCreate">
        新建用户
      </el-button>
    </div>

    <el-card v-loading="loading">
      <el-table :data="filtered" stripe size="large" :empty-text="loading ? '加载中…' : '暂无用户'" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="用户" min-width="220">
          <template #default="{ row }">
            <div style="display:flex;align-items:center;gap:10px">
              <div class="user-avatar" :style="{ background: colorOf(row) }">{{ initialOf(row) }}</div>
              <div>
                <div style="font-weight:600">{{ row.full_name || row.username }}</div>
                <div class="muted small">@{{ row.username }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="120">
          <template #default="{ row }">
            <el-tag :type="row.role_code === 'admin' ? 'danger' : row.role_code === 'manager' ? 'warning' : 'primary'" effect="light">
              {{ row.role_name }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="180">
          <template #default="{ row }">
            <span v-if="row.email">{{ row.email }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" effect="light">启用</el-tag>
            <el-tag v-else type="danger" effect="light">停用</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="isAdmin" label="操作" width="180" align="right" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '新建用户'" width="500px">
      <el-form label-position="top">
        <el-form-item label="用户名 *">
          <el-input v-model="form.username" :disabled="isEdit" size="large" placeholder="2-64 个字符" />
        </el-form-item>
        <el-form-item :label="isEdit ? '密码（留空则不修改）' : '密码 *'">
          <el-input v-model="form.password" type="password" show-password size="large" placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.full_name" size="large" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" size="large" />
        </el-form-item>
        <el-form-item label="角色 *">
          <el-select v-model="form.role_id" size="large" style="width:100%">
            <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="r.name" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-switch v-model="form.is_active" active-text="启用账号" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.user-avatar {
  width: 36px; height: 36px;
  border-radius: 50%;
  color: white; font-weight: 600;
  display: flex; align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.muted { color: var(--text-3); }
.small { font-size: 12px; }
</style>
