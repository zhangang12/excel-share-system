<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import {
  Fold, Expand,
  FolderOpened, User, OfficeBuilding, Document, DataLine,
  Key, SwitchButton, Grid, Lock,
  Suitcase, EditPen, Lightning, SetUp, Scissor, ShoppingCart,
  Box, Van, Money, Service, TrendCharts, Bell, Stamp, ChatDotRound, ChatLineRound,
} from '@element-plus/icons-vue'
import { messagesApi } from '@/api/messages'
import { userFeedbackApi } from '@/api/userFeedback'
import HelperFloating from '@/components/HelperFloating.vue'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

// 🆕 v3：新增部门菜单的图标映射（菜单本体由后端 /api/auth/menus 下发）
const MENU_ICONS: Record<string, any> = {
  sales: Suitcase, design: EditPen, electric: Lightning, produce: SetUp,
  sheet: Scissor, purchase: ShoppingCart, warehouse: Box, logistics: Van,
  finance: Money, aftersales: Service, report: TrendCharts, messages: Bell,
  approve: Stamp, wxbind: ChatDotRound, 'user-feedback': ChatLineRound,
}
const ADMIN_EXTRA = ['approve', 'wxbind', 'user-feedback']
// 业务部门菜单（排除 messages 单独放底部、管理组的归管理组）
const bizMenus = computed(() =>
  auth.deptMenus.filter(m => !['messages', ...ADMIN_EXTRA].includes(m.key)))
const adminExtraMenus = computed(() =>
  auth.deptMenus.filter(m => ADMIN_EXTRA.includes(m.key)))

// 🆕 未读消息角标（轻量轮询，60s）
const unread = ref(0)
let unreadTimer: number | undefined
async function refreshUnread() {
  if (!auth.isLoggedIn || !auth.hasMenu('messages')) return
  try { unread.value = await messagesApi.unreadCount() } catch { /* 静默 */ }
}
// 🆕 消息中心标已读后即时清零角标（监听 MessagesView 派发的事件，免等 60s 轮询）
function onMessagesRead() { unread.value = 0 }

// 🆕 登录后检查用户反馈的「处理意见回复」，右下角弹窗提醒查看
async function checkFeedbackReplies() {
  if (!auth.isLoggedIn) return
  try {
    const rows = await userFeedbackApi.myUnreadReplies()
    if (!rows.length) return
    const n = ElNotification({
      title: '反馈处理回复',
      message: `您有 ${rows.length} 条反馈已收到处理意见回复，点击查看`,
      type: 'success',
      position: 'bottom-right',
      duration: 0,
      onClick: () => { window.dispatchEvent(new Event('pms:open-my-feedback')); n.close() },
    })
  } catch { /* 静默 */ }
}

const collapsed = ref(localStorage.getItem('pms_sidebar_collapsed') === '1')
function toggleCollapse() {
  collapsed.value = !collapsed.value
  localStorage.setItem('pms_sidebar_collapsed', collapsed.value ? '1' : '0')
}

const activeKey = computed(() => route.name as string)
function go(name: string) { router.push({ name }) }

async function logout() {
  await auth.logout()
  router.push('/login')
}

// 用户首字母（头像）
const initials = computed(() => {
  const n = auth.user?.full_name || auth.user?.username || '?'
  return n.charAt(0).toUpperCase()
})

// 改密对话框
const changePwdVisible = ref(false)
const pwdForm = ref({ old: '', new1: '', new2: '' })
const pwdLoading = ref(false)

async function submitChangePwd() {
  if (!pwdForm.value.old || !pwdForm.value.new1) {
    ElMessage.warning('请填写完整'); return
  }
  if (pwdForm.value.new1 !== pwdForm.value.new2) {
    ElMessage.warning('两次新密码不一致'); return
  }
  if (pwdForm.value.new1.length < 6) {
    ElMessage.warning('新密码至少 6 位'); return
  }
  pwdLoading.value = true
  try {
    await auth.changePassword(pwdForm.value.old, pwdForm.value.new1)
    ElMessage.success('密码已修改')
    changePwdVisible.value = false
    pwdForm.value = { old: '', new1: '', new2: '' }
  } finally {
    pwdLoading.value = false
  }
}

