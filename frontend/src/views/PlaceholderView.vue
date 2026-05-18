<script setup lang="ts">
import { Notebook } from '@element-plus/icons-vue'
defineProps<{ title: string }>()
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>{{ title }}</h1>
        <div class="desc">本页将在后续 Sprint 实现</div>
      </div>
    </div>
    <el-card>
      <el-empty :image-size="120" description="即将上线，敬请期待">
        <template #image>
          <el-icon style="font-size: 80px; color: var(--text-3)"><Notebook /></el-icon>
        </template>
      </el-empty>
    </el-card>
  </div>
</template>
