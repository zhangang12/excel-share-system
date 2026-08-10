"""🆕 提问卡（```ask 块）的解析与降级。

为什么单独一个文件：这是**第二个**会出现在模型正文里、又绝不能漏给用户看的块
（第一个是 ```render）。render 块踩过的坑——坏 JSON 泄漏裸文本——这里一个不落地再测一遍。
"""
from app.routers.agent_router import apply_render, extract_ask, ask_to_text


def test_摘出提问卡_正文里不留痕迹():
    body, ask = extract_ask(
        '060 有两个项目。\n\n```ask\n{"q":"查哪个？","options":'
        '[{"label":"2026-060A","send":"2026-060A 进度"},'
        '{"label":"2026-060B","send":"2026-060B 进度"}]}\n```')
    assert body == "060 有两个项目。"
    assert "```" not in body and "{" not in body      # 一个字符都不能漏
    assert ask["q"] == "查哪个？"
    assert [o["label"] for o in ask["options"]] == ["2026-060A", "2026-060B"]
    assert ask["options"][0]["send"] == "2026-060A 进度"


def test_没有块时原样返回():
    body, ask = extract_ask("本月销售额 328 万。")
    assert body == "本月销售额 328 万。" and ask is None


def test_坏JSON只删块不泄漏():
    """⚠️ 和 render 块同款铁律：宁可少一张卡，也不能把裸 JSON 甩给用户。"""
    body, ask = extract_ask('结论在这。\n```ask\n{"q":"查哪个？","options":[{,,,]}\n```')
    assert ask is None
    assert body == "结论在这。"
    assert "options" not in body and "{" not in body


def test_选项为空不出卡():
    """空 options 出一张没得点的卡，比不出卡更糟。"""
    body, ask = extract_ask('结论。\n```ask\n{"q":"查哪个？","options":[]}\n```')
    assert ask is None and body == "结论。"


def test_只写label没写send时用label当问题发出去():
    _, ask = extract_ask('```ask\n{"q":"哪个？","options":[{"label":"2026-060A"}]}\n```')
    assert ask["options"][0]["send"] == "2026-060A"


def test_选项最多四个():
    """手机上一屏点得完就这么多，多了等于没给选择。"""
    opts = ",".join('{"label":"P%d","send":"P%d"}' % (i, i) for i in range(9))
    _, ask = extract_ask('```ask\n{"q":"哪个？","options":[%s]}\n```' % opts)
    assert len(ask["options"]) == 4


def test_脏数据不炸():
    """模型什么都写得出来：options 是字符串、整个块是数组、字段缺失。"""
    for bad in ('{"options":"随便"}', '[1,2,3]', '{"q":"只有问题"}',
                '{"options":[{"label":""},{"send":"没有label"}]}'):
        body, ask = extract_ask("正文。\n```ask\n%s\n```" % bad)
        assert ask is None, bad
        assert body == "正文。", bad


def test_降级成纯文本_给渲染不了卡片的客户端():
    """⚠️ 桌面端渲染不了卡片，但**不能因此把问题整个丢掉**——
       那用户就只收到一句没头没尾的结论。"""
    ask = {"q": "查哪个 060？", "options": [
        {"label": "2026-060A 双行星混合机", "send": "2026-060A 进度"},
        {"label": "2026-060B 提升式压料机", "send": "2026-060B 进度"}]}
    t = ask_to_text(ask)
    assert t == "查哪个 060？\n1. 2026-060A 双行星混合机\n2. 2026-060B 提升式压料机"
    assert ask_to_text(None) == ""


def test_render块也不许漏_哪怕JSON写成数组():
    """回归：捕获组原来写死 `{...}`，模型把编排块写成数组时正则不匹配 → **整块漏给用户**。

    ⚠️ 这两个块的第一职责是「绝不出现在用户眼前」。解析失败可以少一段明细，
       但块本身必须已经被删干净。
    """
    for bad in ("[1,2,3]", "not json at all", '{"sort":,,,}'):
        out = apply_render("结论。\n```render\n%s\n```" % bad, {"items": [{"a": 1}]})
        assert "```" not in out, bad
        assert out.strip() == "结论。", bad


def test_超长文本被截断():
    _, ask = extract_ask('```ask\n{"q":"%s","options":[{"label":"%s","send":"%s"}]}\n```'
                         % ("问" * 300, "标" * 300, "发" * 300))
    assert len(ask["q"]) <= 100
    assert len(ask["options"][0]["label"]) <= 40
    assert len(ask["options"][0]["send"]) <= 120
