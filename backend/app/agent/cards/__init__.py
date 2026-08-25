"""卡片装配层。对外只暴露 assemble 与 registry 查询，其余是实现细节。"""
from .registry import CARD_TYPES, allows, is_known   # noqa: F401
from .pay_req import assemble_pay_req_cards, pending_pay_reqs   # noqa: F401
from .ledger_settle import (assemble_settle_cards, blind_ledgers,   # noqa: F401
                            summarize as summarize_settle)
from .sales_order import assemble_order_cards, pending_orders   # noqa: F401
from .mgmt_extend import assemble_extend_cards, pending_extends   # noqa: F401
from .mgmt_send import assemble_send_cards, pending_drafts   # noqa: F401
from .oa_req import assemble_oa_cards, pending_oa_requests   # noqa: F401

# type → 装配函数。verify-action 用它按类型重新装配拿最新 flags，
# 加新卡类型时这里也要登记，否则那类卡的动作永远校验不过。
ASSEMBLERS = {
    "pay_req_approve": assemble_pay_req_cards,
    "ledger_settle": assemble_settle_cards,
    "sales_order_approve": assemble_order_cards,
    "mgmt_todo_extend": assemble_extend_cards,
    "mgmt_todo_send": assemble_send_cards,
    "oa_approve": assemble_oa_cards,
}
