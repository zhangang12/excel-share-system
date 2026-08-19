"""卡片类型白名单（原则三：能力可枚举）。

第一期只登记一类：请款审批。依据是生产数据——杨坛做过 40 笔请款审批
（37 已付 + 3 已批，¥80,260），是第二名（订单审批 9 笔）的 4 倍多。
开票审批他只做了 5 笔，且赵仁辉也做了 4 笔，不主要在他手上。

新增一类卡的检查单：
  1. 这里登记 type
  2. 写 assemble_xxx：facts 用 current 重查、flags 逐条对上端点的 raise
  3. 前端 cardRegistry.ts 补同名条目（URL 只写在那里）
  4. 补测试：越权 / 职责分离 / 未知 type 不渲染
少任何一步都不要上。
"""

# type → 动作 key 集合。动作的 URL/method 不在这里——那是前端常量表的事，
# 后端只认「这个 type 允许哪些 key」，多一个字都不给模型发挥的空间。
CARD_TYPES: dict[str, set[str]] = {
    "pay_req_approve": {"approve", "reject"},
    # 🆕 回款登记：查询工具找出 49 笔 ¥253 万无人管的应收，这张卡让他当场销账。
    #    两个动作都打 PUT /sales/ledger/{id}/payment-note，可逆（删批注即恢复）。
    "ledger_settle": {"settle_ship", "settle_balance"},
    # 🆕 销售订单审批：打 POST /sales/ledger/{id}/order-approve|order-reject
    "sales_order_approve": {"approve", "reject"},
    # 🆕 管理层待办·顺延申请：打 POST /mgmt-todos/{target_id}/extend/decide
    #    ⚠️ ref 是 **target_id**（某人对某条待办的处理行），不是 todo_id。
    #       一条待办发给多个人时每人一行，各自独立申请顺延。
    "mgmt_todo_extend": {"approve", "reject"},
}


def is_known(card_type: str) -> bool:
    return card_type in CARD_TYPES


def allows(card_type: str, action_key: str) -> bool:
    return action_key in CARD_TYPES.get(card_type, set())
