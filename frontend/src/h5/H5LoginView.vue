<script setup lang="ts">
/**
 * H5 登录：两步。第一步账号密码，命中外网闸门则转第二步输 6 位验证码。
 * 后端一行没改，走的还是 /auth/login + /auth/login/verify-gate。
 *
 * H5 必然是「浏览器 + 外网」，所以每次登录都会走到第二步——这是用户拍板保留的，
 * 不给 H5 开免闸口子（闸门正是为外网这个场景设的）。
 */
import { ref, reactive, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { http, errText } from './http'
import { setSession, type H5User } from './session'

interface LoginResp { access_token: string; user: H5User }
interface LoginResult extends Partial<LoginResp> {
  gate_required?: boolean; pre_token?: string; message?: string
}

const router = useRouter()

const REMEMBER_KEY = 'pms_h5_remember'

const step = ref<1 | 2>(1)
// 记住的只有用户名。密码一个字都不存——「记住我」延长的是服务端令牌有效期(30 天)，
// 不是把密码缓存在手机上。手机丢了点「退出」即失效。
const form = reactive({ username: localStorage.getItem(REMEMBER_KEY) || '', password: '' })
const remember = ref(!!localStorage.getItem(REMEMBER_KEY))
const showPwd = ref(false)
const preToken = ref('')
const digits = ref<string[]>(['', '', '', '', '', ''])
const boxRefs = ref<HTMLInputElement[]>([])
const busy = ref(false)
const err = ref('')

async function finishLogin(resp: LoginResp) {
  setSession(resp.access_token, resp.user)
  if (remember.value) localStorage.setItem(REMEMBER_KEY, form.username)
  else localStorage.removeItem(REMEMBER_KEY)
  await router.replace('/')
}

async function submit() {
  if (busy.value || !form.username || !form.password) return
  busy.value = true; err.value = ''
  try {
    const { data: resp } = await http.post<LoginResult>('/auth/login',
      { username: form.username, password: form.password, remember: remember.value })
    if (resp.gate_required && resp.pre_token) {
      preToken.value = resp.pre_token
      step.value = 2
      await nextTick(); boxRefs.value[0]?.focus()
    } else {
      await finishLogin(resp as LoginResp)
    }
  } catch (e: any) {
    err.value = errText(e, '登录失败')
  } finally { busy.value = false }
}

function onDigit(i: number, e: Event) {
  const el = e.target as HTMLInputElement
  const v = el.value.replace(/\D/g, '')
  if (v.length > 1) {
    // 从企微复制过来的整串码：拆开自动填满，别让人一位一位敲
    v.split('').slice(0, 6 - i).forEach((ch, k) => { digits.value[i + k] = ch })
    boxRefs.value[Math.min(5, i + v.length)]?.focus()
  } else {
    digits.value[i] = v
    if (v) boxRefs.value[i + 1]?.focus()
  }
  el.value = digits.value[i] || ''
  if (digits.value.every((d) => d)) verify()
}

function onBack(i: number, e: KeyboardEvent) {
  if (e.key === 'Backspace' && !digits.value[i] && i > 0) boxRefs.value[i - 1]?.focus()
}

async function verify() {
  const code = digits.value.join('')
  if (busy.value || code.length < 6) return
  busy.value = true; err.value = ''
  try {
    const { data } = await http.post<LoginResp>('/auth/login/verify-gate',
      { username: form.username, pre_token: preToken.value, code, remember: remember.value })
    await finishLogin(data)
  } catch (e: any) {
    err.value = errText(e, '验证码不正确')
    digits.value = ['', '', '', '', '', '']
    await nextTick(); boxRefs.value[0]?.focus()
  } finally { busy.value = false }
}

async function resend() {
  if (busy.value) return
  busy.value = true; err.value = ''
  try {
    const { data: resp } = await http.post<LoginResult>('/auth/login',
      { username: form.username, password: form.password, remember: remember.value })
    if (resp.pre_token) preToken.value = resp.pre_token
    err.value = '新验证码已发出，旧码作废'
  } catch (e: any) {
    err.value = errText(e, '发送失败')
  } finally { busy.value = false }
}
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <!-- 应用图标：渐变 + 光晕，跟设计稿一致 -->
      <div class="orb-wrap">
        <div class="orb-glow"></div>
        <div class="orb">
          <svg width="30" height="30" viewBox="0 0 20 20" fill="none">
            <path d="M10 2.5c.9 4.2 3.3 6.6 7.5 7.5-4.2.9-6.6 3.3-7.5 7.5-.9-4.2-3.3-6.6-7.5-7.5 4.2-.9 6.6-3.3 7.5-7.5Z" fill="#fff"/>
          </svg>
        </div>
      </div>

      <template v-if="step === 1">
        <h1>登录</h1>
        <p class="sub">同辉项目管理 · AI 助手</p>
        <div class="fields">
          <label class="row">
            <span class="k">账号</span>
            <input v-model.trim="form.username" class="v" autocomplete="username"
                   placeholder="用户名" @keyup.enter="submit" />
          </label>
          <label class="row">
            <span class="k">密码</span>
            <input v-model="form.password" class="v" :type="showPwd ? 'text' : 'password'"
                   autocomplete="current-password" placeholder="密码" @keyup.enter="submit" />
            <button class="eye" type="button" @click="showPwd = !showPwd">
              {{ showPwd ? '隐藏' : '显示' }}
            </button>
          </label>
        </div>
        <label class="remember">
          <input type="checkbox" v-model="remember" />
          <span>30 天内免登录</span>
        </label>
        <p v-if="err" class="err">{{ err }}</p>
        <button class="h5-btn" :disabled="busy" @click="submit">
          {{ busy ? '登录中…' : '继续' }}
        </button>
        <p class="foot">外网环境登录需企业微信验证码二次验证</p>
      </template>

      <template v-else>
        <h1>输入验证码</h1>
        <p class="sub">检测到外网登录，需二次验证</p>
        <div class="otp">
          <input v-for="(_, i) in 6" :key="i" :ref="(el) => { if (el) boxRefs[i] = el as HTMLInputElement }"
                 class="box" :class="{ cur: digits[i] }" inputmode="numeric" maxlength="6"
                 :value="digits[i]" @input="onDigit(i, $event)" @keydown="onBack(i, $event)" />
        </div>
        <div class="tip">
          <span class="ti">i</span>
          <span>6 位验证码已通过<b>企业微信</b>发给管理层，10 分钟内有效，可复制后粘贴自动填入</span>
        </div>
        <p v-if="err" class="err">{{ err }}</p>
        <button class="h5-btn" :disabled="busy" @click="verify">
          {{ busy ? '验证中…' : '验证并进入' }}
        </button>
        <div class="links">
          <button class="lk" @click="resend">重新发送</button>
          <button class="lk plain" @click="step = 1; err = ''">返回上一步</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh; min-height: 100dvh;
  background: var(--h5-bg); font-family: var(--h5-font);
  display: flex; align-items: center; justify-content: center; padding: 24px 18px;
}
.panel {
  width: 100%; max-width: 360px; text-align: center;
  background: var(--h5-panel); background-image: var(--h5-screen-wash), var(--h5-panel);
  border-radius: var(--h5-r-panel); box-shadow: var(--h5-sh-panel);
  padding: 40px 24px 30px;
}
.orb-wrap { position: relative; width: 64px; height: 64px; margin: 0 auto 22px }
.orb {
  position: relative; z-index: 1; width: 64px; height: 64px; border-radius: 50%;
  background: var(--h5-grad-orb); box-shadow: var(--h5-sh-orb);
  display: grid; place-items: center; animation: h5OrbFloat 4s ease-in-out infinite;
}
.orb-glow {
  position: absolute; inset: -14px; border-radius: 50%;
  background: radial-gradient(circle, rgba(76, 141, 255, .42), rgba(76, 141, 255, 0) 70%);
  animation: h5GlowPulse 3.2s ease-in-out infinite;
}
h1 { margin: 0; font-size: 22px; font-weight: 700; color: var(--h5-ink); letter-spacing: .3px }
.sub { margin: 6px 0 22px; font-size: 12.5px; color: var(--h5-ink-3) }

