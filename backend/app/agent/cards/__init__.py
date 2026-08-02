"""卡片装配层。对外只暴露 assemble 与 registry 查询，其余是实现细节。"""
from .registry import CARD_TYPES, allows, is_known   # noqa: F401
from .pay_req import assemble_pay_req_cards, pending_pay_reqs   # noqa: F401
