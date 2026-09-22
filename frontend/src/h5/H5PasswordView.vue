<script setup lang="ts">
/**
 * H5 修改密码。
 *
 * 🆕 2026-09-23 智能体推广到全公司时补的。以前 APP 里**根本没有改密码的地方** ——
 * 后端一直有 `password_must_change`（管理员建账号、重置密码后都会置 True），
 * 网页端会拦着让你改，而手机端既不拦也没页面：一线工人手机上用着初始密码，
 * 想改也只能去找电脑。26 个账号全量推开之前必须补上这一页。
 *
 * 两种进入方式：
 *   · 首页「···」菜单主动进来改（`force=false`）；
 *   · 登录后 `password_must_change` 为真，被路由守卫强制送进来（`force=true`）——
 *     这时不给「返回」，只给「退出登录」，否则守卫会和返回键互相打架。
 *
 * 后端一行没改，走的还是 POST /auth/change-password。
 */
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { http, errText } from './http'
import { clearSession, user } from './session'
import { toast } from './ui'

const route = useRoute()
const router = useRouter()

/** 强制改密：登录返回 password_must_change=true 时由守卫带上。 */
const force = computed(() => route.query.force === '1')

const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const showPwd = ref(false)
const busy = ref(false)
const err = ref('')

// 口径与后端 schemas.ChangePasswordIn 一致（min_length=6）。
// 前端先挡一道只是为了少一次往返，**不是**把校验搬到前端——后端那道仍在。
const MIN = 6

function localCheck(): string {
  if (!oldPwd.value) return '先填原密码'
  if (newPwd.value.length < MIN) return `新密码至少 ${MIN} 位`
  if (newPwd.value === oldPwd.value) return '新密码不能和原密码一样'
  if (newPwd.value !== newPwd2.value) return '两次输入的新密码不一致'
  return ''
}

async function submit() {
  err.value = localCheck()
  if (err.value) return
  busy.value = true
  try {
    await http.post('/auth/change-password',
      { old_password: oldPwd.value, new_password: newPwd.value })
    // 本地那份用户信息里的标记也要清掉，否则守卫会把人又送回这一页
    try {
      const u = JSON.parse(localStorage.getItem('pms_user') || 'null')
      if (u) {
        u.password_must_change = false
        localStorage.setItem('pms_user', JSON.stringify(u))
      }
    } catch { /* 存储不可用时不影响改密本身 */ }
    toast('密码已修改', 'ok')
    router.replace('/')
  } catch (e: any) {
    err.value = errText(e, '修改失败')
  } finally {
    busy.value = false
  }
}

function quit() {
  clearSession()
  router.replace('/login')
}
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <h1>{{ force ? '请先设置新密码' : '修改密码' }}</h1>
      <p class="sub">{{ user?.full_name || user?.username || '' }}</p>

      <div v-if="force" class="tip">
        <span class="ti">!</span>
        <span>你的账号还在用管理员给的<b>初始密码</b>，先改一个只有你自己知道的再用。</span>
      </div>

      <div class="fields">
        <label class="row">
          <span class="k">原密码</span>
          <input v-model="oldPwd" class="v" :type="showPwd ? 'text' : 'password'"
                 autocomplete="current-password" placeholder="管理员给你的那个" />
        </label>
        <label class="row">
          <span class="k">新密码</span>
          <input v-model="newPwd" class="v" :type="showPwd ? 'text' : 'password'"
                 autocomplete="new-password" :placeholder="`至少 ${MIN} 位`" />
        </label>
        <label class="row">
          <span class="k">再输一次</span>
          <input v-model="newPwd2" class="v" :type="showPwd ? 'text' : 'password'"
                 autocomplete="new-password" placeholder="和上面一致"
                 @keyup.enter="submit" />
        </label>
      </div>

      <label class="remember">
        <input type="checkbox" v-model="showPwd" />
        <!-- 手机上打密码最容易错，给个明文开关比让人反复重试强 -->
        <span>显示密码</span>
      </label>

      <p v-if="err" class="err">{{ err }}</p>
      <button class="h5-btn" :disabled="busy" @click="submit">
        {{ busy ? '提交中…' : '确认修改' }}
      </button>

      <div class="links">
        <button v-if="!force" class="lk plain" @click="router.back()">返回</button>
        <button v-else class="lk plain" @click="quit">退出登录</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 样式与登录页同源（同一套设计令牌），刻意不抽公共组件：
   两页加起来就这点 CSS，抽出去反而多一个要同步的地方。 */
.wrap {
  min-height: 100vh; min-height: 100dvh;
  background: var(--h5-bg); font-family: var(--h5-font);
  display: flex; align-items: center; justify-content: center; padding: 24px 18px;
}
.panel {
  width: 100%; max-width: 360px; text-align: center;
  background: var(--h5-panel); background-image: var(--h5-screen-wash), var(--h5-panel);
  border-radius: var(--h5-r-panel); box-shadow: var(--h5-sh-panel);
  padding: 34px 24px 26px;
}
h1 { margin: 0; font-size: 21px; font-weight: 700; color: var(--h5-ink); letter-spacing: .3px }
.sub { margin: 6px 0 20px; font-size: 12.5px; color: var(--h5-ink-3) }
.tip {
  display: flex; gap: 9px; text-align: left; border-radius: var(--h5-r-card);
  background: rgba(169, 106, 8, .08); border: 1px solid rgba(169, 106, 8, .18);
  padding: 12px 14px; font-size: 12px; line-height: 1.65; color: var(--h5-ink-2);
  margin-bottom: 16px;
}
.tip b { color: var(--h5-warn); font-weight: 600 }
.ti {
  flex: none; width: 16px; height: 16px; border-radius: 50%; background: var(--h5-warn);
  color: #fff; display: grid; place-items: center; font-size: 11px; font-weight: 700;
}
.fields {
  background: rgba(255, 255, 255, .6); border: 1px solid rgba(255, 255, 255, .85);
  border-radius: var(--h5-r-card); overflow: hidden; text-align: left;
}
.row { display: flex; align-items: center; gap: 12px; padding: 13px 15px; min-height: 48px }
.row + .row { border-top: 1px solid rgba(24, 32, 50, .06) }
.k { width: 58px; flex: none; font-size: 13px; color: var(--h5-ink-3) }
.v {
  flex: 1; min-width: 0; border: 0; background: transparent; outline: none;
  font: 500 14px/1.4 var(--h5-font); color: var(--h5-ink);
}
.v::placeholder { color: var(--h5-ink-4); font-weight: 400 }
.remember {
  display: flex; align-items: center; gap: 8px; margin-top: 14px;
  font-size: 12.5px; color: var(--h5-ink-2); cursor: pointer;
}
.remember input { width: 17px; height: 17px; accent-color: var(--h5-blue); margin: 0 }
.err {
  margin: 16px 0 0; font-size: 12px; color: var(--h5-danger);
  background: rgba(196, 54, 47, .09); border-radius: 10px; padding: 9px 12px; text-align: left;
}
.h5-btn { margin-top: 16px }
.err + .h5-btn { margin-top: 10px }
.h5-btn:disabled { opacity: .6; cursor: not-allowed }
.links { display: flex; gap: 18px; justify-content: center; margin-top: 14px }
.lk { border: 0; background: none; font-size: 12.5px; color: var(--h5-blue); cursor: pointer }
.lk.plain { color: var(--h5-ink-3) }
</style>
