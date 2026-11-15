"""#455：项目库存隔离、通用库存扣减、冲突保护与总览口径。"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix='fb455-')
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
        await seed(db)
        await run_all(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        r = await c.post('/api/auth/login', json={'username':'admin', 'password':'admin123'})
        h = {'Authorization':'Bearer ' + r.json()['access_token']}
        projects = []
        for code in ('455-A', '455-B', '455-C'):
            r = await c.post('/api/projects', headers=h, json={'code':code, 'name':code})
            assert r.status_code == 200, r.text
            projects.append(r.json()['id'])
        a,b,d = projects
        r = await c.post('/api/wh/materials', headers=h, json={'name':'共享规格测试', 'spec':'455', 'unit':'个', 'init_stock':3})
        assert r.status_code == 200, r.text
        mid = r.json()['id']
        async def txn(direction, qty, pid=None):
            return await c.post('/api/wh/txns', headers=h, json={'material_id':mid,'direction':direction,'qty':qty,'project_id':pid,'biz_date':'2026-11-01'})
        assert (await txn('in',5,a)).status_code == 200
        assert (await txn('in',4,b)).status_code == 200
        # B 的库存不能被 C 领用；C 只能使用三件通用库存。
        r = await txn('out',4,d)
        assert r.status_code == 400, r.text
        assert (await txn('out',2,d)).status_code == 200
        r = await txn('out',2,d)
        assert r.status_code == 400, r.text
        # 未领完的通用库存按所有项目的领用扣减；不得再次调拨或批量领走他人库存。
        r = await c.post('/api/wh/txns/batch-out',headers=h,json={'project_id':d,'biz_date':'2026-11-01','lines':[{'material_id':mid,'qty':2}]})
        assert r.status_code == 400, r.text
        r = await c.post('/api/wh/transfer-to-project',headers=h,json={'project_id':d,'lines':[{'material_id':mid,'qty':2}]})
        assert r.status_code == 400, r.text
        # 本项目先消耗自己的库存，随后允许领通用库存；再次核对其他项目的专属库存。
        r = await c.post(f'/api/wh/demand/{a}/issue',headers=h,json={'lines':[{'material_id':mid,'qty':6}]})
        assert r.status_code == 200, r.text
        assert (await txn('out',1,d)).status_code == 400
        r = await c.get(f'/api/wh/demand/{b}', headers=h)
        row = next(x for x in r.json() if x['material_id']==mid)
        assert row['stock_project']==4 and row['stock_general']==0 and row['stock']==4, row
        # 人工模拟历史跨项目领用冲突，不能把归属不明的剩余物料重新分配。
        async with SessionLocal() as db:
            db.add(models.WhTxn(material_id=mid,biz_date='2026-11-02',direction='out',qty=1,project_id=d,ref_no='HIST-455'))
            await db.commit()
        r = await c.get(f'/api/wh/demand/{b}', headers=h)
        row = next(x for x in r.json() if x['material_id']==mid)
        assert row['stock_conflict'] and row['stock']==0, row
        assert (await txn('out',1,b)).status_code == 400
        overview = (await c.get('/api/wh/demand-overview',headers=h)).json()
        assert next(x for x in overview if x['project_id']==b)['project_status'] == '进行中'
        # 同型号在同项目的两条清单行出现时，历史领用只能扣减一次。
        from app.routers.warehouse_router import _demand_sheet_cols
        sheet_name, cols = next(iter(_demand_sheet_cols().items()))
        async with SessionLocal() as db:
            material = models.WhMaterial(name='重复清单料',spec='455-repeat',unit='件',init_stock=8)
            db.add(material); await db.flush()
            sheet = models.Datasheet(project_id=d,name=sheet_name); db.add(sheet); await db.flush()
            fields = {}
            for i, name in enumerate(cols):
                f = models.Field(datasheet_id=sheet.id,name=name,type='text',sort_order=i)
                db.add(f); await db.flush(); fields[name] = str(f.id)
            for i in range(2):
                db.add(models.Record(datasheet_id=sheet.id,sort_order=i,values={fields[cols[0]]:'重复清单料',fields[cols[1]]:'455-repeat',fields[cols[2]]:'3'}))
            db.add(models.WhTxn(material_id=material.id,biz_date='2026-11-02',direction='out',qty=2,project_id=d,ref_no='REPEAT-455'))
            await db.commit()
        rows = (await c.get(f'/api/wh/demand/{d}',headers=h)).json()
        repeated = [x for x in rows if x['item_name']=='重复清单料']
        assert len(repeated)==2 and [x['issued_qty'] for x in repeated]==[2,0], repeated
        if os.environ.get('FB455_DATABASE_URL'):
            # 真 PostgreSQL：两个项目同时领同一件通用库存，行锁必须让其中一个失败。
            m = (await c.post('/api/wh/materials',headers=h,json={'name':'并发通用料','spec':'455-race','unit':'个','init_stock':1})).json()['id']
            async def take(pid):
                return await c.post('/api/wh/txns',headers=h,json={'material_id':m,'direction':'out','qty':1,'project_id':pid,'biz_date':'2026-11-01'})
            rs = await asyncio.gather(take(a), take(b))
            assert sorted(x.status_code for x in rs) == [200,400], [(x.status_code,x.text) for x in rs]
            print('PASSED PostgreSQL 并发领用不会超领')
    print('PASSED #455 项目与通用库存、跨项目限制及历史冲突')


if __name__ == '__main__':
    asyncio.run(main())
