<script setup lang="ts">
// 🆕 人事部一期：员工花名册(CRUD/Excel导入/到期提醒) + 部门月度工资总额(盈利改善人工分摊数据源)
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Upload, Download, ArrowDown, Search, Refresh } from '@element-plus/icons-vue'
import { http } from '@/api'
import { fmtMoney } from '@/api/sales'
import EmptyHint from '@/components/EmptyHint.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const tv = (name: string) => auth.tabVisible('hr', name)
const tab = ref('roster')

interface Dept { id: number; name: string }
interface Emp {
  id: number; emp_no?: string | null; name: string; department_id?: number | null; department_name?: string | null
  position?: string | null; hire_date?: string | null; regular_date?: string | null
  contract_end?: string | null; status: string; leave_date?: string | null
  id_card?: string | null; phone?: string | null; emergency_contact?: string | null; emergency_contact_phone?: string | null
  user_id?: number | null; user_name?: string | null; note?: string | null
}
interface RosterStats { active: number; probation: number; expiring30: number; joined_month: number; left_month: number }

const depts = ref<Dept[]>([])
async function loadDepts() {
  try { depts.value = (await http.get<Dept[]>('/oa/departments', { params: { enabled_only: true } })).data }
  catch { depts.value = [] }
}

// ===== 花名册 =====
const emps = ref<Emp[]>([])
const stats = ref<RosterStats | null>(null)
const loading = ref(false)
const fStatus = ref('')
const fDept = ref<number | ''>('')
const fKw = ref('')
async function loadEmps() {
  loading.value = true
  try {
    const r = (await http.get<{ rows: Emp[]; stats: RosterStats }>('/hr/employees', {
      params: { status: fStatus.value || undefined, department_id: fDept.value || undefined, kw: fKw.value || undefined },
    })).data
    emps.value = r.rows; stats.value = r.stats
  } finally { loading.value = false }
}
const STATUS_TAG: Record<string, any> = { 试用: 'warning', 在职: 'success', 离职: 'info' }
// 合同临期高亮:30天内(含已过期)
function contractClass(e: Emp) {
  if (e.status === '离职' || !e.contract_end) return ''
  const d = Math.floor((new Date(e.contract_end).getTime() - Date.now()) / 86400000)
  return d <= 30 ? 'danger' : ''
}

