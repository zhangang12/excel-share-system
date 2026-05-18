"""验证 manager 已拥有与 admin 同等的全部权限。"""
import urllib.request, urllib.error, json, time

HOST = 'http://localhost:8000'

def call(method, url, data=None, tok=None):
    h = {}
    if tok: h['Authorization'] = f'Bearer {tok}'
    body = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
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

fails = []
def expect(c, m):
    print(f'  [{"PASS" if c else "FAIL"}] {m}')
    if not c: fails.append(m)

mtok = login('manager', 'manager123')
ts = str(int(time.time()))
print('manager logged in\n')

# 1. overview 导出
s, _ = call('GET', '/api/overview/export', tok=mtok)
expect(s == 200, f'manager 导出项目一览 /api/overview/export -> {s}')

# 2. 操作审计（原仅 admin）
s, _ = call('GET', '/api/admin/audit?limit=5', tok=mtok)
expect(s == 200, f'manager 查操作审计 /api/admin/audit -> {s}')

# 3. 建项目 + 删项目（删原仅 admin）
s, r = call('POST', '/api/projects', {'code': f'MP-{ts}', 'name': 'manager权限测试'}, tok=mtok)
expect(s == 200, f'manager 创建项目 -> {s}')
if s == 200:
    pid = r['id']
    s, _ = call('DELETE', f'/api/projects/{pid}', tok=mtok)
    expect(s == 200, f'manager 删除项目 -> {s}')

# 4. 建用户 + 改用户 + 删用户（改/删原仅 admin）
_, roles = call('GET', '/api/admin/roles', tok=mtok)
designer_id = next((x['id'] for x in roles if x['code'] == 'designer'), None)
s, r = call('POST', '/api/admin/users',
            {'username': f'mp_{ts}', 'password': 'test123', 'role_id': designer_id}, tok=mtok)
expect(s == 200, f'manager 创建用户 -> {s}')
if s == 200:
    uid = r['id']
    s, _ = call('PUT', f'/api/admin/users/{uid}', {'full_name': '改名测试'}, tok=mtok)
    expect(s == 200, f'manager 修改用户 -> {s}')
    s, _ = call('DELETE', f'/api/admin/users/{uid}', tok=mtok)
    expect(s == 200, f'manager 删除用户 -> {s}')

# 5. manager 改项目状态
_, projs = call('GET', '/api/projects', tok=mtok)
if projs:
    pid = projs[0]['id']
    orig = projs[0]['status']
    s, r = call('PUT', f'/api/projects/{pid}', {'status': '已完成'}, tok=mtok)
    expect(s == 200 and r.get('status') == '已完成', f'manager 改项目状态 -> {s}')
    call('PUT', f'/api/projects/{pid}', {'status': orig}, tok=mtok)  # 还原

print()
if fails:
    print(f'FAIL: {len(fails)} 项')
    for m in fails: print(f'  - {m}')
else:
    print('[OK] manager 已拥有全部权限，全部通过')
