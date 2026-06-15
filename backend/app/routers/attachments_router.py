"""🆕 v3 统一附件：上传 / 下载 / 列表 / 删除。

全系统业务文件（合同/开票申请/发票/图纸包/产物/采购清单/发货单/发货清单/
售后物料清单…）统一经此存取，按附件 ID 关联与撤回（不按文件名匹配）。
文件本体落 settings.files_dir/yyyymm/uuid.ext，原始文件名仅存库用于展示。
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from .. import models, schemas
from ..config import settings
from ..deps import get_current_user
from ..utils import write_audit

router = APIRouter(prefix="/api/attachments", tags=["附件"])

# 允许的业务类型（新模块按需追加；未知类型一律拒绝，防滥用）
ALLOWED_BIZ_TYPES = {
    "contract", "invoice_apply", "invoice",
    "order_input", "order_start_output", "order_output",
    "ship_doc", "ship_list", "aftersales_mat", "purchase_list", "misc",
    "user_feedback",  # 🆕 用户反馈截图
}

# 统一允许的扩展名（按业务需要可在端点层再细化）
ALLOWED_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "csv",
    "jpg", "jpeg", "png", "gif", "bmp", "webp",
    "dwg", "dxf", "zip", "rar", "7z", "ofd", "txt",
}


def _safe_ext(filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    return ext if ext in ALLOWED_EXTS else ""


def _att_to_out(a: models.Attachment) -> schemas.AttachmentOut:
    return schemas.AttachmentOut.model_validate(a)


async def save_upload(
    db: AsyncSession,
    file: UploadFile,
    *,
    biz_type: str,
    biz_id: Optional[int] = None,
    kind: Optional[str] = None,
    project_id: Optional[int] = None,
    user: Optional[models.User] = None,
) -> models.Attachment:
    """供其它业务路由复用的保存函数（如销售合同、任务产物上传走业务端点时内部调用）。
    调用方负责 commit。"""
    if biz_type not in ALLOWED_BIZ_TYPES:
        raise HTTPException(400, f"不支持的附件业务类型: {biz_type}")
    name = (file.filename or "未命名").strip()[:255]
    ext = _safe_ext(name)
    if not ext:
        raise HTTPException(400, "不支持的文件类型")
    content = await file.read()
    if len(content) > settings.max_upload_size:
        raise HTTPException(413, "文件过大")
    sub = datetime.now(timezone.utc).strftime("%Y%m")
    rel = f"{sub}/{uuid.uuid4().hex}.{ext}"
    dest = Path(settings.files_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    a = models.Attachment(
        biz_type=biz_type, biz_id=biz_id, kind=kind, project_id=project_id,
        name=name, ext=ext, size=len(content), path=rel,
        uploaded_by=user.id if user else None,
    )
    db.add(a)
    await db.flush()
    return a


async def delete_attachment_file(db: AsyncSession, att: models.Attachment) -> None:
    """删除附件记录与磁盘文件（撤回场景复用）。调用方负责 commit。"""
    try:
        f = Path(settings.files_dir) / att.path
        if f.is_file():
            f.unlink()
    except OSError:
        pass  # 磁盘清理失败不阻塞业务（孤儿文件可由运维清理）
    await db.delete(att)


@router.post("", response_model=schemas.AttachmentOut)
async def upload(
    file: UploadFile = File(...),
    biz_type: str = Form(...),
    biz_id: Optional[int] = Form(None),
    kind: Optional[str] = Form(None),
    project_id: Optional[int] = Form(None),
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    a = await save_upload(
        db, file, biz_type=biz_type, biz_id=biz_id, kind=kind,
        project_id=project_id, user=current,
    )
    await db.commit()
    await db.refresh(a)
    await write_audit(db, user=current, action="upload", target_type="attachment",
                      target_id=a.id, detail=f"{biz_type}:{a.name}")
    return _att_to_out(a)


@router.get("", response_model=List[schemas.AttachmentOut])
async def list_attachments(
    biz_type: str = Query(...),
    biz_id: Optional[int] = Query(None),
    project_id: Optional[int] = Query(None),
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(models.Attachment).where(models.Attachment.biz_type == biz_type)
    if biz_id is not None:
        q = q.where(models.Attachment.biz_id == biz_id)
    if project_id is not None:
        q = q.where(models.Attachment.project_id == project_id)
    res = await db.execute(q.order_by(models.Attachment.id).limit(200))
    return [_att_to_out(a) for a in res.scalars().all()]


@router.get("/{aid}/download")
async def download(
    aid: int,
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Attachment).where(models.Attachment.id == aid))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "附件不存在")
    f = Path(settings.files_dir) / a.path
    if not f.is_file():
        raise HTTPException(404, "文件已丢失")
    return FileResponse(f, filename=a.name)


@router.delete("/{aid}", response_model=schemas.Msg)
async def remove(
    aid: int,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.Attachment).where(models.Attachment.id == aid))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(404, "附件不存在")
    is_mgr = current.role and current.role.code in ("admin", "manager")
    if not is_mgr and a.uploaded_by != current.id:
        raise HTTPException(403, "仅上传者本人或管理层可删除")
    name = a.name
    await delete_attachment_file(db, a)
    await db.commit()
    await write_audit(db, user=current, action="delete", target_type="attachment",
                      target_id=aid, detail=name)
    return schemas.Msg(message="已删除")
