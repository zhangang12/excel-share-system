"""真实业务场景端到端测试。

模拟机械制造企业的实际使用流程：
  - 管理层 (manager) 给设计师/采购员/生产文员分配字段权限
  - 各角色按权限只能改自己负责的列
  - 多人同时在线编辑（WebSocket 实时协作）
  - 项目从创建到归档的完整生命周期
  - 成员协作（edit / view）
  - 字段权限 × 项目权限 的叠加效果
  - 审计追溯

依赖: websockets (pip install websockets)
"""
import urllib.request, urllib.error, json, time, asyncio
HOST = 'http://localhost:8000'
WS_HOST = 'ws://localhost:8000'

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
    if not c: bugs.append(m)
def header(t): print(f'\n{"="*62}\n {t}\n{"="*62}')

ROLE_ID = {  # seed 顺序
    'admin': 1, 'manager': 2, 'designer': 3, 'production_clerk': 4,
    'warehouse': 5, 'buyer_standard': 6, 'buyer_outsource': 7, 'hr': 8,
}

# ============ Setup ============
atok = login('admin', 'admin123')
mtok = login('manager', 'manager123')
ts = str(int(time.time()))

def mk_user(uname, role_id):
    s, r = call('POST', '/api/admin/users',
                {'username': uname, 'password': 'init123', 'role_id': role_id}, tok=atok)
    if s != 200: raise SystemExit(f'mk_user {uname}: {s} {r}')
    uid = r['id']
    tok = login(uname, 'init123')
    call('POST', '/api/auth/change-password',
         {'old_password': 'init123', 'new_password': 'real_pwd_1'}, tok=tok)
    return login(uname, 'real_pwd_1'), uid

# 三个真实角色用户
desg_tok, desg_uid = mk_user(f'biz_designer_{ts}', ROLE_ID['designer'])
buyer_tok, buyer_uid = mk_user(f'biz_buyer_{ts}', ROLE_ID['buyer_standard'])
clerk_tok, clerk_uid = mk_user(f'biz_clerk_{ts}', ROLE_ID['production_clerk'])
print(f'created users: designer={desg_uid}, buyer={buyer_uid}, clerk={clerk_uid}')

# 测试项目 + 数据表 + 字段
_, r = call('POST', '/api/projects',
            {'code': f'BIZ-{ts}', 'name': '业务场景测试项目'}, tok=atok)
pid = r['id']
_, r = call('POST', f'/api/projects/{pid}/datasheets', {'name': '钣金进度表'}, tok=atok)
did = r['id']
# 三个字段：设计内容 / 采购内容 / 生产内容
_, r = call('POST', f'/api/datasheets/{did}/fields', {'name': '设计内容', 'type': 'text'}, tok=atok)
f_design = r['id']
_, r = call('POST', f'/api/datasheets/{did}/fields', {'name': '采购内容', 'type': 'text'}, tok=atok)
f_buy = r['id']
_, r = call('POST', f'/api/datasheets/{did}/fields', {'name': '生产内容', 'type': 'text'}, tok=atok)
f_prod = r['id']
# 一行数据
_, r = call('POST', f'/api/datasheets/{did}/records', {'values': {}}, tok=atok)
rid = r['id']
print(f'project={pid}, datasheet={did}, fields=[{f_design},{f_buy},{f_prod}], record={rid}')

# ============ Scenario A: manager 配置字段权限 ============
header('Scenario A: manager 给各角色分配字段权限')

# 设计内容：只有 designer 可编辑，buyer/clerk 只读
s, r = call('PUT', f'/api/permissions/fields/{f_design}', {'permissions': [
    {'role_id': ROLE_ID['designer'],        'can_view': True, 'can_edit': True},
    {'role_id': ROLE_ID['buyer_standard'],  'can_view': True, 'can_edit': False},
    {'role_id': ROLE_ID['production_clerk'],'can_view': True, 'can_edit': False},
]}, tok=mtok)
expect(s == 200, f'manager 配置"设计内容"权限: {s}')

# 采购内容：只有 buyer 可编辑
s, r = call('PUT', f'/api/permissions/fields/{f_buy}', {'permissions': [
    {'role_id': ROLE_ID['designer'],        'can_view': True, 'can_edit': False},
    {'role_id': ROLE_ID['buyer_standard'],  'can_view': True, 'can_edit': True},
    {'role_id': ROLE_ID['production_clerk'],'can_view': True, 'can_edit': False},
]}, tok=mtok)
expect(s == 200, f'manager 配置"采购内容"权限: {s}')

