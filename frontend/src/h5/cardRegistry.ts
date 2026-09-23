/**
 * H5 的卡片注册表入口。
 *
 * 🆕 2026-09-24：表本身搬到了 `src/shared/agentCards.ts`，与网页版/桌面客户端共用。
 * 原因是网页版原来**一张卡都没有**，而这张表里是端点、method、字段名 ——
 * 抄两份意味着哪天后端改了路径，有一端会静默打到不存在的地址上。
 *
 * 这个文件只剩两件事：① 把 H5 自己的 axios 实例注入进去；② 保持原有 import 路径不变。
 */
import { http } from './http'
import { setCardHttp } from '../shared/agentCards'

setCardHttp(http)

export type { CardFact, CardFlag, CardAction, AgentCard } from '../shared/agentCards'
export { CARD_REGISTRY, isKnownCard, cardTitle, runCardAction } from '../shared/agentCards'
