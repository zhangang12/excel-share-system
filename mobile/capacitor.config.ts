import type { CapacitorConfig } from '@capacitor/cli'

/**
 * 同辉项目管理 · 手机端
 *
 * 与旧壳（直接 loadUrl 服务器 /h5/）的关键差别：**前端资源打进包里，从本地加载**。
 * 这一条同时解决三件事：
 *   ① 冷启动不再等网络，弱网/断网也能开到登录页
 *   ② 页面 origin 变成 `http://localhost` —— 属于 W3C「潜在可信来源」，
 *      `isSecureContext === true`，需要安全上下文的浏览器能力不再被拦
 *   ③ 有了本地包才谈得上热更新（换包 = 换目录，见 PmsUpdaterPlugin）
 *
 * ⚠️ **androidScheme 必须是 http，不能用 Capacitor 默认的 https**：
 *   默认 `https://localhost` 是安全页面，而我们的 API 是明文 `http://8.141.123.141` ——
 *   安全页面发明文请求会被**混合内容拦截**，APP 里每个接口都直接失败。
 *   改成 http 之后页面不是 https、不触发混合内容规则，而 localhost 本身仍是可信来源，
 *   安全上下文照样成立。等服务器上了 HTTPS 再换回默认值。
 */
const config: CapacitorConfig = {
  appId: 'com.tonghui.pms',
  appName: '同辉项目管理',
  webDir: 'www',
  android: {
    // 见上方大段说明，改这一行之前先把 API 换成 HTTPS
    webContentsDebuggingEnabled: false,
  },
  server: {
    androidScheme: 'http',
    hostname: 'localhost',
  },
  plugins: {},
}

export default config
