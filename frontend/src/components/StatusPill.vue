<script setup lang="ts">
// 🆕 v4 统一状态 pill: 圆点 dot + 文字; 替代散落的 el-tag。
// 用法: <StatusPill text="已开票" variant="success" />
defineProps<{
  text: string
  variant?: 'success' | 'warn' | 'warning' | 'info' | 'danger' | 'primary' | 'muted'
  /** 不显示圆点(用于单纯标签) */
  noDot?: boolean
}>()
</script>

<template>
  <span class="v-pill" :class="`v-pill--${variant || 'muted'}`">
    <span v-if="!noDot" class="v-pill__dot"></span>
    {{ text }}
  </span>
</template>

<style scoped>
.v-pill {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  line-height: 18px;
  white-space: nowrap;
  vertical-align: middle;
}
.v-pill__dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }

.v-pill--success { background: #D1FAE5; color: #047857; }
.v-pill--success .v-pill__dot { background: #10B981; }

.v-pill--warn, .v-pill--warning { background: #FEF3C7; color: #B45309; }
.v-pill--warn .v-pill__dot, .v-pill--warning .v-pill__dot { background: #F59E0B; }

.v-pill--info { background: #DBEAFE; color: #1E40AF; }
.v-pill--info .v-pill__dot { background: #3B82F6; }

.v-pill--danger { background: #FEE2E2; color: #B91C1C; }
.v-pill--danger .v-pill__dot { background: #EF4444; }

.v-pill--primary { background: #E0E7FF; color: #3730A3; }
.v-pill--primary .v-pill__dot { background: #6366F1; }

.v-pill--muted { background: #F1F5F9; color: #64748B; border: 1px solid #E2E8F0; }
.v-pill--muted .v-pill__dot { background: #94A3B8; }
</style>
