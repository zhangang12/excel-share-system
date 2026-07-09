<script setup lang="ts">
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, User as UserIcon } from '@element-plus/icons-vue'
import { adminApi } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import type { User, Role } from '@/types'
import StatusPill from '@/components/StatusPill.vue'

const auth = useAuthStore()
const isAdmin = computed(() => auth.hasRole('admin', 'manager'))
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
  role_ids: [] as number[],   // 🆕 多角色（平等，无主次）
  is_active: true,
})

// 🆕 #7 二级菜单(tab)权限：管理层给某账号勾掉不可见的 tab
const tabRegistry = ref<{ menu_key: string; menu_label: string; tabs: { key: string; label: string }[] }[]>([])
const tabDlgVisible = ref(false)
const tabDlgUser = ref<User | null>(null)
const tabHiddenSet = ref<Set<string>>(new Set())
const tabSaving = ref(false)
async function openTabPerm(u: User) {
  tabDlgUser.value = u
  tabHiddenSet.value = new Set(u.hidden_tabs || [])
  if (!tabRegistry.value.length) { try { tabRegistry.value = await adminApi.tabRegistry() } catch { /* */ } }
  tabDlgVisible.value = true
}
function tabChecked(key: string) { return !tabHiddenSet.value.has(key) }   // 勾选=可见
function toggleTab(key: string, visible: boolean) {
  const s = new Set(tabHiddenSet.value)
  if (visible) s.delete(key); else s.add(key)
  tabHiddenSet.value = s
}
function toggleMenuAll(g: { tabs: { key: string }[] }, visible: boolean) {
  const s = new Set(tabHiddenSet.value)
  for (const t of g.tabs) { if (visible) s.delete(t.key); else s.add(t.key) }
  tabHiddenSet.value = s
}
async function saveTabPerm() {
  if (!tabDlgUser.value) return
  tabSaving.value = true
  try {
    await adminApi.updateUser(tabDlgUser.value.id, { hidden_tabs: [...tabHiddenSet.value] } as Partial<User>)
    ElMessage.success('二级菜单权限已保存（该账号重新登录或刷新后生效）')
    tabDlgVisible.value = false
    await load()
  } catch { /* handled */ } finally { tabSaving.value = false }
}

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

function tagType(code?: string | null) {
  return code === 'admin' ? 'danger' : code === 'manager' ? 'warning' : 'primary'
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
    role_ids: roles.value[0]?.id ? [roles.value[0].id] : [], is_active: true,
  })
  dialogVisible.value = true
}

function openEdit(u: User) {
  isEdit.value = true
  Object.assign(form, {
    id: u.id, username: u.username, password: '',
    full_name: u.full_name || '', email: u.email || '',
    // 优先用全部角色；缺失则回退锚点角色
    role_ids: (u.role_ids && u.role_ids.length) ? [...u.role_ids] : (u.role_id ? [u.role_id] : []),
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
  if (!form.role_ids.length) { ElMessage.warning('请至少选择一个角色'); return }
  try {
    if (isEdit.value) {
      const body: Record<string, unknown> = {
        full_name: form.full_name, email: form.email,
        role_ids: form.role_ids,
        is_active: form.is_active,
      }
      if (form.password) body.password = form.password
      await adminApi.updateUser(form.id, body as Partial<User> & { password?: string })
    } else {
      await adminApi.createUser({
        username: form.username, password: form.password,
        full_name: form.full_name || undefined,
        email: form.email || undefined,
        role_ids: form.role_ids,
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
        <el-table-column label="角色" min-width="180">
          <template #default="{ row }">
            <span class="role-tags">
              <el-tag v-for="(rn, i) in (row.role_names?.length ? row.role_names : [row.role_name])" :key="i"
                      :type="tagType((row.role_codes || [row.role_code])[i])" effect="light" size="small">
                {{ rn }}
              </el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="180">
          <template #default="{ row }">
            <span v-if="row.email">{{ row.email }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <StatusPill v-if="row.is_active" text="启用" variant="success" />
            <StatusPill v-else text="停用" variant="danger" />
          </template>
        </el-table-column>
        <el-table-column v-if="isAdmin" label="操作" width="270" align="right" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" @click="openTabPerm(row)">二级菜单权限</el-button>
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
        <el-form-item label="角色 *（可多选，权限取并集）">
          <el-select v-model="form.role_ids" multiple filterable collapse-tags collapse-tags-tooltip
                     size="large" style="width:100%" placeholder="可选一个或多个角色">
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

    <!-- 🆕 #7 二级菜单(tab)权限：勾选=该账号可见该 tab -->
    <el-dialog v-model="tabDlgVisible" :title="`二级菜单权限 — ${tabDlgUser?.full_name || tabDlgUser?.username || ''}`" width="min(720px, 96vw)" top="6vh">
      <el-alert type="info" :closable="false" style="margin-bottom:12px"
        title="勾选=该账号可见该二级菜单(tab)，取消勾选=对该账号隐藏。这里只控制页面内的二级 tab；顶层菜单仍由角色决定。管理层不受此限制。" />
      <div v-for="g in tabRegistry" :key="g.menu_key" style="margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
          <b>{{ g.menu_label }}</b>
          <el-button size="small" link type="primary" @click="toggleMenuAll(g, true)">全选</el-button>
          <el-button size="small" link @click="toggleMenuAll(g, false)">全不选</el-button>
        </div>
        <el-checkbox v-for="t in g.tabs" :key="t.key" :model-value="tabChecked(t.key)"
                     @change="(v: any) => toggleTab(t.key, !!v)" style="margin-right:18px">{{ t.label }}</el-checkbox>
      </div>
      <template #footer>
        <el-button @click="tabDlgVisible = false">取消</el-button>
        <el-button type="primary" :loading="tabSaving" @click="saveTabPerm">保存</el-button>
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
.role-tags { display: inline-flex; flex-wrap: wrap; gap: 4px; }
</style>
