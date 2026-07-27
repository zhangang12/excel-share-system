<script setup lang="ts">
// 🆕 外网登录闸门配置（admin/manager）：开关 + 内网 IP/网段名单（存后端 app_settings，保存即生效）
import { ref, onMounted } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { gateApi } from '@/api/gate'

const loading = ref(false)
const saving = ref(false)
const enabled = ref(true)
const cidrs = ref<string[]>([])

async function load() {
  loading.value = true
  try {
    const res = await gateApi.get()
    enabled.value = res.enabled
    cidrs.value = [...res.cidrs]
  } finally { loading.value = false }
}
onMounted(load)

function addRow() { cidrs.value.push('') }
function removeRow(i: number) { cidrs.value.splice(i, 1) }

async function save() {
  const list = cidrs.value.map((s) => s.trim()).filter(Boolean)
  saving.value = true
  try {
    const res = await gateApi.save({ enabled: enabled.value, cidrs: list })
    cidrs.value = [...res.cidrs]
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

    <el-card shadow="never">
      <template #header><span>说明</span></template>
      <ul class="notes">
        <li>只卡「浏览器 + 外网 IP」登录：验证密码后还需输入 6 位验证码（10 分钟内有效），通过才发登录凭证</li>
        <li>免闸：桌面客户端、admin 账号、内网名单 IP；回环/私网地址（127.x、10.x、172.16-31.x、192.168.x）恒按内网处理</li>
        <li>管理层（manager）本人外网登录同样要过闸；验证码发到管理层角色池的站内消息 + 企业微信（绑定了企微即可收到），由管理层核实身份后告知本人</li>
        <li>同一账号限频 1 条/分钟、10 条/天；连续错 5 次锁定，重发新码后旧码自动作废</li>
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
.notes { margin: 0; padding-left: 18px; font-size: 13px; color: var(--el-text-color-regular); line-height: 2; }
</style>
