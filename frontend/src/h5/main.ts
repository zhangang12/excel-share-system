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
import '../styles/h5-tokens.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./H5LoginView.vue'),
      meta: { public: true } },
    { path: '/', name: 'chat', component: () => import('./H5ChatView.vue') },
    { path: '/:rest(.*)', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  return isLoggedIn.value ? true : { name: 'login' }
})

createApp(App).use(router).mount('#app')
