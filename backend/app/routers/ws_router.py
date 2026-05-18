"""简化版实时协作：WebSocket 广播单元格变更通知"""
import asyncio
import json
import logging
from typing import Set, Dict
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from ..auth import decode_token

log = logging.getLogger("ws")

router = APIRouter(prefix="/ws", tags=["实时"])


class ConnectionManager:
    """按 room（datasheet:{id} / overview）分组管理 WebSocket"""
    def __init__(self) -> None:
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def join(self, room: str, ws: WebSocket) -> None:
        async with self._lock:
            self.rooms.setdefault(room, set()).add(ws)

    async def leave(self, room: str, ws: WebSocket) -> None:
        async with self._lock:
            if room in self.rooms:
                self.rooms[room].discard(ws)
                if not self.rooms[room]:
                    del self.rooms[room]

    async def broadcast(self, room: str, message: dict, exclude: WebSocket | None = None) -> None:
        targets = list(self.rooms.get(room, ()))
        for ws in targets:
            if ws is exclude: continue
            try:
                await ws.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


def _verify_token(token: str) -> int | None:
    payload = decode_token(token)
    if not payload: return None
    sub = payload.get("sub")
    return int(sub) if sub else None


@router.websocket("/datasheets/{did}")
async def ws_datasheet(ws: WebSocket, did: int, token: str = Query(...)):
    uid = _verify_token(token)
    if not uid:
        await ws.close(code=1008); return
    await ws.accept()
    room = f"datasheet:{did}"
    await manager.join(room, ws)
    log.info("WS join %s by user %s", room, uid)
    # 通知其他人："我进来了"
    await manager.broadcast(room, {"type": "presence", "action": "join", "user_id": uid}, exclude=ws)
    try:
        while True:
            data = await ws.receive_json()
            # 客户端发送 keepalive / 主动广播自己的光标位置
            if isinstance(data, dict) and data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif isinstance(data, dict) and data.get("type") == "presence":
                await manager.broadcast(room, {**data, "user_id": uid}, exclude=ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("WS error: %s", e)
    finally:
        await manager.leave(room, ws)
        await manager.broadcast(room, {"type": "presence", "action": "leave", "user_id": uid})


@router.websocket("/overview")
async def ws_overview(ws: WebSocket, token: str = Query(...)):
    uid = _verify_token(token)
    if not uid:
        await ws.close(code=1008); return
    await ws.accept()
    room = "overview"
    await manager.join(room, ws)
    await manager.broadcast(room, {"type": "presence", "action": "join", "user_id": uid}, exclude=ws)
    try:
        while True:
            data = await ws.receive_json()
            if isinstance(data, dict) and data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif isinstance(data, dict) and data.get("type") == "presence":
                await manager.broadcast(room, {**data, "user_id": uid}, exclude=ws)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.leave(room, ws)
        await manager.broadcast(room, {"type": "presence", "action": "leave", "user_id": uid})


# 提供给其他 router 调用的"广播变更"函数
async def broadcast_cell_changed(scope: str, scope_id: int | None,
                                 record_id: int | None, project_id: int | None,
                                 field_id: int, value, by_user_id: int) -> None:
    """scope: 'datasheet' or 'overview'"""
    room = f"datasheet:{scope_id}" if scope == "datasheet" else "overview"
    await manager.broadcast(room, {
        "type": "cell_changed",
        "record_id": record_id,
        "project_id": project_id,
        "field_id": field_id,
        "value": value,
        "by_user_id": by_user_id,
    })
