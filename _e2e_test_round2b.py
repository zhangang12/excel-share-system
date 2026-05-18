"""Round-2B deep e2e: more edges."""
import urllib.request
import urllib.parse
import urllib.error
import json
import time

HOST = 'http://localhost:8000'

def call(method, url, data=None, tok=None):
    h = {}
    if tok: h['Authorization'] = f'Bearer {tok}'
    if data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
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
    if s != 200: raise SystemExit(f'login {u} failed: {s} {r}')
    return r['access_token']

bugs = []
def expect(cond, msg, is_bug=False):
    print(f'  [{"PASS" if cond else ("BUG" if is_bug else "FAIL")}] {msg}')
    if not cond: bugs.append((is_bug, msg))

def header(t):
    print(f'\n{"="*50}\n {t}\n{"="*50}')

atok = login('admin', 'admin123')
mtok = login('manager', 'manager123')

# === A. change_password 边界 ===
header('A. change_password validations')

ts = str(int(time.time()))
uname = f'pwd_{ts}'
s, r = call('POST', '/api/admin/users',
            {'username': uname, 'password': 'init123', 'role_id': 3}, tok=atok)
expect(s == 200, f'create user: {s}')
utok = login(uname, 'init123')

# 1. 同密码
s, r = call('POST', '/api/auth/change-password',
            {'old_password': 'init123', 'new_password': 'init123'}, tok=utok)
expect(s == 400, f'same password rejected: {s} {r}')

# 2. 错原密码
s, r = call('POST', '/api/auth/change-password',
            {'old_password': 'wrong', 'new_password': 'newpwd1'}, tok=utok)
expect(s == 400, f'wrong old password rejected: {s} {r}')

# 3. 新密码 < 6 位
s, r = call('POST', '/api/auth/change-password',
            {'old_password': 'init123', 'new_password': 'abc'}, tok=utok)
expect(s == 422, f'short new password rejected (422): {s} {r}')

# 4. 正常修改
s, r = call('POST', '/api/auth/change-password',
            {'old_password': 'init123', 'new_password': 'newpwd1'}, tok=utok)
expect(s == 200, f'change password OK: {s} {r}')

# 5. 修改后 must_change=false
s, r = call('GET', '/api/auth/me', tok=utok)
expect(s == 200 and r.get('password_must_change') == False, f'must_change=false after change: {r.get("password_must_change")}')

# === B. invalid tokens / missing auth ===
header('B. Auth edge cases')

# 没 token
s, r = call('GET', '/api/auth/me')
expect(s == 401, f'no token -> 401: {s} {r}')

# 无效 token
s, r = call('GET', '/api/auth/me', tok='invalid_token_xxx')
expect(s == 401, f'invalid token -> 401: {s} {r}')

# Bearer 大小写
class _CallNoBearer:
    pass
# 自己拼一下 lowercase 头
req = urllib.request.Request(HOST + '/api/auth/me', headers={'Authorization': f'bearer {atok}'})
try:
    resp = urllib.request.urlopen(req)
    s = resp.status
except urllib.error.HTTPError as e: s = e.code
expect(s == 200, f'lowercase "bearer" works: {s}')

# === C. 不可见 / 禁用账号的访问 ===
header('C. Disabled account cannot access')

# 禁用 pwd_xxx
s, r = call('PUT', f'/api/admin/users/{call("GET", "/api/admin/users", tok=atok)[1][-1]["id"]}',
            {'is_active': False}, tok=atok)
expect(s == 200, f'disable user: {s}')

# 旧 token 仍试用
s, r = call('GET', '/api/auth/me', tok=utok)
expect(s == 401, f'disabled user old token -> 401: {s} {r}')

# 重新登录禁用账号
s, r = call('POST', '/api/auth/login', {'username': uname, 'password': 'newpwd1'})
expect(s == 403, f'disabled user login -> 403: {s} {r}')

# === D. 项目可见性 - 非成员/非 manager 不能看 ===
header('D. Designer non-member cannot access project')

