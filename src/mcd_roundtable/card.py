"""麦门决议卡：把一次会议的结论导成一张**可以截图、可以发出去**的 SVG。

为什么单独做这一步？

命令行输出有一个天然缺陷：**它传播不出去**。
截图带终端边框和滚动条很难看，抄成文字又丢掉了所有排版。

所以这里直接生成一张自包含的 SVG（零依赖、单文件、可缩放）：
用户跑完一次，就顺手得到一张可以直接贴到朋友圈、Issue、小红书的决议卡。
它是这个项目"能被看见"的关键一环，也是别人愿意给它点 Star 的理由之一。

设计原则：
* 不使用任何外部字体文件与图片，**纯形状 + 系统字体**，离线可渲染；
* CJK 用等宽度量对齐，中日韩字符按两个西文字符宽度估算；
* 金额一律取自官方 `calculate-price`，卡片底部显式标注价格来源。
"""

from __future__ import annotations

from html import escape
from pathlib import Path
import zlib

WIDTH = 760
PAD = 44

BRAND_RED = "#DA291C"
BRAND_YELLOW = "#FFC72C"
INK = "#1B1B1B"
MUTED = "#6B6B6B"
FAINT = "#B5B5B5"
HAIRLINE = "#E4E4E4"
PAPER = "#FFFFFF"
CANVAS = "#F4F1EC"

# 终端里的 emoji 在 SVG 里可能没有字形，统一换成文字标签
FONT = "'PingFang SC','Microsoft YaHei','Hiragino Sans GB','Noto Sans CJK SC',sans-serif"
MONO = "'SF Mono','Consolas','Menlo',monospace"


def _w(text: str, size: float) -> float:
    """估算字符串宽度：CJK 记 2，其余记 1（单位：em 的百分比）。"""
    total = 0.0
    for ch in text:
        if (
            "\u4e00" <= ch <= "\u9fff"
            or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef"
            or "\u3040" <= ch <= "\u30ff"
        ):
            total += 2.0
        elif ch in "¥￥":
            total += 1.0
        else:
            total += 1.0
    return total * size * 0.5


class _Canvas:
    """按行累积 SVG，最后统一算高度。"""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.y = 0.0

    def text(self, x: float, y: float, content: str, *, size: float = 15,
             fill: str = INK, weight: str = "400", family: str = FONT,
             anchor: str = "start", spacing: str = "") -> None:
        extra = f' letter-spacing="{spacing}"' if spacing else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family={family!r} font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{extra}>'
            f"{escape(content)}</text>"
        )

    def right_text(self, x: float, y: float, content: str, **kw) -> None:
        kw.setdefault("anchor", "end")
        self.text(x, y, content, **kw)

    def line(self, x1: float, y: float, x2: float, *, color: str = HAIRLINE, width: float = 1.0,
             dash: str = "") -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="{width}"{d}/>'
        )

    def rect(self, x: float, y: float, w: float, h: float, *, fill: str,
             rx: float = 0.0, opacity: float = 1.0) -> None:
        op = f' opacity="{opacity}"' if opacity != 1.0 else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}"{op}/>'
        )


