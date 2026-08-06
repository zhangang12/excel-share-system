#!/usr/bin/env python3
"""从公司标识生成 Android 全套应用图标。**可重复运行** —— 换了 logo 就重跑一次。

    python3 mobile/tools/make-icons.py

为什么要有这个脚本，而不是手工导一堆图
--------------------------------------
Android 一套图标是 20+ 个文件（5 档密度 × 传统/圆形/自适应前景 + 单色 + 商店图）。
手工导必然漏、必然不一致，而且下次换 logo 没人记得当初怎么导的。

源图
----
`frontend/src/assets/logo.png`（1181×1082，**透明底**）—— 同辉的 TH 齿轮标。
桌面客户端 `desktop/build/icon.png` 是同一个标的白底版，两端保持一致。

自适应图标的尺寸纪律（最容易做错的地方）
----------------------------------------
⚠️ 画布是 **108dp**，但**只有中间 72dp 保证可见**：各家启动器会用圆形／方圆形／
   水滴形去裁，裁的就是外面那圈。内容画到边上，在圆形启动器上直接被切掉。
⚠️ 而且圆形裁切下，72dp **方形的四角**也在圆外 —— 严格算内容要塞进
   直径 72dp 的圆，即边长 72/√2 ≈ 51dp。同辉这个标整体近似圆盘（外圈是月牙），
   所以按 **62% 见方**放能兼顾「不被切」和「不至于小得像枚邮票」。
   改这个数之前先跑 --preview 看圆形裁切效果。

传统图标（API 24/25 没有自适应图标）
------------------------------------
`ic_launcher.png` 走方圆形、`ic_launcher_round.png` 走正圆，都是**自己画好底**，
因为老系统不会帮你裁也不会帮你补背景。
"""
import os
import sys
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "frontend/src/assets/logo.png")
RES = os.path.join(ROOT, "mobile/android/app/src/main/res")

# 品牌色（取自 logo 实际像素，不是眼估）
NAVY = (5, 35, 55)
GOLD = (216, 174, 87)
# 底色：白 → 极浅冷灰的竖向渐变。纯白在一堆彩色图标里发飘，
# 一点点渐变就能有体积感，又不动品牌色。
BG_TOP = (255, 255, 255)
BG_BOTTOM = (237, 241, 246)

# 自适应图标：内容占画布的比例。见文件头「尺寸纪律」。
ADAPTIVE_SCALE = 0.62
# 传统图标：老系统不裁，可以铺满一些
LEGACY_SCALE = 0.66

DENSITIES = {          # 目录后缀 → 传统图标边长(px)
    "mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192,
}
# 自适应前景是 108dp 画布，所以每档 = 传统边长 / 48 * 108
FG_SIZE = {k: round(v / 48 * 108) for k, v in DENSITIES.items()}


def load_logo() -> Image.Image:
    """读源图并裁到内容边界 —— 源图四周有 30px 空白，不裁的话实际内容会偏小。"""
    im = Image.open(SRC).convert("RGBA")
    box = im.getbbox()
    if not box:
        sys.exit("源图整张透明？")
    return im.crop(box)


def optical_offset(logo: Image.Image) -> tuple[float, float]:
    """视觉重心相对几何中心的偏移（比例）。

    ⚠️ 按外接矩形居中**看起来是偏的**：这个标左边是一道厚月牙、右边是笔画细的
       TH，实测重心比几何中心偏左 3.4%、偏下 2.2%。按矩形摆，圆形启动器里
       一眼就能看出没居中。这里按不透明像素的重心校正回来。
    """
    a = logo.split()[3]
    w, h = logo.size
    px = a.load()
    sx = sy = n = 0
    for y in range(h):
        for x in range(w):
            v = px[x, y]
            if v:
                sx += x * v; sy += y * v; n += v
    if not n:
        return 0.0, 0.0
    return (sx / n - w / 2) / w, (sy / n - h / 2) / h


