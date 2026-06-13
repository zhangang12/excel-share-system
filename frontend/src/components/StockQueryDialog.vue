<script setup lang="ts">
// 🆕 v3 M07 设计师只读查库存（设计部工作台引用）
import { ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { whApi, type WhMaterial } from '@/api/warehouse'

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
  <el-dialog v-model="visible" title="🔎 查库存（只读）" width="640px">
    <el-input v-model="kw" placeholder="搜索物料" :prefix-icon="Search" clearable style="width:240px;margin-bottom:10px" @change="load" />
    <el-table :data="materials" stripe size="small" v-loading="loading" max-height="50vh">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="spec" label="规格型号" min-width="120"><template #default="{ row }">{{ row.spec || '—' }}</template></el-table-column>
      <el-table-column prop="unit" label="单位" width="60" />
      <el-table-column label="现存" width="90"><template #default="{ row }"><b :class="{ low: row.low }">{{ row.stock }}</b></template></el-table-column>
      <el-table-column prop="location" label="库位" width="90"><template #default="{ row }">{{ row.location || '—' }}</template></el-table-column>
    </el-table>
  </el-dialog>
</template>

<style scoped>
.low { color: #dc2626; }
</style>
