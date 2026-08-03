<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
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
function onTab() { if (active.value === 'matcat') loadCats(); else load() }

// ===== 🆕 物料编码分类(3级树)：大类(1位) → 中类(2位) → 细分类(2位)，编码=前缀+4位流水 =====
interface CatNode { id: number; parent_id: number | null; level: number; seg_code: string; name: string; sort_order: number; enabled: boolean; children?: CatNode[]; prefix?: string }
const catFlat = ref<CatNode[]>([])
const catLoading = ref(false)
// 展开状态自己管。原来用 default-expand-all，但 catTree 是 computed，
// 每次保存后 loadCats 换掉 catFlat → 整个 :data 被重建 → el-tree 重新套用
// default-expand-all → 用户辛苦收起来的分支又全展开了（#342）。
const expandedKeys = ref<number[]>([])
const catFirstLoad = ref(true)
function onNodeExpand(d: CatNode) {
  if (!expandedKeys.value.includes(d.id)) expandedKeys.value = [...expandedKeys.value, d.id]
}
function onNodeCollapse(d: CatNode) {
  expandedKeys.value = expandedKeys.value.filter(k => k !== d.id)
}
async function loadCats() {
  catLoading.value = true
  try { catFlat.value = (await http.get<CatNode[]>('/wh/material-categories')).data }
  catch { catFlat.value = [] } finally { catLoading.value = false }
  if (catFirstLoad.value) {
    // 首次进来还是全展开（原来的观感不变），之后就听用户的
    catFirstLoad.value = false
    const hasChild = new Set(catFlat.value.map(c => c.parent_id).filter((x): x is number => x != null))
    expandedKeys.value = catFlat.value.filter(c => hasChild.has(c.id)).map(c => c.id)
  }
}
const catTree = computed<CatNode[]>(() => {
  const byParent = new Map<number | null, CatNode[]>()
  for (const c of catFlat.value) {
    const k = c.parent_id ?? null
    if (!byParent.has(k)) byParent.set(k, [])
    byParent.get(k)!.push({ ...c })
  }
  const build = (pid: number | null, prefix: string): CatNode[] =>
    (byParent.get(pid) || [])
      // 后端按 sort_order 排，并列时按 id；并列的按段码排才跟编码顺序一致
      .sort((a, b) => a.sort_order - b.sort_order || a.seg_code.localeCompare(b.seg_code))
      .map(c => ({ ...c, prefix: prefix + c.seg_code, children: build(c.id, prefix + c.seg_code) }))
  return build(null, '')
})
const LEVEL_NAME: Record<number, string> = { 1: '大类', 2: '中类', 3: '细分类' }
const SEG_HINT: Record<number, string> = { 1: '1位数字，如 1', 2: '2位数字，如 01', 3: '2位数字，如 01' }
const catDlg = ref(false)
const catSaving = ref(false)
const catEditing = ref<CatNode | null>(null)
const catParent = ref<CatNode | null>(null)   // 新增时的上级(null=新增大类)
const catForm = ref({ seg_code: '', name: '', sort_order: 0, enabled: true })
const catFormLevel = computed(() => catEditing.value ? catEditing.value.level : (catParent.value ? catParent.value.level + 1 : 1))
const SEG_LEN: Record<number, number> = { 1: 1, 2: 2, 3: 2 }   // 与后端 _SEG_LEN 一致

/** 同级里顺延出下一个可用段码（#341）。留空让人自己猜下一个是几，
 *  猜错了后端还要甩个 409「同级下该段码已存在」。填好默认值，照样能改。 */
function nextSeg(parentId: number | null, level: number): string {
  const w = SEG_LEN[level]
  const used = new Set(catFlat.value.filter(c => (c.parent_id ?? null) === parentId)
                                    .map(c => Number(c.seg_code)))
  const max = 10 ** w - 1              // 1位→9，2位→99
  for (let n = 1; n <= max; n++) {     // 从 1 起找第一个空位：删掉中间某个后能补回去
    if (!used.has(n)) return String(n).padStart(w, '0')
  }
  return ''                            // 排满了就别乱填，让人自己处理
}

