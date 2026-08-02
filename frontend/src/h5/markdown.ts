/**
 * 助手回复的 Markdown 渲染。
 *
 * ⚠️ `html: false` 与下游的 v-html 是**成对**的，谁也不能单独改：
 * markdown-it 在 html:false 时把原始 HTML 转义成文本，v-html 才是安全的。
 * 一旦为了「让模型画个好看的卡片」把它开成 true，或者绕过 render 直接
 * v-html 模型输出，XSS 防线当场归零（手册 3.4.2）。
 *
 * 攻击面不只是「模型自己乱写」：工具返回的数据里带着用户自由填写的
 * 备注、OA 详情、记录值，里面完全可以有 <img src=x onerror=...>，
 * 模型很可能原样复述出来。
 */
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  html: false,      // ← 与 H5ChatView 里的 v-html 成对，不要单独改
  linkify: false,   // H5 里不做自动链接：模型给的 URL 不该可点（原则三）
  breaks: true,     // 单换行即换行，符合聊天场景的书写习惯
})

export function renderMd(text: string): string {
  return md.render(text || '')
}
