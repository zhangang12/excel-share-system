/**
 * H5 的 Markdown 渲染入口。
 *
 * 🆕 2026-09-24：实现全部搬到 `src/shared/agentMarkdown.ts`，**网页版/桌面客户端
 * 与这里共用同一个实例**。原因是用户截图报的 bug：语义着色和图表当初只加在 H5，
 * 网页版自己 new 了一个 MarkdownIt，于是每张表都漏出 `[[danger:30 天]]`
 * `[[muted:—]]`，图更是把整段 JSON 原样甩给用户。
 *
 * 这个文件留着只为两件事：
 *   ① H5 侧的 import 路径不变（H5ChatView 等照旧 `from './markdown'`）；
 *   ② 明确写下「别在这里再加插件」——加了就又只有一边有。
 *
 * ⚠️ 要改渲染行为（新标记、新围栏、markdown-it 选项），去改 shared/agentMarkdown.ts。
 */
export { renderAgentMd as renderMd } from '../shared/agentMarkdown'