onMounted(async () => {
  if (auth.isLoggedIn && !auth.user) await auth.fetchMe()
  if (auth.mustChangePassword) {
    ElMessageBox.alert('为了账号安全，首次登录请先修改密码', '强制改密', {
      confirmButtonText: '去修改', type: 'warning',
    }).then(() => { changePwdVisible.value = true })
  }
  // 🆕 v3：拉取可见菜单 + 未读消息轮询
  await auth.fetchMenus()
  refreshUnread()
  unreadTimer = window.setInterval(refreshUnread, 60_000)
  window.addEventListener('pms:messages-read', onMessagesRead)
  checkFeedbackReplies()   // 🆕 登录后提醒未读的反馈处理回复
})

onUnmounted(() => {
  if (unreadTimer) window.clearInterval(unreadTimer)
  window.removeEventListener('pms:messages-read', onMessagesRead)
})
</script>

<template>
  <div class="layout" :class="{ collapsed }">
    <aside class="sidebar">
      <!-- 顶部 brand + 折叠按钮 -->
      <div class="brand">
        <div class="brand-logo">
          <el-icon><Grid /></el-icon>
        </div>
        <span v-if="!collapsed" class="brand-text">同辉项目管理</span>
        <button
          type="button"
          class="collapse-btn"
          @click.stop.prevent="toggleCollapse"
          :title="collapsed ? '展开' : '折叠'"
        >
          <el-icon v-if="collapsed"><Expand /></el-icon>
          <el-icon v-else><Fold /></el-icon>
        </button>
      </div>

      <!-- 导航 -->
      <nav>
        <a v-if="auth.hasMenu('catalog')" :class="{ active: activeKey === 'overview' }" @click="go('overview')">
          <el-icon class="nav-icon"><DataLine /></el-icon>
          <span v-if="!collapsed">项目目录</span>
        </a>
        <a v-if="auth.hasMenu('list')" :class="{ active: activeKey === 'projects' }" @click="go('projects')">
          <el-icon class="nav-icon"><FolderOpened /></el-icon>
          <span v-if="!collapsed">项目详单</span>
        </a>

        <!-- 🆕 v3 业务部门菜单（后端按角色下发） -->
        <a v-for="m in bizMenus" :key="m.key"
           :class="{ active: activeKey === m.key }" @click="go(m.key)">
          <el-icon class="nav-icon"><component :is="MENU_ICONS[m.key] || Grid" /></el-icon>
          <span v-if="!collapsed">{{ m.label }}</span>
        </a>

        <!-- 🆕 消息中心（带未读角标） -->
        <a v-if="auth.hasMenu('messages')"
           :class="{ active: activeKey === 'messages' }" @click="go('messages')">
          <el-icon class="nav-icon"><Bell /></el-icon>
          <span v-if="!collapsed">消息中心</span>
          <span v-if="unread > 0" class="nav-badge">{{ unread > 99 ? '99+' : unread }}</span>
        </a>

        <template v-if="auth.isAdmin">
          <div v-if="!collapsed" class="section-title">管理</div>
          <div v-else class="section-divider"></div>

          <a :class="{ active: activeKey === 'admin-users' }" @click="go('admin-users')">
            <el-icon class="nav-icon"><User /></el-icon>
            <span v-if="!collapsed">用户</span>
          </a>
          <a :class="{ active: activeKey === 'admin-perms' }" @click="go('admin-perms')">
            <el-icon class="nav-icon"><Lock /></el-icon>
            <span v-if="!collapsed">权限管理</span>
          </a>
          <a :class="{ active: activeKey === 'admin-audit' }" @click="go('admin-audit')">
            <el-icon class="nav-icon"><Document /></el-icon>
            <span v-if="!collapsed">操作审计</span>
          </a>
          <!-- 🆕 v3 管理组新菜单（导出审批/企微绑定） -->
          <a v-for="m in adminExtraMenus" :key="m.key"
             :class="{ active: activeKey === m.key }" @click="go(m.key)">
            <el-icon class="nav-icon"><component :is="MENU_ICONS[m.key] || Grid" /></el-icon>
            <span v-if="!collapsed">{{ m.label }}</span>
          </a>
        </template>
      </nav>

      <!-- 底部用户区 -->
      <div class="footer" :class="{ collapsed }">
        <div class="user-row">
          <div class="avatar">{{ initials }}</div>
          <div v-if="!collapsed" class="user-info">
            <div class="uname">{{ auth.user?.full_name || auth.user?.username }}</div>
            <div class="ubadges">
              <span v-for="rn in (auth.user?.role_names?.length ? auth.user.role_names : [auth.user?.role_name])"
                    :key="rn || ''" class="badge primary">{{ rn }}</span>
              </div>
          </div>
        </div>
        <div class="footer-actions" :class="{ collapsed }">
          <el-tooltip content="修改密码" placement="top">
            <button class="icon-btn" @click="changePwdVisible = true">
              <el-icon><Key /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip content="退出登录" placement="top">
            <button class="icon-btn danger" @click="logout">
              <el-icon><SwitchButton /></el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>
    </aside>

    <main class="main">
      <router-view />
    </main>

    <!-- 改密 -->
    <el-dialog
      v-model="changePwdVisible" title="修改密码" width="440px"
      :close-on-click-modal="!auth.mustChangePassword"
      :show-close="!auth.mustChangePassword">
      <el-form label-position="top">
        <el-form-item label="原密码">
          <el-input v-model="pwdForm.old" type="password" show-password size="large" />
        </el-form-item>
        <el-form-item label="新密码（至少 6 位）">
          <el-input v-model="pwdForm.new1" type="password" show-password size="large" />
        </el-form-item>
        <el-form-item label="再次输入新密码">
          <el-input v-model="pwdForm.new2" type="password" show-password size="large" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="changePwdVisible = false" :disabled="auth.mustChangePassword">取消</el-button>
        <el-button type="primary" :loading="pwdLoading" @click="submitChangePwd">确定</el-button>
      </template>
    </el-dialog>

    <!-- 🆕 全局用户反馈小助手（任意登录用户可见） -->
    <HelperFloating />
  </div>
