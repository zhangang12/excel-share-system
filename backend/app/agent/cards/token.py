"""卡片令牌：把「谁、哪类卡、哪条记录」绑死，15 分钟过期。

它不是权限凭证——真正的鉴权还是用户自己的 Bearer token 打业务端点。
它防的是另一件事：模型或页面把 A 卡的按钮接到 B 条记录上。
后端收到动作时校验 token 里的 (user_id, type, ref) 与被点的动作是否一致，
对不上直接拒，不去猜意图。
"""
import base64
import hashlib
import hmac
import json
import time

from ..cards.registry import CARD_TYPES
from ...config import settings

_TTL_SEC = 15 * 60


def _sign(payload: bytes) -> str:
    sig = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(user_id: int, card_type: str, ref: int, *, now: float | None = None) -> str:
    """签发。now 参数只为测试注入时间，业务侧不要传。"""
    body = json.dumps({"u": user_id, "t": card_type, "r": ref,
                       "iat": int(now if now is not None else time.time())},
                      separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str, user_id: int, card_type: str, ref: int,
           *, now: float | None = None) -> tuple[bool, str]:
    """校验 → (是否通过, 失败原因)。任何异常都当作校验不通过，不向调用方抛。"""
    try:
        raw, sig = token.split(".", 1)
        body = _unb64(raw)
    except Exception:
        return False, "令牌格式不对"
    # 先比签名再看内容：内容是攻击者可控的，未验签之前不做任何解释
    if not hmac.compare_digest(_sign(body), sig):
        return False, "令牌签名不匹配"
    try:
        d = json.loads(body)
    except Exception:
        return False, "令牌内容无法解析"
    t = int(now if now is not None else time.time())
    if t - int(d.get("iat", 0)) > _TTL_SEC:
        return False, "令牌已过期，请重新打开卡片"
    if d.get("u") != user_id:
        return False, "令牌不属于当前用户"
    if d.get("t") != card_type or d.get("r") != ref:
        return False, "令牌与所点动作不匹配"
    if d.get("t") not in CARD_TYPES:
        return False, "卡片类型不在白名单内"
    return True, ""
