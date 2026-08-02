<script setup lang="ts">
// 🆕 外网登录闸门配置（admin/manager）：开关 + 内网 IP/网段名单（存后端 app_settings，保存即生效）
import { ref, onMounted } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { gateApi } from '@/api/gate'

const loading = ref(false)
const saving = ref(false)
const enabled = ref(true)
const cidrs = ref<string[]>([])
const deviceGate = ref(false)
const deviceIds = ref<string[]>([])

async function load() {
  loading.value = true
  try {
    const res = await gateApi.get()
    enabled.value = res.enabled
    cidrs.value = [...res.cidrs]
    deviceGate.value = !!res.device_gate
    deviceIds.value = [...(res.device_ids ?? [])]
  } finally { loading.value = false }
}
onMounted(load)

function addRow() { cidrs.value.push('') }
function removeRow(i: number) { cidrs.value.splice(i, 1) }
function addDeviceRow() { deviceIds.value.push('') }
function removeDeviceRow(i: number) { deviceIds.value.splice(i, 1) }

async function save() {
  const list = cidrs.value.map((s) => s.trim()).filter(Boolean)
  const dev = [...new Set(deviceIds.value.map((s) => s.trim()).filter(Boolean))]
  // 开着设备闸就是要限制机器：名单外的客户端全部要走验证码，而码只发给管理层两个人。
  // 名单空着还开开关 = 所有人都要码——这是字面语义，但必须让他明确知道再保存。
  // （admin 恒免闸，不会锁死没救）
  if (deviceGate.value) {
    const msg = dev.length
      ? `保存后，只有名单里这 ${dev.length} 台机器的客户端免验证码，其它机器装了客户端也要走验证码。确认名单收齐了？`
      : '设备闸已打开但名单是空的，保存后【所有人】用客户端登录都要验证码。确定要这样？'
    try {
      await ElMessageBox.confirm(msg, '限制客户端登录设备',
        { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '再看看' })
    } catch { return }   // 点了取消
  }
  saving.value = true
  try {
    const res = await gateApi.save({
      enabled: enabled.value, cidrs: list,
      device_gate: deviceGate.value, device_ids: dev,
    })
    cidrs.value = [...res.cidrs]
    deviceGate.value = !!res.device_gate
    deviceIds.value = [...(res.device_ids ?? [])]
    ElMessage.success('已保存，即时生效')
  } catch { /* 拦截器已弹错误 */ } finally { saving.value = false }
}
</script>

<template>
  <div>
    <div class="page-header">
      <div>
        <h1>外网访问</h1>
        <div class="desc">浏览器外网登录需输入验证码（随机码自动发到管理层企业微信，由管理层告知本人）</div>
      </div>
      <div class="spacer"></div>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </div>

    <el-card shadow="never" style="margin-bottom:14px" v-loading="loading">
      <template #header><span>闸门开关</span></template>
      <div class="row">
        <el-switch v-model="enabled" active-text="已启用" inactive-text="已关闭" />
        <span class="hint">关闭后所有登录均不需要验证码</span>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-bottom:14px" v-loading="loading">
      <template #header>
        <div class="cidr-head">
          <span>内网 IP / 网段（名单内登录免验证码）</span>
          <el-button size="small" :icon="Plus" @click="addRow">添加</el-button>
        </div>
      </template>
      <template v-if="cidrs.length">
        <div v-for="(_, i) in cidrs" :key="i" class="cidr-row">
          <el-input v-model="cidrs[i]" placeholder="如 192.168.0.0/16 或 10.8.0.12" clearable />
          <el-button size="small" type="danger" plain @click="removeRow(i)">删除</el-button>
        </div>
      </template>
      <div v-else class="hint">暂无内网名单——所有来源 IP 均按外网处理（浏览器登录都需验证码）</div>
      <div class="hint" style="margin-top:10px">每行一个：支持单 IP（如 10.8.0.12）或 CIDR 网段（如 192.168.0.0/16）；非法条目会被忽略</div>
    </el-card>

    <el-card shadow="never" style="margin-bottom:14px" v-loading="loading">
      <template #header><span>客户端设备限制</span></template>
      <div class="row">
        <el-switch v-model="deviceGate" active-text="已启用" inactive-text="已关闭" />
        <span class="hint">
          关闭时：装了客户端就免验证码（当前行为）。开启后：设备 ID 要在下面名单里才免验证码
        </span>
      </div>
    </el-card>

    <el-card shadow="never" style="margin-bottom:14px" v-loading="loading">
      <template #header>
        <div class="cidr-head">
          <span>客户端设备 ID 名单（{{ deviceIds.length }} 台）</span>
          <el-button size="small" :icon="Plus" @click="addDeviceRow">添加</el-button>
        </div>
      </template>
      <template v-if="deviceIds.length">
        <div v-for="(_, i) in deviceIds" :key="i" class="cidr-row">
          <el-input v-model="deviceIds[i]" class="dev-input"
                    placeholder="粘贴设备 ID，如 3f2a9c14-8b7d-4e05-9a61-c2d83f0e7b45" clearable />
          <el-button size="small" type="danger" plain @click="removeDeviceRow(i)">删除</el-button>
        </div>
      </template>
      <div v-else class="hint">名单为空。此时若开启上面的开关，所有人用客户端登录都要验证码</div>
      <div class="hint" style="margin-top:10px">
        设备 ID 由使用者自己提供：<b>客户端登录页底部会显示并可一键复制</b>，让他发给你粘贴到这里。
        一台机器一行，重复的会自动去重。
      </div>
      <div class="hint" style="margin-top:6px">
        重装客户端、换网络、换 IP 都<b>不会</b>改变设备 ID；换电脑或重装系统才会变，届时需重新录入
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header><span>说明</span></template>
      <ul class="notes">
        <li>只卡「浏览器 + 外网 IP」登录：验证密码后还需输入 6 位验证码（10 分钟内有效），通过才发登录凭证</li>
        <li>免闸：桌面客户端（设备限制开启后还要设备 ID 在名单里）、内网名单 IP；回环/私网地址（127.x、10.x、172.16-31.x、192.168.x）恒按内网处理</li>
        <li><b>admin 角色恒免闸</b>——设备名单填错把大家挡在门外时，用 admin 登录进来改回即可</li>
        <li>设备限制只影响<b>客户端</b>；浏览器登录一直是走上面那道外网闸门，不看设备 ID</li>
        <li>管理层（manager）本人外网登录同样要过闸；验证码发到管理层角色池的站内消息 + 企业微信（绑定了企微即可收到），由管理层核实身份后告知本人</li>
        <li>同一账号发码间隔 1 条/分钟；重发新码后旧码自动作废</li>
      </ul>
    </el-card>
  </div>
</template>

<style scoped>
.row { display: flex; align-items: center; gap: 14px; }
.hint { font-size: 12.5px; color: var(--el-text-color-secondary); }
.cidr-head { display: flex; align-items: center; justify-content: space-between; }
.cidr-row { display: flex; gap: 10px; margin-bottom: 10px; }
.cidr-row .el-input { max-width: 360px; }
/* UUID 是 36 字符，360px 显示不全，看不全就没法核对 */
.cidr-row .dev-input { max-width: 420px; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.notes { margin: 0; padding-left: 18px; font-size: 13px; color: var(--el-text-color-regular); line-height: 2; }
</style>
