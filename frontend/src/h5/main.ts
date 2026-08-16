/**
 * H5 独立入口。构建产物走 dist-h5/，与网页端 dist/ 完全分开。
 *
 * 这里只挂 vue + vue-router 两个依赖：
 * 不引 element-plus、vxe-table、pinia，也不引桌面端的 router/store。
 * 目的就是「H5 不影响网页端和客户端」，反过来也一样——两边各自构建，互不牵连。
 *
 * 用 hash 路由：H5 由外层 nginx 以 /h5/ 前缀托管，hash 模式不需要为深链接
 * 再配一条 try_files 规则，少一处能配错的地方。
 */
import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './H5App.vue'
import { isLoggedIn } from './session'
import { notifyReady, notifyFailed } from './native'
import { tryWecomLogin } from './wecom'
import '../styles/h5-tokens.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./H5LoginView.vue'),
      meta: { public: true } },
    { path: '/', name: 'home', component: () => import('./H5HomeView.vue') },
    { path: '/chat', name: 'chat', component: () => import('./H5ChatView.vue') },
    // 🆕 #382 个人待办同步到 APP。独立一页而不是塞进首页：
    //   业务选「底部我的」而不是「首页」，意思就是别挤首页；H5 没有底部导航，
    //   于是入口放首页顶栏，意图一样落到。
    { path: '/todos', name: 'todos', component: () => import('./H5TodoView.vue') },
    { path: '/:rest(.*)', redirect: '/' },
  ],
})

/**
 * 🆕 企微静默登录只在**第一次**导航时试一次。
 *
 * ⚠️ 不能每次导航都试：code 是一次性的，第二次必然失败，还白搭一个来回。
 * ⚠️ 也不能放到 mount 之前 await：那会让所有非企微用户（绝大多数）
 *    干等一次网络往返才看到界面。放在守卫里，没 code 就立刻放行。
 */
let wecomTried = false

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  if (isLoggedIn.value) return true
  if (!wecomTried) {
    wecomTried = true
    if (await tryWecomLogin()) return true
  }
  return { name: 'login' }
})

const app = createApp(App)

/**
 * 启动失败上报。**只在报平安之前有效**（判断在 native.ts 里）——
 * 起来之后某个组件抛异常是业务 bug，不该把整个前端包回滚掉。
 */
app.config.errorHandler = (err) => {
  notifyFailed(String((err as any)?.message || err))
  console.error(err)
}

// mount 保持原样立即执行：这条路径是线上跑通过的，不为了加回执去改启动时序。
app.use(router)
app.mount('#app')

/**
 * 热更新回执：**等路由真的解析完**再报平安。
 *
 * mount 返回不代表页面可用 —— 异步路由组件还没加载。一个 chunk 缺失的坏包
 * 照样能 mount 成功然后白屏。isReady 兑现才说明 JS 解析了、Vue 起来了、
 * 首个路由组件也拿到了。
 *
 * 收不到这一声，壳会在下次启动**自动退回上一个好包**（见 native.ts / PmsUpdater）。
 */
router.isReady().then(
  () => notifyReady(),
  (err) => notifyFailed(String(err?.message || err)),
)
