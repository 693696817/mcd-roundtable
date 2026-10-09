"""终端渲染。

这一段是项目的"门面"：GitHub 上能不能被点开、被截图、被转发，
很大程度上取决于这里的视觉效果。

设计目标只有一个：**让人愿意截图。**
所以最终结论被排成一张小票（麦门决议），有编号、有明细、有金额、
有一句可以直接念出来的话。
"""

from __future__ import annotations

import unicodedata

from rich.console import Console, Group
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .council import CouncilResult, headcount_label

BRAND_RED = "bright_red"
BRAND_YELLOW = "yellow"
MUTED = "grey62"
WIDTH = 76

ROLE_STYLE = {
    "saver": "bright_yellow",
    "macro": "bright_cyan",
    "purist": "bright_red",
    "light": "bright_green",
    "novelty": "magenta",
}

_ROLE_BG = {
    "saver": "dark_orange3",
    "macro": "dark_cyan",
    "purist": "dark_red",
    "light": "dark_green",
    "novelty": "purple4",
}


def make_console(no_color: bool = False, width: int = WIDTH) -> Console:
    """固定宽度，保证重定向到文件或截图时排版稳定。"""
    return Console(highlight=False, no_color=no_color, soft_wrap=False, width=width)


def render(result: CouncilResult, console: Console) -> None:
    c = result.constraint
    quote = result.verdict.quote

    console.print()
    console.print(
        Text.assemble(
            ("  🍟 ", ""),
            ("麦门圆桌", f"bold {BRAND_RED}"),
            ("   一句话，五个 AI 吵出一个能下单的方案", MUTED),
        )
    )
    console.print()

    _render_meta(result, console)
    console.print()

    _render_round1(result, console)
    if result.rebuttals:
        _render_round2(result, console)
    _render_trials(result, console)
    _render_receipt(result, console)

    if result.warnings:
        console.print()
        console.print(
            Text.assemble(
                ("  ⚠ ", "yellow"),
                ("；".join(result.warnings[:3]), MUTED),
            )
        )
    console.print()


# --------------------------------------------------------------------------- #
# 会议信息
# --------------------------------------------------------------------------- #

def _render_meta(result: CouncilResult, console: Console) -> None:
    c = result.constraint
    data = result.data
    meta = Table.grid(padding=(0, 2), expand=False)
    meta.add_column(style=MUTED, justify="right")
    meta.add_column()

    meta.add_row("议题", Text(c.raw_query or "（未指定，默认：随便来一份）", style="bold"))
    meta.add_row("会议规模", Text(_constraint_line(c)))
    meta.add_row(
        "就餐门店",
        Text(data.store_name or "（未定位，请用 --city 与 --keyword 指定）"),
    )
    if data.coupons:
        names = " / ".join(x.name for x in data.coupons[:3])
        more = f" …等 {len(data.coupons)} 张" if len(data.coupons) > 3 else ""
        meta.add_row("可用券", Text(f"{len(data.coupons)} 张 · {names}{more}"))
    pts = _num(_pick(data.account, "availablePoint", "availablePoints"))
    if pts:
        exp = _num(_pick(data.account, "expiringPoint", "expiringPoints"))
        tail = f"，{exp:.0f} 分即将过期" if exp else ""
        meta.add_row("积分", Text(f"{pts:.0f} 分{tail}"))
    meta.add_row("数据来源", Text(_mode_label(result)))
    console.print(meta)


def _mode_label(result: CouncilResult) -> str:
    if result.data.is_demo:
        return "离线演示数据（示例数据，非实时价格）"
    return f"麦当劳 MCP 实时接口（大脑：{result.brain_label}）"


# --------------------------------------------------------------------------- #
# 第 1 轮
# --------------------------------------------------------------------------- #

def _render_round1(result: CouncilResult, console: Console) -> None:
    console.print(Rule("[bold]第 1 轮 · 各自提案[/bold]", style=MUTED))
    console.print()
    for proposal in result.proposals:
        style = ROLE_STYLE.get(proposal.role_key, "white")
        head = Text.assemble(
            (f" {proposal.role_name} ", f"bold white on {_ROLE_BG.get(proposal.role_key, 'grey37')}"),
            ("  ", ""),
            (_stance_of(proposal.role_key), MUTED),
            ("   ", ""),
            (f"¥{proposal.amount:.1f}", f"bold {BRAND_YELLOW}"),
        )
        body = Text()
        body.append("\n" + _para(proposal.pitch))
        if proposal.rationale:
            body.append("\n" + _para(proposal.rationale), style=MUTED)
        console.print(Panel(Group(head, body), border_style=style, padding=(0, 1)))


