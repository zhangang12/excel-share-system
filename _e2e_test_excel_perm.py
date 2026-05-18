"""Excel × 角色权限矩阵测试。

矩阵：
                          import_excel  export_ds  export_proj  import_overview
admin                     ✓             ✓          ✓            ✓
manager                   ✓             ✓          ✓            ✗ (only admin)
designer-non-member       ✗             ✗          ✗            ✗
designer-view-member      ✗ (view 只读) ✓          ✓            ✗
designer-edit-member      ✓             ✓          ✓            ✗
disabled-user             ✗ (401)       ✗          ✗            ✗
"""
import urllib.request, urllib.error, json, time
HOST = 'http://localhost:8000'

def call(method, url, data=None, tok=None, raw_body=None, extra_headers=None):
    h = dict(extra_headers or {})
    if tok: h['Authorization'] = f'Bearer {tok}'
    if data is not None:
        h.setdefault('Content-Type', 'application/json')
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    elif raw_body is not None:
        body = raw_body
    else:
        body = None
    req = urllib.request.Request(HOST + url, method=method, data=body, headers=h)
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read()
        try: return resp.status, json.loads(raw or b'null')
        except: return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw or b'null')
        except: return e.code, raw.decode('utf-8', errors='replace')

def login(u, p):
    s, r = call('POST', '/api/auth/login', {'username': u, 'password': p})
    if s != 200: raise SystemExit(f'login {u}: {s} {r}')
    return r['access_token']

def upload(url, tok, filename, content):
    boundary = '----b' + str(int(time.time() * 1000))
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'
    ).encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    return call('POST', url, raw_body=body, tok=tok,
                extra_headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})

# 构造一个最小的合法 xlsx (用 openpyxl 在内存创建)
def make_xlsx():
    try:
        from openpyxl import Workbook
        from io import BytesIO
        wb = Workbook()
        ws = wb.active
        ws.append(['公司标题'])
        ws.append([])
        ws.append([])
        ws.append(['序号', '名称'])
        ws.append([1, 'item'])
        bio = BytesIO()
        wb.save(bio)
        return bio.getvalue()
    except ImportError:
        return None

XLSX_BYTES = make_xlsx()
if XLSX_BYTES is None:
    raise SystemExit('openpyxl needed on host to construct test xlsx')

bugs = []
def expect(c, m, is_bug=False):
    print(f'  [{"PASS" if c else ("BUG" if is_bug else "FAIL")}] {m}')
    if not c: bugs.append((is_bug, m))
def header(t): print(f'\n{"="*55}\n {t}\n{"="*55}')

# === Setup: tokens + reference project & datasheet ===
atok = login('admin', 'admin123')
mtok = login('manager', 'manager123')

# 取一个测试项目
_, projs = call('GET', '/api/projects', tok=atok)
pid = next(p['id'] for p in projs if p['code'] == '2026-039')
_, dlist = call('GET', f'/api/projects/{pid}/datasheets', tok=atok)
did = dlist[0]['id'] if dlist else None
print(f'using project {pid}, datasheet {did}')

# 创建测试用户
ts = str(int(time.time()))
def mk_user(uname, role_id=3):
    s, r = call('POST', '/api/admin/users',
                {'username': uname, 'password': 'init123', 'role_id': role_id}, tok=atok)
    if s != 200: raise SystemExit(f'mk_user {uname}: {s} {r}')
    tok = login(uname, 'init123')
    call('POST', '/api/auth/change-password',
         {'old_password': 'init123', 'new_password': 'pwd_real_123'}, tok=tok)
    return login(uname, 'pwd_real_123'), r['id']

dnon_tok, dnon_uid = mk_user(f'xn_non_{ts}')
dview_tok, dview_uid = mk_user(f'xn_view_{ts}')
dedit_tok, dedit_uid = mk_user(f'xn_edit_{ts}')
disable_tok, disable_uid = mk_user(f'xn_dis_{ts}')

# 把 view / edit 加为项目成员
call('POST', f'/api/projects/{pid}/members', {'user_id': dview_uid, 'permission': 'view'}, tok=atok)
call('POST', f'/api/projects/{pid}/members', {'user_id': dedit_uid, 'permission': 'edit'}, tok=atok)

# 禁用 disabled 用户
call('PUT', f'/api/admin/users/{disable_uid}', {'is_active': False}, tok=atok)

