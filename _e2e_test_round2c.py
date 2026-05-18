"""Round-2C: more deep tests."""
import urllib.request, urllib.error, json, time
HOST = 'http://localhost:8000'

def call(method, url, data=None, tok=None):
    h = {}
    if tok: h['Authorization'] = f'Bearer {tok}'
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    if data is not None: h['Content-Type'] = 'application/json'
    req = urllib.request.Request(HOST + url, method=method, data=body, headers=h)
    try:
        resp = urllib.request.urlopen(req); raw = resp.read()
        try: return resp.status, json.loads(raw or b'null')
        except: return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw or b'null')
        except: return e.code, raw.decode('utf-8', errors='replace')

def login(u,p): return call('POST', '/api/auth/login', {'username':u,'password':p})[1]['access_token']

bugs = []
def expect(c, m, is_bug=False):
    print(f'  [{"PASS" if c else ("BUG" if is_bug else "FAIL")}] {m}')
    if not c: bugs.append((is_bug, m))
def header(t): print(f'\n{"="*50}\n {t}\n{"="*50}')

atok = login('admin','admin123')
ts = str(int(time.time()))

# === A. select 字段：旧值在选项被改后的处理 ===
header('A. select field config update')
s, r = call('POST', '/api/projects/22/datasheets', {'name':f'sel_test_{ts}'}, tok=atok)
did = r['id']
s, r = call('POST', f'/api/datasheets/{did}/fields',
            {'name':'status','type':'select','config':{'options':['A','B','C']}}, tok=atok)
fid = r['id']
s, r = call('POST', f'/api/datasheets/{did}/records', {'values':{}}, tok=atok)
rid = r['id']
s, r = call('PUT', f'/api/records/{rid}/cell', {'field_id': fid, 'value': 'A'}, tok=atok)
expect(s == 200, f'set select value A: {s}')

# 修改字段配置：把选项改成 B,C,D （'A' 不在新选项里）
s, r = call('PUT', f'/api/fields/{fid}', {'config':{'options':['B','C','D']}}, tok=atok)
expect(s == 200, f'update select options: {s}')

# 列 records，旧值 'A' 应当还在
s, r = call('GET', f'/api/datasheets/{did}/records', tok=atok)
rec = next((x for x in r if x['id']==rid), None)
expect(rec and rec['values'].get(str(fid))=='A', f'old value A preserved: {rec["values"] if rec else None}')

# cleanup
call('DELETE', f'/api/datasheets/{did}', tok=atok)

# === B. record sort_order 添加多行后顺序 ===
header('B. Record ordering')
s, r = call('POST', '/api/projects/22/datasheets', {'name':f'ord_test_{ts}'}, tok=atok)
did = r['id']
s, r = call('POST', f'/api/datasheets/{did}/fields', {'name':'n','type':'text'}, tok=atok)
fid = r['id']
ids = []
for i in range(3):
    s, r = call('POST', f'/api/datasheets/{did}/records', {'values':{str(fid):f'row{i}'}}, tok=atok)
    ids.append(r['id'])
s, r = call('GET', f'/api/datasheets/{did}/records', tok=atok)
got_ids = [x['id'] for x in r]
expect(got_ids == ids, f'records returned in insertion order: {got_ids} vs {ids}')
call('DELETE', f'/api/datasheets/{did}', tok=atok)

# === C. Overview field sort_order after delete ===
header('C. Overview field delete + sort')
s, r = call('POST', '/api/overview/fields', {'name':f'ovf_{ts}_X','type':'text'}, tok=atok)
fa = r['id']
s, r = call('POST', '/api/overview/fields', {'name':f'ovf_{ts}_Y','type':'text'}, tok=atok)
fb = r['id']
s, r = call('POST', '/api/overview/fields', {'name':f'ovf_{ts}_Z','type':'text'}, tok=atok)
fc = r['id']

# delete middle
s, _ = call('DELETE', f'/api/overview/fields/{fb}', tok=atok)
expect(s == 200, f'delete middle field: {s}')

# overview should not have fb
s, ov = call('GET', '/api/overview', tok=atok)
fids = [f['id'] for f in ov['fields']]
expect(fb not in fids, f'fb removed: {fb not in fids}')
expect(fa in fids and fc in fids, f'fa fc still: {fa in fids and fc in fids}')

# cleanup
call('DELETE', f'/api/overview/fields/{fa}', tok=atok)
call('DELETE', f'/api/overview/fields/{fc}', tok=atok)

# === D. 删除项目后能否再用 GET ===
header('D. Cannot GET soft-deleted project')
s, r = call('POST', '/api/projects', {'code':f'DEL_{ts}','name':'del test'}, tok=atok)
pid = r['id']
call('DELETE', f'/api/projects/{pid}', tok=atok)
s, r = call('GET', f'/api/projects/{pid}', tok=atok)
expect(s == 404, f'deleted project GET -> 404: {s} {r}')
s, ps = call('GET', '/api/projects', tok=atok)
expect(not any(p['id']==pid for p in ps), f'deleted not in list: ok')

# === E. Permission matrix dataset structure ===
header('E. Permission matrix structure')
s, m = call('GET', '/api/permissions/matrix', tok=atok)
expect(s == 200, f'matrix: {s}')
expect('roles' in m and 'overview' in m and 'datasheets' in m,
       f'matrix has keys: {list(m.keys())}')
expect('admin' not in [r['code'] for r in m['roles']], 'admin excluded')
expect('manager' not in [r['code'] for r in m['roles']], 'manager excluded')

# === F. project member with view permission ===
header('F. Project member with view perm')
# 创建 designer
uname = f'view_{ts}'
s, r = call('POST', '/api/admin/users', {'username':uname,'password':'init123','role_id':3}, tok=atok)
duid = r['id']
dtok = login(uname,'init123')
call('POST', '/api/auth/change-password', {'old_password':'init123','new_password':'newpwd9'}, tok=dtok)
dtok = login(uname,'newpwd9')

# 加为 view 成员
s, m = call('POST', '/api/projects/22/members', {'user_id':duid,'permission':'view'}, tok=atok)
expect(s == 200, f'add view member: {s}')

# 应能 list datasheets / records
s, ds = call('GET', '/api/projects/22/datasheets', tok=dtok)
expect(s == 200, f'view member lists datasheets: {s}')

# 不能修改单元格
did = ds[0]['id']
s, recs = call('GET', f'/api/datasheets/{did}/records', tok=dtok)
expect(s == 200 and len(recs) > 0, f'view member lists records: {s}')
rid = recs[0]['id']
_, flds = call('GET', f'/api/datasheets/{did}/fields', tok=dtok)
fid = flds[1]['id']
s, r = call('PUT', f'/api/records/{rid}/cell', {'field_id':fid,'value':'hacked'}, tok=dtok)
expect(s == 403, f'view member PUT cell forbidden: {s} {r}')

# 不能添加成员 / 改成员权限
s, r = call('POST', '/api/projects/22/members', {'user_id':2,'permission':'edit'}, tok=dtok)
expect(s == 403, f'view member cannot add members: {s} {r}')

# === Summary ===
header('Summary')
real = [m for is_bug,m in bugs if is_bug]
fails = [m for is_bug,m in bugs if not is_bug]
print(f'BUGs: {len(real)}')
for m in real: print(f'   - {m}')
print(f'Assertion fails: {len(fails)}')
for m in fails: print(f'   - {m}')
