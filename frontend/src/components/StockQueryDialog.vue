<script setup lang="ts">
// 🆕 v3 M07 设计师只读查库存（设计部工作台引用）
import { ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { whApi, type WhMaterial } from '@/api/warehouse'
import EmptyHint from '@/components/EmptyHint.vue'

const visible = defineModel<boolean>({ required: true })
const loading = ref(false)
const materials = ref<WhMaterial[]>([])
const kw = ref('')

async function load() {
  loading.value = true
  try { materials.value = (await whApi.materials(kw.value || undefined)).materials }
  finally { loading.value = false }
}
watch(visible, (v) => { if (v) load() })
</script>

<template>
  <el-dialog v-model="visible" title="🔎 查库存（只读）" width="70%" top="6vh" class="stock-query-dialog">
    <el-input v-model="kw" placeholder="搜索物料名称 / 规格 / 编码" :prefix-icon="Search" clearable style="width:320px;margin-bottom:10px" @change="load" />
    <el-table :data="materials" stripe size="small" v-loading="loading" max-height="70vh" :scrollbar-always-on="true">
      <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
      <el-table-column prop="spec" label="规格型号" min-width="160"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
      <el-table-column prop="category" label="类别" min-width="110"><template #default="{ row }">{{ row.category || '—' }}</template></el-table-column>
      <el-table-column prop="unit" label="单位" width="70" align="center" />
      <el-table-column label="现存" width="100" align="right"><template #default="{ row }"><b :class="{ low: row.low }">{{ row.stock }}</b></template></el-table-column>
      <el-table-column prop="safety_stock" label="安全库存" width="100" align="right" />
      <el-table-column prop="location" label="库位" min-width="110"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
    </el-table>
    <EmptyHint v-if="!loading && !materials.length" :text="kw ? '未找到匹配物料' : '暂无物料'" size="sm" />
  </el-dialog>
</template>

<style scoped>
.low { color: var(--danger); }
</style>