# 生产内容：clerk 可编辑，designer 完全不可见
s, r = call('PUT', f'/api/permissions/fields/{f_prod}', {'permissions': [
    {'role_id': ROLE_ID['designer'],        'can_view': False, 'can_edit': False},
    {'role_id': ROLE_ID['buyer_standard'],  'can_view': True,  'can_edit': False},
    {'role_id': ROLE_ID['production_clerk'],'can_view': True,  'can_edit': True},
]}, tok=mtok)
expect(s == 200, f'manager 配置"生产内容"权限: {s}')

# 验证保存正确
s, r = call('GET', f'/api/permissions/fields/{f_design}', tok=mtok)
desg_perm = next((p for p in r if p['role_id'] == ROLE_ID['designer']), None)
expect(desg_perm and desg_perm['can_edit'] == True, f'回读"设计内容" designer can_edit=true')

# ============ Scenario B: 各角色按字段权限编辑 ============
header('Scenario B: 各角色按权限编辑数据表')

# 三个角色都加为项目 edit 成员
for uid in (desg_uid, buyer_uid, clerk_uid):
    call('POST', f'/api/projects/{pid}/members', {'user_id': uid, 'permission': 'edit'}, tok=atok)

# designer 改"设计内容" → 成功
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': 'B01 钣金图纸已出'}, tok=desg_tok)
expect(s == 200, f'designer 编辑"设计内容": {s}')

# designer 改"采购内容" → 403
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_buy, 'value': '越权采购'}, tok=desg_tok)
expect(s == 403, f'designer 编辑"采购内容"被拒: {s}')

# designer 改"生产内容"(对它不可见) → 403
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_prod, 'value': '越权生产'}, tok=desg_tok)
expect(s == 403, f'designer 编辑不可见的"生产内容"被拒: {s}')

# buyer 改"采购内容" → 成功
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_buy, 'value': '标准件已下单'}, tok=buyer_tok)
expect(s == 200, f'buyer 编辑"采购内容": {s}')

# buyer 改"设计内容" → 403
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': '越权设计'}, tok=buyer_tok)
expect(s == 403, f'buyer 编辑"设计内容"被拒: {s}')

# clerk 改"生产内容" → 成功
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_prod, 'value': '钣金加工中'}, tok=clerk_tok)
expect(s == 200, f'clerk 编辑"生产内容": {s}')

# 验证 my_perms 正确反映给前端
s, perms = call('GET', f'/api/permissions/me/datasheet/{did}', tok=desg_tok)
expect(perms.get(str(f_prod), {}).get('can_view') == False,
       f'designer my_perms: "生产内容" can_view=false')
expect(perms.get(str(f_buy), {}).get('can_edit') == False,
       f'designer my_perms: "采购内容" can_edit=false')

# 最终数据正确
s, recs = call('GET', f'/api/datasheets/{did}/records', tok=atok)
vals = recs[0]['values']
expect(vals.get(str(f_design)) == 'B01 钣金图纸已出', f'设计内容值正确: {vals.get(str(f_design))}')
expect(vals.get(str(f_buy)) == '标准件已下单', f'采购内容值正确: {vals.get(str(f_buy))}')
expect(vals.get(str(f_prod)) == '钣金加工中', f'生产内容值正确: {vals.get(str(f_prod))}')

# ============ Scenario C: 多人 WebSocket 实时协作 ============
header('Scenario C: 多人在线协作（WebSocket 广播）')

async def recv_until(ws, pred, timeout=5):
    """循环接收消息直到 pred(dict) 为真；超时返回 None。
    （WebSocket 是消息流，broadcast 不 exclude 发送者，需 drain 到目标消息）"""
    import time as _t
    deadline = _t.monotonic() + timeout
    while True:
        remain = deadline - _t.monotonic()
        if remain <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), remain)
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if pred(d):
            return d

