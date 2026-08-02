<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api/auth'
import type { LoginResp } from '@/types'
import { ElMessage } from 'element-plus'
import logoUrl from '@/assets/logo.png'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const showPwd = ref(false)   // 仅 UI：密码明文/密文切换
const form = reactive({ username: '', password: '' })

// 🆕 外网登录两步闸门：第一步命中闸门（gate_required）→ 第二步输 6 位码（码由管理层企微告知）。
//   桌面客户端默认免闸；但「客户端设备限制」开启后，不在名单里的机器同样会走到第二步。
const step = ref<'pwd' | 'gate'>('pwd')
const preToken = ref('')
const gateCode = ref('')

// 🆕 设备 ID：只在客户端里显示。管理层要把它录进「外网访问 → 客户端设备 ID 名单」，
//   而使用者自己在 %APPDATA% 里翻文件太难为人——被拦下时人就卡在这一页，
//   放这儿他能直接复制发给管理层。
const deviceId = window.pmsDesktop?.isDesktop ? (window.pmsDesktop.deviceId || '') : ''
const copied = ref(false)
async function copyDeviceId() {
  try {
    await navigator.clipboard.writeText(deviceId)
  } catch {
    // file:// 下 clipboard API 可能不可用，退回老办法；再失败就提示手抄
    const ta = document.createElement('textarea')
    ta.value = deviceId
    ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    if (!ok) { ElMessage.warning('复制失败，请手动选中上面的编号'); return }
  }
  copied.value = true
  ElMessage.success('设备 ID 已复制，发给管理层录入即可')
  setTimeout(() => { copied.value = false }, 2000)
}

// 🆕 记住用户名：勾选后登录成功把账号存本地，下次开页自动回填；取消勾选即清除
const remember = ref(false)
const REMEMBER_KEY = 'pms_remember_name'
const savedName = localStorage.getItem(REMEMBER_KEY)
if (savedName) { form.username = savedName; remember.value = true }

// 登录成功收尾（与 stores/auth.login 同一套持久化；闸门流程需先按响应分支，故不走 store.login）
async function finishLogin(resp: LoginResp) {
  auth.token = resp.access_token
  auth.user = resp.user
  localStorage.setItem('pms_token', resp.access_token)
  localStorage.setItem('pms_user', JSON.stringify(resp.user))
  auth.menus = null  // 切换账号清菜单缓存，登录后重新拉取
  localStorage.removeItem('pms_menus')
  await auth.fetchMenus()
  if (remember.value) localStorage.setItem(REMEMBER_KEY, form.username)
  else localStorage.removeItem(REMEMBER_KEY)
  // 🆕 每次登录成功触发客户端静默检查更新（仅桌面端；30 分钟节流，有新版静默下载后提示重启）
  window.pmsDesktop?.checkUpdateSilent?.()
  ElMessage.success('登录成功')
  router.push('/overview')
}

async function onSubmit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const resp = await authApi.login(form.username, form.password)
    if (resp.gate_required && resp.pre_token) {
      preToken.value = resp.pre_token
      gateCode.value = ''
      step.value = 'gate'
      return
    }
    await finishLogin(resp)
  } catch {
    /* 拦截器已弹错误 */
  } finally {
    loading.value = false
  }
}

async function onVerify() {
  if (!/^\d{6}$/.test(gateCode.value)) {
    ElMessage.warning('请输入 6 位数字验证码')
    return
  }
  loading.value = true
  try {
    const resp = await authApi.verifyGate(form.username, preToken.value, gateCode.value)
    await finishLogin(resp)
  } catch {
    /* 拦截器已弹错误（验证码错误/过期/锁定） */
  } finally {
    loading.value = false
  }
}

// 重新发送：用当前表单账号密码重调 login 发码（后端限频 1 条/分，超限由拦截器弹 429 提示）
async function onResend() {
  loading.value = true
  try {
    const resp = await authApi.login(form.username, form.password)
    if (resp.gate_required && resp.pre_token) {
      preToken.value = resp.pre_token
      gateCode.value = ''
      ElMessage.success(resp.message || '已重新通知管理层')
    } else {
      await finishLogin(resp)   // 闸门恰被关闭/网络变内网：直接完成登录
    }
  } catch {
    /* 拦截器已弹错误 */
  } finally {
    loading.value = false
  }
}

