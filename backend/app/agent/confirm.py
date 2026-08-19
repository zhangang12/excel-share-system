"""🆕 写操作的「确认令牌」：把一次**即将发生的写**和它的内容绑死。

和 `cards/token.py` 是同一套思路，但绑的东西不同：
  · 卡片令牌绑 (谁, 哪类卡, **哪条已存在的记录**) —— 防止把 A 卡按钮接到 B 条记录上
  · 这里绑 (谁, 哪个动作, **内容本身**)          —— 记录还不存在，只能绑内容

为什么需要它：智能体发待办是「问几轮 → 草稿 → 点确认 → 真发出」。
草稿阶段没有数据库行，给不出 ref，卡片那套用不上；而「确认」这一步
**必须是模型无法自己跳过的**——不然模型多想一步就把待办发出去了，
收件人凭空收到一条任务，还撤不回来。

做法：草稿阶段服务端按内容签一个令牌交给模型；模型把它原样带回来才允许写。
令牌是 HMAC，模型编不出来；内容改一个字签名就对不上，
所以也不能「拿着确认 A 的令牌去发 B」。
"""
import base64
import hashlib
import hmac
import json
import time

from ..config import settings

_TTL_SEC = 10 * 60          # 草稿放十分钟够了；过期就重新问一遍，不猜


def _canon(action: str, user_id: int, payload: dict) -> bytes:
    """规范化：键排序 + 去空格，保证同样的内容签出同样的名。"""
    return json.dumps({"a": action, "u": user_id, "p": payload},
                      separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False).encode()


def _sign(body: bytes) -> str:
    sig = hmac.new(settings.secret_key.encode(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def issue(action: str, user_id: int, payload: dict, *,
          now: float | None = None) -> str:
    """签发确认令牌。now 只为测试注入时间，业务侧不要传。"""
    iat = int(now if now is not None else time.time())
    body = _canon(action, user_id, {**payload, "iat": iat})
    return f"{iat}.{_sign(body)}"


def verify(token: str, action: str, user_id: int, payload: dict, *,
           now: float | None = None) -> tuple[bool, str]:
    """校验 → (是否通过, 失败原因)。任何异常都当没通过，不向上抛。"""
    try:
        iat_s, sig = (token or "").strip().split(".", 1)
        iat = int(iat_s)
    except Exception:
        return False, "确认码格式不对，请重新确认一次"
    t = int(now if now is not None else time.time())
    if t - iat > _TTL_SEC:
        return False, "确认码超过 10 分钟已失效，请重新说一遍"
    if iat - t > 60:                       # 时钟偏差容忍一分钟，未来的一律不认
        return False, "确认码时间异常"
    expect = _sign(_canon(action, user_id, {**payload, "iat": iat}))
    if not hmac.compare_digest(expect, sig):
        # ⚠️ 这里**不要**提示「内容变了」之外的细节：具体哪一项对不上
        #    是攻击者可控的探测面。
        return False, "确认码和要发的内容对不上，请重新确认一次"
    return True, ""