def render_card_svg(result, path: str | Path) -> Path:
    """把一次会议结果写成 SVG 决议卡，返回实际写入路径。"""
    c = result.constraint
    quote = result.verdict.quote
    card = _Canvas()

    left = PAD
    right = WIDTH - PAD
    content_w = right - left

    # ---------------- 顶部品牌带 ----------------
    header_h = 104.0
    card.rect(0, 0, WIDTH, header_h, fill=BRAND_RED)
    card.rect(0, header_h, WIDTH, 7, fill=BRAND_YELLOW)
    card.text(left, 50, "麦门圆桌", size=30, fill=PAPER, weight="700", spacing="1")
    card.text(left, 76, "MCD ROUNDTABLE  ·  AI 议会点单", size=12.5, fill="#FFD9D5",
              weight="500", spacing="1.4")
    card.right_text(right, 50, "决 议 书", size=15, fill=BRAND_YELLOW, weight="700", spacing="3")
    # 「决议书编号」必须是**同一句话永远同一个号**。
    # 原来用内置 hash()，而 str 的哈希受 PYTHONHASHSEED 随机化影响：
    # 同一句「中午想吃饱」昨天是 NO. 4821、今天是 NO. 3097，
    # 编号一旦不稳定，"文书"感就成了穿帮。
    # crc32 跨进程、跨平台都是确定值。
    _doc_no = zlib.crc32((c.raw_query or "mcd").encode("utf-8")) % 9000 + 1000
    card.right_text(right, 76, f"NO. {_doc_no:04d}",
                    size=12, fill="#FFD9D5", family=MONO)

    y = header_h + 52

    # ---------------- 议题 ----------------
    topic = c.raw_query or "（未指定需求）"
    card.text(left, y, "议 题", size=11.5, fill=FAINT, weight="600", spacing="2")
    y += 24
    for chunk in _wrap(topic, content_w, 19):
        card.text(left, y, chunk, size=19, weight="700")
        y += 27
    y += 10

    # ---------------- 门店 / 人数 / 数据来源 ----------------
    meta_bits = []
    if result.data.store_name:
        meta_bits.append(result.data.store_name)
    meta_bits.append(result.verdict.headcount_label or f"{c.people} 人")
    meta_bits.append("真实 MCP 接口" if not result.data.is_demo else "离线演示数据")
    card.text(left, y, "  ·  ".join(meta_bits), size=12, fill=MUTED)
    y += 16
    if c.keywords or c.budget_total or c.budget_per_person or c.max_kcal_per_person:
        card.text(left, y, _constraint_text(c), size=12, fill=FAINT)
        y += 16

    y += 12
    card.line(left, y, right, color=HAIRLINE)
    y += 30

    # ---------------- 明细 ----------------
    card.text(left, y, "决 议 明 细", size=11.5, fill=FAINT, weight="600", spacing="2")
    y += 26

    for line in quote.lines:
        name = line.name if _w(line.name, 15) < content_w - 150 else _clip(line.name, 30)
        card.text(left, y, name, size=15, weight="600")
        card.right_text(right, y, f"¥{line.amount:.1f}", size=15, family=MONO, weight="600")
        card.text(right - 78, y, f"×{line.qty}", size=12.5, fill=FAINT, family=MONO,
                  anchor="end")
        y += 25

    y += 6
    card.line(left, y, right, color=HAIRLINE, dash="4 4")
    y += 26

    for label, value, color in _money_rows(quote):
        card.text(left + 8, y, label, size=13, fill=MUTED)
        card.right_text(right, y, value, size=13.5, family=MONO, fill=color)
        y += 22

    y += 10
    card.rect(left, y - 4, content_w, 62, fill="#FFF6F5", rx=10)
    card.text(left + 18, y + 24, "应  付", size=13, fill=MUTED, weight="600")
    card.text(left + 74, y + 30, f"¥{quote.payable:.1f}", size=27, fill=BRAND_RED, weight="700",
              family=MONO)
    if quote.people > 1:
        card.right_text(right - 18, y + 30, f"人均 ¥{quote.per_person:.1f}", size=15,
                        weight="700", family=MONO)
    y += 84

    # ---------------- 凑单提示（只在真的省钱时才显示） ----------------
    net = round(quote.next_saving - quote.discount - quote.next_gap, 2)
    if quote.next_gap > 0 and net > 0.01:
        card.rect(left, y - 6, content_w, 44, fill="#FFFBEB", rx=8)
        card.rect(left, y - 6, 4, 44, fill=BRAND_YELLOW, rx=2)
        card.text(left + 20, y + 21,
                  f"再点 ¥{quote.next_gap:.1f} 跨到下一档优惠，实付净降 ¥{net:.1f}",
                  size=13.5, fill="#8A6A00", weight="600")
        y += 60

    # ---------------- 一句话 ----------------
    if result.verdict.summary:
        card.text(left, y, "一 句 话", size=11.5, fill=FAINT, weight="600", spacing="2")
        y += 24
        for chunk in _wrap(result.verdict.summary, content_w, 15):
            card.text(left, y, chunk, size=15, weight="600", fill=INK)
            y += 23
        y += 8

    if result.verdict.why_not_cheapest:
        for chunk in _wrap(result.verdict.why_not_cheapest, content_w, 12.5):
            card.text(left, y, chunk, size=12.5, fill=MUTED)
            y += 19
        y += 8

    # ---------------- 委员 ----------------
    if result.verdict.adopted:
        y += 6
        card.line(left, y, right, color=HAIRLINE, dash="4 4")
        y += 26
        card.text(left, y, "谁 赢 了", size=11.5, fill=FAINT, weight="600", spacing="2")
        y += 24
        for chunk in _wrap(" · ".join(result.verdict.adopted), content_w, 13):
            card.text(left, y, chunk, size=13, fill=INK)
            y += 20
        if result.verdict.rejected:
            y += 4
            for chunk in _wrap("未采纳：" + "、".join(result.verdict.rejected), content_w, 12):
                card.text(left, y, chunk, size=12, fill=FAINT)
                y += 18

    # ---------------- 页脚 ----------------
    y += 26
    card.line(left, y, right, color=HAIRLINE)
    y += 24
    src = {
        "calculate-price": "金额来自麦当劳官方 calculate-price 实时试算",
        "local-fallback": "⚠ 官方试算不可用，以下为本地推算价",
        "demo-data": "演示数据，非实时价格",
    }.get(quote.source, quote.source)
    card.text(left, y, src, size=11.5, fill=FAINT)
    y += 18
    card.text(left, y, "麦门圆桌 · 本项目不参与任何支付环节，下单请前往麦当劳官方渠道",
              size=11.5, fill=FAINT)
    y += 34

    # 底部留 24px 的"桌面"色带，模拟一张纸放在桌上的观感
    total_h = y + 4

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{total_h:.0f}" '
        f'viewBox="0 0 {WIDTH} {total_h:.0f}" role="img" '
        f'aria-label="麦门圆桌决议卡">',
        f'<rect width="{WIDTH}" height="{total_h:.0f}" fill="{CANVAS}"/>',
        f'<rect x="0" y="0" width="{WIDTH}" height="{total_h - 24:.0f}" fill="{PAPER}"/>',
        *card.parts,
        "</svg>",
    ]

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(svg), encoding="utf-8")
    return target


