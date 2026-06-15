<script setup lang="ts">
// 🆕 v3 物流发货部：发货看板 + D5 闸门 + 收货信息 + 确认发货回传销售台账
import { ref, onMounted, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Van, Edit, Clock, Phone, Location } from '@element-plus/icons-vue'
import { http } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { downloadAttachment } from '@/api/orders'
import EmptyHint from '@/components/EmptyHint.vue'
import FilePicker from '@/components/FilePicker.vue'
import StatusPill from '@/components/StatusPill.vue'

interface DeptState { state: string; label: string }
interface AttOut { id: number; name: string }
interface BoardRow {
  id: number
  project_id: number
  code: string
  name: string
  status: 'pending' | 'shipped'
  design_files: AttOut[]
  electric_files: AttOut[]
  design_state: DeptState
  electric_state: DeptState
  produce_state: DeptState
  ship_list_files: AttOut[]
  receiver_name?: string | null
  receiver_phone?: string | null
  receiver_addr?: string | null
  ship_doc_name?: string | null
  ship_doc_id?: number | null
  can_ship: boolean
  gate_missing: string[]
}

const auth = useAuthStore()
const isMgr = computed(() => auth.isAdmin)
const loading = ref(false)
const rows = ref<BoardRow[]>([])

async function load() {
  loading.value = true
  try {
    rows.value = (await http.get<BoardRow[]>('/logistics/board')).data
  } finally {
    loading.value = false
  }
}
onMounted(load)

function stateTag(s: DeptState): 'success' | 'primary' | 'info' {
  return s.state === 'done' ? 'success' : s.state === 'doing' ? 'primary' : 'info'
}

// 收货信息编辑
const rcvVisible = ref(false)
const rcvRow = ref<BoardRow | null>(null)
const rcvForm = reactive({ name: '', phone: '', addr: '' })
function openReceiver(r: BoardRow) {
  rcvRow.value = r
  rcvForm.name = r.receiver_name || ''
  rcvForm.phone = r.receiver_phone || ''
  rcvForm.addr = r.receiver_addr || ''
  rcvVisible.value = true
}
const savingRcv = ref(false)
async function saveReceiver() {
  if (!rcvRow.value) return
  savingRcv.value = true
  try {
    await http.put(`/logistics/${rcvRow.value.id}/receiver`, { ...rcvForm })
    ElMessage.success('收货信息已保存（修改留痕）')
    rcvVisible.value = false
    await load()
  } finally { savingRcv.value = false }
}