async def ws_collab_test():
    try:
        import websockets
    except ImportError:
        print('  (websockets 库未安装，跳过 WS 测试)')
        return

    results = []
    room = f'/ws/datasheets/{did}'

    desg_ws = await websockets.connect(f'{WS_HOST}{room}?token={desg_tok}', open_timeout=5)
    buyer_ws = await websockets.connect(f'{WS_HOST}{room}?token={buyer_tok}', open_timeout=5)
    try:
        # 1. designer 应收到 buyer 的 presence join
        d = await recv_until(desg_ws,
            lambda m: m.get('type') == 'presence' and m.get('action') == 'join')
        results.append(('presence join 广播', d is not None, d))

        # 2. buyer 改"采购内容" → designer 应收到对应 cell_changed
        call('PUT', f'/api/records/{rid}/cell',
             {'field_id': f_buy, 'value': 'WS协作-采购更新'}, tok=buyer_tok)
        d = await recv_until(desg_ws,
            lambda m: m.get('type') == 'cell_changed' and m.get('field_id') == f_buy)
        ok = (d and d.get('value') == 'WS协作-采购更新'
              and d.get('record_id') == rid and d.get('by_user_id') == buyer_uid)
        results.append(('designer 收到 buyer 的 cell_changed', ok, d))

        # 3. designer 改"设计内容" → buyer 应收到对应 cell_changed
        call('PUT', f'/api/records/{rid}/cell',
             {'field_id': f_design, 'value': 'WS协作-设计更新'}, tok=desg_tok)
        d = await recv_until(buyer_ws,
            lambda m: m.get('type') == 'cell_changed' and m.get('field_id') == f_design)
        ok = (d and d.get('value') == 'WS协作-设计更新'
              and d.get('by_user_id') == desg_uid)
        results.append(('buyer 收到 designer 的 cell_changed', ok, d))

        # 4. ping/pong
        await desg_ws.send(json.dumps({'type': 'ping'}))
        d = await recv_until(desg_ws, lambda m: m.get('type') == 'pong')
        results.append(('ping/pong', d is not None, d))

        # 5. buyer 断开 → designer 应收到 presence leave
        await buyer_ws.close()
        d = await recv_until(desg_ws,
            lambda m: m.get('type') == 'presence' and m.get('action') == 'leave')
        results.append(('presence leave 广播', d is not None, d))
    finally:
        await desg_ws.close()
        try: await buyer_ws.close()
        except: pass
    return results

ws_results = asyncio.run(ws_collab_test())
if ws_results:
    for label, ok, info in ws_results:
        expect(ok, f'{label}: {info}')

# ============ Scenario D: overview 字段权限 + 多角色编辑 ============
header('Scenario D: 项目一览 字段权限 + 多角色编辑')

# 取一个 overview 字段
s, ov = call('GET', '/api/overview', tok=atok)
if ov['fields']:
    ovf = ov['fields'][0]['id']
    ovf_name = ov['fields'][0]['name']
    # manager 配置：designer 可编辑，buyer 只读
    s, r = call('PUT', f'/api/permissions/overview-fields/{ovf}', {'permissions': [
        {'role_id': ROLE_ID['designer'],       'can_view': True, 'can_edit': True},
        {'role_id': ROLE_ID['buyer_standard'], 'can_view': True, 'can_edit': False},
    ]}, tok=mtok)
    expect(s == 200, f'manager 配置一览字段"{ovf_name}"权限: {s}')

    # 注意：一览 cell 编辑也要项目级 edit 权限。
    # designer/buyer 都是测试项目 pid 的 edit 成员，所以用 pid 这一行。
    # designer 改 → 200（项目成员 + 字段 can_edit=true）
    s, r = call('PUT', f'/api/overview/projects/{pid}/cell',
                {'field_id': ovf, 'value': 'designer填'}, tok=desg_tok)
    expect(s == 200, f'designer 编辑一览字段(本项目成员+有字段权): {s}')
    # buyer 改 → 403（字段 can_edit=false）
    s, r = call('PUT', f'/api/overview/projects/{pid}/cell',
                {'field_id': ovf, 'value': 'buyer越权'}, tok=buyer_tok)
    expect(s == 403, f'buyer 编辑只读一览字段被拒: {s}')
    # 清理：重置该字段权限
    call('PUT', f'/api/permissions/overview-fields/{ovf}', {'permissions': []}, tok=mtok)
else:
    print('  (overview 无字段，跳过)')

# ============ Scenario E: 项目生命周期 ============
header('Scenario E: 项目生命周期 进行中→已完成→已归档→删除')

s, r = call('POST', '/api/projects',
            {'code': f'LIFE-{ts}', 'name': '生命周期测试', 'status': '进行中'}, tok=mtok)
expect(s == 200 and r['status'] == '进行中', f'创建项目(进行中): {s}')
life_pid = r['id']

s, r = call('PUT', f'/api/projects/{life_pid}', {'status': '已完成'}, tok=mtok)
expect(s == 200 and r['status'] == '已完成', f'改状态→已完成: {r.get("status")}')

s, r = call('PUT', f'/api/projects/{life_pid}', {'status': '已归档'}, tok=mtok)
expect(s == 200 and r['status'] == '已归档', f'改状态→已归档: {r.get("status")}')

s, r = call('DELETE', f'/api/projects/{life_pid}', tok=atok)
expect(s == 200, f'admin 软删项目: {s}')