.fields {
  background: rgba(255, 255, 255, .6); border: 1px solid rgba(255, 255, 255, .85);
  border-radius: var(--h5-r-card); overflow: hidden; text-align: left;
}
.row { display: flex; align-items: center; gap: 12px; padding: 13px 15px; min-height: 48px }
.row + .row { border-top: 1px solid rgba(24, 32, 50, .06) }
.k { width: 40px; flex: none; font-size: 13px; color: var(--h5-ink-3) }
.v {
  flex: 1; min-width: 0; border: 0; background: transparent; outline: none;
  font: 500 14px/1.4 var(--h5-font); color: var(--h5-ink);
}
.v::placeholder { color: var(--h5-ink-4); font-weight: 400 }
.eye { border: 0; background: none; color: var(--h5-ink-3); font-size: 11.5px; cursor: pointer }

.otp { display: flex; gap: 8px; justify-content: center; margin-bottom: 16px }
.box {
  width: 44px; height: 54px; text-align: center; border-radius: var(--h5-r-card);
  border: 1px solid rgba(255, 255, 255, .85); background: rgba(255, 255, 255, .6);
  font: 700 22px/1 var(--h5-font); color: var(--h5-ink); outline: none;
  font-variant-numeric: tabular-nums;
}
.box.cur { border-color: var(--h5-blue); box-shadow: 0 0 0 2px rgba(43, 110, 246, .18) }

.tip {
  display: flex; gap: 9px; text-align: left; border-radius: var(--h5-r-card);
  background: rgba(255, 255, 255, .55); border: 1px solid rgba(255, 255, 255, .75);
  padding: 12px 14px; font-size: 12px; line-height: 1.65; color: var(--h5-ink-2);
  margin-bottom: 18px;
}
.tip b { color: var(--h5-blue); font-weight: 600 }
.ti {
  flex: none; width: 16px; height: 16px; border-radius: 50%; background: var(--h5-blue);
  color: #fff; display: grid; place-items: center; font-size: 11px; font-weight: 700;
}
.remember {
  display: flex; align-items: center; gap: 8px; margin-top: 14px;
  font-size: 12.5px; color: var(--h5-ink-2); cursor: pointer;
}
.remember input { width: 17px; height: 17px; accent-color: var(--h5-blue); margin: 0 }
.err {
  margin: 18px 0 0; font-size: 12px; color: var(--h5-danger);
  background: rgba(196, 54, 47, .09); border-radius: 10px; padding: 9px 12px; text-align: left;
}
.h5-btn { margin-top: 16px }
/* 错误提示出现时它自己顶了 18px，按钮就不再重复留白 */
.err + .h5-btn { margin-top: 10px }
.h5-btn:disabled { opacity: .6; cursor: not-allowed }
.foot { margin: 12px 0 0; font-size: 11.5px; color: var(--h5-ink-4) }
.links { display: flex; gap: 18px; justify-content: center; margin-top: 14px }
.lk { border: 0; background: none; font-size: 12.5px; color: var(--h5-blue); cursor: pointer }
.lk.plain { color: var(--h5-ink-3) }
</style>
