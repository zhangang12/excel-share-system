<script setup lang="ts">
// 🆕 v4 通用文件选择: 替代原生 <input type="file"> 的 "Choose File / No file chosen" 英文按钮。
// 用法: <FilePicker v-model="file" accept=".pdf" placeholder="选择发货单" />
import { ref, watch } from 'vue'
import { Paperclip, Close } from '@element-plus/icons-vue'

const props = defineProps<{
  modelValue: File | null
  accept?: string
  placeholder?: string
  disabled?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: File | null): void }>()

const inputRef = ref<HTMLInputElement | null>(null)
const localFile = ref<File | null>(props.modelValue || null)
watch(() => props.modelValue, (v) => { localFile.value = v })

function trigger() {
  if (props.disabled) return
  inputRef.value?.click()
}
function onChange(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0] || null
  localFile.value = f
  emit('update:modelValue', f)
}
function clear(e: MouseEvent) {
  e.stopPropagation()
  localFile.value = null
  if (inputRef.value) inputRef.value.value = ''
  emit('update:modelValue', null)
}

function fmtSize(n: number): string {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}
</script>

<template>
  <div class="fp" :class="{ 'fp--picked': !!localFile, 'fp--disabled': disabled }" @click="trigger">
    <el-icon class="fp__ico"><Paperclip /></el-icon>
    <template v-if="localFile">
      <span class="fp__name" :title="localFile.name">{{ localFile.name }}</span>
      <span class="fp__size">{{ fmtSize(localFile.size) }}</span>
      <el-icon class="fp__clear" @click="clear"><Close /></el-icon>
    </template>
    <template v-else>
      <span class="fp__hint">{{ placeholder || '点击选择文件' }}</span>
    </template>
    <input ref="inputRef" type="file" :accept="accept" :disabled="disabled" hidden @change="onChange" />
  </div>
</template>

<style scoped>
.fp {
  display: inline-flex; align-items: center; gap: 8px;
  height: 36px; padding: 0 12px;
  background: var(--bg-card, #fff);
  border: 1px dashed var(--border, #d1d5db);
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-3, #9ca3af);
  transition: all .15s;
  user-select: none;
  min-width: 200px;
  max-width: 100%;
}
.fp:hover { border-color: var(--primary, #2563eb); color: var(--primary, #2563eb); }
.fp--picked {
  border-style: solid;
  border-color: var(--success, #10b981);
  background: rgba(16, 185, 129, 0.04);
  color: var(--text-1, #1f2937);
}
.fp--picked:hover { border-color: var(--primary, #2563eb); }
.fp--disabled { opacity: 0.5; cursor: not-allowed; }
.fp__ico { font-size: 15px; flex-shrink: 0; }
.fp__hint { flex: 1; }
.fp__name {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-weight: 500;
  color: var(--text-1, #1f2937);
}
.fp__size {
  font-size: 12px; color: var(--text-3, #9ca3af);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}
.fp__clear {
  font-size: 14px; color: var(--text-3, #9ca3af);
  cursor: pointer;
  padding: 2px; border-radius: 4px;
  transition: all .12s;
  flex-shrink: 0;
}
.fp__clear:hover { background: rgba(239, 68, 68, 0.1); color: var(--danger, #ef4444); }
</style>