// 确认发货
const shipVisible = ref(false)
const shipRow = ref<BoardRow | null>(null)
const shipFile = ref<File | null>(null)
const shipping = ref(false)
function openShip(r: BoardRow) {
  shipRow.value = r
  shipFile.value = null
  shipVisible.value = true
}
function pickShipFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) shipFile.value = f
}
async function confirmShip(force = false) {
  const r = shipRow.value
  if (!r) return
  if (!shipFile.value) { ElMessage.warning('请上传发货单'); return }
  shipping.value = true
  try {
    const fd = new FormData()
    fd.append('file', shipFile.value)
    fd.append('force', String(force))
    const resp = await http.post(`/logistics/${r.id}/ship`, fd)
    ElMessage.success((resp.data as any).message || '已发货')
    shipVisible.value = false
    await load()
  } finally {
    shipping.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>物流发货部</h1>
        <div class="desc">
          新项目自动建发货待办；设计/电工产物汇总，生产显示完成状态；
          <b>发货闸门：已下单任务全部完成才可发（D5）</b>；发货日期自动回传销售台账
        </div>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" stripe v-loading="loading" max-height="calc(100vh - 240px)" :scrollbar-always-on="true">
        <el-table-column label="项目" width="110" fixed>
          <template #default="{ row }"><b class="code">{{ row.code }}</b></template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
        <el-table-column label="设计资料" min-width="130">
          <template #default="{ row }">
            <el-tag v-for="f in row.design_files" :key="f.id" size="small" effect="plain"
                    class="fc" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
            <span v-if="!row.design_files.length" class="muted">暂无</span>
          </template>
        </el-table-column>
        <el-table-column label="电工资料" min-width="130">
          <template #default="{ row }">
            <el-tag v-for="f in row.electric_files" :key="f.id" size="small" effect="plain"
                    class="fc" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
            <span v-if="!row.electric_files.length" class="muted">暂无</span>
          </template>
        </el-table-column>
        <el-table-column label="生产状态" width="92">
          <template #default="{ row }">
            <StatusPill :text="row.produce_state.label" :variant="stateTag(row.produce_state)" />
          </template>
        </el-table-column>
        <el-table-column label="仓库发货清单" min-width="120">
          <template #default="{ row }">
            <el-tag v-for="f in row.ship_list_files" :key="f.id" size="small" effect="plain"
                    class="fc" @click="downloadAttachment(f)">{{ f.name }}</el-tag>
            <span v-if="!row.ship_list_files.length" class="muted">暂无</span>
          </template>
        </el-table-column>
        <el-table-column label="收货信息" min-width="240">
          <template #default="{ row }">
            <div class="rcv-cell">
              <template v-if="row.receiver_name">
                <div class="rcv-info">
                  <div class="rcv-name">{{ row.receiver_name }}</div>
                  <div class="rcv-line">
                    <el-icon class="rcv-ico"><Phone /></el-icon>{{ row.receiver_phone || '—' }}
                  </div>
                  <div class="rcv-line">
                    <el-icon class="rcv-ico"><Location /></el-icon>{{ row.receiver_addr || '—' }}
                  </div>
                </div>
                <el-tooltip content="编辑收货信息" placement="top">
                  <el-button class="rcv-edit" size="small" link :icon="Edit" @click="openReceiver(row)" />
                </el-tooltip>
              </template>
              <template v-else>
                <StatusPill text="待完善" variant="warn" />
                <el-button size="small" link type="primary" :icon="Edit" @click="openReceiver(row)">完善信息</el-button>
              </template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <StatusPill
              :text="row.status === 'shipped' ? '已发货' : (row.can_ship ? '可发货' : '待齐')"
              :variant="row.status === 'shipped' ? 'success' : (row.can_ship ? 'primary' : 'warn')" />
          </template>
        </el-table-column>
        <el-table-column label="发货闸门" width="200" fixed="right">
          <template #default="{ row }">
            <template v-if="row.status === 'shipped'">
              <el-button v-if="row.ship_doc_id" size="small" link type="success"
                         @click="downloadAttachment({ id: row.ship_doc_id!, name: row.ship_doc_name || '发货单' })">
                📎 {{ row.ship_doc_name }}
              </el-button>
            </template>
            <el-button v-else-if="row.can_ship" type="primary" size="small" :icon="Van" @click="openShip(row)">
              已发货（传发货单）
            </el-button>
            <el-tooltip v-else :content="`待部门完成：${row.gate_missing.join('、')}`" placement="top">
              <span>
                <el-button size="small" disabled :icon="Clock">待部门完成</el-button>
                <el-button v-if="isMgr" size="small" type="warning" plain @click="openShip(row)">强制</el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
      <EmptyHint v-if="!loading && !rows.length" text="暂无待发货项目" />
    </el-card>

    <!-- 收货信息 -->
    <el-dialog v-model="rcvVisible" :title="`📍 收货信息 · ${rcvRow?.code || ''}`" width="460px">
      <el-form label-position="top">
        <el-form-item label="收货人 / 单位" required><el-input v-model="rcvForm.name" /></el-form-item>
        <el-form-item label="联系电话" required><el-input v-model="rcvForm.phone" /></el-form-item>
        <el-form-item label="收货地址" required><el-input v-model="rcvForm.addr" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rcvVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingRcv" @click="saveReceiver">保存</el-button>
      </template>
    </el-dialog>

    <!-- 确认发货 -->
    <el-dialog v-model="shipVisible" :title="`📦 确认发货 · ${shipRow?.code || ''}`" width="480px">
      <el-alert v-if="shipRow?.receiver_name" type="success" :closable="false" style="margin-bottom: 12px"
                :title="`${shipRow.receiver_name} ｜ ${shipRow.receiver_phone || '—'} ｜ ${shipRow.receiver_addr || '—'}`" />
      <el-alert v-else type="warning" :closable="false" style="margin-bottom: 12px"
                title="⚠ 收货信息待完善，建议先在列表「收货信息」列填写" />
      <el-alert v-if="shipRow && !shipRow.can_ship" type="error" :closable="false" style="margin-bottom: 12px"
                :title="`闸门未通过（${shipRow.gate_missing.join('、')}未完成），管理层可强制发货`" />
      <el-form label-position="top">
        <el-form-item label="发货单（PDF/图片，必传）" required>
          <FilePicker v-model="shipFile" accept=".pdf,.jpg,.jpeg,.png" placeholder="选择发货单（PDF/JPG/PNG）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="shipVisible = false">取消</el-button>
        <el-button type="primary" :loading="shipping"
                   @click="confirmShip(shipRow ? !shipRow.can_ship : false)">
          确认已发货
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.code { color: var(--primary, #2563eb); }
.muted { color: var(--el-text-color-secondary); font-size: 12.5px; }
.fc { cursor: pointer; margin: 2px 4px 2px 0; }
.rcv { font-size: 12.5px; line-height: 1.5; }

/* 🆕 v4 收货信息 cell: 已填→紧凑姓名/电话/地址 + 右上 hover 编辑;未填→pill+完善按钮 */
.rcv-cell {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 2px 0;
  position: relative;
}
.rcv-info { flex: 1; min-width: 0; line-height: 1.55; }
.rcv-name {
  font-weight: 500;
  color: var(--el-text-color-primary);
  font-size: 13.5px;
  margin-bottom: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rcv-line {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rcv-ico {
  font-size: 12px; color: var(--text-4, #cbd5e1); flex-shrink: 0;
}
.rcv-edit {
  flex-shrink: 0;
  opacity: 0; transition: opacity .15s;
  color: var(--el-text-color-secondary);
  align-self: flex-start;
}
.rcv-edit:hover { color: var(--primary, #2563eb); }
.el-table__row:hover .rcv-edit { opacity: 1; }
</style>