</template>

<style scoped>
.layout { min-height: 100vh; }

.sidebar {
  position: fixed; top: 0; bottom: 0; left: 0;
  width: var(--sidebar-w);
  background: var(--sidebar-bg);
  color: var(--sidebar-text);
  display: flex; flex-direction: column;
  z-index: 100;
  transition: width .25s cubic-bezier(.4,0,.2,1);
  box-shadow: 0 0 24px rgba(0,0,0,.1);
}
.layout.collapsed .sidebar { width: var(--sidebar-w-collapsed); }

/* Brand */
.brand {
  padding: 20px 16px 16px;
  display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid rgba(255,255,255,.06);
  overflow: hidden; white-space: nowrap;
  min-height: 76px;
}
.brand-logo {
  width: 36px; height: 36px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--primary) 0%, #6366f1 100%);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  color: white; font-size: 20px;
  box-shadow: 0 4px 12px rgba(37,99,235,.3);
}
.brand-text {
  flex: 1;
  font-size: 17px; font-weight: 600; color: white;
  letter-spacing: .5px;
}

/* Collapse btn —— 放在 brand 内部，固定位置不漂移 */
.collapse-btn {
  flex-shrink: 0;
  width: 30px; height: 30px;
  background: rgba(255,255,255,.08);
  border: none;
  color: var(--sidebar-text-light);
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  outline: none;
  transition: background .15s, color .15s;
}
.collapse-btn:hover { background: rgba(255,255,255,.16); color: white; }
.collapse-btn:active { background: rgba(255,255,255,.22); }
.collapse-btn .el-icon { font-size: 16px; }
.layout.collapsed .brand { padding: 20px 0; justify-content: center; }
.layout.collapsed .brand .collapse-btn {
  position: absolute; top: 80px;
  width: 28px; height: 28px;
  background: white; color: var(--text-2);
  box-shadow: 0 2px 8px rgba(0,0,0,.12);
  right: -14px;
}
.layout.collapsed .brand .collapse-btn:hover {
  background: var(--primary); color: white;
}