function backToPwd() {
  step.value = 'pwd'
  gateCode.value = ''
  preToken.value = ''
}
</script>

<template>
  <div class="lg-wrap">
    <!-- 科技感背景层 -->
    <div class="lg-bg">
      <div class="lg-grid"></div>
      <div class="lg-ring lg-ring1"></div>
      <div class="lg-ring lg-ring2"></div>
      <div class="lg-glow lg-glow1"></div>
      <div class="lg-glow lg-glow2"></div>
    </div>

    <!-- 顶部品牌 + 状态 -->
    <div class="lg-top">
      <div class="lg-brand">
        <div class="lg-logo"><img :src="logoUrl" alt="同辉智能" /></div>
        <div class="lg-brand-txt">
          <div class="lg-brand-name">同辉智能</div>
          <div class="lg-brand-sub">TONGHUI</div>
        </div>
      </div>
      <div class="lg-status">
        <span class="lg-dot"></span>智能制造 · 系统在线
      </div>
    </div>

    <!-- 毛玻璃登录卡（第一步：账号密码） -->
    <form v-if="step === 'pwd'" class="lg-card" @submit.prevent="onSubmit">
      <div class="lg-sys">同辉智能项目管理系统</div>
      <div class="lg-welcome">欢迎登录</div>
      <div class="lg-rule"></div>

      <label class="lg-label">账号</label>
      <div class="lg-field">
        <input v-model="form.username" placeholder="工号 / 手机号" autocomplete="username" />
      </div>

      <label class="lg-label">密码</label>
      <div class="lg-field">
        <input v-model="form.password" :type="showPwd ? 'text' : 'password'"
               placeholder="请输入密码" autocomplete="current-password" @keyup.enter="onSubmit" />
        <span class="lg-toggle" @click="showPwd = !showPwd">{{ showPwd ? '隐藏' : '显示' }}</span>
      </div>

      <label class="lg-remember">
        <input type="checkbox" v-model="remember" />
        <span>记住用户名</span>
      </label>

      <button class="lg-submit" type="submit" :disabled="loading">
        {{ loading ? '登 录 中…' : '登 录' }}
      </button>
    </form>

    <!-- 🆕 外网登录第二步：验证码卡（码已发管理层企微，联系管理层获取） -->
    <form v-else class="lg-card" @submit.prevent="onVerify">
      <div class="lg-sys">同辉智能项目管理系统</div>
      <div class="lg-welcome">外网登录验证</div>
      <div class="lg-rule"></div>

      <div class="lg-gate-tip">已通知管理层，请联系管理层获取验证码（10 分钟内有效）</div>

      <label class="lg-label">验证码</label>
      <div class="lg-field">
        <input v-model="gateCode" placeholder="请输入 6 位验证码" maxlength="6"
               inputmode="numeric" autocomplete="one-time-code" @keyup.enter="onVerify" />
      </div>

      <button class="lg-submit" type="submit" :disabled="loading">
        {{ loading ? '验 证 中…' : '验证并登录' }}
      </button>

      <div class="lg-gate-links">
        <span class="lg-gate-link" @click="onResend">重新发送</span>
        <span class="lg-gate-link" @click="backToPwd">返回重输</span>
      </div>
    </form>

    <!-- 🆕 设备 ID：仅客户端显示。两步都在这一层，被闸门拦下时也看得到、复制得到 -->
    <div v-if="deviceId" class="lg-dev">
      <span class="lg-dev-k">本机设备 ID</span>
      <code class="lg-dev-v">{{ deviceId }}</code>
      <button class="lg-dev-btn" type="button" @click="copyDeviceId">
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>

    <div class="lg-foot">同辉智能装备（无锡）有限公司 · 项目管理系统</div>
  </div>
