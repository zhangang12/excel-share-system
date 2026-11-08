"""收款附件权限与销售台账可见范围一致；项目创建状态由服务端控制。"""
import asyncio, io, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="sales-note-scope-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import ensure_schema_columns, run_all
from app import models


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        async def login(name, password):
            r = await c.post('/api/auth/login', json={'username': name, 'password': password})
            assert r.status_code == 200, r.text
            return {'Authorization': 'Bearer ' + r.json()['access_token']}
        admin = await login('admin', 'admin123')
        roles = {r['code']: r['id'] for r in (await c.get('/api/admin/roles', headers=admin)).json()}
        ids = []
        for name, role in [('owner', 'sales'), ('other', 'sales'), ('lead', 'sales_lead'), ('worker', 'designer')]:
            r = await c.post('/api/admin/users', headers=admin, json={'username': name, 'password': 'pass123', 'full_name': name, 'role_ids': [roles[role]]})
            assert r.status_code == 200, r.text
            ids.append(r.json()['id'])
        owner, other, lead, worker = [await login(n, 'pass123') for n in ('owner', 'other', 'lead', 'worker')]
        for status in ('已完成', '已归档'):
            r = await c.post('/api/projects', headers=admin, json={'code': 'STATUS-' + status, 'name': '状态校验', 'status': status})
            assert r.status_code == 400, r.text
        r = await c.post('/api/projects', headers=admin, json={'code': 'STATUS-OK', 'name': '状态校验'})
        assert r.status_code == 200 and r.json()['status'] == '进行中', r.text
        pid = r.json()['id']
        async with SessionLocal() as db:
            led = models.SalesLedger(project_id=pid, sales_uid=ids[0], customer='客户')
            db.add(led)
            await db.commit()
            lid = led.id
        async def upload(headers):
            return await c.post('/api/attachments', headers=headers, data={'biz_type': 'sales_note', 'biz_id': str(lid), 'kind': 'prepay', 'project_id': '999999'}, files={'file': ('receipt.png', io.BytesIO(b'proof'), 'image/png')})
        r = await upload(owner)
        assert r.status_code == 200 and r.json()['project_id'] == pid, r.text
        aid = r.json()['id']
        assert (await upload(other)).status_code == 403
        assert (await upload(worker)).status_code == 403
        for headers, allowed in ((owner, True), (other, False), (lead, True), (worker, False), (admin, True)):
            page = await c.get('/api/sales/ledger', headers=headers, params={'page_size': 200})
            visible = page.status_code == 200 and any(row['id'] == lid for row in page.json()['rows'])
            assert visible == allowed, (page.status_code, page.text)
            listing = await c.get('/api/attachments', headers=headers, params={'biz_type': 'sales_note', 'biz_id': lid})
            download = await c.get(f'/api/attachments/{aid}/download', headers=headers)
            package = await c.post('/api/attachments/package', headers=headers, json={'attachment_ids': [aid]})
            assert all((x.status_code == 200) == visible for x in (listing, download, package)), (listing.text, download.status_code, package.status_code)
        assert (await c.get('/api/attachments', headers=owner, params={'biz_type': 'sales_note'})).status_code == 400
        assert (await c.delete(f'/api/attachments/{aid}', headers=other)).status_code == 403
        assert (await c.delete(f'/api/attachments/{aid}', headers=lead)).status_code == 200
    print('收款附件权限与项目创建状态：全部通过')


if __name__ == '__main__':
    asyncio.run(main())
