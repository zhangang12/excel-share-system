<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ElMessage } from 'element-plus'
import logoUrl from '@/assets/logo.png'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const showPwd = ref(false)   // 仅 UI：密码明文/密文切换
const form = reactive({ username: '', password: '' })

async function onSubmit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    router.push('/overview')
  } catch {
    /* */
  } finally {
    loading.value = false
  }
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

    <!-- 毛玻璃登录卡 -->
    <form class="lg-card" @submit.prevent="onSubmit">
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

      <button class="lg-submit" type="submit" :disabled="loading">
        {{ loading ? '登 录 中…' : '登 录' }}
      </button>
    </form>

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
.lg-foot {
  position: absolute; bottom: 22px; left: 0; right: 0; z-index: 3; text-align: center;
  color: rgba(255,255,255,.34); font-size: 12px; letter-spacing: .02em;
}
@media (max-width: 560px) {
  .lg-top { left: 20px; right: 20px; top: 20px; }
  .lg-card { padding: 28px 24px; }
}
</style>
