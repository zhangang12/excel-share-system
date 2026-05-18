#!/bin/bash
# 应急：重置 admin 用户密码（admin 忘密码 / 被锁时）
#
# 用法:
#   bash reset-admin-password.sh                  # 交互式输入新密码
#   bash reset-admin-password.sh admin 'NewPwd!'  # 指定用户名 + 密码

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

USERNAME="${1:-admin}"
NEWPWD="$2"

if [[ -z "$NEWPWD" ]]; then
    read -s -p "新密码（至少 6 位）: " NEWPWD; echo
    read -s -p "再输入一次: " NEWPWD2; echo
    [[ "$NEWPWD" != "$NEWPWD2" ]] && { echo "两次不一致"; exit 1; }
fi

if [[ "${#NEWPWD}" -lt 6 ]]; then
    echo "密码至少 6 位"
    exit 1
fi

echo "重置 [$USERNAME] 密码..."

docker exec -i pms2_backend python - <<PYEOF
import asyncio, sys
sys.path.insert(0, '/app')
from sqlalchemy import select
from app.database import SessionLocal
from app import models
from app.auth import hash_password

USERNAME = "$USERNAME"
NEWPWD = """$NEWPWD"""

async def main():
    async with SessionLocal() as db:
        res = await db.execute(select(models.User).where(models.User.username == USERNAME))
        u = res.scalar_one_or_none()
        if not u:
            print(f'ERROR: 用户 {USERNAME} 不存在')
            sys.exit(1)
        u.password_hash = hash_password(NEWPWD)
        u.password_must_change = False
        u.is_active = True
        await db.commit()
        print(f'OK: {USERNAME} 密码已重置；is_active=True；password_must_change=False')

asyncio.run(main())
PYEOF
