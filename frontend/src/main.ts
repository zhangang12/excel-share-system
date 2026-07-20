import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// 🆕 v4 中文化: element-plus 全局 zh-cn (日期/分页器/表格空态等)
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import VxeUIAll from 'vxe-pc-ui'
import 'vxe-pc-ui/lib/style.css'
import VxeUITable from 'vxe-table'
import 'vxe-table/lib/style.css'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.use(VxeUIAll)
app.use(VxeUITable)
app.mount('#app')
// 桌面客户端：通知主进程首屏已挂载，关启动页亮主窗口（浏览器端 pmsDesktop 为 undefined，自动跳过）
window.pmsDesktop?.notifyReady?.()