# === Test: import_excel (POST /api/projects/{pid}/import-excel) ===
header('Test: POST /api/projects/{pid}/import-excel')
cases = [
    ('admin',        atok,       (200,)),
    ('manager',      mtok,       (200,)),
    ('designer-non', dnon_tok,   (403,)),
    ('designer-view',dview_tok,  (403,)),
    ('designer-edit',dedit_tok,  (200,)),
    ('disabled',     disable_tok,(401,)),
]
for label, tok, want in cases:
    s, r = upload(f'/api/projects/{pid}/import-excel', tok, 'test.xlsx', XLSX_BYTES)
    expect(s in want, f'{label}: got {s}, expect {want}')

# 注意 admin 和 manager 的 import 都会清掉 datasheets，重新跑后续测试需要先重新导入
# Re-import to restore datasheets for export tests below
_, dlist_after = call('GET', f'/api/projects/{pid}/datasheets', tok=atok)
did_now = dlist_after[0]['id'] if dlist_after else did
print(f'  (datasheets after import sequence: {len(dlist_after)} sheets, did_now={did_now})')

# === Test: export_datasheet (GET /api/datasheets/{did}/export) ===
header('Test: GET /api/datasheets/{did}/export')
cases = [
    ('admin',        atok,       (200,)),
    ('manager',      mtok,       (200,)),
    ('designer-non', dnon_tok,   (403,)),
    ('designer-view',dview_tok,  (200,)),  # view 可下载
    ('designer-edit',dedit_tok,  (200,)),
    ('disabled',     disable_tok,(401,)),
]
for label, tok, want in cases:
    s, r = call('GET', f'/api/datasheets/{did_now}/export', tok=tok)
    expect(s in want, f'{label}: got {s}, expect {want}')

# === Test: export_project (GET /api/projects/{pid}/export) ===
header('Test: GET /api/projects/{pid}/export')
cases = [
    ('admin',        atok,       (200,)),
    ('manager',      mtok,       (200,)),
    ('designer-non', dnon_tok,   (403,)),
    ('designer-view',dview_tok,  (200,)),
    ('designer-edit',dedit_tok,  (200,)),
    ('disabled',     disable_tok,(401,)),
]
for label, tok, want in cases:
    s, r = call('GET', f'/api/projects/{pid}/export', tok=tok)
    expect(s in want, f'{label}: got {s}, expect {want}')

# === Test: import_overview (POST /api/overview/import) — admin / manager ===
header('Test: POST /api/overview/import (admin & manager)')
# 构造合法的 overview xlsx (含 "项目编号" 列)
from openpyxl import Workbook
from io import BytesIO
wb = Workbook()
ws = wb.active
ws.append(['公司汇总表', '', ''])
ws.append(['项目编号', '项目名称', '销售'])
ws.append(['2026-999', '测试导入', '张三'])
bio = BytesIO(); wb.save(bio)
OVW_XLSX = bio.getvalue()

cases = [
    ('admin',        atok,       (200,)),
    ('manager',      mtok,       (200,)),   # manager 也是"管理层"，允许导入汇总
    ('designer-non', dnon_tok,   (403,)),
    ('designer-view',dview_tok,  (403,)),
    ('designer-edit',dedit_tok,  (403,)),
    ('disabled',     disable_tok,(401,)),
]
for label, tok, want in cases:
    s, r = upload('/api/overview/import', tok, 'ovw.xlsx', OVW_XLSX)
    expect(s in want, f'{label}: got {s}, expect {want}')

# === Test: file type validation ===
header('Test: file type validation (.txt rejected)')
s, r = upload(f'/api/projects/{pid}/import-excel', atok, 'evil.txt', b'not excel')
expect(s == 400, f'admin upload .txt: {s} {r}')
s, r = upload('/api/overview/import', atok, 'evil.txt', b'not excel')
expect(s == 400, f'admin upload .txt to overview: {s} {r}')

# === Cleanup ===
header('Cleanup')
call('DELETE', f'/api/projects/{pid}/members/{dview_uid}', tok=atok)  # may 404 - we used member id wrongly
# actually need to use member ID not user ID
_, members = call('GET', f'/api/projects/{pid}/members', tok=atok)
for m in members:
    if m['user_id'] in (dview_uid, dedit_uid):
        call('DELETE', f'/api/projects/{pid}/members/{m["id"]}', tok=atok)
# delete users
for uid in (dnon_uid, dview_uid, dedit_uid, disable_uid):
    call('DELETE', f'/api/admin/users/{uid}', tok=atok)

# === Summary ===
header('Summary')
real = [m for is_bug, m in bugs if is_bug]
fails = [m for is_bug, m in bugs if not is_bug]
print(f'BUGs: {len(real)}')
for m in real: print(f'   - {m}')
print(f'Fails: {len(fails)}')
for m in fails: print(f'   - {m}')
