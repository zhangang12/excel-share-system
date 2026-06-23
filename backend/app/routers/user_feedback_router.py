"""🆕 用户反馈小助手（任意角色可提交，管理层汇总+导出 HTML）。

- POST /api/user-feedback        任意登录用户提交（form: kind/content/page_url + 可选 file 截图）
- GET  /api/user-feedback        管理层看全部 / 普通用户只看自己
- POST /api/user-feedback/{id}/done  管理层标记已处理
- GET  /api/user-feedback/export.html  管理层导出为 HTML（截图 base64 内嵌，单文件自包含）
"""
import base64
from datetime import datetime, timezone
from html import escape
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, Depends, Form, UploadFile, File, Query, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_admin_or_manager
from ..utils import write_audit
from .attachments_router import save_upload

router = APIRouter(prefix="/api/user-feedback", tags=["用户反馈"])

KINDS = {"bug", "suggest", "other"}
KIND_LABEL = {"bug": "问题反馈", "suggest": "意见建议", "other": "其它"}


def _row_out(fb: models.UserFeedback) -> schemas.UserFeedbackRow:
    return schemas.UserFeedbackRow(
        id=fb.id, kind=fb.kind, content=fb.content, page_url=fb.page_url,
        user_agent=fb.user_agent, status=fb.status, created_at=fb.created_at,
        user_id=fb.user_id,
        user_name=(fb.user.full_name or fb.user.username) if fb.user else None,
        user_role=("/".join(sorted(fb.user.role_codes)) or None) if fb.user else None,
        shot_file_id=fb.shot_file_id,
        shot_file_name=fb.shot.name if fb.shot else None,
        reply=fb.reply,
        replied_at=fb.replied_at,
        replier_name=(fb.replier.full_name or fb.replier.username) if fb.replier else None,
        reply_read=bool(fb.reply_read),
    )