const empVisible = ref(false)
const empSaving = ref(false)
const empForm = reactive<any>({
  id: null, name: '', department_id: null, position: '', hire_date: '', regular_date: '',
  contract_end: '', status: '在职', leave_date: '', id_card: '', phone: '',
  emergency_contact: '', emergency_contact_phone: '', user_id: null, note: '',
})
function openEmp(row?: Emp) {
  if (row) Object.assign(empForm, { ...row })
  else Object.assign(empForm, { id: null, name: '', department_id: null, position: '', hire_date: '',
    regular_date: '', contract_end: '', status: '在职', leave_date: '', id_card: '', phone: '',
    emergency_contact: '', emergency_contact_phone: '', user_id: null, note: '' })
  empVisible.value = true
}
async function submitEmp() {
  if (!empForm.name.trim()) { ElMessage.warning('请填写姓名'); return }
  empSaving.value = true
  const payload = { ...empForm }
  delete payload.id; delete payload.department_name; delete payload.user_name
  try {
    if (empForm.id) await http.put(`/hr/employees/${empForm.id}`, payload)
    else await http.post('/hr/employees', payload)
    ElMessage.success('已保存')
    empVisible.value = false
    await loadEmps()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
  finally { empSaving.value = false }
}
async function deleteEmp(row: Emp) {
  try {
    await ElMessageBox.confirm(`删除 ${row.name} 的档案？仅用于录错的行——正常人员变动请改「离职」状态留痕。`,
      '删除档案', { type: 'warning', confirmButtonText: '删除' })
  } catch { return }
  try {
    await http.delete(`/hr/employees/${row.id}`)
    ElMessage.success('已删除')
    await loadEmps()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}

// Excel 导入
async function downloadEmpTemplate() {
  try {
    const res = await http.get('/hr/employees/import-template', { responseType: 'blob' })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url; a.download = '员工导入模板.xlsx'
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { ElMessage.error('模板下载失败') }
}
const empImporting = ref(false)
function importEmps() {
  const input = document.createElement('input')
  input.type = 'file'; input.accept = '.xlsx'
  input.onchange = async () => {
    const f = input.files?.[0]; if (!f) return
    const fd = new FormData(); fd.append('file', f)
    empImporting.value = true
    try {
      const r = (await http.post<{ message: string; errors: string[] }>('/hr/employees/import', fd)).data
      if (r.errors?.length) ElMessageBox.alert(r.errors.join('\n'), r.message, { type: 'warning' })
      else ElMessage.success(r.message)
      await loadEmps()
    } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '导入失败') }
    finally { empImporting.value = false }
  }
  input.click()
}

// ===== 工资总额 =====
interface PayrollRow { department_id: number; department_name: string; total_amount: number; note?: string | null }
const pMonth = ref(new Date().toISOString().slice(0, 7))
const pRows = ref<PayrollRow[]>([])
const pTotal = computed(() => pRows.value.reduce((s, r) => s + (Number(r.total_amount) || 0), 0))
const pLoading = ref(false)
async function loadPayroll() {
  pLoading.value = true
  try { pRows.value = (await http.get<{ rows: PayrollRow[] }>('/hr/payroll', { params: { month: pMonth.value } })).data.rows }
  finally { pLoading.value = false }
}
const pSaving = ref(false)
async function savePayroll() {
  pSaving.value = true
  try {
    const r: any = (await http.put(`/hr/payroll/${pMonth.value}`, {
      rows: pRows.value.map(x => ({ department_id: x.department_id, total_amount: Number(x.total_amount) || 0, note: x.note })),
    })).data
    ElMessage.success(r.message || '已保存')
    await loadSummary()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '保存失败') }
  finally { pSaving.value = false }
}
const sumYear = ref(new Date().getFullYear())
const sumRows = ref<{ month: string; total: number }[]>([])
const sumTotal = ref(0)
async function loadSummary() {
  try {
    const r = (await http.get<{ rows: any[]; total: number }>('/hr/payroll-summary', { params: { year: sumYear.value } })).data
    sumRows.value = r.rows; sumTotal.value = r.total
  } catch { sumRows.value = [] }
}
function onTab(name: string) {
  if (name === 'payroll') { loadPayroll(); loadSummary() }
}