def _money_rows(quote) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = [("小计", f"¥{quote.subtotal:.1f}", INK)]
    if quote.discount:
        rows.append(("优惠券抵扣", f"−¥{quote.discount:.1f}", BRAND_RED))
    if quote.delivery_fee:
        rows.append(("配送费", f"¥{quote.delivery_fee:.1f}", INK))
    if quote.packing_fee:
        rows.append(("打包费", f"¥{quote.packing_fee:.1f}", INK))
    return rows


def _constraint_text(c) -> str:
    bits: list[str] = []
    if c.budget_total is not None:
        bits.append(f"总预算 ≤¥{c.budget_total:.0f}")
    elif c.budget_per_person is not None:
        bits.append(f"人均 ≤¥{c.budget_per_person:.0f}")
    if c.max_kcal_per_person:
        bits.append(f"≤{c.max_kcal_per_person:.0f}kcal/人")
    if c.min_protein_per_person:
        bits.append(f"蛋白 ≥{c.min_protein_per_person:.0f}g/人")
    if c.keywords:
        bits.append(" · ".join(c.keywords))
    if not c.takeout:
        bits.append("外送")
    return "  ·  ".join(bits)


def _wrap(text: str, max_width: float, size: float) -> list[str]:
    """按估算宽度折行；CJK 逐字断，西文按词断。"""
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        trial = current + token
        if current and _w(trial, size) > max_width:
            lines.append(current.rstrip())
            current = token.lstrip() if token.strip() else ""
        else:
            current = trial
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        elif ch == " ":
            buf += ch
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
