"""把一段终端输出渲染成 PNG（开发期生成 README 配图用，不是运行时依赖）。

为什么单独写这个：README 里放一段文字代码块，读者要自己在脑子里排版；
放一张终端截图，读者一秒就能看懂这是什么工具。

用 Windows 自带的 **NSimSun**（等宽且中文占两格），所以表格框线能对齐。

用法::

    python tools/render_terminal_png.py input.txt assets/terminal-demo.png

依赖 Pillow（仅本脚本需要，不是项目运行依赖）::

    pip install pillow
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "C:/Windows/Fonts/simsun.ttc"
FONT_INDEX = 1  # 1 = NSimSun（等宽），0 = SimSun（比例）
FONT_SIZE = 19
LINE_H = 28
PAD_X = 26
PAD_TOP = 62
PAD_BOTTOM = 26

BG = (18, 16, 15)
TITLE_BG = (34, 31, 29)
FG = (233, 229, 224)
DIM = (118, 112, 106)
RED = (255, 92, 82)
YELLOW = (255, 199, 44)
GREEN = (108, 214, 152)
CYAN = (116, 205, 232)

BOX_CHARS = set("┌┐└┘├┤┬┴┼─│")
CLR = "\x00"  # 占位，避免与正文冲突


def _strip_emoji(text: str) -> str:
    """去掉 emoji。

    NSimSun 没有 emoji 字形，留着不会画出来，但 `_cells()` 仍会给它算 2 格，
    结果是文字里凭空多出一段空白。不如直接删掉。
    """
    return "".join(
        ch for ch in text if ord(ch) < 0x1F000 and ch not in (0xFE0F, 0x200D)
    )


def _palette_for(line: str) -> tuple:
    """按内容给出 (默认色)，让关键信息跳出来。"""
    s = line.strip()
    if s.startswith("◆"):
        return RED
    if s.startswith("◇"):
        return DIM
    if s.startswith("麦门圆桌") or "麦门决议" in s:
        return YELLOW
    if "应付" in s or "主 持 人 裁 决" in s:
        return RED
    if s.startswith("真实试算") or "第 1 轮" in s or "第 2 轮" in s:
        return CYAN
    if s.startswith("决策依据") or s.startswith("再点") or s.startswith("💡"):
        return YELLOW
    if all(ch in BOX_CHARS or ch == " " for ch in s) and s:
        return DIM
    return FG


_AMOUNT_RE = re.compile(r"¥\s?\d+(?:\.\d+)?")


def draw_panel(
    lines: list[str],
    *,
    title: str = "mcd-roundtable",
    max_cells: int | None = None,
    n_lines: int | None = None,
) -> Image.Image:
    """把若干行文本画成一张终端风格图。

    ``max_cells`` / ``n_lines`` 用于**锁定画布尺寸** —— 生成动画（GIF）时
    每一帧必须同宽同高，否则播放时会跳。
    """
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE, index=FONT_INDEX)
    # NSimSun 的 ASCII 步进 = FONT_SIZE * 0.5，中文 = FONT_SIZE；这里实测取整
    cell = font.getlength("0")

    if max_cells is None:
        max_cells = max((_cells(line) for line in lines), default=60)
    rows = max(n_lines or 0, len(lines))
    width = int(PAD_X * 2 + cell * max_cells) + 8
    height = PAD_TOP + LINE_H * rows + PAD_BOTTOM

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # 标题栏
    d.rectangle([0, 0, width, 40], fill=TITLE_BG)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = 20 + i * 20
        d.ellipse([cx - 6, 14, cx + 6, 26], fill=color)
    d.text((PAD_X + 54, 12), title, font=font, fill=DIM)

    y = PAD_TOP
    for line in lines:
        base = _palette_for(line)
        x = PAD_X
        for chunk, is_amount in _split_amounts(line):
            color = RED if is_amount else base
            d.text((x, y), chunk, font=(font if is_amount else font), fill=color)
            x += cell * _cells(chunk)
        y += LINE_H
    return img


def render(
    text: str,
    out_path: str | Path,
    *,
    title: str = "mcd-roundtable",
    start: int | None = None,
    end: int | None = None,
) -> Path:
    """start / end 是 1-based 的**闭区间**行号，用于只截取输出的某一段。

    完整输出往往是"又长又窄"的，直接放进 README 会很难看；
    截出「质询」和「决议」两段分别成图，观感好得多。
    """
    all_lines = text.replace("\t", "    ").splitlines()
    lo = (start - 1) if start else 0
    hi = end if end else len(all_lines)
    lines = [_strip_emoji(ln) for ln in all_lines[lo:hi]]

    img = draw_panel(lines, title=title)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target


def _cells(text: str) -> int:
    """按字符格数计算宽度，口径与 rich 保持一致，否则框线会漂移。

    * 东亚宽度 W / F（中日韩、全角标点、emoji）→ 2 格
    * 东亚宽度 A（`¥` `·` `—` `◆` `─` `│` 等）→ rich 默认按 **1 格** 渲染
    * 其余 → 1 格

    上一版把制表符 `─│┌┐└┘` 也当成 2 格，结果整张图右侧全部错位。
    """
    total = 0
    for ch in text:
        if ch == "\t":
            total += 4
            continue
        if ord(ch) >= 0x1F000:  # emoji 及其他星平面符号
            total += 2
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _split_amounts(line: str):
    """把金额切出来单独上红色。"""
    pos = 0
    for m in _AMOUNT_RE.finditer(line):
        if m.start() > pos:
            yield line[pos : m.start()], False
        yield m.group(), True
        pos = m.end()
    if pos < len(line):
        yield line[pos:], False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    src, dst = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else "mcd-roundtable"
    start = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
    end = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
    written = render(
        Path(src).read_text(encoding="utf-8"), dst, title=title, start=start, end=end
    )
    print(f"written: {written}")
