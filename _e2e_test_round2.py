"""Round-2 deep e2e test: discover remaining bugs.

Runs from host against localhost:8000. Does not require any project files; uses raw urllib.
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import sys

HOST = 'http://localhost:8000'


def call(method, url, data=None, tok=None, raw_body=None):
    h = {}
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    if data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    elif raw_body is not None:
        body = raw_body
    else:
        body = None
    req = urllib.request.Request(HOST + url, method=method, data=body, headers=h)
    try:
        resp = urllib.request.urlopen(req)
        ct = resp.headers.get('Content-Type', '')
        raw = resp.read()
        if 'application/json' in ct:
            return resp.status, json.loads(raw or b'null')
        return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b'null')
        except Exception:
            return e.code, raw.decode('utf-8', errors='replace')


def login(u, p):
    s, r = call('POST', '/api/auth/login', {'username': u, 'password': p})
    assert s == 200, f'login {u}: {s} {r}'
    return r['access_token']


def header(t):
    print('\n' + '=' * 50)
    print(' ' + t)
    print('=' * 50)


bugs = []


def expect(cond, msg, *, is_bug=False):
    status = 'PASS' if cond else ('BUG' if is_bug else 'FAIL')
    print(f'  [{status}] {msg}')
    if not cond:
        bugs.append((is_bug, msg))
    return cond


# === setup ===
atok = login('admin', 'admin123')
mtok = login('manager', 'manager123')
print('admin + manager logged in')

# 找一个测试项目 + datasheet
_, projs = call('GET', '/api/projects', tok=atok)
pid = next(p['id'] for p in projs if p['code'] == '2026-039')
print(f'using project pid={pid}')
_, dlist = call('GET', f'/api/projects/{pid}/datasheets', tok=atok)
did = dlist[0]['id']
print(f'using datasheet did={did}')
_, fields = call('GET', f'/api/datasheets/{did}/fields', tok=atok)
fid_target = fields[1]['id']
print(f'using field fid={fid_target}')
_, records = call('GET', f'/api/datasheets/{did}/records', tok=atok)
rid_target = records[0]['id']
print(f'using record rid={rid_target}')

# === A. 字段权限 can_view/can_edit 真实生效 ===
header('A. Field permission enforcement (designer role)')

# 创建 designer 用户
ts = str(int(time.time()))
uname = f'dsg_{ts}'
s, r = call('POST', '/api/admin/users',
            {'username': uname, 'password': 'test123', 'role_id': 3, 'full_name': 'TestDesigner'},
            tok=atok)
expect(s == 200, f'admin creates designer user: {s} {r}')
duid = r['id']

# 改密让它能用
dtok = login(uname, 'test123')
s, _ = call('POST', '/api/auth/change-password',
            {'old_password': 'test123', 'new_password': 'newpwd123'}, tok=dtok)
dtok = login(uname, 'newpwd123')
print('  designer can login')

# 加为项目成员（edit 权限）
s, m = call('POST', f'/api/projects/{pid}/members',
            {'user_id': duid, 'permission': 'edit'}, tok=atok)
expect(s == 200, f'admin adds designer as edit member: {s}')

# designer 应能 list datasheets / records
s, _ = call('GET', f'/api/projects/{pid}/datasheets', tok=dtok)
expect(s == 200, f'designer lists datasheets: {s}')

s, dperms = call('GET', f'/api/permissions/me/datasheet/{did}', tok=dtok)
expect(s == 200, f'designer GET my perms: {s}')
expect(dperms[str(fid_target)]['can_edit'] == True, 'default can_edit=true')

# admin 设 designer 对 fid_target 的 can_edit=false
s, _ = call('PUT', f'/api/permissions/fields/{fid_target}',
            {'permissions': [{'role_id': 3, 'can_view': True, 'can_edit': False}]},
            tok=atok)
expect(s == 200, f'admin sets designer can_edit=false: {s}')

# designer 拿到的 perms
s, dperms = call('GET', f'/api/permissions/me/datasheet/{did}', tok=dtok)
expect(dperms[str(fid_target)]['can_edit'] == False,
       f'designer my_perms reflects can_edit=false: got {dperms[str(fid_target)]}')

# designer 尝试编辑该字段 cell → 应 403
s, r = call('PUT', f'/api/records/{rid_target}/cell',
            {'field_id': fid_target, 'value': 'hacked'}, tok=dtok)
expect(s == 403, f'designer PUT cell forbidden: {s} {r}')

# 改成 can_view=false（仍 can_edit=false）
s, _ = call('PUT', f'/api/permissions/fields/{fid_target}',
            {'permissions': [{'role_id': 3, 'can_view': False, 'can_edit': False}]},
            tok=atok)
expect(s == 200, f'admin sets can_view=false: {s}')

s, dperms = call('GET', f'/api/permissions/me/datasheet/{did}', tok=dtok)
expect(dperms[str(fid_target)]['can_view'] == False,
       f'designer my_perms can_view=false: got {dperms[str(fid_target)]}')

# 模拟"can_view=false 但 can_edit=true"的脏数据，看后端是否拦
s, _ = call('PUT', f'/api/permissions/fields/{fid_target}',
            {'permissions': [{'role_id': 3, 'can_view': False, 'can_edit': True}]},
            tok=atok)
s, r = call('PUT', f'/api/records/{rid_target}/cell',
            {'field_id': fid_target, 'value': 'should be blocked by can_view=false'},
            tok=dtok)
expect(s == 403, f'BUG-A1: designer can edit when can_view=false (backend only checks can_edit): {s} {r}',
       is_bug=True)

# 清理
s, _ = call('PUT', f'/api/permissions/fields/{fid_target}',
            {'permissions': []}, tok=atok)  # 重置

# === B. 软删除项目导致 code 冲突 ===
header('B. Soft-deleted project blocks code reuse')

# 用 admin 创建一个项目
s, r = call('POST', '/api/projects', {'code': f'SOFT-{ts}', 'name': 'soft del test'}, tok=atok)
expect(s == 200, f'create project: {s}')
sdpid = r['id']

s, _ = call('DELETE', f'/api/projects/{sdpid}', tok=atok)
expect(s == 200, f'soft delete project: {s}')

s, r = call('POST', '/api/projects', {'code': f'SOFT-{ts}', 'name': 'soft del test reuse'}, tok=atok)
if s == 400 and '已存在' in str(r):
    expect(False, f'BUG-B1: cannot reuse code of soft-deleted project: {s} {r}', is_bug=True)
else:
    expect(s == 200, f'can reuse code: {s} {r}')

# === C. LIKE wildcard injection ===
header('C. LIKE wildcard escape')

# 创建独特 marker 的项目
import urllib.parse
marker = f'tag{ts}'
s, r = call('POST', '/api/projects',
            {'code': f'PCT-{ts}', 'name': f'wld_{marker}_100%and_under'}, tok=atok)
expect(s == 200, f'create project with %_ in name: {s}')

# 用唯一 marker 搜索（不含通配符）应当只命中本次项目
s, r = call('GET', f'/api/projects?q={marker}', tok=atok)
expect(s == 200 and isinstance(r, list) and len(r) == 1,
       f'unique tag matches exactly 1: {len(r) if isinstance(r,list) else r}')

# 搜索 "_" / "%"：如果未转义会匹配全表；总数对比
_, all_projs = call('GET', '/api/projects', tok=atok)
total = len(all_projs) if isinstance(all_projs, list) else 0

s, r = call('GET', f'/api/projects?q={urllib.parse.quote("_")}', tok=atok)
if isinstance(r, list):
    if len(r) >= total - 2:
        expect(False, f'BUG-C1: LIKE _ wildcard matches almost all ({len(r)}/{total})', is_bug=True)
    else:
        expect(True, f'search q=_ literal: {len(r)}/{total}')

s, r = call('GET', f'/api/projects?q={urllib.parse.quote("%")}', tok=atok)
if isinstance(r, list):
    if len(r) >= total - 2:
        expect(False, f'BUG-C2: LIKE % wildcard matches almost all ({len(r)}/{total})', is_bug=True)
    else:
        expect(True, f'search q=% literal: {len(r)}/{total}')

# === D. 字段类型变更 ===
header('D. Field type change preserves data')

# 用 admin 在 did 上创建一个 text 字段
s, r = call('POST', f'/api/datasheets/{did}/fields',
            {'name': f'tmp_text_{ts}', 'type': 'text'}, tok=atok)
expect(s == 200, f'create text field: {s}')
tmp_fid = r['id']

# 添加一条 record 并赋非数字值
s, r = call('POST', f'/api/datasheets/{did}/records', {'values': {}}, tok=atok)
tmp_rid = r['id']
s, _ = call('PUT', f'/api/records/{tmp_rid}/cell',
            {'field_id': tmp_fid, 'value': 'abc非数字'}, tok=atok)
expect(s == 200, f'set text value: {s}')

# 把字段改成 number
s, r = call('PUT', f'/api/fields/{tmp_fid}', {'type': 'number'}, tok=atok)
expect(s == 200, f'change field type to number: {s} {r}')

# 列出 records，应当不崩
s, recs = call('GET', f'/api/datasheets/{did}/records', tok=atok)
expect(s == 200, f'list records after type change: {s}')
# 找回 tmp_rid
target = next((r for r in recs if r['id'] == tmp_rid), None)
expect(target is not None, 'tmp record still exists')
if target:
    print(f'  tmp record values: {target["values"]}')

# 导出 xlsx
s, body = call('GET', f'/api/datasheets/{did}/export', tok=atok)
expect(s == 200, f'export xlsx: {s} type={type(body).__name__}')

# 清理
call('DELETE', f'/api/records/{tmp_rid}', tok=atok)
call('DELETE', f'/api/fields/{tmp_fid}', tok=atok)

# === E. WebSocket auth (use websockets lib if available, else skip) ===
header('E. WebSocket auth')
try:
    import asyncio
    try:
        import websockets
        async def check_ws():
            results = []
            # invalid token
            try:
                async with websockets.connect(
                    'ws://localhost:8000/ws/overview?token=invalid_xyz',
                    open_timeout=3, close_timeout=3,
                ) as ws:
                    msg = await asyncio.wait_for(ws.recv(), 2)
                    results.append(('invalid token connected', False, msg))
            except websockets.exceptions.ConnectionClosedError as e:
                results.append(('invalid token rejected', True, e.code))
            except Exception as e:
                results.append(('invalid token rejected (error)', True, str(e)[:60]))

            # valid token
            try:
                async with websockets.connect(
                    f'ws://localhost:8000/ws/overview?token={atok}',
                    open_timeout=3, close_timeout=3,
                ) as ws:
                    await ws.send(json.dumps({'type': 'ping'}))
                    msg = await asyncio.wait_for(ws.recv(), 3)
                    data = json.loads(msg)
                    results.append(('valid token ping/pong', data.get('type') == 'pong', data))
            except Exception as e:
                results.append(('valid token failed', False, str(e)[:60]))
            return results

        for label, ok, info in asyncio.run(check_ws()):
            expect(ok, f'WS {label}: {info}')
    except ImportError:
        print('  (websockets lib not available, skipping)')
except Exception as e:
    print(f'  WS test failed: {e}')

# === F. Excel import edge cases ===
header('F. Excel import edge cases')

# F1: 上传 .txt 文件
fake_txt = b'this is not excel\n'
boundary = '----' + ts
body = (
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
    f'Content-Type: text/plain\r\n\r\n'
).encode() + fake_txt + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(
    HOST + f'/api/projects/{pid}/import-excel', method='POST',
    data=body,
    headers={'Authorization': f'Bearer {atok}',
             'Content-Type': f'multipart/form-data; boundary={boundary}'})
try:
    resp = urllib.request.urlopen(req)
    s = resp.status; r = resp.read()
except urllib.error.HTTPError as e:
    s = e.code; r = e.read()
expect(s == 400, f'reject .txt upload: {s} {r[:200]}')

# F2: 上传内容为空（损坏）的 xlsx
fake_xlsx = b'PK\x03\x04corrupt'
body = (
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="file"; filename="empty.xlsx"\r\n'
    f'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'
).encode() + fake_xlsx + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(
    HOST + f'/api/projects/{pid}/import-excel', method='POST',
    data=body,
    headers={'Authorization': f'Bearer {atok}',
             'Content-Type': f'multipart/form-data; boundary={boundary}'})
try:
    resp = urllib.request.urlopen(req)
    s = resp.status; r = resp.read()
except urllib.error.HTTPError as e:
    s = e.code; r = e.read()
expect(s == 400, f'reject corrupt xlsx: {s} {r[:200]}')

# === Summary ===
header('Summary')
real_bugs = [m for is_bug, m in bugs if is_bug]
fails = [m for is_bug, m in bugs if not is_bug]
print(f'  BUGs found: {len(real_bugs)}')
for m in real_bugs:
    print(f'   - {m}')
print(f'  Assertion fails (test bugs?): {len(fails)}')
for m in fails:
    print(f'   - {m}')
# 不 exit 1，方便后面看输出