/* Nav */
nav { flex: 1; overflow-y: auto; padding: 12px 12px; }
nav a {
  display: flex; align-items: center; gap: 12px;
  padding: 11px 14px;
  margin-bottom: 4px;
  color: var(--sidebar-text);
  font-size: 14px; font-weight: 500;
  cursor: pointer;
  white-space: nowrap; overflow: hidden;
  border-radius: var(--radius-sm);
  transition: all .15s;
}
nav a:hover {
  background: var(--sidebar-bg-hover);
  color: var(--sidebar-text-active);
}
nav a.active {
  background: var(--sidebar-bg-active);
  color: var(--sidebar-text-active);
  box-shadow: 0 4px 12px rgba(37,99,235,.25);
}
.nav-icon { font-size: 18px !important; flex-shrink: 0; }

/* 🆕 v3 未读消息角标 */
.nav-badge {
  margin-left: auto;
  min-width: 18px; height: 18px; padding: 0 5px;
  background: #ef4444; color: #fff;
  font-size: 11px; font-weight: 700; line-height: 18px;
  text-align: center; border-radius: 9px;
}

.section-title {
  padding: 16px 16px 6px;
  font-size: 11px; color: var(--sidebar-text-light);
  text-transform: uppercase; letter-spacing: 1.2px;
  font-weight: 600;
}
.section-divider {
  margin: 12px 14px; border-top: 1px solid rgba(255,255,255,.08);
}

/* Footer */
.footer {
  padding: 16px;
  border-top: 1px solid rgba(255,255,255,.06);
}
.footer.collapsed { padding: 12px 8px; }
.user-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.avatar {
  width: 38px; height: 38px; flex-shrink: 0;
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  color: white;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; font-size: 15px;
}
.user-info { flex: 1; overflow: hidden; }
.uname {
  color: white; font-weight: 500; font-size: 14px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-bottom: 3px;
}
.ubadges { display: flex; flex-wrap: wrap; gap: 4px; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--sidebar-bg-hover);
  color: var(--sidebar-text);
}
.badge.primary { background: rgba(37,99,235,.2); color: #60a5fa; }

.footer-actions { display: flex; gap: 8px; }
.footer-actions.collapsed { flex-direction: column; align-items: center; }
.icon-btn {
  flex: 1; padding: 8px;
  background: transparent;
  color: var(--sidebar-text-light);
  border: 1px solid rgba(255,255,255,.08);
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px;
  transition: all .15s;
}
.footer-actions.collapsed .icon-btn { flex: none; width: 100%; }
.icon-btn:hover { background: var(--sidebar-bg-hover); color: white; }
.icon-btn.danger:hover { background: rgba(239,68,68,.2); color: #f87171; }

/* Main */
.main {
  margin-left: var(--sidebar-w);
  min-height: 100vh;
  padding: 28px 32px;
  transition: margin-left .25s cubic-bezier(.4,0,.2,1);
}
.layout.collapsed .main { margin-left: var(--sidebar-w-collapsed); }

/* 小屏笔记本 / 平板：压缩主内容区边距，给表格腾空间 */
@media (max-height: 800px) {
  .main { padding: 12px 18px; }
}
@media (max-width: 1024px) {
  .main { padding: 14px 16px; }
}
</style>
