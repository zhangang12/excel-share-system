"""🆕 云端语音识别（阿里云·智能语音交互·一句话识别）。

为什么要有：Android WebView 没有 Web Speech API，原生 SpeechRecognizer 又依赖
系统语音服务 —— 无 GMS 的国产机（生产实测：华为）直接报
「这台手机没有可用的语音识别服务」。云端识别是唯一能覆盖所有手机的路。

链路（密钥永不出服务端）：
    H5 录 16k 单声道 PCM → POST /api/speech/recognize（Bearer 鉴权）
    → 本模块拿 AK 换临时 token（缓存）→ 转发阿里云一句话识别 → 返回文字

⚠️ 三个配置留空 = 功能关（/available 报 false，前端不显示云端麦克风），
   与现状完全一致。可逆开关红线。
⚠️ 录音会发到阿里云做识别。语音输入的内容是「问句」不是图纸，
   风险量级与企微推送同类；音频不落库、不落盘。
"""
import base64
import hashlib
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import models
from ..config import settings
from ..deps import get_current_user

log = logging.getLogger("speech")
router = APIRouter(prefix="/api/speech", tags=["语音识别"])

# 一句话识别的硬限制：最长 60 秒。16k × 16bit 单声道 → 每秒 32000 字节。
_MAX_AUDIO_BYTES = 60 * 32000 + 44          # +44 给可能的 wav 头留量
_MIN_AUDIO_BYTES = 3200                      # 不足 0.1 秒的多半是误触

_TOKEN_URL = "https://nls-meta.cn-shanghai.aliyuncs.com/"
_ASR_URL = "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr"

# 临时 token 缓存（进程级）。阿里云 token 有效期约 24h，提前 5 分钟换新。
_token_cache: dict = {"token": "", "expire": 0.0}


def _enabled() -> bool:
    return bool(settings.speech_asr_appkey
                and settings.speech_asr_ak_id
                and settings.speech_asr_ak_secret)


# ── 阿里云 RPC 签名（CreateToken 用）─────────────────────────────────
#
# ⚠️ 百分号编码必须按阿里云的规矩来：空格→%20（不是 +）、`*`→%2A、`~` 不编码。
#    用错任何一条，签名校验必然失败且报错里不会告诉你差在哪。

def _pct(s: str) -> str:
    return quote(str(s), safe="~")


def _sign_rpc(params: dict, secret: str) -> str:
    """GET 方式的 RPC 签名：canonicalized query → StringToSign → HMAC-SHA1。"""
    canon = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    to_sign = "GET&" + _pct("/") + "&" + _pct(canon)
    dig = hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(dig).decode()


async def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expire"] > now + 300:
        return _token_cache["token"]
    params = {
        "AccessKeyId": settings.speech_asr_ak_id,
        "Action": "CreateToken",
        "Format": "JSON",
        "RegionId": "cn-shanghai",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2019-02-28",
    }
    params["Signature"] = _sign_rpc(params, settings.speech_asr_ak_secret)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(_TOKEN_URL, params=params)
        data = r.json()
    tok = (data.get("Token") or {}).get("Id")
    exp = (data.get("Token") or {}).get("ExpireTime")
    if not tok:
        # ⚠️ 不把阿里云的原始报错透给前端（里面会带 AccessKeyId 等），只记日志
        log.warning("[speech] CreateToken 失败: %s", str(data)[:300])
        raise HTTPException(502, "语音服务暂时不可用（token）")
    _token_cache["token"] = tok
    _token_cache["expire"] = float(exp or (now + 3600))
    return tok


@router.get("/available")
async def speech_available(_: models.User = Depends(get_current_user)):
    """前端探测用：要不要显示云端麦克风。留空配置 = false，按钮维持现状。"""
    return {"enabled": _enabled()}


@router.post("/recognize")
async def recognize(request: Request,
                    current: models.User = Depends(get_current_user)):
    """一段 16k/16bit/单声道 PCM → 文字。

    ⚠️ 请求体就是**裸音频字节**（Content-Type: application/octet-stream），
       不走 multipart —— 前端是 ArrayBuffer 直接 POST，少一层封装少一类 bug。
    """
    if not _enabled():
        raise HTTPException(503, "云端语音识别未开通")
    audio = await request.body()
    if len(audio) < _MIN_AUDIO_BYTES:
        raise HTTPException(400, "没听清，说长一点再试")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(400, "一次最多说 60 秒")

    token = await _get_token()
    params = {
        "appkey": settings.speech_asr_appkey,
        "format": "pcm",
        "sample_rate": "16000",
        # 标点和数字规整都开：问句里「查一下2026-071」要能还原成数字
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(_ASR_URL, params=params, content=audio,
                                  headers={"X-NLS-Token": token,
                                           "Content-Type": "application/octet-stream"})
            data = r.json()
    except Exception as e:  # noqa: BLE001 —— 网络问题统一转 502，不带内部细节
        log.warning("[speech] ASR 调用失败: %s", e)
        raise HTTPException(502, "语音服务暂时不可用") from e

    if data.get("status") in (40000004, 40010004) or r.status_code in (401, 403):
        # token 失效/被拒 → 清缓存，让下一次重取（本次直接报错，不自动重试拖时长）
        _token_cache["token"] = ""
    if data.get("status") != 20000000:
        log.warning("[speech] ASR 返回异常: %s", str(data)[:300])
        raise HTTPException(502, "识别失败，再说一次试试")
    text = (data.get("result") or "").strip()
    if not text:
        raise HTTPException(400, "没听出内容，再说一次试试")
    return {"text": text}
