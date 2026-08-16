"""🆕 企业微信内嵌：可点消息 + 静默登录。

锁四件事：

 1. **没配可信域名就发纯文本**——配了才发 textcard。配错顺序（域名没验证就发链接）
    的话，用户点开只会看到企微的报错页，比没有链接更糟。
 2. **开关默认关**。`wecom_oauth_enabled=False` 时 `/api/auth/wecom` 一律 403，
    不给任何旁路。
 3. **找不到绑定的人 → 403，绝不自动建账号**。企微通讯录里有访客和外部联系人，
    自动建号等于给系统开后门。
 4. **入口地址永远不报错**。它配在企微工作台上，报错等于整个应用打不开——
    没开通就 302 回普通登录页。
"""
import os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="wecom")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

import pytest
from app.config import settings
from app import notify
from app.routers.auth_router import wecom_authorize_url


@pytest.fixture
def wecom_cfg():
    """临时给上企微配置，测完还原——settings 是进程级单例，改了不还原会污染别的用例。"""
    old = (settings.wecom_corp_id, settings.wecom_agent_id,
           settings.public_base_url, settings.wecom_oauth_enabled)
    settings.wecom_corp_id = "wwTESTCORP"
    settings.wecom_agent_id = "1000003"
    settings.public_base_url = "https://www.tonghuizhineng.top"
    settings.wecom_oauth_enabled = True
    yield
    (settings.wecom_corp_id, settings.wecom_agent_id,
     settings.public_base_url, settings.wecom_oauth_enabled) = old


def test_没配可信域名就发纯文本():
    """⚠️ 顺序不能反：域名没验证先发链接，用户点开是企微的报错页。"""
    old = settings.public_base_url
    settings.public_base_url = ""
    try:
        settings.wecom_agent_id = "1000003"
        p = notify._wecom_payload(["zhang"], "【逾期】2026-060A 还剩 5 天")
        assert p["msgtype"] == "text"
        assert "url" not in str(p)
        assert notify.h5_url() == ""
    finally:
        settings.public_base_url = old


def test_配了域名就发可点卡片(wecom_cfg):
    p = notify._wecom_payload(["zhang", "li"], "【逾期】2026-060A 还剩 5 天\n卡在电工")
    assert p["msgtype"] == "textcard"
    card = p["textcard"]
    assert card["url"] == "https://www.tonghuizhineng.top/h5/"
    assert card["title"] == "【逾期】2026-060A 还剩 5 天"   # 第一行当标题
    assert "卡在电工" in card["description"]
    assert p["touser"] == "zhang|li"
    assert p["agentid"] == 1000003


def test_标题和正文都截断(wecom_cfg):
    p = notify._wecom_payload(["a"], "标" * 900)
    assert len(p["textcard"]["title"]) <= 36
    assert len(p["textcard"]["description"]) <= 500


def test_单次最多1000人(wecom_cfg):
    p = notify._wecom_payload([f"u{i}" for i in range(1500)], "x")
    assert len(p["touser"].split("|")) == 1000


def test_授权地址是静默模式(wecom_cfg):
    """snsapi_base = 不弹授权页。用 snsapi_userinfo 会让每个人先点一次「同意」。"""
    u = wecom_authorize_url("https://www.tonghuizhineng.top/h5/")
    assert "scope=snsapi_base" in u
    assert "appid=wwTESTCORP" in u
    assert u.endswith("#wechat_redirect")          # 少这个尾巴企微不认
    assert "redirect_uri=https%3A%2F%2F" in u      # 必须 URL 编码


def test_开关关掉时登录接口一律拒绝():
    """默认关。域名验证没做完就放开，用户点进来只会看到报错页。"""
    assert settings.wecom_oauth_enabled is False

    import asyncio
    from fastapi import HTTPException
    from app.routers.auth_router import wecom_login
    from app import schemas

    with pytest.raises(HTTPException) as e:
        asyncio.run(wecom_login(schemas.WecomLoginIn(code="anything"),
                                request=None, db=None))
    assert e.value.status_code == 403


class _FakeReq:
    """_client_ip 只读 headers 和 client，给个最小壳就够。"""
    headers: dict = {}
    client = None


async def _prepare_db():
    from app.database import engine, SessionLocal, Base
    from app.data_migration import ensure_schema_columns
    from app import models
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        db.add(models.Role(id=1, code="sales", name="销售"))
        await db.flush()
        db.add(models.User(username="yangtan", full_name="杨坛", password_hash="x",
                           role_id=1, wxid="YangTan"))
        db.add(models.User(username="lixin", full_name="李新新", password_hash="x",
                           role_id=1, wxid="LiXin", is_active=False))
        await db.commit()
    return SessionLocal


def test_绑定了就发token_没绑定只报错不建号(wecom_cfg, monkeypatch):
    """⚠️ 最要紧的一条：企微通讯录里有访客和外部联系人，
       认出身份 ≠ 该给系统账号。自动建号等于开后门。"""
    import asyncio
    from fastapi import HTTPException
    from app.routers.auth_router import wecom_login
    from app import schemas, models
    from sqlalchemy import select

    async def run():
        SessionLocal = await _prepare_db()
        who = {"v": "YangTan"}

        async def fake(code):
            assert code == "CODE"       # code 必须原样透传，别被 strip 掉内容
            return who["v"]
        monkeypatch.setattr("app.notify.wecom_userid_by_code", fake)

        # ① 绑定了 → 发 token
        async with SessionLocal() as db:
            out = await wecom_login(schemas.WecomLoginIn(code="CODE"), _FakeReq(), db)
        assert out.access_token and out.user.username == "yangtan"

        # ② 企微认得、系统里没绑 → 403，且**一个用户都不许多出来**
        who["v"] = "SomeVisitor"
        async with SessionLocal() as db:
            before = len((await db.execute(select(models.User.id))).all())
            with pytest.raises(HTTPException) as e:
                await wecom_login(schemas.WecomLoginIn(code="CODE"), _FakeReq(), db)
            assert e.value.status_code == 403
            after = len((await db.execute(select(models.User.id))).all())
        assert before == after, "绝不能自动建账号"

        # ③ 停用的人 → 403
        who["v"] = "LiXin"
        async with SessionLocal() as db:
            with pytest.raises(HTTPException) as e:
                await wecom_login(schemas.WecomLoginIn(code="CODE"), _FakeReq(), db)
            assert e.value.status_code == 403

        # ④ 企微那边挂了 → 401，且不泄漏内部细节
        async def boom(code):
            raise RuntimeError("企微 getuserinfo 失败: errcode=40029")
        monkeypatch.setattr("app.notify.wecom_userid_by_code", boom)
        async with SessionLocal() as db:
            with pytest.raises(HTTPException) as e:
                await wecom_login(schemas.WecomLoginIn(code="CODE"), _FakeReq(), db)
            assert e.value.status_code == 401
            assert "errcode" not in str(e.value.detail)

    asyncio.run(run())


def test_空code直接拒绝(wecom_cfg):
    import asyncio
    from fastapi import HTTPException
    from app.routers.auth_router import wecom_login
    from app import schemas

    with pytest.raises(HTTPException) as e:
        asyncio.run(wecom_login(schemas.WecomLoginIn(code="   "),
                                request=None, db=None))
    assert e.value.status_code == 400
