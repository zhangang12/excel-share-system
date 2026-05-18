"""数据表/字段/行 权限矩阵测试。

13 个端点 × 5 个角色 = 65 个 case，外加禁用账号 401 抽样。

期望：
- admin / manager: 全部 200
- non-member designer: list/get 也 403（项目不可见），写也 403
- view-member designer: list/get/export 200，所有写 403
- edit-member designer: 全部 200
- 禁用账号: 401
"""
import urllib.request, urllib.error, json, time
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
    if s != 200: raise SystemExit(f'login {u}: {s} {r}')
    return r['access_token']

bugs = []
def expect(c, m, is_bug=True):
    print(f'  [{"PASS" if c else "BUG"}] {m}')
    if not c: bugs.append((is_bug, m))
def header(t): print(f'\n{"="*60}\n {t}\n{"="*60}')

# === Setup ===
atok = login('admin', 'admin123')
mtok = login('manager', 'manager123')

# 测试不破坏现有 2026-039 数据：admin 单独创建一个项目用于测试
ts = str(int(time.time()))
_, r = call('POST', '/api/projects',
            {'code': f'DSPERM-{ts}', 'name': '数据表权限测试'}, tok=atok)
pid = r['id']
print(f'created test project pid={pid}')

# admin 创建一个 datasheet 作为读测试目标
_, r = call('POST', f'/api/projects/{pid}/datasheets', {'name': 'sheet1'}, tok=atok)
did = r['id']
print(f'created test datasheet did={did}')

# 加一个字段 + 一行
_, r = call('POST', f'/api/datasheets/{did}/fields', {'name': 'col1', 'type': 'text'}, tok=atok)
fid = r['id']
_, r = call('POST', f'/api/datasheets/{did}/records', {'values': {}}, tok=atok)
rid = r['id']
print(f'created test field fid={fid}, record rid={rid}')

# 创建测试用户
def mk_user(uname, role_id=3):
    s, r = call('POST', '/api/admin/users',
                {'username': uname, 'password': 'init123', 'role_id': role_id}, tok=atok)
    if s != 200: raise SystemExit(f'mk_user {uname}: {s} {r}')
    tok = login(uname, 'init123')
    call('POST', '/api/auth/change-password',
         {'old_password': 'init123', 'new_password': 'pwd_real_123'}, tok=tok)
    return login(uname, 'pwd_real_123'), r['id']

dnon_tok,  dnon_uid  = mk_user(f'dp_non_{ts}')
dview_tok, dview_uid = mk_user(f'dp_view_{ts}')
dedit_tok, dedit_uid = mk_user(f'dp_edit_{ts}')
disable_tok, disable_uid = mk_user(f'dp_dis_{ts}')

# 项目成员
call('POST', f'/api/projects/{pid}/members', {'user_id': dview_uid, 'permission': 'view'}, tok=atok)
call('POST', f'/api/projects/{pid}/members', {'user_id': dedit_uid, 'permission': 'edit'}, tok=atok)
call('PUT', f'/api/admin/users/{disable_uid}', {'is_active': False}, tok=atok)

# 角色 → token
ROLES = [
    ('admin',         atok,       'edit'),
    ('manager',       mtok,       'edit'),
    ('non-member',    dnon_tok,   'none'),
    ('view-member',   dview_tok,  'read'),
    ('edit-member',   dedit_tok,  'edit'),
]

def check(label_action, want_map):
    """want_map: role_label -> (method, url, data, expected_status_tuple)"""
    header(label_action)
    for role_label, _tok, _level in ROLES:
        method, url, data, want = want_map[role_label]
        s, _ = call(method, url, data, tok=_tok)
        expect(s in want, f'{role_label}: {method} {url[-50:]} -> {s}, want {want}')

# === 1. List datasheets (read) ===
read_url = f'/api/projects/{pid}/datasheets'
check('1. GET /projects/{pid}/datasheets (read)', {
    'admin':       ('GET', read_url, None, (200,)),
    'manager':     ('GET', read_url, None, (200,)),
    'non-member':  ('GET', read_url, None, (403,)),
    'view-member': ('GET', read_url, None, (200,)),
    'edit-member': ('GET', read_url, None, (200,)),
})

# === 2. List fields (read) ===
url = f'/api/datasheets/{did}/fields'
check('2. GET /datasheets/{did}/fields (read)', {
    'admin':       ('GET', url, None, (200,)),
    'manager':     ('GET', url, None, (200,)),
    'non-member':  ('GET', url, None, (403,)),
    'view-member': ('GET', url, None, (200,)),
    'edit-member': ('GET', url, None, (200,)),
})

