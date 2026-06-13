import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      redirect: '/overview',
      children: [
        {
          path: 'projects',
          name: 'projects',
          component: () => import('@/views/ProjectsView.vue'),
        },
        {
          path: 'overview',
          name: 'overview',
          component: () => import('@/views/OverviewView.vue'),
        },
        {
          path: 'projects/:id',
          name: 'project-detail',
          component: () => import('@/views/ProjectDetailView.vue'),
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: () => import('@/views/admin/UsersView.vue'),
          // 用户管理对 admin 和 manager 开放（admin 用户已被后端过滤掉）
        },
        {
          path: 'admin/permissions',
          name: 'admin-perms',
          component: () => import('@/views/admin/PermissionMatrixView.vue'),
          // admin 和 manager 都能访问，不用 requireAdmin
        },
        {
          path: 'admin/audit',
          name: 'admin-audit',
          component: () => import('@/views/admin/AuditView.vue'),
          // admin 和 manager 都能访问（后端 /api/admin/audit 用 require_admin_or_manager）
        },
        // ===== 🆕 v3 增量模块路由（菜单可见性由后端 /api/auth/menus 决定，meta.menuKey 守卫） =====
        {
          path: 'messages',
          name: 'messages',
          component: () => import('@/views/MessagesView.vue'),
          meta: { menuKey: 'messages' },
        },
        {
          path: 'admin/wxbind',
          name: 'wxbind',
          component: () => import('@/views/admin/WxBindView.vue'),
          meta: { menuKey: 'wxbind' },
        },
        // 🆕 部门工作台（设计/电工/生产共用一个视图，按路由名区分部门）
        ...(['design', 'electric', 'produce'] as string[]).map((key) => ({
          path: `dept/${key}`,
          name: key,
          component: () => import('@/views/DeptWorkbenchView.vue'),
          meta: { menuKey: key },
        })),
        {
          path: 'sales',
          name: 'sales',
          component: () => import('@/views/SalesView.vue'),
          meta: { menuKey: 'sales' },
        },
        {
          path: 'logistics',
          name: 'logistics',
          component: () => import('@/views/LogisticsView.vue'),
          meta: { menuKey: 'logistics' },
        },
        {
          path: 'sheet',
          name: 'sheet',
          component: () => import('@/views/SheetMetalView.vue'),
          meta: { menuKey: 'sheet' },
        },
        {
          path: 'purchase',
          name: 'purchase',
          component: () => import('@/views/PurchaseView.vue'),
          meta: { menuKey: 'purchase' },
        },
        {
          path: 'finance',
          name: 'finance',
          component: () => import('@/views/FinanceView.vue'),
          meta: { menuKey: 'finance' },
        },
        {
          path: 'aftersales',
          name: 'aftersales',
          component: () => import('@/views/AfterSalesView.vue'),
          meta: { menuKey: 'aftersales' },
        },
        {
          path: 'report',
          name: 'report',
          component: () => import('@/views/ReportView.vue'),
          meta: { menuKey: 'report' },
        },
        {
          path: 'warehouse',
          name: 'warehouse',
          component: () => import('@/views/WarehouseView.vue'),
          meta: { menuKey: 'warehouse' },
        },
        // 以下模块按开发顺序逐个落地，未实现前为占位页
        ...([
          ['approve', '导出审批'],
        ] as [string, string][]).map(([key, title]) => ({
          path: key,
          name: key,
          component: () => import('@/views/PlaceholderView.vue'),
          props: { title },
          meta: { menuKey: key },
        })),
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isLoggedIn) return { name: 'login' }
  if (to.meta.requireAdmin && !auth.isAdmin) return { name: 'overview' }
  // 🆕 v3 菜单级守卫：菜单已加载且目标菜单不可见 → 跳第一个可见菜单
  const mk = to.meta.menuKey as string | undefined
  if (mk && auth.menus !== null && !auth.hasMenu(mk)) {
    return fallbackRoute(auth)
  }
  // 🆕 v3 详单闸门：无详单权限不可进项目详情/详单页
  if ((to.name === 'project-detail' || to.name === 'projects')
      && auth.menus !== null && !auth.canViewDetail) {
    return fallbackRoute(auth)
  }
  return true
})

// 无权访问时跳到第一个可见菜单（仿原型 render 兜底）
function fallbackRoute(auth: ReturnType<typeof useAuthStore>) {
  const first = auth.menus?.[0]?.key
  if (!first) return { name: 'overview' }
  if (first === 'catalog') return { name: 'overview' }
  if (first === 'list') return { name: 'projects' }
  return { name: first }
}

export default router
