/**
 * 卡片注册表 —— 原则三「能力可枚举」的落点。
 * 依据 docs/ai-agent-erp-handbook 3.4.1。
 *
 * 这是可信代码，模型碰不到。模型的输出里永远不出现 URL、method、字段名，
 * 它只能说「给 ref=1288 渲染一张 pay_req_approve 卡」，端点从这张表里查。
 *
 * type 不在表里 → 整张卡不渲染，降级成纯文本。
 * 加新卡类型前先看 backend/app/agent/cards/registry.py 里的检查单。
 */
import { http } from './http'

export interface CardFact { k: string; v: string; sensitive?: boolean; emphasis?: boolean }
export interface CardFlag { code: string; level: 'block' | 'warn'; msg: string }
export interface CardAction { key: string; primary: boolean; disabled_by?: string | null; needs_reason?: boolean }
export interface AgentCard {
  type: string
  ref: number
  token: string
  facts: CardFact[]
  flags: CardFlag[]
  note?: string
  actions: CardAction[]
}

interface ActionDef {
  label: string
  /** 危险动作在 UI 上走次要样式 */
  danger?: boolean
  needsReason?: boolean
  run: (ref: number, reason?: string) => Promise<unknown>
}

interface CardDef {
  title: string
  /** 图标块里的字形；用字符不用图片，H5 要能离线渲染 */
  glyph: string
  actions: Record<string, ActionDef>
}

export const CARD_REGISTRY: Record<string, CardDef> = {
  /**
   * 回款登记。两个动作都打 payment-note——跟他在台账页点批注是同一个端点。
   * 副作用在后端：settle_ship 清 ship_receivable，settle_balance 清 balance
   * 并把原值存进 balance_contract。两者都可逆（删批注即恢复），
   * 2026-08-03 本地跑过完整往返验证才放出来。
   */
  ledger_settle: {
    title: '回款登记',
    glyph: '收',
    actions: {
      settle_ship: {
        label: '发货款已收',
        needsReason: true,
        run: (ref, reason) => http.put(`/sales/ledger/${ref}/payment-note`,
          { field: 'before_ship', note: reason || '' }),
      },
      settle_balance: {
        label: '尾款已收',
        needsReason: true,
        run: (ref, reason) => http.put(`/sales/ledger/${ref}/payment-note`,
          { field: 'balance', note: reason || '' }),
      },
    },
  },

  sales_order_approve: {
    title: '销售订单审批',
    glyph: '单',
    actions: {
      approve: {
        label: '通过',
        run: (ref) => http.post(`/sales/ledger/${ref}/order-approve`, {}),
      },
      reject: {
        label: '驳回',
        danger: true,
        needsReason: true,
        run: (ref, reason) => http.post(`/sales/ledger/${ref}/order-reject`,
          { reason: reason || '' }),
      },
    },
  },

  pay_req_approve: {
    title: '请款审批',
    glyph: '￥',
    actions: {
      approve: {
        label: '通过',
        run: (ref) => http.put(`/purchase-mgmt/payment-requests/${ref}/approve`),
      },
      reject: {
        label: '驳回',
        danger: true,
        needsReason: true,
        run: (ref, reason) =>
          http.put(`/purchase-mgmt/payment-requests/${ref}/reject`, { reason: reason || '' }),
      },
    },
  },
}

export function isKnownCard(type: string): boolean {
  return Object.prototype.hasOwnProperty.call(CARD_REGISTRY, type)
}

/** 卡片类型 → 给人看的名字。对话里那句「用户气泡」用它，
 *  别在别处再写死一个默认值——写死过一次，结果不管点哪类卡片，
 *  气泡都显示「待我审批的请款单」。 */
export function cardTitle(type: string): string {
  return CARD_REGISTRY[type]?.title || '待办'
}

/**
 * 执行卡片动作：先让后端核一遍这张卡（令牌、白名单、最新 flags），
 * 通过之后才打真正的业务端点——用的是用户自己的 token，
 * 跟他在财务页面上点「审批通过」是同一个请求。
 */
export async function runCardAction(card: AgentCard, actionKey: string, reason?: string) {
  const def = CARD_REGISTRY[card.type]
  if (!def) throw new Error('未知的卡片类型')
  const act = def.actions[actionKey]
  if (!act) throw new Error('该卡片不支持这个动作')

  await http.post('/agent/cards/verify-action', {
    type: card.type, ref: card.ref, token: card.token, action: actionKey,
  })
  return act.run(card.ref, reason)
}