# === 3. List records (read) ===
url = f'/api/datasheets/{did}/records'
check('3. GET /datasheets/{did}/records (read)', {
    'admin':       ('GET', url, None, (200,)),
    'manager':     ('GET', url, None, (200,)),
    'non-member':  ('GET', url, None, (403,)),
    'view-member': ('GET', url, None, (200,)),
    'edit-member': ('GET', url, None, (200,)),
})

# === 4. Create datasheet (write) ===
url = f'/api/projects/{pid}/datasheets'
check('4. POST /projects/{pid}/datasheets (create)', {
    'admin':       ('POST', url, {'name': f'tmp_a_{ts}'}, (200,)),
    'manager':     ('POST', url, {'name': f'tmp_m_{ts}'}, (200,)),
    'non-member':  ('POST', url, {'name': f'tmp_n_{ts}'}, (403,)),
    'view-member': ('POST', url, {'name': f'tmp_v_{ts}'}, (403,)),
    'edit-member': ('POST', url, {'name': f'tmp_e_{ts}'}, (200,)),
})

# === 5. Update datasheet (write) ===
url = f'/api/datasheets/{did}'
check('5. PUT /datasheets/{did} (rename)', {
    'admin':       ('PUT', url, {'name': 'r_admin'}, (200,)),
    'manager':     ('PUT', url, {'name': 'r_mgr'}, (200,)),
    'non-member':  ('PUT', url, {'name': 'hack'}, (403,)),
    'view-member': ('PUT', url, {'name': 'hack'}, (403,)),
    'edit-member': ('PUT', url, {'name': 'r_edit'}, (200,)),
})

# === 6. Create field (write) ===
url = f'/api/datasheets/{did}/fields'
check('6. POST /datasheets/{did}/fields', {
    'admin':       ('POST', url, {'name': f'fa_{ts}', 'type': 'text'}, (200,)),
    'manager':     ('POST', url, {'name': f'fm_{ts}', 'type': 'text'}, (200,)),
    'non-member':  ('POST', url, {'name': f'fn_{ts}', 'type': 'text'}, (403,)),
    'view-member': ('POST', url, {'name': f'fv_{ts}', 'type': 'text'}, (403,)),
    'edit-member': ('POST', url, {'name': f'fe_{ts}', 'type': 'text'}, (200,)),
})

# === 7. Update field (write) ===
url = f'/api/fields/{fid}'
check('7. PUT /fields/{fid}', {
    'admin':       ('PUT', url, {'name': 'col_a'}, (200,)),
    'manager':     ('PUT', url, {'name': 'col_m'}, (200,)),
    'non-member':  ('PUT', url, {'name': 'hack'}, (403,)),
    'view-member': ('PUT', url, {'name': 'hack'}, (403,)),
    'edit-member': ('PUT', url, {'name': 'col_e'}, (200,)),
})

# === 8. Create record (write) ===
url = f'/api/datasheets/{did}/records'
check('8. POST /datasheets/{did}/records', {
    'admin':       ('POST', url, {'values': {}}, (200,)),
    'manager':     ('POST', url, {'values': {}}, (200,)),
    'non-member':  ('POST', url, {'values': {}}, (403,)),
    'view-member': ('POST', url, {'values': {}}, (403,)),
    'edit-member': ('POST', url, {'values': {}}, (200,)),
})

# === 9. Update cell (write) ===
url = f'/api/records/{rid}/cell'
check('9. PUT /records/{rid}/cell', {
    'admin':       ('PUT', url, {'field_id': fid, 'value': 'a'}, (200,)),
    'manager':     ('PUT', url, {'field_id': fid, 'value': 'm'}, (200,)),
    'non-member':  ('PUT', url, {'field_id': fid, 'value': 'hack'}, (403,)),
    'view-member': ('PUT', url, {'field_id': fid, 'value': 'hack'}, (403,)),
    'edit-member': ('PUT', url, {'field_id': fid, 'value': 'e'}, (200,)),
})

