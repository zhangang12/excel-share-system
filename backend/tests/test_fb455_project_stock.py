"""#455 后续：恢复全仓领用口径，同时保留项目状态供页面筛选。"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix='fb455-resume-')
os.environ['DATABASE_URL'] = os.environ.get('FB455_DATABASE_URL', f'sqlite+aiosqlite:///{tmp}/test.db')
os.environ['FILES_DIR'] = f'{tmp}/files'
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        r = await c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})
        h = {'Authorization': 'Bearer ' + r.json()['access_token']}
        pids = []
        for code in ('455-A', '455-B'):
            r = await c.post('/api/projects', headers=h, json={'code': code, 'name': code})
            assert r.status_code == 200, r.text
            pids.append(r.json()['id'])
        a, b = pids
        async with SessionLocal() as db:
            p = await db.get(models.Project, a); p.status = '已完成'
            await db.commit()
        r = await c.post('/api/wh/materials', headers=h, json={'name':'恢复领用测试','spec':'455','unit':'个','init_stock':0})
        mid = r.json()['id']
        async def txn(direction, qty, pid):
            return await c.post('/api/wh/txns', headers=h, json={'material_id':mid,'direction':direction,'qty':qty,'project_id':pid,'biz_date':'2026-11-01'})
        assert (await txn('in', 5, a)).status_code == 200
        # B 可从全仓领用 A 的同型号库存，保持原有登记和库存上限校验。
        r = await txn('out', 2, b)
        assert r.status_code == 200, r.text
        r = await c.post(f'/api/wh/demand/{b}/issue',headers=h,json={'lines':[{'material_id':mid,'qty':2}]})
        assert r.status_code == 200, r.text
        r = await txn('out', 2, b)
        assert r.status_code == 400, r.text  # 全仓剩余 1，仍拦截超出库存
        r = await c.get('/api/wh/demand-overview', headers=h)
        assert r.status_code == 200, r.text
        historic = next(x for x in r.json() if x['project_id'] == a)
        assert historic['project_status'] == '已完成', historic
        print('PASSED 全仓领用恢复、库存上限保留、已完成项目保留历史')


if __name__ == '__main__':
    asyncio.run(main())