# --------------------------------------------------------------------------- #
# 第 2 轮
# --------------------------------------------------------------------------- #

def _render_round2(result: CouncilResult, console: Console) -> None:
    console.print(Rule("[bold]第 2 轮 · 交叉质询[/bold]", style=MUTED))
    console.print()
    for rb in result.rebuttals:
        style = ROLE_STYLE.get(rb.role_key, "white")
        text = Text.assemble(
            (f" {rb.role_name} ", f"bold white on {_ROLE_BG.get(rb.role_key, 'grey37')}"),
        )
        if rb.target_name:
            text.append("  →  ", style=MUTED)
            text.append(rb.target_name, style="bold")
        text.append("\n" + _para(rb.text))
        console.print(Panel(text, border_style=style, padding=(0, 1)))


# --------------------------------------------------------------------------- #
# 真实试算明细
# --------------------------------------------------------------------------- #

def _render_trials(result: CouncilResult, console: Console) -> None:
    trials = result.verdict.trials
    if not trials:
        return

    console.print(Rule(f"[bold]真实试算 · 共 {len(trials)} 组[/bold]", style=MUTED))
    console.print()
    # padding 收到 (0,1)：6 列 × 2 列省出 12 列，全给"组合"列，
    # 否则形如「双层吉士汉堡、可口可乐（中） ×3」的长组合名会被挤到第二行
    table = Table(show_header=True, header_style=f"bold {BRAND_YELLOW}", box=None, padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("组合", min_width=34, no_wrap=True, overflow="ellipsis")
    table.add_column("原价", justify="right")
    table.add_column("优惠", justify="right")
    table.add_column("实付", justify="right")
    table.add_column("人均", justify="right")

    for t in trials:
        mark = "[bold red]◆[/bold red]" if t.adopted else "[grey46]◇[/grey46]"
        # 品名来自官方菜单，里面出现 `[` `]` 完全可能（规格写法、进口商品名）。
        # rich 会把它们当标记解析：`巨无霸[/bold]套餐` 直接抛 MarkupError 崩掉整屏，
        # `巨无霸[red]套餐` 更阴——不报错，但悄悄把方括号内容吃掉换成颜色。
        # 凡是"外部字符串进标记模板"的地方都必须先转义。
        label = markup_escape(t.label)
        name = f"[bold]{label}[/bold]" if t.adopted else f"[grey62]{label}[/grey62]"
        discount = f"[{BRAND_RED}]−¥{t.discount:.1f}[/{BRAND_RED}]" if t.discount else "[grey46]—[/grey46]"
        payable = f"[bold {BRAND_RED}]¥{t.payable:.1f}[/bold {BRAND_RED}]" if t.adopted else f"[grey62]¥{t.payable:.1f}[/grey62]"
        table.add_row(mark, name, f"¥{t.original:.1f}", discount, payable, f"¥{t.per_person:.1f}")
    console.print(table)

    if any(not t.adopted for t in trials):
        console.print()
        console.print(
            Text.assemble(
                ("  金额均由麦当劳官方 calculate-price 实时试算得出，非本工具估算。", MUTED),
            )
        )


# --------------------------------------------------------------------------- #
# 麦门决议（小票）
# --------------------------------------------------------------------------- #

def _render_receipt(result: CouncilResult, console: Console) -> None:
    quote = result.verdict.quote
    c = result.constraint
    console.print()
    console.print(Rule(f"[bold {BRAND_RED}]主 持 人 裁 决[/bold {BRAND_RED}]", style=BRAND_RED))
    console.print()

    rows: list = []
    rows.append(
        Text.assemble(
            ("  麦门决议", f"bold {BRAND_RED}"),
            (f"   MCC-ROUNDTABLE · {headcount_label(c)}", MUTED),
        )
    )
    rows.append(Text(""))
    rows.append(
        Text.assemble(
            ("  议  题   ", MUTED),
            (c.raw_query or "（未指定）", ""),
        )
    )
    if result.data.store_name:
        rows.append(Text.assemble(("  门  店   ", MUTED), (result.data.store_name, "")))
    rows.append(Text(""))
    rows.append(Text("  " + "─" * 70, style="grey37"))

    for line in quote.lines:
        name = line.name
        qty = f"×{line.qty}"
        amount = f"¥{line.amount:.1f}"
        # 行宽对齐到 72 列，与下方分隔线一致，保证小票左右边缘整齐
        pad = max(1, 68 - _width(name) - _width(qty) - _width(amount))
        rows.append(
            Text.assemble(
                ("  ", ""),
                (name, "bold"),
                (" " * pad, ""),
                (qty, MUTED),
                ("  ", ""),
                (amount, "bold"),
            )
        )

    rows.append(Text("  " + "─" * 70, style="grey37"))

    money = Table.grid(padding=(0, 1))
    money.add_column(justify="right", style=MUTED, min_width=12)
    money.add_column(justify="right", min_width=10)
    money.add_row("小  计", f"¥{quote.subtotal:.1f}")
    if quote.discount:
        money.add_row("优惠券抵扣", f"[{BRAND_RED}]−¥{quote.discount:.1f}[/{BRAND_RED}]")
    if quote.delivery_fee:
        money.add_row("配  送  费", f"¥{quote.delivery_fee:.1f}")
    if quote.packing_fee:
        money.add_row("打  包  费", f"¥{quote.packing_fee:.1f}")
    rows.append(money)

    rows.append(Text(""))
    rows.append(
        Text.assemble(
            ("  应        付   ", MUTED),
            (f"¥{quote.payable:.2f}", f"bold {BRAND_RED}"),
            ("      人均   ", MUTED),
            (f"¥{quote.per_person:.2f}", "bold"),
        )
    )
    rows.append(
        Text.assemble(
            ("  价格来源       ", MUTED),
            (_source_label(quote.source), "grey62"),
        )
    )

    if quote.next_gap > 0 and quote.next_saving > 0:
        # 真正该看的不是"下一档优惠多少"，而是**净收益**：
        #   多加的 ¥next_gap 换回 ¥(next_saving − 当前已享) 的优惠
        # 只有当净收益为正时才值得提示，否则就是劝人多花钱。
        net = round(quote.next_saving - quote.discount - quote.next_gap, 2)
        if net > 0.01:
            rows.append(Text(""))
            rows.append(
                Text.assemble(
                    ("  💡 再点 ", MUTED),
                    (f"¥{quote.next_gap:.1f}", f"bold {BRAND_YELLOW}"),
                    (" 跨到下一档优惠，实付净降 ", MUTED),
                    (f"¥{net:.1f}", f"bold {BRAND_YELLOW}"),
                    ("（官方试算口径）", MUTED),
                )
            )

    rows.append(Text(""))
    if result.verdict.adopted:
        for index, chunk in enumerate(_fold(" · ".join(result.verdict.adopted), 60)):
            label = "  采  纳   " if index == 0 else " " * 12
            rows.append(Text.assemble((label, MUTED), (chunk, "")))
    if result.verdict.rejected:
        for index, chunk in enumerate(_fold("、".join(result.verdict.rejected), 60)):
            label = "  未采纳    " if index == 0 else " " * 12
            rows.append(Text.assemble((label, MUTED), (chunk, MUTED)))
    for index, chunk in enumerate(_fold(result.verdict.summary, 60)):
        label = "  一句话    " if index == 0 else " " * 12
        rows.append(Text.assemble((label, MUTED), (chunk, "bold")))
    if result.verdict.why_not_cheapest:
        rows.append(Text(""))
        # 自己折行：交给 rich 折的话，续行会丢掉缩进，小票右边缘看着就歪了
        for index, chunk in enumerate(_fold(result.verdict.why_not_cheapest, 60)):
            label = "  决策依据  " if index == 0 else " " * 12
            rows.append(Text.assemble((label, MUTED), (chunk, "grey62")))

    rows.append(Text(""))
    rows.append(Text("  " + "─" * 70, style="grey37"))
    rows.append(
        Text.assemble(
            ("  本决议由「麦门圆桌」生成，价格与可售性以麦当劳 APP / 小程序为准。", "grey46"),
        )
    )

    panel = Panel(
        Group(*rows),
        border_style=BRAND_RED,
        padding=(1, 1),
        width=WIDTH,
        expand=False,
    )
    console.print(panel)

    if result.topped_up:
        console.print()
        console.print(Text.assemble(("  ", ""), (result.topped_up.strip(), f"bold {BRAND_YELLOW}")))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _para(text: str, *, indent: int = 4, limit: int = 68) -> str:
    """把一段话折成带**续行缩进**的多行文本。

    交给 rich 自动折行的话，续行会顶到面板最左边，看起来像断掉的碎片。
    这里自己折，保证每一行都带同样的缩进。
    """
    pad = " " * indent
    return "\n".join(pad + chunk for chunk in _fold(text, limit))


# 行首禁则：这些标点不允许出现在一行的开头（中文排版规矩）。
_NO_LINE_START = "，。、；：！？）》」』】…～%‰℃.,;:!?)]}"

_HANG_ROOM = 2  # 留给"悬挂标点"的列数


def _fold(text: str, limit: int) -> list[str]:
    """按显示列宽折行（CJK 记 2 列），``limit`` 是**硬上限**。

    以"词"为单位断行：中日韩逐字可断，西文（含数字、小数、单位）
    整块搬运 —— 否则会出现 "60kcal" 被拆成 "6" / "0kcal" 这种断法。

    为了满足行首禁则（不能让 "。" 起一行），正常折行会**提前 2 列**断开，
    把这 2 列留给标点悬挂。这样任何一行都不会超出 ``limit``，
    也就不会被外层（rich）二次折行 —— rich 二次折行会把缩进吃掉。
    """
    fold_at = max(1, limit - _HANG_ROOM)
    out: list[str] = []
    current = ""
    for token in _tokens(text):
        if current and _width(current) + _width(token) > fold_at:
            # 标点若能塞进上一行（且不超硬上限），就悬挂上去
            if token[0] in _NO_LINE_START and _width(current) + _width(token) <= limit:
                current += token
                continue
            out.append(current.rstrip())
            current = token.lstrip()
        else:
            current += token
    if current.strip():
        out.append(current.rstrip())
    return out or [""]


def _tokens(text: str):
    """切成可断行的最小单元：中日韩 / 全角标点逐字可断，西文与数字整块搬运。"""
    buf = ""
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            if buf:
                yield buf
                buf = ""
            yield ch
        elif ch == " ":
            buf += ch
            yield buf
            buf = ""
        else:
            buf += ch
    if buf:
        yield buf


def _width(text: str) -> int:
    """中日韩字符按 2 列算，用于对齐小票。"""
    total = 0
    for ch in text:
        total += 2 if ("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef") else 1
    return total


def _pick(mapping: dict, *keys):
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) not in (None, ""):
            return mapping[key]
    return None