function openCatAdd(parent: CatNode | null) {
  catEditing.value = null; catParent.value = parent
  const lv = parent ? parent.level + 1 : 1
  const sibs = catFlat.value.filter(c => (c.parent_id ?? null) === (parent?.id ?? null))
  catForm.value = {
    seg_code: nextSeg(parent?.id ?? null, lv),
    name: '',
    sort_order: sibs.length ? Math.max(...sibs.map(c => c.sort_order)) + 1 : 0,
    enabled: true,
  }
  catDlg.value = true
}
function openCatEdit(n: CatNode) {
  catEditing.value = n; catParent.value = null
  catForm.value = { seg_code: n.seg_code, name: n.name, sort_order: n.sort_order, enabled: n.enabled }
  catDlg.value = true
}
async function saveCat() {
  const f = catForm.value
  if (!f.seg_code.trim() || !f.name.trim()) { ElMessage.warning('段码和名称必填'); return }
  catSaving.value = true
  try {
    if (catEditing.value) {
      await http.put(`/wh/material-categories/${catEditing.value.id}`, { parent_id: catEditing.value.parent_id, seg_code: f.seg_code.trim(), name: f.name.trim(), sort_order: f.sort_order, enabled: f.enabled })
    } else {
      await http.post('/wh/material-categories', { parent_id: catParent.value?.id ?? null, seg_code: f.seg_code.trim(), name: f.name.trim(), sort_order: f.sort_order, enabled: f.enabled })
    }
    ElMessage.success('已保存')
    catDlg.value = false
    // 存完要能看见刚加的那条：先把父节点标成展开，再重载（重载会按 expandedKeys 铺开）
    const pid = catParent.value?.id
    if (pid != null && !expandedKeys.value.includes(pid)) expandedKeys.value = [...expandedKeys.value, pid]
    await loadCats()
  } catch { /* handled */ } finally { catSaving.value = false }
}
async function removeCat(n: CatNode) {
  try { await ElMessageBox.confirm(`删除「${n.name}」？有子分类或已被物料使用会被拦截。`, '删除分类', { type: 'warning', confirmButtonText: '删除' }) } catch { return }
  try { await http.delete(`/wh/material-categories/${n.id}`); ElMessage.success('已删除'); await loadCats() } catch { /* handled */ }
}
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
        <el-tab-pane label="物料编码分类" name="matcat" />
      </el-tabs>

      <!-- 🆕 物料编码分类(3级树)：大类1位→中类2位→细分2位，物料编码=前缀+4位流水 -->
      <template v-if="active === 'matcat'">
        <div class="bar">
          <span class="hint">三级分类：大类(1位段码) → 中类(2位) → 细分类(2位)；物料在「仓库-物料主数据」选到<b>细分类</b>后保存，自动发码 = 前缀 + 4位流水（如 1·01·01 → 101010001）。改段码只影响之后新发的码。</span>
          <span class="spacer" />
          <el-button type="primary" :icon="Plus" @click="openCatAdd(null)">新增大类</el-button>
        </div>
        <el-tree v-loading="catLoading" :data="catTree" node-key="id"
                 :default-expanded-keys="expandedKeys" @node-expand="onNodeExpand" @node-collapse="onNodeCollapse"
                 :props="{ label: 'name', children: 'children' }" class="cat-tree">
          <template #default="{ data }">
            <div class="cat-node">
              <el-tag size="small" :type="data.level === 1 ? 'primary' : data.level === 2 ? 'warning' : 'success'" effect="plain" class="lv">{{ LEVEL_NAME[data.level] }}</el-tag>
              <b class="seg">{{ data.seg_code }}</b>
              <span class="nm">{{ data.name }}</span>
              <span class="pfx">前缀 {{ data.prefix }}</span>
              <el-tag v-if="!data.enabled" size="small" type="info">停用</el-tag>
              <span class="ops">
                <el-button v-if="data.level < 3" size="small" link type="primary" @click.stop="openCatAdd(data)">＋子类</el-button>
                <el-button size="small" link @click.stop="openCatEdit(data)">编辑</el-button>
                <el-button size="small" link type="danger" @click.stop="removeCat(data)">删除</el-button>
              </span>
            </div>
          </template>
        </el-tree>
        <EmptyHint v-if="!catLoading && !catTree.length" text="还没有分类，点「新增大类」开始（如 1 原材料 / 2 半成品 / 3 成品）" size="sm" />
      </template>

      <template v-else>
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
      </template>
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

    <!-- 🆕 物料编码分类节点弹窗 -->
    <el-dialog v-model="catDlg" :title="catEditing ? `编辑${LEVEL_NAME[catFormLevel]}` : (catParent ? `在「${catParent.name}」下新增${LEVEL_NAME[catFormLevel]}` : '新增大类')" width="420px">
      <el-form label-position="top">
        <el-form-item :label="`段码（${SEG_HINT[catFormLevel]}）`" required>
          <el-input v-model="catForm.seg_code" :maxlength="catFormLevel === 1 ? 1 : 2" placeholder="数字" />
          <div v-if="!catEditing" class="seg-tip">已按同级顺延填好，可以改</div>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="catForm.name" maxlength="64" placeholder="如 原材料 / 配件类 / 硅胶件" />
        </el-form-item>
        <el-form-item label="排序（小在前）">
          <el-input-number v-model="catForm.sort_order" :min="0" :controls="false" style="width:160px" />
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="catForm.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="catDlg = false">取消</el-button>
        <el-button type="primary" :loading="catSaving" @click="saveCat">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.seg-tip { font-size: 12px; color: var(--el-text-color-placeholder); line-height: 1.6; margin-top: 2px }
.cat-tree { margin-top: 4px; }
.cat-tree :deep(.el-tree-node__content) { height: 34px; border-radius: 6px; }
.cat-node { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; padding-right: 8px; }
.cat-node .lv { flex: none; }
.cat-node .seg { font-variant-numeric: tabular-nums; color: var(--el-color-primary); }
.cat-node .nm { font-weight: 500; }
.cat-node .pfx { font-size: 12px; color: var(--el-text-color-secondary); }
.cat-node .ops { margin-left: auto; opacity: 0; transition: opacity .15s; }
.cat-tree :deep(.el-tree-node__content:hover) .ops { opacity: 1; }
.bar { display: flex; align-items: center; gap: 12px; margin: 8px 0 12px; }
.bar .hint { font-size: 13px; color: var(--el-text-color-secondary); }
.bar .spacer { flex: 1; }
</style>