def fit(logo: Image.Image, canvas: int, scale: float,
        offset: tuple[float, float] = (0.0, 0.0)) -> Image.Image:
    """把 logo 等比缩放到 canvas*scale 见方，按**视觉重心**居中。"""
    target = canvas * scale
    w, h = logo.size
    k = target / max(w, h)
    # LANCZOS：源图边缘是硬的（没有羽化），降采样时靠它把锯齿磨掉
    small = logo.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    # 重心偏左就整体右移，偏下就上移 —— 移的是重心偏移量本身
    dx = round(-offset[0] * small.width)
    dy = round(-offset[1] * small.height)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(small, ((canvas - small.width) // 2 + dx,
                      (canvas - small.height) // 2 + dy), small)
    return out


def gradient(size: int) -> Image.Image:
    im = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        im.putpixel((0, y), tuple(
            round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    return im.resize((size, size)).convert("RGBA")


def rounded_mask(size: int, radius_ratio: float) -> Image.Image:
    m = Image.new("L", (size * 4, size * 4), 0)      # 4 倍超采样，边缘才不毛糙
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, size * 4 - 1, size * 4 - 1], radius=round(size * 4 * radius_ratio), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
    return m.resize((size, size), Image.LANCZOS)


def compose(logo: Image.Image, size: int, mask: Image.Image, scale: float,
            offset: tuple[float, float] = (0.0, 0.0)) -> Image.Image:
    base = gradient(size)
    base.alpha_composite(fit(logo, size, scale, offset))
    base.putalpha(mask)
    return base


def main() -> None:
    logo = load_logo()
    off = optical_offset(logo)
    print(f"源图内容 {logo.size}  视觉重心偏移 x{off[0]*100:+.1f}% y{off[1]*100:+.1f}%（已校正）")
    made = 0

    for d, px in DENSITIES.items():
        out = os.path.join(RES, f"mipmap-{d}")
        os.makedirs(out, exist_ok=True)
        # 传统方圆形 / 正圆（API 24/25 用；系统不会帮它们裁或补底）
        compose(logo, px, rounded_mask(px, 0.22), LEGACY_SCALE, off).save(
            os.path.join(out, "ic_launcher.png"))
        compose(logo, px, circle_mask(px), LEGACY_SCALE, off).save(
            os.path.join(out, "ic_launcher_round.png"))
        # 自适应前景：**透明底**，背景是另一层，这里绝不能自己画底
        fg = FG_SIZE[d]
        fit(logo, fg, ADAPTIVE_SCALE, off).save(os.path.join(out, "ic_launcher_foreground.png"))
        # Android 13+ 主题图标：纯剪影，系统按用户主题上色
        sil = Image.new("RGBA", (fg, fg), (0, 0, 0, 0))
        sil.paste((0, 0, 0, 255), (0, 0, fg, fg), fit(logo, fg, ADAPTIVE_SCALE, off).split()[3])
        sil.save(os.path.join(out, "ic_launcher_monochrome.png"))
        made += 4
        print(f"  mipmap-{d:8s} 传统 {px}px / 自适应前景 {fg}px")

    # 应用商店用图（内部分发用不到，但换 logo 时一并出，省得下次现找）
    store = os.path.join(ROOT, "mobile/tools/play-store-icon-512.png")
    compose(logo, 512, Image.new("L", (512, 512), 255), LEGACY_SCALE, off).convert("RGB").save(store)
    print(f"  商店图 512px → {os.path.relpath(store, ROOT)}")

    if "--preview" in sys.argv:
        # 拼一张对照图：圆形/方圆形/正方三种裁切下各是什么样，用来确认没被切掉
        prev = Image.new("RGB", (3 * 220 + 40, 260), (236, 238, 241))
        for i, m in enumerate((circle_mask(192), rounded_mask(192, 0.28),
                               Image.new("L", (192, 192), 255))):
            tile = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
            tile.alpha_composite(gradient(192))
            tile.alpha_composite(fit(logo, 192, ADAPTIVE_SCALE, off))   # 按自适应比例摆
            tile.putalpha(m)
            prev.paste(tile, (20 + i * 220, 34), tile)
        p = os.path.join(ROOT, "mobile/tools/_preview.png")
        prev.save(p)
        print(f"  预览图 → {os.path.relpath(p, ROOT)}（三种裁切）")

    print(f"共 {made} 个图标文件")


if __name__ == "__main__":
    main()