onMounted(async () => { await loadDepts(); await loadEmps() })
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>人事部</h1>
        <div class="desc">员工花名册（合同到期前 30 天 / 试用转正前 7 天自动提醒）；部门月度工资总额（项目毛利"含人工"口径的数据源）</div>
      </div>
    </div>

    <el-card shadow="never">
      <el-tabs v-model="tab" @tab-change="onTab">
        <!-- ===== 员工花名册 ===== -->
        <el-tab-pane v-if="tv('roster')" label="👥 员工花名册" name="roster">
          <div v-if="stats" class="kpi-grid" style="margin-bottom:12px">
            <div class="kpi is-primary"><div class="kpi-v">{{ stats.active }}</div><div class="kpi-l">在职人数（含试用）</div></div>
            <div class="kpi"><div class="kpi-v">{{ stats.probation }}</div><div class="kpi-l">试用期</div></div>
            <div class="kpi"><div class="kpi-v" :class="stats.expiring30 ? 'danger' : ''">{{ stats.expiring30 }}</div><div class="kpi-l">30 天内合同到期</div></div>
            <div class="kpi"><div class="kpi-v">{{ stats.joined_month }} / {{ stats.left_month }}</div><div class="kpi-l">本月入职 / 离职</div></div>
          </div>
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
            <el-select v-model="fStatus" clearable placeholder="全部状态" style="width:110px" @change="loadEmps">
              <el-option value="试用" label="试用" /><el-option value="在职" label="在职" /><el-option value="离职" label="离职" />
            </el-select>
            <el-select v-model="fDept" clearable filterable placeholder="全部部门" style="width:140px" @change="loadEmps">
              <el-option v-for="d in depts" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
            <el-input v-model="fKw" placeholder="搜姓名/电话/岗位" clearable :prefix-icon="Search" style="width:180px" @keyup.enter="loadEmps" @clear="loadEmps" />
            <el-button :icon="Refresh" @click="loadEmps" />
            <span class="flex-spacer" style="flex:1" />
            <el-dropdown style="margin-right:8px">
              <el-button :icon="Upload" :loading="empImporting">批量导入<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item :icon="Upload" @click="importEmps">导入员工 Excel</el-dropdown-item>
                  <el-dropdown-item :icon="Download" @click="downloadEmpTemplate">下载导入模板</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button type="primary" :icon="Plus" @click="openEmp()">新增员工</el-button>
          </div>
          <el-table show-overflow-tooltip :data="emps" v-loading="loading" stripe size="small" class="compact-tbl"
                    max-height="calc(100vh - 380px)" :scrollbar-always-on="true">
            <el-table-column prop="emp_no" label="工号" width="76" fixed="left"><template #default="{ row }"><span class="code">{{ row.emp_no || '—' }}</span></template></el-table-column>
            <el-table-column prop="name" label="姓名" width="90" fixed="left"><template #default="{ row }"><b>{{ row.name }}</b></template></el-table-column>
            <el-table-column prop="department_name" label="部门" width="100"><template #default="{ row }">{{ row.department_name || '—' }}</template></el-table-column>
            <el-table-column prop="position" label="岗位" min-width="100"><template #default="{ row }">{{ row.position || '—' }}</template></el-table-column>
            <el-table-column label="状态" width="70">
              <template #default="{ row }"><el-tag size="small" :type="STATUS_TAG[row.status]" effect="plain">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="hire_date" label="入职" width="100"><template #default="{ row }">{{ row.hire_date || '—' }}</template></el-table-column>
            <el-table-column prop="regular_date" label="转正" width="100"><template #default="{ row }">{{ row.regular_date || '—' }}</template></el-table-column>
            <el-table-column label="合同到期" width="105">
              <template #default="{ row }"><b :class="contractClass(row)">{{ row.contract_end || '—' }}</b></template>
            </el-table-column>
            <el-table-column prop="phone" label="电话" width="115"><template #default="{ row }">{{ row.phone || '—' }}</template></el-table-column>
            <el-table-column prop="emergency_contact" label="紧急联系人" min-width="150"><template #default="{ row }">
              <span>{{ row.emergency_contact || '—' }}</span>
              <span v-if="row.emergency_contact_phone" style="color:var(--text-3);margin-left:6px">{{ row.emergency_contact_phone }}</span>
            </template></el-table-column>
            <el-table-column prop="user_name" label="登录账号" width="90"><template #default="{ row }">{{ row.user_name || '—' }}</template></el-table-column>
            <el-table-column prop="leave_date" label="离职日期" width="100"><template #default="{ row }">{{ row.leave_date || '—' }}</template></el-table-column>
            <el-table-column prop="note" label="备注" min-width="120"><template #default="{ row }">{{ row.note || '—' }}</template></el-table-column>
            <el-table-column label="操作" width="110" fixed="right" :show-overflow-tooltip="false">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openEmp(row)">编辑</el-button>
                <el-button size="small" link type="danger" @click="deleteEmp(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <EmptyHint v-if="!loading && !emps.length" text="暂无员工档案，点「新增员工」或「批量导入」开始" />
        </el-tab-pane>

        <!-- ===== 部门月度工资总额 ===== -->
        <el-tab-pane v-if="tv('payroll')" label="💰 工资总额" name="payroll">
          <el-alert type="info" :closable="false" style="margin-bottom:10px"
            title="只填每部门的月度工资总额（不到个人），用于经营分析里的人工成本与项目毛利「含人工」口径。部门在 OA审批-部门 里维护。" />
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
            <el-date-picker v-model="pMonth" type="month" value-format="YYYY-MM" :clearable="false" style="width:130px" @change="loadPayroll" />
            <span>本月合计 <b class="amt">¥{{ fmtMoney(pTotal) }}</b></span>
            <el-button type="primary" :loading="pSaving" @click="savePayroll">保存本月</el-button>
          </div>
          <el-row :gutter="16">
            <el-col :span="13">
              <el-table show-overflow-tooltip :data="pRows" v-loading="pLoading" stripe size="small" class="compact-tbl" max-height="calc(100vh - 340px)">
                <el-table-column prop="department_name" label="部门" min-width="110" />
                <el-table-column label="工资总额(元)" width="180">
                  <template #default="{ row }">
                    <el-input-number v-model="row.total_amount" :min="0" :precision="2" :controls="false" style="width:100%" />
                  </template>
                </el-table-column>
                <el-table-column label="备注" min-width="140">
                  <template #default="{ row }"><el-input v-model="row.note" size="small" placeholder="选填" maxlength="128" /></template>
                </el-table-column>
              </el-table>
              <EmptyHint v-if="!pLoading && !pRows.length" text="没有启用的部门——请先到 OA审批-部门 里维护" size="sm" />
            </el-col>
            <el-col :span="11">
              <div class="section-title" style="display:flex;align-items:center;gap:10px">
                年度走势
                <el-select v-model="sumYear" style="width:100px" @change="loadSummary">
                  <el-option v-for="y in [sumYear + 1, sumYear, sumYear - 1, sumYear - 2].filter((v, i, a) => a.indexOf(v) === i)" :key="y" :label="y + ' 年'" :value="y" />
                </el-select>
                <span class="muted small">年合计 ¥{{ fmtMoney(sumTotal) }}</span>
              </div>
              <el-table show-overflow-tooltip :data="sumRows" stripe size="small" class="compact-tbl" max-height="calc(100vh - 380px)">
                <el-table-column prop="month" label="月份" width="100" />
                <el-table-column label="工资总额" align="right"><template #default="{ row }">{{ row.total ? fmtMoney(row.total) : '—' }}</template></el-table-column>
              </el-table>
            </el-col>
          </el-row>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 员工弹窗 -->
    <el-dialog v-model="empVisible" :title="empForm.id ? '编辑员工' : '新增员工'" width="640px" class="v3-scroll-dialog">
      <el-form label-position="top">
        <div class="frow">
          <el-form-item label="工号" style="flex:0 0 120px">
            <el-input :model-value="empForm.emp_no || '保存后自动生成'" disabled />
          </el-form-item>
          <el-form-item label="姓名" required style="flex:1"><el-input v-model="empForm.name" maxlength="64" /></el-form-item>
          <el-form-item label="部门" style="flex:1">
            <el-select v-model="empForm.department_id" clearable filterable placeholder="选择部门" style="width:100%">
              <el-option v-for="d in depts" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="岗位" style="flex:1"><el-input v-model="empForm.position" maxlength="64" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="入职日期" style="flex:1"><el-date-picker v-model="empForm.hire_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
          <el-form-item label="转正日期(试用期满)" style="flex:1"><el-date-picker v-model="empForm.regular_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
          <el-form-item label="合同到期日" style="flex:1"><el-date-picker v-model="empForm.contract_end" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="状态" style="flex:1">
            <el-select v-model="empForm.status" style="width:100%">
              <el-option value="试用" label="试用" /><el-option value="在职" label="在职" /><el-option value="离职" label="离职" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="empForm.status === '离职'" label="离职日期" style="flex:1">
            <el-date-picker v-model="empForm.leave_date" type="date" value-format="YYYY-MM-DD" style="width:100%" />
          </el-form-item>
          <el-form-item label="电话" style="flex:1"><el-input v-model="empForm.phone" maxlength="32" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="身份证(选填,仅人事/管理层可见)" style="flex:1"><el-input v-model="empForm.id_card" maxlength="32" /></el-form-item>
        </div>
        <div class="frow">
          <el-form-item label="紧急联系人" style="flex:1"><el-input v-model="empForm.emergency_contact" maxlength="64" placeholder="姓名/关系" /></el-form-item>
          <el-form-item label="紧急联系人电话" style="flex:1"><el-input v-model="empForm.emergency_contact_phone" maxlength="32" placeholder="联系电话" /></el-form-item>
        </div>
        <el-form-item label="备注"><el-input v-model="empForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="empVisible = false">取消</el-button>
        <el-button type="primary" :loading="empSaving" @click="submitEmp">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.frow { display: flex; gap: 12px; }
.muted { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.danger { color: var(--el-color-danger); }
.amt { color: var(--el-color-primary); }
.section-title { font-weight: 600; font-size: 14px; margin: 4px 0 8px; }
</style>
