"""记忆与知识 —— 智能体 v2 阶段四、六、七（见 docs/agent-architecture-v2.md）。

v1 每条消息从零开始：说不了「南京那个项目」、说不了「这笔先放着」、
说不了「刚才那个客户还有什么单」。

三层记忆（**不上向量库**，量级几百条，Postgres/JSON 够用）
----------------------------------------------------------
| 层       | 存哪                       | 内容                    | 失效     |
|----------|----------------------------|-------------------------|----------|
| 会话焦点 | 请求上下文（history 里带） | 当前在聊哪个项目/客户   | 会话结束 |
| 用户状态 | `app_settings`（已有模式） | 已阅/暂缓、关注的指标   | 手动/冷却|
| 术语别名 | `app_settings`             | 「南京那个」→ 项目号    | 长期     |

知识库（RAG）索引什么
---------------------
**不索引业务数据** —— 那是工具的活，RAG 查数据只会带来幻觉。
索引的是**规则与经验**：字段口径、已知数据坑、SOP。

这些口径原来**硬编码在提示词里**，加一条改一次代码，不可持续。
搬出来之后业务也能自己维护。
"""
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models

_KEY_ALIAS = "agent_alias"        # 术语别名
_KEY_KNOWLEDGE = "agent_knowledge"  # 口径/SOP 知识条目
_KEY_USER_STATE = "agent_user_state"

# 出厂自带的口径条目。**这些原来写死在提示词里**，现在搬到这，业务可增删。
# 每条：{"q": 匹配词(空格分隔), "a": 说明}
DEFAULT_KNOWLEDGE: list[dict] = [
    {"q": "发货款应收 发货款 ship_receivable",
     "a": "「发货款应收」是台账上约定在发货环节收的那笔钱。⚠️ 生产上 35 张发货单有 32 张"
          "还停在「待发货」、shipped_at 全库 90 条只填了 3 条 —— 所以它**不等于客户欠钱**，"
          "先确认货到底发了没，再决定催不催。"},
    {"q": "尾款 到期日 balance_date 催办",
     "a": "尾款催办是按 `balance_date` 扫的。**没填到期日的那些永远扫不到**，"
          "只能靠人工盯或走助手的「盯不住的应收」。"},
    {"q": "合同额 0 毛利 假亏损",
     "a": "台账合同额为 0 时，项目毛利 = 收入 0 - 成本，会被算成**假亏损**。"
          "看到毛利异常为负，先查合同额是不是没填。"},
    {"q": "账龄 挂了多久 逾期天数",
     "a": "⚠️ 系统里**没有可靠的账龄字段**：`sales_ledger.ship_date` 全空、"
          "`shipments.shipped_at` 90 条只填了 3 条。能站住的只有「台账建了 N 天」，"
          "**不要说「逾期 N 天」**。"},
    {"q": "软删 已删除 幽灵 is_deleted",
     "a": "项目有软删。任何统计都要带 `Project.is_deleted == False`，"
          "漏了会多算 28 行幽灵数据（生产实测）。"},
    {"q": "未到货 到期未到货 口径",
     "a": "「到期未到货」= 有预计到货日、日期已到（含当天）、且 `arrival_date` 为空。"
          "口径见 `overdue.scan_po_arrival_overdue`，别自创第二套。"},
]


async def _get(db: AsyncSession, key: str, default):
    r = (await db.execute(select(models.AppSetting).where(
        models.AppSetting.key == key))).scalar_one_or_none()
    if not r or not r.value:
        return default
    try:
        return json.loads(r.value)
    except (TypeError, ValueError):
        return default


async def _set(db: AsyncSession, key: str, value):
    r = (await db.execute(select(models.AppSetting).where(
        models.AppSetting.key == key))).scalar_one_or_none()
    payload = json.dumps(value, ensure_ascii=False)
    if r:
        r.value = payload
    else:
        db.add(models.AppSetting(key=key, value=payload))
    await db.commit()


# ══════════════════════ 术语别名 ══════════════════════

async def aliases(db: AsyncSession) -> dict[str, str]:
    return await _get(db, _KEY_ALIAS, {})


async def set_alias(db: AsyncSession, word: str, target: str):
    a = await aliases(db)
    a[word.strip()] = target.strip()
    await _set(db, _KEY_ALIAS, a)


async def expand(db: AsyncSession, message: str) -> str:
    """把口头简称替换成系统里的准确说法。

    ⚠️ 只做**整词替换**，不做模糊。别把用户打的字改成别的意思 ——
       v1 那个贪婪正则把「查询一下所有的待审批的待办?」劫持成查请款单，
       教训是：**宁可不认识，也不能改写用户的问题**。
    """
    a = await aliases(db)
    if not a:
        return message
    out = message or ""
    for word, target in sorted(a.items(), key=lambda kv: -len(kv[0])):
        if word and word in out:
            out = out.replace(word, target)
    return out


# ══════════════════════ 知识库（RAG）══════════════════════

async def knowledge(db: AsyncSession) -> list[dict]:
    items = await _get(db, _KEY_KNOWLEDGE, None)
    return items if isinstance(items, list) and items else DEFAULT_KNOWLEDGE


async def save_knowledge(db: AsyncSession, items: list[dict]):
    await _set(db, _KEY_KNOWLEDGE, items)


_WORD = re.compile(r"[\w一-鿿]+")


async def recall(db: AsyncSession, message: str, top: int = 2) -> list[str]:
    """按关键词召回口径条目。

    **不上向量库**：条目量级几百，关键词命中足够，而且可解释 ——
    出了问题能一眼看出为什么召回了这条。
    """
    text = message or ""
    hits = []
    for item in await knowledge(db):
        kws = [k for k in _WORD.findall(item.get("q", "")) if len(k) >= 2]
        score = sum(1 for k in kws if k in text)
        if score:
            hits.append((score, item.get("a", "")))
    hits.sort(key=lambda x: -x[0])
    return [a for _, a in hits[:top] if a]


# ══════════════════════ 用户状态 ══════════════════════

async def user_state(db: AsyncSession, username: str) -> dict:
    return (await _get(db, _KEY_USER_STATE, {})).get(username, {})


async def set_user_state(db: AsyncSession, username: str, patch: dict):
    all_ = await _get(db, _KEY_USER_STATE, {})
    cur = all_.get(username, {})
    cur.update(patch)
    all_[username] = cur
    await _set(db, _KEY_USER_STATE, all_)


# ══════════════════════ 会话焦点 ══════════════════════

_FOCUS_RE = re.compile(r"(?:项目\s*)?((?:20\d{2}|TH)[-\w]{3,})")


def focus_from_history(history: list[dict]) -> dict[str, str]:
    """从最近几轮里抓出「当前在聊什么」。

    只认**明确出现过的编号**，不猜。抓不到就返回空 —— 让模型自己去 find_entity，
    比塞一个猜错的焦点强。
    """
    out: dict[str, str] = {}
    for h in reversed(history[-6:]):
        m = _FOCUS_RE.search(h.get("content") or "")
        if m:
            out["project"] = m.group(1)
            break
    return out


def focus_hint(focus: dict[str, str]) -> str:
    if not focus:
        return ""
    bits = [f"{k}={v}" for k, v in focus.items()]
    return f"\n\n（当前话题：{'、'.join(bits)}。用户说「这个/那个」时指的多半是它。）"