</template>

<style scoped>
.lg-wrap {
  min-height: 100vh; position: relative; overflow: hidden;
  background: radial-gradient(120% 100% at 78% 12%, #1c3350 0%, #16293f 42%, #0f1d30 100%);
  font-family: 'Manrope', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
/* ===== 背景装饰 ===== */
.lg-bg { position: absolute; inset: 0; overflow: hidden; }
.lg-grid {
  position: absolute; inset: 0;
  background-image:
    linear-gradient(rgba(120,150,190,.06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(120,150,190,.06) 1px, transparent 1px);
  background-size: 46px 46px;
  mask-image: radial-gradient(120% 90% at 50% 30%, #000 40%, transparent 78%);
  -webkit-mask-image: radial-gradient(120% 90% at 50% 30%, #000 40%, transparent 78%);
}
.lg-ring {
  position: absolute; border-radius: 50%;
  border: 2px dashed rgba(200,162,79,.16);
}
.lg-ring1 { width: 520px; height: 520px; top: -160px; right: -120px; animation: lgSpin 90s linear infinite; }
.lg-ring2 { width: 380px; height: 380px; bottom: -140px; left: -90px; border-color: rgba(120,160,210,.14); animation: lgSpin 70s linear infinite reverse; }
.lg-glow { position: absolute; border-radius: 50%; filter: blur(70px); }
.lg-glow1 { width: 420px; height: 420px; top: -80px; left: 8%; background: rgba(53,96,168,.24); }
.lg-glow2 { width: 360px; height: 360px; bottom: -60px; right: 12%; background: rgba(200,162,79,.12); }
@keyframes lgSpin { to { transform: rotate(360deg); } }

/* ===== 顶部品牌/状态 ===== */
.lg-top {
  position: absolute; top: 30px; left: 40px; right: 40px; z-index: 3;
  display: flex; align-items: center; justify-content: space-between;
}
.lg-brand { display: flex; align-items: center; gap: 12px; }
.lg-logo {
  width: 46px; height: 46px; border-radius: 12px;
  background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.14);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 6px 20px rgba(0,0,0,.25);
}
.lg-logo img { width: 32px; height: 32px; object-fit: contain; }
.lg-brand-name { color: #fff; font-size: 17px; font-weight: 700; letter-spacing: .04em; line-height: 1.1; }
.lg-brand-sub { color: rgba(255,255,255,.5); font-size: 11px; letter-spacing: .28em; margin-top: 2px; }
.lg-status {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 14px; border-radius: 999px;
  background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.13);
  backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  color: #d6ddea; font-size: 12px; letter-spacing: .02em;
}
.lg-dot { width: 7px; height: 7px; border-radius: 50%; background: #4ade80; box-shadow: 0 0 8px #4ade80; }

/* ===== 毛玻璃卡片 ===== */
.lg-card {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 3;
  width: 392px; max-width: calc(100% - 48px);
  background: rgba(15, 27, 45, .62);
  backdrop-filter: blur(22px) saturate(1.1); -webkit-backdrop-filter: blur(22px) saturate(1.1);
  border: 1px solid rgba(255,255,255,.16); border-radius: 20px;
  padding: 34px 36px 30px;
  box-shadow: 0 28px 66px -26px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.14);
  animation: lgFadeIn .7s ease-out both;
}
@keyframes lgFadeIn { from { opacity: 0; transform: translate(-50%, -46%); } to { opacity: 1; transform: translate(-50%, -50%); } }
.lg-sys { font-size: 13.5px; color: #e0c98a; font-weight: 700; letter-spacing: .05em; }
.lg-welcome { font-size: 28px; font-weight: 800; color: #fff; margin-top: 8px; letter-spacing: -.01em; }
.lg-rule { height: 3px; width: 56px; background: #c8a24f; border-radius: 2px; margin: 16px 0 26px; }
.lg-label { display: block; font-size: 12px; font-weight: 600; color: rgba(255,255,255,.66); margin-bottom: 8px; }
.lg-field {
  display: flex; align-items: center; height: 48px;
  border: 1px solid rgba(255,255,255,.18); border-radius: 11px;
  padding: 0 15px; background: rgba(255,255,255,.06);
  transition: border-color .2s, box-shadow .2s, background .2s;
  margin-bottom: 18px;
}
.lg-field:focus-within {
  border-color: #c8a24f; background: rgba(255,255,255,.1);
  box-shadow: 0 0 0 4px rgba(200,162,79,.16);
}
.lg-field input {
  flex: 1; height: 100%; border: 0; outline: 0; background: transparent;
  color: #fff; font-size: 14.5px; font-family: inherit;
}
.lg-field input::placeholder { color: rgba(255,255,255,.42); }
.lg-toggle { font-size: 12.5px; color: rgba(255,255,255,.55); cursor: pointer; user-select: none; padding-left: 10px; }
.lg-toggle:hover { color: #e0c98a; }
/* 🆕 记住用户名 */
.lg-remember {
  display: flex; align-items: center; gap: 8px; margin: -6px 0 14px;
  font-size: 12.5px; color: rgba(255,255,255,.66); cursor: pointer; user-select: none;
}
.lg-remember input { accent-color: #c8a24f; width: 14px; height: 14px; cursor: pointer; }
.lg-remember:hover { color: #e0c98a; }
.lg-submit {
  width: 100%; height: 48px; margin-top: 8px; cursor: pointer;
  border: 0; border-radius: 12px;
  background: linear-gradient(180deg, #d8b45f 0%, #c8a24f 55%, #b8862f 100%);
  color: #16293f; font-size: 15px; font-weight: 700; letter-spacing: 4px;
  box-shadow: 0 10px 26px -10px rgba(200,162,79,.6);
  transition: filter .15s, transform .05s;
}
.lg-submit:hover { filter: brightness(1.06); }
.lg-submit:active { transform: translateY(1px); }
.lg-submit:disabled { opacity: .7; cursor: default; }
/* 🆕 外网登录第二步验证码卡 */
.lg-gate-tip {
  font-size: 13px; line-height: 1.7; color: rgba(255,255,255,.72);
  background: rgba(200,162,79,.1); border: 1px solid rgba(200,162,79,.28);
  border-radius: 10px; padding: 10px 14px; margin-bottom: 22px;
}
.lg-gate-links { display: flex; justify-content: space-between; margin-top: 16px; }
.lg-gate-link { font-size: 12.5px; color: rgba(255,255,255,.55); cursor: pointer; user-select: none; }
.lg-gate-link:hover { color: #e0c98a; }
.lg-foot {
  position: absolute; bottom: 22px; left: 0; right: 0; z-index: 3; text-align: center;
  color: rgba(255,255,255,.34); font-size: 12px; letter-spacing: .02em;
}
/* 🆕 设备 ID 条：坐在页脚正上方，两个登录步骤都看得到 */
.lg-dev {
  position: absolute; bottom: 50px; left: 0; right: 0; z-index: 3;
  display: flex; align-items: center; justify-content: center; gap: 10px;
  color: rgba(255,255,255,.46); font-size: 12px;
}
.lg-dev-k { letter-spacing: .02em; }
.lg-dev-v {
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 12px;
  color: rgba(255,255,255,.72); background: rgba(255,255,255,.07);
  border: 1px solid rgba(255,255,255,.12); border-radius: 6px; padding: 3px 9px;
  user-select: all;   /* 复制按钮失效时还能一键全选 */
}
.lg-dev-btn {
  background: rgba(255,255,255,.09); border: 1px solid rgba(255,255,255,.16);
  color: rgba(255,255,255,.8); border-radius: 6px; padding: 4px 12px;
  font-size: 12px; cursor: pointer; font-family: inherit;
}
.lg-dev-btn:hover { background: rgba(255,255,255,.16); }
@media (max-width: 560px) {
  .lg-top { left: 20px; right: 20px; top: 20px; }
  .lg-card { padding: 28px 24px; }
}
</style>
