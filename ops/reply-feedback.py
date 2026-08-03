#!/usr/bin/env python3
"""批量回复用户反馈 —— 修完问题后把结论回给提出人。

在后端容器里跑（需要 app 包），走的是 `/api/user-feedback/{id}/reply` 同一套逻辑：
置 reply/replied_at/replied_by、status=done、reply_read=False（触发提出人登录弹窗），
并 push_message 双通道通知（站内 + 企微）。写审计。

⚠️ **这是真发出去的**：提出人会在企业微信上收到。跑之前先 --dry-run 看清楚发给谁、发什么。

用法（在服务器上）：
    docker cp ops/reply-feedback.py pms2_backend:/tmp/
    docker cp replies.json          pms2_backend:/tmp/
    docker exec -e PYTHONPATH=/app pms2_backend python /tmp/reply-feedback.py /tmp/replies.json --dry-run
    docker exec -e PYTHONPATH=/app pms2_backend python /tmp/reply-feedback.py /tmp/replies.json

replies.json 形如：
    {"339": "是多了……", "340": "已加……"}

回复怎么写（用户反馈过三次，别再写小作文）：
    两三句说完——改了什么、在哪看、要不要更新客户端。不解释技术细节。
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app import models
from app.notify import push_message
from app.utils import write_audit


async def main(path: str, dry: bool, actor: str):
    replies = json.load(open(path, encoding="utf-8"))
    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == actor))).scalar_one_or_none()
        if not me:
            sys.exit(f"❌ 找不到操作人 {actor}")

        rows = []
        for fid_s, text in replies.items():
            fid = int(fid_s)
            fb = (await db.execute(select(models.UserFeedback).where(
                models.UserFeedback.id == fid))).scalar_one_or_none()
            if not fb:
                print(f"  ⚠️  #{fid} 不存在，跳过")
                continue
            if fb.reply:
                # 已经回过就别覆盖——可能是人工回的，覆盖会把人家的话冲掉
                print(f"  ⏭  #{fid} 已有回复，跳过：{fb.reply[:40]}")
                continue
            u = (await db.execute(select(models.User).where(
                models.User.id == fb.user_id))).scalar_one_or_none() if fb.user_id else None
            rows.append((fb, u, text.strip()))

        if not rows:
            print("  没有需要回复的。")
            return

        print(f"\n{'将要发送' if not dry else '【dry-run】只看不发'}：")
        for fb, u, text in rows:
            who = (u.full_name or u.username) if u else "(无提出人，不推送)"
            print(f"\n  ── #{fb.id} → {who} ──")
            print(f"     原文：{(fb.content or '')[:60]}")
            print(f"     回复：{text}")
        if dry:
            print(f"\n  共 {len(rows)} 条。去掉 --dry-run 才会真发。")
            return

        for fb, u, text in rows:
            fb.reply = text[:5000]
            fb.replied_at = datetime.now(timezone.utc)
            fb.replied_by = me.id
            fb.reply_read = False          # 提出人下次登录右下角弹窗
            fb.status = "done"
            await db.commit()
            if fb.user_id and fb.user_id != me.id:
                await push_message(
                    db, to_user_id=fb.user_id, kind="info",
                    text=f"【反馈回复】你反馈的问题（#{fb.id}）已有回复："
                         f"{text[:60]}{'…' if len(text) > 60 else ''}",
                    biz_type="user_feedback", biz_id=fb.id)
            await write_audit(db, user=me, action="user_feedback_reply",
                              target_type="user_feedback", target_id=fb.id,
                              detail=text[:80])
            print(f"  ✅ #{fb.id} 已回复并通知 {(u.full_name if u else '-')}")
        print(f"\n  共 {len(rows)} 条已发出。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="replies.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--actor", default="admin", help="以谁的身份回复（默认 admin）")
    a = ap.parse_args()
    asyncio.run(main(a.path, a.dry_run, a.actor))
