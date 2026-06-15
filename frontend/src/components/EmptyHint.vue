<script setup lang="ts">
// 🆕 v3 统一空状态：图标式、专业（替代 el-empty 的卡通插画）
import { Files } from '@element-plus/icons-vue'
defineProps<{ text?: string; icon?: any; size?: 'sm' | 'md' }>()
</script>

<template>
  <div class="empty-hint" :class="size || 'md'">
    <el-icon class="eh-icon"><component :is="icon || Files" /></el-icon>
    <div class="eh-text">{{ text || '暂无数据' }}</div>
  </div>
</template>

<style scoped>
.empty-hint {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  color: var(--el-text-color-secondary, #909399);
}
.empty-hint.md { padding: 40px 16px; }
.empty-hint.sm { padding: 22px 16px; }
.eh-icon {
  font-size: 30px; color: var(--el-text-color-placeholder, #c0c4cc);
  background: var(--el-fill-color-light, #f5f7fa);
  width: 60px; height: 60px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  margin-bottom: 12px;
}
.empty-hint.sm .eh-icon { width: 46px; height: 46px; font-size: 22px; margin-bottom: 8px; }
.eh-text { font-size: 13px; }
</style>
