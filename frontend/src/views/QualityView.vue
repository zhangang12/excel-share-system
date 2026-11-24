<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { qualityApi, type QualityTask } from '@/api/quality'
import PageRefresh from '@/components/PageRefresh.vue'
import { fmtDateTime } from '@/utils/format'

const tasks = ref<QualityTask[]>([])
const loading = ref(false)
const savingId = ref<number | null>(null)
const showDone = ref(false)
const keyword = ref('')
const pendingCount = computed(() => tasks.value.filter(task => !task.quality_done_at).length)
const visibleTasks = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  return tasks.value.filter(task =>
    Boolean(task.quality_done_at) === showDone.value &&
    `${task.code} ${task.name}`.toLowerCase().includes(q),
  )
})

async function load() {
  loading.value = true
  try {
    tasks.value = await qualityApi.list()
  } finally {
    loading.value = false
  }
}

async function toggleDone(task: QualityTask) {
  savingId.value = task.task_id
  try {
    const res = await qualityApi.setDone(task.task_id, !task.quality_done_at)
    ElMessage.success(res.message)
    await load()
  } catch {
    // HTTP 拦截器展示操作失败原因。
  } finally {
    savingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h1>质检部</h1>
      <div class="spacer"></div>
      <PageRefresh :load="load" />
    </div>
    <el-card v-loading="loading">
      <div class="quality-filters">
        <el-radio-group v-model="showDone">
          <el-radio-button :value="false">待质检（{{ pendingCount }}）</el-radio-button>
          <el-radio-button :value="true">已完成（{{ tasks.length - pendingCount }}）</el-radio-button>
        </el-radio-group>
        <el-input v-model="keyword" :prefix-icon="Search" placeholder="项目编号或名称" clearable style="width: 240px" />
      </div>
      <el-table :data="visibleTasks" row-key="task_id" stripe
                :empty-text="showDone ? '暂无已完成的质检任务' : '暂无待质检任务'">
        <el-table-column prop="code" label="项目编号" min-width="130" />
        <el-table-column prop="name" label="项目名称" min-width="210" show-overflow-tooltip />
        <el-table-column label="装配完成时间" min-width="158">
          <template #default="{ row }">{{ fmtDateTime(row.assembly_done_at, '—', 'Asia/Shanghai') }}</template>
        </el-table-column>
        <el-table-column v-if="showDone" label="质检完成时间" min-width="158">
          <template #default="{ row }">{{ fmtDateTime(row.quality_done_at, '—', 'Asia/Shanghai') }}</template>
        </el-table-column>
        <el-table-column v-if="showDone" prop="quality_done_by" label="质检人" min-width="100" />
        <el-table-column label="操作" width="132" align="center">
          <template #default="{ row }">
            <el-button :type="showDone ? 'default' : 'primary'" size="small"
                       :loading="savingId === row.task_id" :disabled="savingId !== null"
                       @click="toggleDone(row)">{{ showDone ? '撤销完成' : '质检完成' }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.quality-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
</style>
