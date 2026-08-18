"""🆕 Agent 画图：后端只吐**数据**，SVG 由前端拼。

用户原话：「要让 Agent 会画图，不然还是答的太枯燥了」。

⚠️⚠️ 本测试最要紧的一条：**后端发出去的块里不许出现任何标签**。
   H5 的 markdown 是 `html: false` + `v-html` 成对的，后端一旦开始吐 SVG，
   XSS 防线当场归零 —— 而工具数据里带着用户自由填写的备注、物料名、OA 详情，
   `<img src=x onerror=…>` 完全可能混在里面。
   （前端那一半的转义在浏览器里实测过：`<img onerror>` 只当字面量。）
"""
import json
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="chart")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from app.agent import render as rd

_SPEC = {"kind": "bar", "label": "project", "value": "days_left", "title": "距交货日"}


def _block(items, spec=None):
    return rd.chart_block({"items": items, "chart": spec or _SPEC})


def _payload(items, spec=None):
    b = _block(items, spec)
    assert b.startswith("```pmschart\n") and b.endswith("\n```"), b[:40]
    return json.loads(b[len("```pmschart\n"):-len("\n```")])


def test_交期看板画成发散条():
    p = _payload([{"project": "2026-071A", "days_left": -12},
                  {"project": "2026-070", "days_left": 3},
                  {"project": "2026-077", "days_left": 40}])
    assert p["kind"] == "bar" and p["title"] == "距交货日"
    assert p["zero"] == "今天", "中线要标出来，否则看不出正负是什么意思"
    assert [r["label"] for r in p["rows"]] == ["2026-071A", "2026-070", "2026-077"]
    assert [r["value"] for r in p["rows"]] == [-12.0, 3.0, 40.0]


def test_天数说人话():
    """⚠️ 「-5 天」没人看得懂是过期了还是还剩 —— 表格那边早就踩过这个坑。"""
    p = _payload([{"project": "A", "days_left": -5},
                  {"project": "B", "days_left": 0},
                  {"project": "C", "days_left": 9}])
    assert [r["text"] for r in p["rows"]] == ["已过 5 天", "今天到期", "9 天"]


def test_颜色按紧急度():
    p = _payload([{"project": "A", "days_left": -1},
                  {"project": "B", "days_left": 7},
                  {"project": "C", "days_left": 8}])
    assert [r["tone"] for r in p["rows"]] == ["danger", "warn", "good"]


def test_没声明chart就不画():
    """不是所有答案都该配图。一张对不上题的图比没有图更让人分神。"""
    assert rd.chart_block({"items": [{"project": "A", "days_left": 1}]}) == ""
    assert rd.chart_block({"chart": _SPEC}) == ""          # 没有 items
    assert rd.chart_block(None) == ""
    assert rd.chart_block({"items": [], "chart": _SPEC}) == ""


def test_只有一条不画():
    """一根条子不成图，白占半屏。"""
    assert _block([{"project": "A", "days_left": 3}]) == ""


def test_最多八条():
    """手机上一屏能看清的就这么多，再多人就开始滚动找。"""
    p = _payload([{"project": f"P{i}", "days_left": i} for i in range(20)])
    assert len(p["rows"]) == 8


def test_取不到数的行直接丢():
    """days_left 是 None（没填交货日期）的项目不该在图上占一格。"""
    p = _payload([{"project": "A", "days_left": None},
                  {"project": "B", "days_left": 3},
                  {"project": "C", "days_left": 5},
                  {"project": "D"}])
    assert [r["label"] for r in p["rows"]] == ["B", "C"]


def test_布尔值不当数字():
    """Python 里 True 是 int 的子类，不挡的话 True 会画成一根长度 1 的条。"""
    p = _payload([{"project": "A", "days_left": True},
                  {"project": "B", "days_left": 3},
                  {"project": "C", "days_left": 5}])
    assert [r["label"] for r in p["rows"]] == ["B", "C"]