s, r = call('GET', f'/api/projects/{life_pid}', tok=atok)
expect(s == 404, f'删除后不可访问: {s}')

# 同 code 可重建
s, r = call('POST', '/api/projects',
            {'code': f'LIFE-{ts}', 'name': '生命周期重建'}, tok=mtok)
expect(s == 200, f'软删后同 code 可重建: {s}')
if s == 200: call('DELETE', f'/api/projects/{r["id"]}', tok=atok)

# ============ Scenario F: 成员 edit vs view ============
header('Scenario F: 项目成员 edit / view 权限')

view_tok, view_uid = mk_user(f'biz_viewer_{ts}', ROLE_ID['hr'])
call('POST', f'/api/projects/{pid}/members', {'user_id': view_uid, 'permission': 'view'}, tok=atok)

# view 成员可读
s, r = call('GET', f'/api/datasheets/{did}/records', tok=view_tok)
expect(s == 200, f'view 成员可读数据表: {s}')
# view 成员不能写
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': 'view越权'}, tok=view_tok)
expect(s == 403, f'view 成员不能编辑: {s}')
# view 成员不能加成员
s, r = call('POST', f'/api/projects/{pid}/members',
            {'user_id': clerk_uid, 'permission': 'edit'}, tok=view_tok)
expect(s == 403, f'view 成员不能管理成员: {s}')

# 升级为 edit
_, members = call('GET', f'/api/projects/{pid}/members', tok=atok)
view_mid = next(m['id'] for m in members if m['user_id'] == view_uid)
s, r = call('PUT', f'/api/projects/{pid}/members/{view_mid}',
            {'user_id': view_uid, 'permission': 'edit'}, tok=mtok)
expect(s == 200, f'manager 把成员 view→edit: {s}')
# 升级后能写没有字段限制的字段？hr 角色对 f_design 没配权限 → 默认可编辑
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': 'hr升级后编辑'}, tok=view_tok)
expect(s == 200, f'升级 edit 后可编辑(hr 无字段限制): {s}')

# ============ Scenario G: 字段权限 × 项目权限 叠加 ============
header('Scenario G: 字段权限 × 项目权限 叠加')

# clerk 是项目 edit 成员（Scenario B 已加），但"设计内容"对 clerk can_edit=false
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': 'clerk越权设计'}, tok=clerk_tok)
expect(s == 403, f'edit 成员 + 字段无编辑权 → 该字段仍拒绝: {s}')
# clerk 改"生产内容"（有权）→ 成功
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_prod, 'value': 'clerk正常生产'}, tok=clerk_tok)
expect(s == 200, f'edit 成员 + 字段有编辑权 → 成功: {s}')
# 非项目成员即使字段 can_edit=true 也不能编辑（项目级先拦）
nonmem_tok, nonmem_uid = mk_user(f'biz_nonmem_{ts}', ROLE_ID['designer'])
s, r = call('PUT', f'/api/records/{rid}/cell',
            {'field_id': f_design, 'value': '非成员越权'}, tok=nonmem_tok)
expect(s == 403, f'非项目成员即使字段有权也被项目级拦截: {s}')

# ============ Scenario H: 审计追溯 ============
header('Scenario H: 操作审计追溯')

s, audit = call('GET', '/api/admin/audit?limit=100', tok=atok)
expect(s == 200, f'admin 查审计: {s}')
actions = set(a['action'] for a in audit) if isinstance(audit, list) else set()
print(f'  审计动作类型: {sorted(actions)}')
expect('login' in actions, '审计含 login')
expect('create_project' in actions, '审计含 create_project')
expect('change_password' in actions, '审计含 change_password')
# manager 可以查审计（require_admin_or_manager）
s, r = call('GET', '/api/admin/audit', tok=mtok)
expect(s == 200, f'manager 可查审计(管理层): {s}')
# designer 不能查审计
s, r = call('GET', '/api/admin/audit', tok=desg_tok)
expect(s == 403, f'designer 不能查审计: {s}')

# ============ Cleanup ============
header('Cleanup')
call('DELETE', f'/api/projects/{pid}', tok=atok)
for uid in (desg_uid, buyer_uid, clerk_uid, view_uid, nonmem_uid):
    call('DELETE', f'/api/admin/users/{uid}', tok=atok)
print('  测试用户/项目已清理')

# ============ Summary ============
header('Summary')
print(f'BUGs: {len(bugs)}')
for m in bugs: print(f'   - {m}')
if not bugs:
    print('  ✓ 所有业务场景通过')
