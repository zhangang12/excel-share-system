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
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isLoggedIn) return { name: 'login' }
  if (to.meta.requireAdmin && !auth.isAdmin) return { name: 'overview' }
  return true
})

export default router
