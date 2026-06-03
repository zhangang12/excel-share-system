<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete, Search, FolderOpened } from '@element-plus/icons-vue'
import { projectsApi } from '@/api/projects'
import { useAuthStore } from '@/stores/auth'
import type { Project } from '@/types'

const router = useRouter()
const auth = useAuthStore()
const list = ref<Project[]>([])
const loading = ref(false)
const keyword = ref('')
const statusFilter = ref('')

const canCreate = computed(() =>
  ['admin', 'manager'].includes(auth.user?.role_code || '')
)
const canDelete = computed(() =>
  ['admin', 'manager'].includes(auth.user?.role_code || '')
)

const dialogVisible = ref(false)
const isEdit = ref(false)
const form = reactive({
  id: 0, code: '', name: '', description: '', status: '进行中',
})

async function load() {
  loading.value = true
  try { list.value = await projectsApi.list(keyword.value || undefined, statusFilter.value || undefined) }
  finally { loading.value = false }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { id: 0, code: '', name: '', description: '', status: '进行中' })
  dialogVisible.value = true
}

function openEdit(p: Project) {
  isEdit.value = true
  Object.assign(form, {
    id: p.id, code: p.code, name: p.name,
    description: p.description || '', status: p.status,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.code.trim()) { ElMessage.warning('请填写项目编号'); return }
  if (!form.name.trim()) { ElMessage.warning('请填写项目名称'); return }
  try {
    if (isEdit.value) {
      await projectsApi.update(form.id, {
        name: form.name, description: form.description, status: form.status,
      })
    } else {
      await projectsApi.create({
        code: form.code, name: form.name, description: form.description, status: form.status,
      })
    }
    dialogVisible.value = false
    ElMessage.success('已保存')
    load()
  } catch { /* */ }
}

async function remove(p: Project) {
  const r = await ElMessageBox.prompt(
    `确认删除项目「${p.name}」？请输入项目编号 ${p.code} 以确认：`,
    '删除确认',
    {
      type: 'warning',
      inputPattern: new RegExp(`^${p.code}$`),
      inputErrorMessage: '编号不匹配',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    }
  ).catch(() => null)
  if (!r) return
  await projectsApi.remove(p.id)
  ElMessage.success('已删除')
  load()
}

function open(p: Project) { router.push({ name: 'project-detail', params: { id: p.id } }) }

// 卡片整体按状态着色：进行中红 / 已完成绿 / 其他灰
function statusClass(s?: string): 'doing' | 'done' | 'other' {
  if (s === '已完成') return 'done'
  if (s === '进行中') return 'doing'
  return 'other'
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>项目列表</h1>
        <div class="desc">点项目编号进入维护进度表</div>
      </div>
      <div class="spacer"></div>
      <el-input v-model="keyword" placeholder="搜索编号/名称" style="width: 240px"
                size="large" clearable :prefix-icon="Search" @keyup.enter="load" />
      <el-select v-model="statusFilter" placeholder="全部状态" style="width: 140px"
                 size="large" clearable @change="load">
        <el-option label="进行中" value="进行中" />
        <el-option label="已完成" value="已完成" />
        <el-option label="已归档" value="已归档" />
      </el-select>
      <el-button v-if="canCreate" type="primary" size="large" :icon="Plus" @click="openCreate">
        新建项目
      </el-button>
    </div>

    <div v-if="loading" v-loading="loading" class="loading-box"></div>
    <el-empty v-else-if="!list.length" description="暂无可见项目" />
    <div v-else class="grid">
      <el-card v-for="p in list" :key="p.id"
               class="project-card" :class="`card-${statusClass(p.status)}`"
               shadow="hover" @click="open(p)">
        <div class="proj-top">
          <div class="proj-icon" :class="`icon-${statusClass(p.status)}`">
            <el-icon><FolderOpened /></el-icon>
          </div>
          <span class="status-badge" :class="`badge-${statusClass(p.status)}`">
            {{ p.status }}
          </span>
        </div>
        <div class="proj-code">{{ p.code }}</div>
        <div class="proj-name">{{ p.name }}</div>
        <div v-if="p.description" class="proj-desc">{{ p.description }}</div>
        <div class="proj-meta">
          <span>项目经理：<b>{{ p.manager_name || '-' }}</b></span>
          <span>成员 {{ p.member_count }} 人</span>
        </div>
        <div class="proj-actions" @click.stop>
          <el-button size="small" :icon="Edit" @click="openEdit(p)">编辑</el-button>
          <el-button v-if="canDelete" size="small" type="danger" :icon="Delete" @click="remove(p)">删除</el-button>
        </div>
      </el-card>
    </div>

    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑项目' : '新建项目'" width="500px">
      <el-form label-position="top">
        <el-form-item label="项目编号 *">
          <el-input v-model="form.code" :disabled="isEdit" size="large" placeholder="如 2026-040" />
        </el-form-item>
        <el-form-item label="项目名称 *">
          <el-input v-model="form.name" size="large" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="form.status" size="large" style="width:100%">
            <el-option label="进行中" value="进行中" />
            <el-option label="已完成" value="已完成" />
            <el-option label="已归档" value="已归档" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="3" />
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
.grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}
.project-card {
  cursor: pointer;
  transition: transform .15s, box-shadow .15s;
  border-left: 4px solid var(--border);
  overflow: hidden;
}
.project-card:hover { transform: translateY(-2px); }

/* 卡片按状态整体着色，与项目一览状态格视觉一致 */
.project-card.card-doing {
  background: #fef2f2 !important;
  border-left-color: #ef4444;
}
.project-card.card-doing :deep(.el-card__body) { background: #fef2f2; }
.project-card.card-done {
  background: #f0fdf4 !important;
  border-left-color: #10b981;
}
.project-card.card-done :deep(.el-card__body) { background: #f0fdf4; }
.project-card.card-other {
  background: #f8fafc !important;
  border-left-color: #94a3b8;
}
.project-card.card-other :deep(.el-card__body) { background: #f8fafc; }

.proj-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.proj-icon {
  width: 40px; height: 40px;
  background: var(--primary-light); color: var(--primary);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px;
}
.proj-icon.icon-doing { background: #fee2e2; color: #991b1b; }
.proj-icon.icon-done { background: #d1fae5; color: #065f46; }
.proj-icon.icon-other { background: #e2e8f0; color: #475569; }

/* 状态徽章：与项目一览状态格同色系 */
.status-badge {
  display: inline-block;
  padding: 5px 12px;
  border-radius: 4px;
  font-size: 12.5px;
  font-weight: 700;
  letter-spacing: 0.5px;
  border: 1px solid transparent;
}
.status-badge.badge-doing {
  background: #fee2e2;
  color: #991b1b;
  border-color: #fca5a5;
}
.status-badge.badge-done {
  background: #d1fae5;
  color: #065f46;
  border-color: #6ee7b7;
}
.status-badge.badge-other {
  background: #e2e8f0;
  color: #475569;
  border-color: #cbd5e1;
}
.proj-code { font-size: 12px; color: var(--text-3); margin-bottom: 4px; }
.proj-name { font-size: 16px; font-weight: 600; color: var(--text-1); margin-bottom: 8px; }
.proj-desc {
  font-size: 13px; color: var(--text-2); line-height: 1.5;
  margin-bottom: 12px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.proj-meta {
  display: flex; gap: 16px; font-size: 12px; color: var(--text-3);
  margin-bottom: 12px;
}
.proj-meta b { color: var(--text-2); }
.proj-actions {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  display: flex; gap: 8px;
}
.loading-box { min-height: 200px; }
</style>
