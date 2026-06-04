<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ElMessage } from 'element-plus'
import { User, Lock, Grid } from '@element-plus/icons-vue'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
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
  <div class="login-wrap">
    <!-- 左侧装饰区 -->
    <div class="left">
      <div class="left-inner">
        <div class="logo-box">
          <el-icon><Grid /></el-icon>
        </div>
        <h1>同辉智能项目管理系统</h1>
        <p>多人协作维护项目进度</p>
        <ul class="features">
          <li>✓ 字段级权限控制</li>
          <li>✓ 多人实时协作</li>
          <li>✓ Excel 导入导出</li>
          <li>✓ 操作审计可追溯</li>
        </ul>
      </div>
    </div>

    <!-- 右侧登录表单 -->
    <div class="right">
      <div class="login-card">
        <h2>欢迎登录</h2>
        <p class="sub">请使用公司账号登录</p>

        <el-form @submit.prevent="onSubmit" size="large" class="form">
          <el-form-item>
            <el-input v-model="form.username" placeholder="用户名" autocomplete="username">
              <template #prefix><el-icon><User /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item>
            <el-input v-model="form.password" type="password" show-password
                      placeholder="密码" autocomplete="current-password"
                      @keyup.enter="onSubmit">
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-button type="primary" native-type="submit" :loading="loading"
                     size="large" class="submit-btn">
            登 录
          </el-button>
        </el-form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh; display: flex;
}

/* 左侧装饰 */
.left {
  flex: 1.1;
  background: linear-gradient(135deg, #4f46e5 0%, #2563eb 50%, #0ea5e9 100%);
  display: flex; align-items: center; justify-content: center;
  padding: 40px;
  position: relative; overflow: hidden;
}
.left::before {
  content: ''; position: absolute;
  width: 600px; height: 600px;
  background: rgba(255,255,255,.06);
  border-radius: 50%;
  top: -200px; right: -200px;
}
.left::after {
  content: ''; position: absolute;
  width: 400px; height: 400px;
  background: rgba(255,255,255,.04);
  border-radius: 50%;
  bottom: -100px; left: -100px;
}
.left-inner {
  position: relative; z-index: 1;
  color: white; max-width: 460px;
}
.logo-box {
  width: 64px; height: 64px;
  background: rgba(255,255,255,.18);
  backdrop-filter: blur(10px);
  border-radius: 16px;
  display: flex; align-items: center; justify-content: center;
  font-size: 32px; color: white;
  margin-bottom: 30px;
  box-shadow: 0 8px 32px rgba(0,0,0,.15);
}
.left h1 {
  font-size: 32px; font-weight: 600;
  margin: 0 0 12px; letter-spacing: .5px;
}
.left p {
  font-size: 16px; opacity: .9; line-height: 1.6;
  margin: 0 0 32px;
}
.features { list-style: none; padding: 0; margin: 0; }
.features li {
  font-size: 14px; opacity: .85; line-height: 2;
}

/* 右侧表单 */
.right {
  flex: 1;
  background: white;
  display: flex; align-items: center; justify-content: center;
  padding: 40px;
}
.login-card {
  width: 100%; max-width: 380px;
}
.login-card h2 {
  font-size: 28px; font-weight: 600;
  margin: 0 0 8px; color: var(--text-1);
}
.sub {
  color: var(--text-3); font-size: 14px;
  margin: 0 0 36px;
}
.form { margin-bottom: 20px; }
.submit-btn {
  width: 100%;
  height: 46px !important;
  font-size: 15px !important;
  font-weight: 500 !important;
  letter-spacing: 4px;
  margin-top: 8px;
}
.hint {
  font-size: 13px; color: var(--text-3);
  text-align: center; line-height: 1.8;
  padding: 16px;
  background: #f9fafb;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.hint b { color: var(--text-1); }

/* 窄屏隐藏装饰 */
@media (max-width: 900px) {
  .left { display: none; }
}
</style>