def test_标签截短且去掉括号补充():
    """条子左边只有 84px，放不下设备全名。"""
    p = _payload([{"project": "5L双行星分散混合压料一体机（参展机）", "days_left": 3},
                  {"project": "B", "days_left": 5}],
                 {**_SPEC, "label": "project"})
    lab = p["rows"][0]["label"]
    assert lab == "5L双行星分散混合压料一体机"[:10]     # 先去括号，再切 10 个字
    assert "（" not in lab and "参展机" not in lab
    assert len(lab) <= 10


def test_块里绝不含任何标签():
    """⚠️⚠️ 这条是这个文件存在的理由。

    后端发的是**纯 JSON 数据**。数据里带 `<img src=x onerror=…>` 时，
    它必须原样躺在 JSON 字符串里（前端会转义），
    而**块本身不能出现 `<svg`、`<text`、`<rect` 这类结构标签** ——
    一旦后端开始吐结构，`html:false` 就形同虚设。
    """
    evil = '<img src=x onerror=alert(1)>'
    b = _block([{"project": evil, "days_left": -3},
                {"project": "正常项目", "days_left": 5}])
    for tag in ("<svg", "<text", "<rect", "<line", "<div", "</"):
        assert tag not in b, f"后端不许吐 {tag}"
    # 恶意串本身可以在数据里（前端负责转义），但必须是 JSON 字符串的内容
    p = json.loads(b[len("```pmschart\n"):-len("\n```")])
    assert p["rows"][0]["label"].startswith("<img"), "数据原样保留，转义是前端的事"


def test_图不依赖表格_四种模型输出都要有图():
    """🐛 回归：用户反馈「手机的图没有渲染出来」。

    图原来是**跟在表格后面**拼的，于是这两种最常见的情况直接走 early return，
    图一次都出不来：
      · 模型只写了结论（没给 ```render 块，用户也没说「清单」）
      · 模型自己打了明细行（它被「务必简短」和「别自己打明细」两头约束，常这么偷懒）

    图是**工具声明**的，跟模型写没写编排块无关 —— 每条路径都必须带上。
    """
    from app.routers.agent_router import apply_render

    result = {"count": 3, "shown": 3,
              "columns": ["project", "days_left"],
              "chart": _SPEC,
              "items": [{"project": "2026-071A", "days_left": -12},
                        {"project": "2026-070", "days_left": 3},
                        {"project": "2026-077", "days_left": 40}]}

    cases = [
        ("给了编排块", '**结论。**\n\n```render\n{"sort":"days_left"}\n```', False),
        ("只写结论", "**结论。**", False),
        ("要了清单", "**结论。**", True),
        ("自己打了明细行", "**结论。**\n- 2026-071A 已过 12 天\n- 2026-070 剩 3 天", True),
    ]
    for name, reply, want_list in cases:
        out = apply_render(reply, result, want_list)
        assert "pmschart" in out, f"「{name}」这条路径没有图"
        assert "```render" not in out, f"「{name}」把编排块漏出去了"


def test_没声明chart的结果不会凭空长出图():
    """反向：图只在工具声明时出现，不是每条回答都配图。"""
    from app.routers.agent_router import apply_render
    r = {"count": 1, "items": [{"project": "A", "days_left": 3}]}   # 没有 chart 键
    assert "pmschart" not in apply_render("**结论。**", r, True)


def test_开头空白不许被剥掉():
    """⚠️ 流式那边按「已推给用户多少字」切片补发尾巴（final[streamed:]）。

    拼接时若把第一段**开头**的空白也 strip 掉，整段会左移，
    切片就切进正文里 —— 表现正是「图渲染不出来」。第一段只能 rstrip。
    """
    from app.routers.agent_router import _join_parts
    out = _join_parts("\n\n结论。  \n", "表格", "图")
    assert out.startswith("\n\n结论。"), repr(out[:10])
    assert out == "\n\n结论。\n\n表格\n\n图"


def test_非天数场景用通用格式():
    p = _payload([{"supplier": "甲", "over_days": 12},
                  {"supplier": "乙", "over_days": 3}],
                 {"kind": "bar", "label": "supplier", "value": "over_days",
                  "unit": "天", "title": "超期"})
    assert [r["text"] for r in p["rows"]] == ["12天", "3天"]
    assert "zero" not in p, "不是天数倒计时就不画中线"
    assert all(r["tone"] == "good" for r in p["rows"])
