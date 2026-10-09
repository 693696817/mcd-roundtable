"""把一次完整运行录成**滚动播放的 GIF**（开发期生成 README 头图用）。

静态截图只能说明"结果长这样"，GIF 能说清"它是怎么一步步吵出来的"：
先是五位委员各自提案，然后是交叉质询，接着官方试算把金额摆出来，
最后小票落地。README 第一屏放一张会动的图，读者不用读完文档就知道这是什么。

实现上就是**固定视口的滚动重现**：每一帧只画窗口内的若干行，窗口逐帧下移，
所以帧与帧之间同宽同高，播放时不会跳。

用法::

    python tools/render_demo_gif.py .capture-live.txt assets/demo.gif "..." [view] [step]

依赖 Pillow（仅本脚本需要，不是项目运行依赖）::

    pip install pillow
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from render_terminal_png import _cells, draw_panel

VIEW_DEFAULT = 30  # 视口行数（模拟一个终端窗口）
STEP_DEFAULT = 4  # 每帧向下滚动几行
REVEAL_MS = 90  # 滚动帧的停留时长
HOLD_MS = 3200  # 最后一帧停留时长


def _strip_emoji(text: str) -> str:
    """NSimSun 没有 emoji 字形，留着会渲染成豆腐块，直接去掉。"""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp >= 0x1F000 or cp in (0xFE0F, 0x200D):
            continue
        out.append(ch)
    return "".join(out)


def render_gif(
    text: str,
    out_path: str | Path,
    *,
    title: str = "mcd-roundtable",
    view: int = VIEW_DEFAULT,
    step: int = STEP_DEFAULT,
) -> Path:
    lines = [_strip_emoji(ln).replace("\t", "    ") for ln in text.splitlines()]
    if not lines:
        raise SystemExit("输入是空的")

    # 全局锁定画布：取所有行的最大宽度，避免逐帧宽度变化导致播放抖动
    max_cells = max((_cells(ln) for ln in lines), default=60)

    # 帧序列：窗口 [start, start+view)，start 从 0 递增到末行
    frames: list[Image.Image] = []
    total = len(lines)
    starts = list(range(0, max(1, total - view + 1), step))
    if not starts or starts[-1] != max(0, total - view):
        starts.append(max(0, total - view))

    for s in starts:
        window = lines[s : s + view]
        window = window + [""] * (view - len(window))  # 补空行，保证等高等宽
        frames.append(draw_panel(window, title=title, max_cells=max_cells, n_lines=view))

    if len(frames) == 1:
        frames.append(frames[0].copy())

    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=48) for f in frames]

    durations = [REVEAL_MS] * (len(pal) - 1) + [HOLD_MS]
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(
        target,
        save_all=True,
        append_images=pal[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return target


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    src, dst = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else "mcd-roundtable"
    view = int(sys.argv[4]) if len(sys.argv) > 4 else VIEW_DEFAULT
    step = int(sys.argv[5]) if len(sys.argv) > 5 else STEP_DEFAULT
    written = render_gif(
        Path(src).read_text(encoding="utf-8"), dst, title=title, view=view, step=step
    )
    size_kb = Path(written).stat().st_size / 1024
    print(f"written: {written}  ({size_kb:.0f} KB)")