# 创建另一个 designer
uname2 = f'dsg2_{ts}'
s, r = call('POST', '/api/admin/users',
            {'username': uname2, 'password': 'init123', 'role_id': 3}, tok=atok)
duid2 = r['id']
dtok2 = login(uname2, 'init123')
# 改密
s, _ = call('POST', '/api/auth/change-password',
            {'old_password': 'init123', 'new_password': 'newpwd2'}, tok=dtok2)
dtok2 = login(uname2, 'newpwd2')

# 看项目列表 (应当只看到他作为成员的 - 而他不是任何项目成员)
s, r = call('GET', '/api/projects', tok=dtok2)
expect(s == 200 and isinstance(r, list), f'designer GET projects: {s}, count={len(r) if isinstance(r,list) else "?"}')

# 看一览 (应当只看到他作为成员的)
s, r = call('GET', '/api/overview', tok=dtok2)
expect(s == 200, f'designer GET overview: {s}, rows={len(r["rows"])}')

# 试看不属于自己的项目详情
s, r = call('GET', '/api/projects/22', tok=dtok2)
expect(s in (403, 404), f'designer GET unowned project: {s} {r}')

# === E. Audit log content ===
header('E. Audit log')
s, r = call('GET', '/api/admin/audit?limit=20', tok=atok)
expect(s == 200, f'list audit: {s}')
actions = set(x['action'] for x in r)
print(f'  actions seen: {actions}')

# === F. Datasheet rename collision allowed (no unique constraint) ===
header('F. Datasheet rename')
_, dlist = call('GET', '/api/projects/22/datasheets', tok=atok)
if dlist and len(dlist) >= 2:
    did_a = dlist[0]['id']
    did_b = dlist[1]['id']
    name_b = dlist[1]['name']
    # 把 A rename 成 B 的名字（应当允许）
    s, r = call('PUT', f'/api/datasheets/{did_a}', {'name': name_b}, tok=atok)
    expect(s == 200, f'datasheet rename to dup name allowed (no constraint): {s}')
    # rename back
    call('PUT', f'/api/datasheets/{did_a}', {'name': dlist[0]['name']}, tok=atok)

# === G. update_cell value=0 / value=null ===
header('G. update_cell with 0 / null')

# 创建临时 datasheet
s, r = call('POST', '/api/projects/22/datasheets', {'name': 'zero_test'}, tok=atok)
expect(s == 200, f'create tmp datasheet: {s}')
tmp_did = r['id']
s, r = call('POST', f'/api/datasheets/{tmp_did}/fields', {'name': 'num', 'type': 'number'}, tok=atok)
tmp_fid = r['id']
s, r = call('POST', f'/api/datasheets/{tmp_did}/records', {'values': {}}, tok=atok)
tmp_rid = r['id']

# value=0
s, r = call('PUT', f'/api/records/{tmp_rid}/cell', {'field_id': tmp_fid, 'value': 0}, tok=atok)
expect(s == 200 and r['values'].get(str(tmp_fid)) == 0, f'set value=0 stored: {r["values"]}')

# value=null (清除)
s, r = call('PUT', f'/api/records/{tmp_rid}/cell', {'field_id': tmp_fid, 'value': None}, tok=atok)
expect(s == 200 and str(tmp_fid) not in r['values'], f'value=null clears: {r["values"]}')

# value="" (清除)
s, r = call('PUT', f'/api/records/{tmp_rid}/cell', {'field_id': tmp_fid, 'value': ''}, tok=atok)
expect(s == 200 and str(tmp_fid) not in r['values'], f'value="" clears: {r["values"]}')

# cleanup
call('DELETE', f'/api/datasheets/{tmp_did}', tok=atok)

# === Summary ===
header('Summary')
real_bugs = [m for is_bug, m in bugs if is_bug]
fails = [m for is_bug, m in bugs if not is_bug]
print(f'BUGs: {len(real_bugs)}')
for m in real_bugs: print(f'   - {m}')
print(f'Assertion fails: {len(fails)}')
for m in fails: print(f'   - {m}')
