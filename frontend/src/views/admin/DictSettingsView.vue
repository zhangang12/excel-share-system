<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { http } from '@/api'
import EmptyHint from '@/components/EmptyHint.vue'

interface DictItem { id: number; dtype: string; value: string; sort_order: number; enabled: boolean }

// 受管理的字典类型（订单编号=非项目的采购订单编号，下单时只能从这里选）
const DTYPES: { key: string; label: string; hint: string }[] = [
  { key: 'order_no', label: '订单编号', hint: '新建采购单里“订单编号”非项目编号的可选值（不可手打，只能在此维护）' },
  { key: 'category', label: '物料类别', hint: '物料主数据的类别取值' },
  { key: 'unit', label: '计量单位', hint: '物料计量单位取值' },
  { key: 'material_grade', label: '材质', hint: '物料材质取值（304不锈钢/碳钢/铝合金…）' },
  { key: 'supplier_category', label: '供应商分类', hint: '供应商分类取值' },
]

const active = ref('order_no')
const list = ref<DictItem[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    list.value = (await http.get<DictItem[]>('/wh/material-dict', { params: { dtype: active.value } })).data
  } catch { list.value = [] } finally { loading.value = false }
}
onMounted(load)

// 新增/编辑
const dlg = ref(false)
const editing = ref<DictItem | null>(null)
const form = ref<{ value: string; sort_order: number; enabled: boolean }>({ value: '', sort_order: 0, enabled: true })
const saving = ref(false)
function openAdd() { editing.value = null; form.value = { value: '', sort_order: (list.value.length + 1) * 10, enabled: true }; dlg.value = true }
function openEdit(r: DictItem) { editing.value = r; form.value = { value: r.value, sort_order: r.sort_order, enabled: r.enabled }; dlg.value = true }
async function save() {
  const v = form.value.value.trim()
  if (!v) { ElMessage.warning('取值不能为空'); return }
  saving.value = true
  try {
    const body = { dtype: active.value, value: v, sort_order: form.value.sort_order, enabled: form.value.enabled }
    if (editing.value) await http.put(`/wh/material-dict/${editing.value.id}`, body)
    else await http.post('/wh/material-dict', body)
    ElMessage.success('已保存')
    dlg.value = false
    await load()
  } catch { /* handled */ } finally { saving.value = false }
}
async function toggleEnabled(r: DictItem) {
  try {
    await http.put(`/wh/material-dict/${r.id}`, { dtype: r.dtype, value: r.value, sort_order: r.sort_order, enabled: !r.enabled })
    await load()
  } catch { /* handled */ }
}
async function remove(r: DictItem) {
  try { await ElMessageBox.confirm(`删除字典取值「${r.value}」？若已被引用会被拦截，可改为“停用”。`, '删除', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await http.delete(`/wh/material-dict/${r.id}`); ElMessage.success('已删除'); await load() }
  catch { /* handled(如被引用则后端拦截) */ }
}
function onTab() { load() }
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>字典设置</h1>
        <div class="desc">统一维护受管理的取值：订单编号 / 物料类别 / 计量单位 / 材质 / 供应商分类（仅管理员/管理层）</div>
      </div>
    </div>

    <el-card shadow="never">
      <el-tabs v-model="active" @tab-change="onTab">
        <el-tab-pane v-for="t in DTYPES" :key="t.key" :label="t.label" :name="t.key" />
      </el-tabs>

      <div class="bar">
        <span class="hint">{{ DTYPES.find(d => d.key === active)?.hint }}</span>
        <span class="spacer" />
        <el-button type="primary" :icon="Plus" @click="openAdd">新增取值</el-button>
      </div>

      <el-table show-overflow-tooltip :data="list" v-loading="loading" stripe size="small" class="compact-tbl" max-height="calc(100vh - 300px)">
        <el-table-column type="index" label="#" width="52" align="center" />
        <el-table-column prop="value" label="取值" min-width="220" />
        <el-table-column prop="sort_order" label="排序" width="90" align="right" />
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" align="right" fixed="right" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" @click="toggleEnabled(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyHint text="该字典暂无取值，点右上角「新增取值」添加" size="sm" />
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="dlg" :title="editing ? '编辑取值' : '新增取值'" width="420px">
      <el-form label-position="top">
        <el-form-item label="取值" required>
          <el-input v-model="form.value" placeholder="如：2026-备01" maxlength="64" />
        </el-form-item>
        <el-form-item label="排序（小在前）">
          <el-input-number v-model="form.sort_order" :min="0" :controls="false" style="width:160px" />
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bar { display: flex; align-items: center; gap: 12px; margin: 8px 0 12px; }
.bar .hint { font-size: 13px; color: var(--el-text-color-secondary); }
.bar .spacer { flex: 1; }
</style>
