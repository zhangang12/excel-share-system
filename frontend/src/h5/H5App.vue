<script setup lang="ts">
// H5 根组件：只有一个路由出口。全局样式在 h5-tokens.css 里。
</script>

<template>
  <router-view />
</template>

<style>
/* H5 是独立构建，没有桌面端的 style.css，基础重置写在这里 */
* { box-sizing: border-box; }
html, body, #app { margin: 0; padding: 0; height: 100%; }
body {
  background: var(--h5-bg);
  font-family: var(--h5-font);
  -webkit-font-smoothing: antialiased;
  font-feature-settings: "tnum" 1;
  /* 手机上禁掉双击缩放导致的误触，但保留捏合缩放（无障碍不能砍） */
  touch-action: manipulation;
}
input, button { font-family: inherit; }
::-webkit-scrollbar { width: 0; height: 0; }
</style>
