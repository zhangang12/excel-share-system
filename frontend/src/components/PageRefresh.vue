<script setup lang="ts">
// 反馈#359（杨坛）：「每个界面都加一个刷新的功能」。
//
// 原来只有零星几个 tab 里有刷新按钮——财务部就只有「请款审批」那个 tab 有，
// 他在「安装/售后费用」tab 上审批完、或者别人改了数据，看到的还是旧的，
// 想刷新只能整页重载：桌面客户端菜单栏是 autoHideMenuBar 隐藏的，
// 连"重新加载"都找不到，等于没有退路。
//
// 统一放在每个页面标题栏的右侧——位置固定，不用满页找是哪个 tab 里有。
// ⚠️ 只重跑本页的数据请求，**不重载页面**：当前 tab、筛选条件、
//    展开的行、滚动位置全部保住。整页重载会把这些都打回默认，
//    对"审批完想看看新状态"这个场景反而更难用。
import { ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const props = withDefaults(defineProps<{
  /** 本页的数据加载函数（同 onMounted 里那套；有 tab 的页面要带上当前 tab 的加载） */
  load: () => any
  text?: string
  size?: '' | 'large' | 'default' | 'small'
}>(), { text: '刷新', size: '' })

const loading = ref(false)
async function run() {
  if (loading.value) return
  loading.value = true
  try {
    await props.load()
    ElMessage.success('已刷新')
  } catch {
    // 失败提示由 axios 拦截器统一弹，这里再弹一次就是两个 toast（#345 踩过这个坑）
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-button :icon="Refresh" :loading="loading" :size="size" @click="run">{{ text }}</el-button>
</template>
