<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Bell, Warning, ChatDotRound, InfoFilled } from '@element-plus/icons-vue'
import { messagesApi, type Message } from '@/api/messages'
import EmptyHint from '@/components/EmptyHint.vue'
import StatusPill from '@/components/StatusPill.vue'
import { fmtRelative } from '@/utils/format'

const loading = ref(false)
const list = ref<Message[]>([])

// 颜色对齐设计系统语义色 token（--warning/--success/--primary 实际取值）
const KIND_META: Record<string, { icon: any; color: string; label: string }> = {
  warn: { icon: Warning, color: '#f59e0b', label: '预警' },
  wx: { icon: ChatDotRound, color: '#10b981', label: '企微' },
  info: { icon: InfoFilled, color: '#2563eb', label: '通知' },
}

function fmtTime(s: string) {
  const d = new Date(s)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function load() {
  loading.value = true
  try {
    list.value = await messagesApi.list(100)
  } finally {
    loading.value = false
  }
}

async function markAllRead() {
  await messagesApi.readAll()
  list.value.forEach((m) => (m.read = true))
  ElMessage.success('已全部标为已读')
}

onMounted(async () => {
  await load()
  // 进入页面即标已读（同原型口径）
  if (list.value.some((m) => !m.read)) {
    try { await messagesApi.readAll(); list.value.forEach((m) => (m.read = true)) } catch { /* 静默 */ }
  }
})
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>消息中心</h1>
        <div class="desc">企业微信通知、逾期提醒、审批结果汇总到这里</div>
      </div>
      <div class="spacer"></div>
      <el-button @click="markAllRead">全部已读</el-button>
    </div>

    <el-card v-loading="loading" shadow="never">
      <EmptyHint v-if="!loading && list.length === 0" text="暂无消息" :icon="Bell" />

      <div v-else class="msg-list">
        <div v-for="m in list" :key="m.id" class="msg-item" :class="{ unread: !m.read }">
          <el-icon class="msg-icon" :style="{ color: (KIND_META[m.kind] || KIND_META.info).color }">
            <component :is="(KIND_META[m.kind] || KIND_META.info).icon" />
          </el-icon>
          <div class="msg-body">
            <div class="msg-text">{{ m.text }}</div>
            <div class="msg-time">{{ fmtRelative(m.created_at) }}</div>
          </div>
          <StatusPill v-if="!m.read" text="未读" variant="danger" />
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.msg-list { display: flex; flex-direction: column; }
.msg-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 14px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.msg-item:last-child { border-bottom: none; }
.msg-item.unread { background: var(--el-color-primary-light-9); border-radius: 8px; }
.msg-icon { font-size: 20px; margin-top: 2px; flex-shrink: 0; }
.msg-body { flex: 1; min-width: 0; }
.msg-text { font-size: 14px; line-height: 1.6; word-break: break-all; }
.msg-time { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }
</style>