@router.post("", response_model=schemas.UserFeedbackRow)
async def submit(
    request: Request,
    kind: str = Form("bug"),
    content: str = Form(...),
    page_url: str = Form(""),
    file: Optional[UploadFile] = File(None),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """任意登录用户均可提交。截图可选。"""
    k = kind if kind in KINDS else "bug"
    text = (content or "").strip()
    if not text:
        from fastapi import HTTPException
        raise HTTPException(400, "请填写问题/建议内容")
    fb = models.UserFeedback(
        user_id=current.id, kind=k, content=text[:5000],
        page_url=(page_url or "")[:255] or None,
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        status="open",
    )
    db.add(fb)
    await db.flush()
    if file is not None and file.filename:
        a = await save_upload(db, file, biz_type="user_feedback", biz_id=fb.id, user=current)
        fb.shot_file_id = a.id
    await db.commit()
    await db.refresh(fb)
    # 重新加载关联(user/shot)
    r = await db.execute(select(models.UserFeedback).where(models.UserFeedback.id == fb.id))
    fb2 = r.scalar_one()
    await write_audit(db, user=current, action="user_feedback_submit",
                      target_type="user_feedback", target_id=fb.id, detail=text[:80])
    return _row_out(fb2)


@router.get("", response_model=List[schemas.UserFeedbackRow])
async def list_feedback(
    mine: bool = Query(False, description="普通用户强制只看自己;管理层显式传 true 则只看自己"),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """管理层(admin/manager)可见全部,其余角色只看自己。"""
    is_mgr = current.has_role("admin", "manager")
    q = select(models.UserFeedback)
    if not is_mgr or mine:
        q = q.where(models.UserFeedback.user_id == current.id)
    if kind in KINDS:
        q = q.where(models.UserFeedback.kind == kind)
    if status in ("open", "done"):
        q = q.where(models.UserFeedback.status == status)
    q = q.order_by(models.UserFeedback.id.desc()).limit(500)
    rows = list((await db.execute(q)).scalars().all())
    return [_row_out(r) for r in rows]


@router.post("/{fid}/done", response_model=schemas.Msg)
async def mark_done(
    fid: int,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """管理层标记已处理。"""
    r = await db.execute(select(models.UserFeedback).where(models.UserFeedback.id == fid))
    fb = r.scalar_one_or_none()
    if not fb:
        from fastapi import HTTPException
        raise HTTPException(404, "反馈不存在")
    fb.status = "done"
    await db.commit()
    await write_audit(db, user=current, action="user_feedback_done",
                      target_type="user_feedback", target_id=fid)
    return schemas.Msg(message="已标记为已处理")


class ReplyIn(BaseModel):
    reply: str


@router.post("/{fid}/reply", response_model=schemas.UserFeedbackRow)
async def reply_feedback(
    fid: int,
    body: ReplyIn,
    current: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """🆕 管理层回复处理意见（系统回信）。回复即视为已处理；提出人下次登录右下角弹窗提醒查看。"""
    text = (body.reply or "").strip()
    if not text:
        raise HTTPException(400, "请填写处理意见回复")
    r = await db.execute(select(models.UserFeedback).where(models.UserFeedback.id == fid))
    fb = r.scalar_one_or_none()
    if not fb:
        raise HTTPException(404, "反馈不存在")
    fb.reply = text[:5000]
    fb.replied_at = datetime.now(timezone.utc)
    fb.replied_by = current.id
    fb.reply_read = False          # 新回复 → 提出人未读，触发登录弹窗
    fb.status = "done"
    await db.commit()
    r2 = await db.execute(select(models.UserFeedback).where(models.UserFeedback.id == fid))
    fb2 = r2.scalar_one()
    await write_audit(db, user=current, action="user_feedback_reply",
                      target_type="user_feedback", target_id=fid, detail=text[:80])
    return _row_out(fb2)


@router.get("/my-unread-replies", response_model=List[schemas.UserFeedbackRow])
async def my_unread_replies(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提出人本人：有处理意见回复且未读的反馈（登录后右下角弹窗提醒用）。"""
    q = (select(models.UserFeedback)
         .where(models.UserFeedback.user_id == current.id,
                models.UserFeedback.reply.is_not(None),
                models.UserFeedback.reply_read == False)  # noqa: E712
         .order_by(models.UserFeedback.id.desc()))
    rows = list((await db.execute(q)).scalars().all())
    return [_row_out(r) for r in rows]


@router.post("/replies/read", response_model=schemas.Msg)
async def mark_replies_read(
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提出人查看后：把本人全部未读回复标记为已读（消除登录弹窗提醒）。"""
    await db.execute(sa_update(models.UserFeedback)
        .where(models.UserFeedback.user_id == current.id,
               models.UserFeedback.reply.is_not(None),
               models.UserFeedback.reply_read == False)  # noqa: E712
        .values(reply_read=True))
    await db.commit()
    return schemas.Msg(message="已标记为已读")


def _img_data_uri(path: Path) -> Optional[str]:
    if not path or not path.exists():
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp"}.get(ext)
    if not mime:
        return None
    try:
        b = path.read_bytes()
        if len(b) > 4 * 1024 * 1024:  # 单图超 4MB 不内嵌(导出文件过大);只显示链接占位
            return None
        return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"
    except Exception:
        return None


@router.get("/export.html", response_class=HTMLResponse)
async def export_html(
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    _: models.User = Depends(require_admin_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """管理层导出全部反馈为 HTML（自包含，截图 base64 内嵌）。"""
    from ..config import settings
    q = select(models.UserFeedback)
    if kind in KINDS:
        q = q.where(models.UserFeedback.kind == kind)
    if status in ("open", "done"):
        q = q.where(models.UserFeedback.status == status)
    q = q.order_by(models.UserFeedback.id.desc())
    rows = list((await db.execute(q)).scalars().all())

    items_html = []
    for fb in rows:
        uname = (fb.user.full_name or fb.user.username) if fb.user else "—"
        urole = ("/".join(sorted(fb.user.role_codes)) or "—") if fb.user else "—"
        shot_html = ""
        if fb.shot:
            uri = _img_data_uri(Path(settings.files_dir) / fb.shot.path)
            if uri:
                shot_html = f'<div class="shot"><img src="{uri}" alt="{escape(fb.shot.name)}"/></div>'
            else:
                shot_html = f'<div class="shot-miss">[截图未内嵌：{escape(fb.shot.name)}]</div>'
        kind_label = KIND_LABEL.get(fb.kind, fb.kind)
        status_label = "已处理" if fb.status == "done" else "待处理"
        items_html.append(f"""<article class="card status-{fb.status}">
  <header>
    <span class="id">#{fb.id}</span>
    <span class="kind kind-{fb.kind}">{escape(kind_label)}</span>
    <span class="status">{status_label}</span>
    <span class="user">{escape(uname)} · {escape(urole)}</span>
    <span class="time">{fb.created_at.strftime('%Y-%m-%d %H:%M')}</span>
  </header>
  <div class="content">{escape(fb.content)}</div>
  {f'<div class="page">页面：<code>{escape(fb.page_url)}</code></div>' if fb.page_url else ''}
  {shot_html}
</article>""")

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>用户反馈导出 · {now}</title>
<style>
  body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#1f2937;background:#f3f5f9}}
  h1{{font-size:22px;margin:0 0 6px}}
  .meta{{color:#9ca3af;font-size:13px;margin-bottom:18px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
  .card header{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px;margin-bottom:10px}}
  .id{{color:#9ca3af;font-weight:600}}
  .kind{{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}}
  .kind-bug{{background:#fee2e2;color:#991b1b}}
  .kind-suggest{{background:#dbeafe;color:#1e40af}}
  .kind-other{{background:#f3f4f6;color:#4b5563}}
  .status{{padding:2px 8px;border-radius:10px;background:#fef3c7;color:#92400e;font-size:12px}}
  .status-done .status{{background:#d1fae5;color:#065f46}}
  .user{{color:#4b5563}} .time{{color:#9ca3af;margin-left:auto}}
  .content{{font-size:14.5px;line-height:1.6;white-space:pre-wrap;color:#1f2937}}
  .page{{margin-top:8px;font-size:12.5px;color:#9ca3af}} .page code{{background:#f3f4f6;padding:1px 6px;border-radius:4px;color:#4b5563}}
  .shot{{margin-top:10px}} .shot img{{max-width:100%;border:1px solid #e5e7eb;border-radius:6px;display:block}}
  .shot-miss{{margin-top:10px;color:#9ca3af;font-size:12.5px;background:#f9fafb;padding:8px 10px;border-radius:6px}}
</style></head>
<body>
  <h1>用户反馈导出</h1>
  <div class="meta">共 {len(rows)} 条 · 导出时间：{now}</div>
  {''.join(items_html) if items_html else '<div class="card">暂无反馈</div>'}
</body></html>"""
    headers = {"Content-Disposition": f'attachment; filename="user-feedback-{now.replace(" ","_").replace(":","")}.html"'}
    return HTMLResponse(content=html_doc, headers=headers)
