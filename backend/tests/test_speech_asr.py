"""🆕 云端语音识别（阿里云一句话识别代理）。

锁四类事：
 1. **开关**：三个配置留空 = /available 报 false、/recognize 一律 503。
    不配就是现状，可逆开关红线。
 2. **签名**：阿里云 RPC 签名的百分号编码规矩（空格→%20、`*`→%2A、`~` 不编码），
    错任何一条签名必然失败且报错不会说差在哪。
 3. **音频闸**：太短（误触）、太长（超一句话识别 60s 上限）都在本地拒掉，
    别把注定失败的请求发出去白花钱。
 4. **不泄漏**：阿里云的原始报错（含 AccessKeyId）绝不透给前端。
"""
import asyncio
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="speech")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers import speech_router as sp


@pytest.fixture
def enabled():
    old = (settings.speech_asr_appkey, settings.speech_asr_ak_id,
           settings.speech_asr_ak_secret)
    settings.speech_asr_appkey = "testAppKey"
    settings.speech_asr_ak_id = "LTAItest"
    settings.speech_asr_ak_secret = "secret"
    sp._token_cache.update({"token": "", "expire": 0.0})
    yield
    (settings.speech_asr_appkey, settings.speech_asr_ak_id,
     settings.speech_asr_ak_secret) = old
    sp._token_cache.update({"token": "", "expire": 0.0})


class _Req:
    def __init__(self, body: bytes):
        self._b = body

    async def body(self):
        return self._b


def test_默认关_available为false():
    assert settings.speech_asr_appkey == ""
    assert sp._enabled() is False


def test_默认关_识别一律503():
    with pytest.raises(HTTPException) as e:
        asyncio.run(sp.recognize(_Req(b"\x00" * 64000), None))
    assert e.value.status_code == 503


def test_开了之后enabled为真(enabled):
    assert sp._enabled() is True


def test_签名的百分号编码规矩():
    """空格→%20（不是 +）、`*`→%2A、`~` 不编码 —— 阿里云铁律。"""
    assert sp._pct("a b") == "a%20b"
    assert sp._pct("a*b") == "a%2Ab"
    assert sp._pct("a~b") == "a~b"
    assert sp._pct("a/b") == "a%2Fb"


def test_签名稳定且随密钥变化():
    p = {"Action": "CreateToken", "AccessKeyId": "LTAIx", "Timestamp": "2026-08-27T00:00:00Z"}
    s1 = sp._sign_rpc(dict(p), "secretA")
    s2 = sp._sign_rpc(dict(reversed(list(p.items()))), "secretA")
    assert s1 == s2, "参数顺序不影响签名（内部要排序）"
    assert sp._sign_rpc(dict(p), "secretB") != s1, "换密钥签名必须变"


def test_音频太短太长都本地拒(enabled):
    with pytest.raises(HTTPException) as e:
        asyncio.run(sp.recognize(_Req(b"\x00" * 100), None))
    assert e.value.status_code == 400 and "没听清" in e.value.detail

    with pytest.raises(HTTPException) as e2:
        asyncio.run(sp.recognize(_Req(b"\x00" * (sp._MAX_AUDIO_BYTES + 1)), None))
    assert e2.value.status_code == 400 and "60" in e2.value.detail


def test_识别成功链路_mock阿里云(enabled, monkeypatch):
    calls = {}

    class _Resp:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls["token_params"] = params
            return _Resp({"Token": {"Id": "tok123", "ExpireTime": 9999999999}})

        async def post(self, url, params=None, content=None, headers=None):
            calls["asr_params"] = params
            calls["asr_headers"] = headers
            calls["asr_len"] = len(content)
            return _Resp({"status": 20000000, "result": "查一下2026-071的进度"})

    monkeypatch.setattr(sp.httpx, "AsyncClient", _Client)
    out = asyncio.run(sp.recognize(_Req(b"\x00" * 64000), None))
    assert out == {"text": "查一下2026-071的进度"}
    assert calls["token_params"]["Signature"], "token 请求带了签名"
    assert calls["asr_params"]["appkey"] == "testAppKey"
    assert calls["asr_params"]["format"] == "pcm"
    assert calls["asr_params"]["sample_rate"] == "16000"
    assert calls["asr_headers"]["X-NLS-Token"] == "tok123"
    assert calls["asr_len"] == 64000, "音频原样转发，不动一个字节"
    # token 已缓存：再来一次不再打 token 接口
    calls.pop("token_params")
    asyncio.run(sp.recognize(_Req(b"\x00" * 64000), None))
    assert "token_params" not in calls, "第二次用缓存的 token"


def test_阿里云报错不泄漏给前端(enabled, monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"ErrMsg": "InvalidAccessKeyId LTAIxxxx not found"}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(sp.httpx, "AsyncClient", _Client)
    with pytest.raises(HTTPException) as e:
        asyncio.run(sp.recognize(_Req(b"\x00" * 64000), None))
    assert e.value.status_code == 502
    assert "LTAI" not in str(e.value.detail), "**阿里云原始报错（含 AK）不许透出去**"


def test_识别失败状态码_清token缓存(enabled, monkeypatch):
    class _Resp:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return _Resp({"Token": {"Id": "tokX", "ExpireTime": 9999999999}})

        async def post(self, url, **k):
            return _Resp({"status": 40000004, "result": ""})   # token 失效

    monkeypatch.setattr(sp.httpx, "AsyncClient", _Client)
    with pytest.raises(HTTPException) as e:
        asyncio.run(sp.recognize(_Req(b"\x00" * 64000), None))
    assert e.value.status_code == 502
    assert sp._token_cache["token"] == "", "token 失效要清缓存，下次重取"