# === 10. Update record (write) ===
url = f'/api/records/{rid}'
check('10. PUT /records/{rid}', {
    'admin':       ('PUT', url, {'values': {str(fid): 'A'}}, (200,)),
    'manager':     ('PUT', url, {'values': {str(fid): 'M'}}, (200,)),
    'non-member':  ('PUT', url, {'values': {}}, (403,)),
    'view-member': ('PUT', url, {'values': {}}, (403,)),
    'edit-member': ('PUT', url, {'values': {str(fid): 'E'}}, (200,)),
})

# === 11. Delete record (write) ===
# 各角色测试用独立 record，避免删了别人的
header('11. DELETE /records/{rid} (per-role)')
records_to_delete = {}
for role_label, _tok, _level in ROLES:
    _, r = call('POST', f'/api/datasheets/{did}/records', {'values': {}}, tok=atok)
    records_to_delete[role_label] = r['id']

want_map = {
    'admin': (200,),
    'manager': (200,),
    'non-member': (403,),
    'view-member': (403,),
    'edit-member': (200,),
}
for role_label, _tok, _level in ROLES:
    target_rid = records_to_delete[role_label]
    s, _ = call('DELETE', f'/api/records/{target_rid}', tok=_tok)
    want = want_map[role_label]
    expect(s in want, f'{role_label}: DELETE /records/{target_rid} -> {s}, want {want}')
    if s != 200:
        call('DELETE', f'/api/records/{target_rid}', tok=atok)  # cleanup

# === 12. Delete field (write) ===
header('12. DELETE /fields/{fid} (per-role)')
for role_label, _tok, _level in ROLES:
    _, rf = call('POST', f'/api/datasheets/{did}/fields',
                 {'name': f'del_{role_label}_{ts}', 'type': 'text'}, tok=atok)
    target_fid = rf['id']
    s, _ = call('DELETE', f'/api/fields/{target_fid}', tok=_tok)
    want = want_map[role_label]
    expect(s in want, f'{role_label}: DELETE /fields/{target_fid} -> {s}, want {want}')
    if s != 200:
        call('DELETE', f'/api/fields/{target_fid}', tok=atok)

# === 13. Delete datasheet (write) ===
header('13. DELETE /datasheets/{did} (per-role)')
for role_label, _tok, _level in ROLES:
    _, rd = call('POST', f'/api/projects/{pid}/datasheets',
                 {'name': f'del_{role_label}_{ts}'}, tok=atok)
    target_did = rd['id']
    s, _ = call('DELETE', f'/api/datasheets/{target_did}', tok=_tok)
    want = want_map[role_label]
    expect(s in want, f'{role_label}: DELETE /datasheets/{target_did} -> {s}, want {want}')
    if s != 200:
        call('DELETE', f'/api/datasheets/{target_did}', tok=atok)

# === 14. Disabled user (sampling) ===
header('14. Disabled user: all endpoints -> 401')
for method, url, data in [
    ('GET', f'/api/projects/{pid}/datasheets', None),
    ('GET', f'/api/datasheets/{did}/fields', None),
    ('GET', f'/api/datasheets/{did}/records', None),
    ('POST', f'/api/datasheets/{did}/records', {'values': {}}),
    ('PUT', f'/api/records/{rid}/cell', {'field_id': fid, 'value': 'x'}),
    ('PUT', f'/api/records/{rid}', {'values': {}}),
    ('DELETE', f'/api/records/{rid}', None),
    ('POST', f'/api/datasheets/{did}/fields', {'name': 'x', 'type': 'text'}),
    ('PUT', f'/api/fields/{fid}', {'name': 'x'}),
    ('DELETE', f'/api/fields/{fid}', None),
    ('PUT', f'/api/datasheets/{did}', {'name': 'x'}),
    ('POST', f'/api/projects/{pid}/datasheets', {'name': 'x'}),
    ('DELETE', f'/api/datasheets/{did}', None),
]:
    s, _ = call(method, url, data, tok=disable_tok)
    expect(s == 401, f'disabled {method} {url[-40:]} -> {s}')

# === Cleanup ===
header('Cleanup')
# delete test project (admin)
call('DELETE', f'/api/projects/{pid}', tok=atok)
# delete test users
for uid in (dnon_uid, dview_uid, dedit_uid, disable_uid):
    call('DELETE', f'/api/admin/users/{uid}', tok=atok)

# === Summary ===
header('Summary')
real = [m for is_bug, m in bugs if is_bug]
fails = [m for is_bug, m in bugs if not is_bug]
print(f'BUGs/FAILs: {len(real)+len(fails)}')
for m in real + fails: print(f'   - {m}')
print(f'\nTotal cases: {65 + 13}')
