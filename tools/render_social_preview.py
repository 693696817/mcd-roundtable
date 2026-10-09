# -*- coding: utf-8 -*-
"""
生成 GitHub 仓库的社交预览图（Social preview，1280×640）。

为什么要有它：这个链接一旦被发到群里、社交平台，别人看到的就是这张卡片 ——
它决定点不点进来，也就直接关系到 Star。

配色不另起一套：直接从 html_report.py 里 import 品牌色，
保证它和决议卡、网页输出永远是同一个红黄，改一处全都跟着变。

注意：GitHub 不提供设置 social preview 的 API，生成后需要手工上传到
Settings → Social preview。本脚本只负责把图做出来（这样它也符合
README 里那句「assets 里的图没有一张是手工画的」）。

用法：
    python tools/render_social_preview.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from mcd_roundtable.html_report import BRAND_RED, BRAND_YELLOW, INK  # noqa: E402

W, H = 1280, 640
OUT = os.path.join(os.path.dirname(HERE), "assets", "social-preview.png")

CJK_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
CJK_REG = "C:/Windows/Fonts/msyh.ttc"
MONO_BOLD = "C:/Windows/Fonts/consolab.ttf"


def f(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def mix(c1, c2, t):
    """在两色之间插值，t=0 取 c1、t=1 取 c2。"""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main():
    img = Image.new("RGB", (W, H), BRAND_RED)
    d = ImageDraw.Draw(img)

    # ---- 背景：右下角一团更深的红 ----
    # 不要用「一层层画同心圆」的办法：那会产生肉眼可见的环形色带。
    # 正确做法是在一张极小的图上算好渐变，再用 BICUBIC 放大 —— 插值天然平滑，
    # 而且代价只有 64x32 次运算。
    low = Image.new("RGB", (64, 32))
    px = low.load()
    for y in range(32):
        for x in range(64):
            dx = (x / 63 - 0.86) * 1.55          # 光心在右下角
            dy = (y / 31 - 0.98)
            dist = min(1.0, (dx * dx + dy * dy) ** 0.5)
            px[x, y] = mix(BRAND_RED, "#7E150D", (1.0 - dist) ** 1.6 * 0.9)
    img.paste(low.resize((W, H), Image.BICUBIC), (0, 0))

    # ---- 顶部金拱门黄条 ----
    d.rectangle([0, 0, W, 10], fill=BRAND_YELLOW)

    # ---- 左侧文案 ----
    d.text((80, 108), "麦门圆桌", font=f(CJK_BOLD, 92), fill="#FFFFFF")
    d.text((84, 226), "mcd-roundtable", font=f(MONO_BOLD, 40), fill=BRAND_YELLOW)

    d.line([80, 296, 700, 296], fill=mix(BRAND_RED, "#FFFFFF", 0.34), width=2)

    d.text((80, 330), "五个 AI 人格当场吵起来，", font=f(CJK_BOLD, 34), fill="#FFFFFF")
    d.text((80, 382), "最后吵出一个真能下单的结果。", font=f(CJK_BOLD, 34), fill="#FFFFFF")

    d.text((80, 462), "省钱部长 · 健身总监 · 麦门老饕 · 养生专员 · 尝鲜委员",
           font=f(CJK_REG, 22), fill=mix("#FFFFFF", BRAND_YELLOW, 0.55))

    d.text((80, 512), "基于麦当劳官方 MCP（mcd-mcp）", font=f(CJK_REG, 22),
           fill=mix("#FFFFFF", BRAND_RED, 0.22))
    d.text((80, 546), "模型负责「吵」，官方接口负责「算」", font=f(CJK_REG, 22),
           fill=mix("#FFFFFF", BRAND_RED, 0.22))

    # ---- 右侧：锯齿小票（与 --html 输出里那张同构）----
    x0, y0, x1, y1 = 800, 96, 1200, 508
    d.rectangle([x0, y0, x1, y1], fill="#FFFFFF")
    teeth, tooth = 14, 13
    step = (x1 - x0) / teeth
    for i in range(teeth):
        tx = x0 + i * step
        d.polygon([(tx, y1), (tx + step, y1), (tx + step / 2, y1 + tooth)], fill="#FFFFFF")

    mono = f(MONO_BOLD, 26)
    cjk = f(CJK_BOLD, 24)
    small = f(CJK_REG, 19)

    def dashed(y, color="#D8D2CA"):
        x = x0 + 26
        while x < x1 - 26:
            d.line([x, y, min(x + 9, x1 - 26), y], fill=color, width=2)
            x += 16

    d.text((x0 + 26, y0 + 26), "麦门决议卡", font=cjk, fill=INK)
    d.text((x1 - 26, y0 + 30), "NO. 5414", font=small, fill="#9CA3AF", anchor="ra")
    dashed(y0 + 68)

    rows = [("巨无霸套餐 ×1", "29.5"), ("麦辣鸡腿堡 ×1", "22.0"), ("中杯可乐 ×2", "19.0")]
    yy = y0 + 92
    for name, amt in rows:
        d.text((x0 + 26, yy), name, font=small, fill="#475569")
        d.text((x1 - 26, yy), "¥" + amt, font=small, fill=INK, anchor="ra")
        yy += 42
    dashed(yy + 4)
    d.text((x0 + 26, yy + 24), "优惠", font=small, fill="#B7791F")
    d.text((x1 - 26, yy + 24), "−¥6.0", font=small, fill="#B7791F", anchor="ra")

    d.text((x0 + 26, yy + 84), "实付", font=cjk, fill=INK)
    d.text((x1 - 26, yy + 78), "¥64.5", font=f(MONO_BOLD, 38), fill=INK, anchor="ra")

    dashed(yy + 142)
    d.text((x0 + 26, yy + 166), "官方 calculate-price 真实试算", font=small, fill="#9CA3AF")

    # ---- 左下角：不要把 GitHub 用户名写上去，卡片是给转发用的 ----
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print("已生成 %s" % OUT)
    print("尺寸 %dx%d，%.1f KB" % (img.width, img.height, os.path.getsize(OUT) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