def _num(value) -> float:
    """真机返回的数字**经常是字符串**（`availablePoint: "598.2"`），必须容错。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stance_of(key: str) -> str:
    from .roles import ROLES

    role = ROLES.get(key)
    return role.stance if role else ""


def _constraint_line(c) -> str:
    bits = []
    if c.people > 1:
        bits.append(f"{c.people} 人")
    if c.budget_total is not None:
        bits.append(f"总预算 ≤¥{c.budget_total:.0f}")
    elif c.budget_per_person is not None:
        bits.append(f"人均 ≤¥{c.budget_per_person:.0f}")
    bits.extend(c.keywords)
    if c.avoid:
        bits.append("避开 " + "/".join(c.avoid))
    if c.max_kcal_per_person:
        bits.append(f"≤{c.max_kcal_per_person:.0f}kcal/人")
    if c.min_protein_per_person:
        bits.append(f"蛋白 ≥{c.min_protein_per_person:.0f}g/人")
    if not c.takeout:
        bits.append("外送")
    return " · ".join(bits) if bits else "未识别到硬性约束"


def _source_label(source: str) -> str:
    return {
        "calculate-price": "麦当劳官方 calculate-price 实时试算",
        "demo-data": "演示数据（非实时价格）",
        "local-fallback": "⚠ 本地兜底计算，未取到官方试算",
    }.get(source, source)
