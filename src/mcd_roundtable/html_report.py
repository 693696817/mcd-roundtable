"""麦门圆桌 · 交互式决议网页（Single-File HTML）。

为什么还要一个网页版？

终端输出和 SVG 决议卡各有短板：
* 终端 —— 有边框和滚动条，截图不好看；
* SVG  —— 是一张**静态图**，没法把"五个委员怎么吵的"过程呈现出来。

这个 HTML 是给**传播**用的：单文件、离线可看、双击即开，
打开时五位委员会依次"冒出来"发言，最后落一张带锯齿边的麦当劳长小票。
发到群里 / 小红书 / Issue 里，别人不用装任何东西就能看懂这个项目在干什么。

三个刻意的工程选择：

1. **零 JavaScript**。动画全部用 CSS `animation-delay` 做，
   页面在禁用 JS 的环境下依然完整可读 —— 内容在服务端就渲染好了。
2. **全部文本走 `html.escape`**。菜名来自 MCP、query 来自用户，
   直接拼进 HTML 既会断版（``&`` ``<``）也可能被注入。
3. **金额与营养只在有真实数据时才显示**。营养表查不到的菜绝不显示 0，
   与 CLI / SVG 卡的口径保持一致。
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

# 与 render.py / card.py 保持同一套品牌色
BRAND_RED = "#DA291C"
BRAND_YELLOW = "#FFC72C"
INK = "#1F1D1B"
MUTED = "#6B7280"
FAINT = "#9CA3AF"
HAIRLINE = "#E5E7EB"
PAPER = "#FFFFFF"
CANVAS = "#F4F1EC"

# 委员配色：徽章底色 + 主色（与 roles.py 的五个 key 对应）
ROLE_STYLE: dict[str, tuple[str, str, str]] = {
    "saver": ("💰", "#B7791F", "#FFF8E1"),
    "macro": ("🏋️", "#0E7490", "#ECFEFF"),
    "purist": ("🍔", "#DA291C", "#FFF1F0"),
    "light": ("🌿", "#15803D", "#F0FDF4"),
    "novelty": ("🎲", "#7C3AED", "#F5F3FF"),
}
DEFAULT_STYLE = ("🤖", "#475569", "#F1F5F9")

DEVELOPER_WX = "zyj118"
REPO_NAME = "mcd-roundtable"

# CSS 单独放，避免 f-string 里的花括号转义地狱
_CSS = """
:root{
  --red:#DA291C; --yellow:#FFC72C; --ink:#1F1D1B; --muted:#6B7280;
  --faint:#9CA3AF; --line:#E5E7EB; --paper:#fff; --canvas:#F4F1EC;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--canvas); color:var(--ink); padding:18px 12px 40px;
  display:flex; justify-content:center; align-items:flex-start;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  line-height:1.6; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:660px;width:100%}

/* ---------- 顶部品牌带 ---------- */
.hero{background:var(--red);color:#fff;padding:22px 24px;border-radius:14px 14px 0 0;
  box-shadow:0 6px 18px rgba(218,41,28,.18)}
.hero-top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.hero h1{font-size:21px;font-weight:800;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
.chip{background:var(--yellow);color:#3A2E00;font-size:11px;font-weight:800;
  padding:3px 9px;border-radius:999px;letter-spacing:.5px;white-space:nowrap}
.hero .stamp{font-size:11px;opacity:.85;font-variant-numeric:tabular-nums}
.ask{margin-top:14px;background:rgba(0,0,0,.16);border-radius:9px;padding:11px 14px;font-size:14px}
.ask b{color:var(--yellow)}
.ask .sub{font-size:12px;opacity:.85;margin-top:5px}

/* ---------- 采集提示（降级 / 换店）---------- */
/* 这些提示在终端里是末尾那行 ⚠。HTML 是拿去分享的，收到的人只看得到
   "门店：XXX" 那一行；如果不把同一批提示放进页面，分享出去的结论就会
   比终端里更"确定"——那是信息失真，不是美化。 */
.caveat{background:#FFFBEB;border:1px solid #FDE68A;border-top:0;padding:10px 24px 11px;
  font-size:12px;color:#8A5A00;line-height:1.75}
.caveat .ct{font-weight:800;margin-right:5px}
.caveat ul{margin:4px 0 0;padding-left:18px}
.caveat li{margin-top:3px}

/* ---------- 作者条 ---------- */
.author{background:#fff;border:1px solid var(--line);border-top:0;padding:10px 24px;
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  font-size:12.5px;color:var(--muted);flex-wrap:wrap}
.author b{color:var(--red)}
.author .wx{font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}

/* ---------- 通用卡片 ---------- */
.card{background:#fff;border:1px solid var(--line);border-top:0;padding:20px 24px 22px}
.card.last{border-radius:0 0 14px 14px;border-bottom:1px solid var(--line)}
.sec{font-size:13px;font-weight:800;color:#475569;letter-spacing:.6px;
  display:flex;align-items:center;gap:7px;margin-bottom:16px}
.sec::after{content:"";flex:1;height:1px;background:var(--line)}

/* ---------- 辩论时间线 ---------- */
.msg{display:flex;gap:11px;margin-bottom:15px;opacity:0;transform:translateY(9px);
  animation:rise .40s cubic-bezier(.2,.8,.3,1) forwards;animation-delay:var(--d,0s)}
@keyframes rise{to{opacity:1;transform:none}}
.badge{width:38px;height:38px;border-radius:11px;flex:0 0 38px;display:flex;
  align-items:center;justify-content:center;font-size:19px;line-height:1}
.badge.r{border-radius:50%}
.mbody{flex:1;min-width:0}
.mhead{display:flex;align-items:center;gap:7px;margin-bottom:4px;flex-wrap:wrap}
.who{font-size:13px;font-weight:800}
.arrow{font-size:11.5px;color:var(--faint)}
.bubble{background:#F8FAFC;border:1px solid #EDF1F5;border-radius:12px;
  padding:9px 13px;font-size:13.5px;color:#334155;word-break:break-word}
.bubble.win{background:#FFFBEB;border-color:#FDE68A}
.pitch{margin-top:7px;font-size:12.5px;color:var(--muted);padding-left:11px;
  border-left:2px solid var(--line);word-break:break-word}
.amt{font-weight:800;color:#B7791F;font-variant-numeric:tabular-nums}

/* ---------- 试算表 ---------- */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{font-size:11px;color:var(--faint);text-align:left;padding:0 0 8px;
  border-bottom:1px solid var(--line);font-weight:700}
.tbl td{padding:9px 0;border-bottom:1px dashed #F1F5F9;color:#475569}
.tbl tr:last-child td{border-bottom:0}
.tbl .n{text-align:right;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,Menlo,Consolas,monospace}
.tbl tr.win td{color:var(--ink);font-weight:700}
.tbl tr.win{background:#FFFBEB}
.tbl .lab{max-width:0;width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mark{font-size:10px;margin-right:6px}
.mark.on{color:var(--red)}
.mark.off{color:#CBD5E1}
.tnote{margin-top:11px;font-size:11.5px;color:var(--faint)}

/* ---------- 小票 ---------- */
.receipt{position:relative;background:#fff;border:1px solid var(--line);border-top:0;
  padding:22px 24px 26px}
.receipt::after{content:"";position:absolute;left:-1px;right:-1px;bottom:-11px;height:12px;
  background-image:radial-gradient(circle at 10px 0,#fff 10px,transparent 10.5px);
  background-size:20px 12px;background-repeat:repeat-x}
.rhead{text-align:center;margin-bottom:16px}
.rhead h2{font-size:15px;font-weight:800;letter-spacing:2.5px;color:var(--ink)}
.rhead p{font-size:11.5px;color:var(--faint);margin-top:3px}
.items{width:100%;border-collapse:collapse;margin-bottom:4px}
.items td{padding:10px 0;border-bottom:1px dashed #F1F5F9;font-size:14px;vertical-align:top}
.items .qty{text-align:center;color:var(--faint);font-size:12.5px;width:52px;
  font-variant-numeric:tabular-nums}
.items .pr{text-align:right;font-weight:700;width:88px;
  font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.sum{display:flex;justify-content:space-between;font-size:13.5px;color:var(--muted);padding:5px 0}
.sum.save{color:#15803D}
.total{display:flex;justify-content:space-between;align-items:baseline;margin-top:12px;
  padding-top:13px;border-top:2px solid var(--line)}
.total .lab{font-size:13px;font-weight:700;color:var(--muted)}
.total .val{font-size:27px;font-weight:800;color:var(--red);
  font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.total .pp{font-size:13px;font-weight:700;margin-left:10px;color:var(--ink)}
.tags{display:flex;gap:7px;flex-wrap:wrap;margin-top:15px}
.tag{font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:999px}
.tag.p{background:#EFF6FF;color:#1D4ED8}
.tag.k{background:#FEF2F2;color:#B91C1C}
.tag.n{background:#F0FDF4;color:#15803D}
.note{margin-top:14px;background:#FFFBEB;border-left:3px solid var(--yellow);
  border-radius:0 7px 7px 0;padding:10px 13px;font-size:12.5px;color:#8A6A00}
.pay{display:block;width:100%;margin-top:18px;padding:14px;border-radius:11px;text-align:center;
  background:var(--yellow);color:#3A2E00;font-weight:800;font-size:15.5px;text-decoration:none;
  box-shadow:0 5px 14px rgba(255,199,44,.45);letter-spacing:.4px}
.pay.off{background:#EEF1F4;color:#6B7280;box-shadow:none;font-weight:700;font-size:13.5px}
.source{margin-top:14px;font-size:11.5px;color:var(--faint);text-align:center}

/* ---------- 谁赢了 ---------- */
.win-row{display:flex;gap:9px;align-items:flex-start;font-size:13px;margin-bottom:9px}
.win-row .k{font-size:11.5px;font-weight:800;color:var(--faint);flex:0 0 52px;padding-top:2px}
.win-row .v{color:var(--ink);word-break:break-word}
.win-row .v.dim{color:var(--faint)}

/* ---------- 页脚 ---------- */
footer{margin-top:26px;text-align:center;font-size:11.5px;color:var(--faint);line-height:1.85}
footer b{color:var(--red)}
footer .wx{color:var(--ink);font-weight:800;font-variant-numeric:tabular-nums}

@media (prefers-reduced-motion:reduce){
  .msg{animation:none;opacity:1;transform:none}
}
@media (max-width:480px){
  body{padding:10px 8px 30px}
  .hero,.card,.receipt{padding-left:16px;padding-right:16px}
  .hero h1{font-size:18px}
  .total .val{font-size:23px}
}
@media print{
  body{background:#fff;padding:0}
  .hero{box-shadow:none}
  .pay{display:none}
}
"""


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #

def _roster(key: str) -> tuple[str, str, str]:
    return ROLE_STYLE.get(key, DEFAULT_STYLE)


def _money(value: float) -> str:
    return f"¥{value:.1f}"


def _nutrition(result) -> list[tuple[str, str, str]]:
    """按官方营养表统计本单营养。

    ⚠️ 营养表**不覆盖全部商品**（套餐常常查不到）。
    这里遵循与 CLI / 卡片一致的口径：查不到就不显示，绝不拿 0 冒充真实值。
    """
    quote = result.verdict.quote
    kcal = protein = sodium = 0.0
    hit = 0
    for line in quote.lines:
        item = result.data.by_code(line.code)
        if item is None:
            continue
        if item.kcal or item.protein or item.sodium:
            hit += 1
        kcal += item.kcal * line.qty
        protein += item.protein * line.qty
        sodium += item.sodium * line.qty

    tags: list[tuple[str, str, str]] = []
    if hit == 0:
        return tags
    people = max(1, quote.people)
    if protein:
        tags.append((f"🥩 蛋白质 ≈ {protein / people:.0f}g/人", "p", ""))
    if kcal:
        tags.append((f"🔥 热量 ≈ {kcal / people:.0f}kcal/人", "k", ""))
    if sodium:
        tags.append((f"🧂 钠 ≈ {sodium / people:.0f}mg/人", "n", ""))
    if hit < len(quote.lines):
        tags.append((f"⚠ {len(quote.lines) - hit} 项官方营养表未收录", "n", ""))
    return tags


def _debate(result) -> str:
    """第 1 轮提案 + 第 2 轮质询，按时间顺序串成聊天流。"""
    rows: list[str] = []
    delay = 0.0

    def slot() -> str:
        nonlocal delay
        out = f' style="--d:{delay:.2f}s"'
        delay += 0.12
        return out

    for p in result.proposals:
        emoji, color, bg = _roster(p.role_key)
        rows.append(
            f'<div class="msg"{slot()}>'
            f'<div class="badge" style="background:{bg}">{emoji}</div>'
            f'<div class="mbody"><div class="mhead">'
            f'<span class="who" style="color:{color}">{escape(p.role_name)}</span>'
            f'<span class="arrow">第 1 轮 · 提案</span>'
            f'<span class="amt">{_money(p.amount)}</span>'
            f"</div>"
            f'<div class="bubble">{escape(p.pitch)}</div>'
            + (
                f'<div class="pitch">{escape(p.rationale)}</div>'
                if p.rationale
                else ""
            )
            + "</div></div>"
        )

    for rb in result.rebuttals:
        emoji, color, bg = _roster(rb.role_key)
        target = (
            f'<span class="arrow">→ 质询 {escape(rb.target_name)}</span>'
            if rb.target_name
            else '<span class="arrow">第 2 轮 · 质询</span>'
        )
        rows.append(
            f'<div class="msg"{slot()}>'
            f'<div class="badge r" style="background:{bg}">{emoji}</div>'
            f'<div class="mbody"><div class="mhead">'
            f'<span class="who" style="color:{color}">{escape(rb.role_name)}</span>'
            f"{target}</div>"
            f'<div class="bubble">{escape(rb.text)}</div>'
            f"</div></div>"
        )
    return "\n".join(rows)


def _trials(result) -> str:
    trials = result.verdict.trials
    if not trials:
        return ""
    rows = []
    for t in trials:
        cls = ' class="win"' if t.adopted else ""
        mark = (
            '<span class="mark on">◆</span>'
            if t.adopted
            else '<span class="mark off">◇</span>'
        )
        disc = f"−{_money(t.discount)}" if t.discount else "—"
        rows.append(
            f"<tr{cls}>"
            f'<td><span class="lab">{mark}{escape(t.label)}</span></td>'
            f'<td class="n">{_money(t.original)}</td>'
            f'<td class="n">{disc}</td>'
            f'<td class="n">{_money(t.payable)}</td>'
            f'<td class="n">{_money(t.per_person)}</td>'
            f"</tr>"
        )
    return (
        '<div class="card">'
        f'<div class="sec">🧾 真实试算 · 共 {len(trials)} 组</div>'
        '<table class="tbl"><thead><tr>'
        "<th>候选组合</th><th style='text-align:right'>原价</th>"
        "<th style='text-align:right'>优惠</th><th style='text-align:right'>实付</th>"
        "<th style='text-align:right'>人均</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        '<div class="tnote">以上每一组都由麦当劳官方 calculate-price 实时试算，'
        "不是本工具的估算值。</div>"
        "</div>"
    )


def _receipt(result, pay_url: str | None) -> str:
    quote = result.verdict.quote
    c = result.constraint

    items = "".join(
        f"<tr><td>{escape(line.name)}</td>"
        f'<td class="qty">×{line.qty}</td>'
        f'<td class="pr">{_money(line.amount)}</td></tr>'
        for line in quote.lines
    )

    sums = [f'<div class="sum"><span>小计</span><span>{_money(quote.subtotal)}</span></div>']
    if quote.discount:
        sums.append(
            f'<div class="sum save"><span>优惠券抵扣</span>'
            f"<span>−{_money(quote.discount)}</span></div>"
        )
    if quote.delivery_fee:
        sums.append(
            f'<div class="sum"><span>配送费</span><span>{_money(quote.delivery_fee)}</span></div>'
        )
    if quote.packing_fee:
        sums.append(
            f'<div class="sum"><span>打包费</span><span>{_money(quote.packing_fee)}</span></div>'
        )

    per_person = (
        f'<span class="pp">人均 {_money(quote.per_person)}</span>' if quote.people > 1 else ""
    )

    tags = _nutrition(result)
    tag_html = (
        '<div class="tags">'
        + "".join(f'<span class="tag {css}">{escape(text)}</span>' for text, css, _ in tags)
        + "</div>"
        if tags
        else ""
    )

    # 凑单提示：只在"净收益为正"时才出现。多花钱的建议一律不给。
    net = round(quote.next_saving - quote.discount - quote.next_gap, 2)
    note = ""
    if quote.next_gap > 0 and net > 0.01:
        note = (
            f'<div class="note">💡 再点 {_money(quote.next_gap)} 跨到下一档优惠，'
            f"实付净降 <b>{_money(net)}</b>（官方试算口径）</div>"
        )

    advice = result.verdict.why_not_cheapest or result.verdict.summary
    advice_html = (
        f'<div class="note">📝 裁决依据：{escape(advice)}</div>' if advice else ""
    )

    if pay_url:
        pay = (
            f'<a class="pay" href="{escape(pay_url, quote=True)}" '
            'target="_blank" rel="noopener noreferrer">'
            "🚀 前往麦当劳官方页面完成支付</a>"
        )
    else:
        pay = (
            '<div class="pay off">✅ 决议已敲定 · '
            "加 <code>--order</code> 可生成麦当劳官方支付链接</div>"
        )

    src = {
        "calculate-price": "金额来自麦当劳官方 calculate-price 实时试算",
        "local-fallback": "⚠ 官方试算不可用，以上为本地推算价，仅供参考",
        "demo-data": "演示数据，非实时价格",
    }.get(quote.source, escape(quote.source))

    # 门店名与人数标签都来自官方接口，是真机可变的字符串。
    # 这里是全文件唯一一处"看起来是常量、其实是外部输入"的插值——
    # 漏一次 escape，`<script>` 就能从门店名里长出来。
    meta = " · ".join(
        x
        for x in [
            escape(result.data.store_name) if result.data.store_name else "麦当劳餐厅",
            escape(result.verdict.headcount_label or f"{c.people} 人"),
            "离线演示数据" if result.data.is_demo else "真实 MCP 接口",
        ]
        if x
    )

    return (
        '<div class="receipt">'
        '<div class="rhead"><h2>MCD ROUNDTABLE RECEIPT</h2>'
        "<p>官方 MCP 试算 · 绝无模型幻觉金额</p></div>"
        f'<table class="items"><tbody>{items}</tbody></table>'
        + "".join(sums)
        + '<div class="total"><span class="lab">应&nbsp;&nbsp;付</span>'
        f'<span><span class="val">{_money(quote.payable)}</span>{per_person}</span></div>'
        + tag_html
        + note
        + advice_html
        + pay
        + f'<div class="source">{meta}<br>{src}</div>'
        + "</div>"
    )


def _who_won(result) -> str:
    v = result.verdict
    if not v.adopted and not v.rejected:
        return ""
    rows = []
    if v.adopted:
        rows.append(
            '<div class="win-row"><span class="k">采 纳</span>'
            f'<span class="v">{escape(" · ".join(v.adopted))}</span></div>'
        )
    if v.rejected:
        rows.append(
            '<div class="win-row"><span class="k">未采纳</span>'
            f'<span class="v dim">{escape("、".join(v.rejected))}</span></div>'
        )
    if v.summary:
        rows.append(
            '<div class="win-row"><span class="k">一句话</span>'
            f'<span class="v">{escape(v.summary)}</span></div>'
        )
    return f'<div class="card"><div class="sec">🏆 谁赢了</div>{"".join(rows)}</div>'


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def _caveats(result) -> str:
    """把采集层的 warnings 搬进页面。

    终端里这些是末尾那行 ⚠，但 HTML 是拿去分享的：收到的人只看得到
    "📍 门店：XXX"，不知道这家店是不是退而求其次来的、候选池是不是只扫了两家。
    不放进页面，分享出去的结论就比终端里更"确定" —— 那是信息失真，不是美化。
    """
    items = [str(w).strip() for w in (getattr(result, "warnings", None) or []) if str(w).strip()]
    if not items:
        return ""
    lis = "".join(f"<li>{escape(w)}</li>" for w in items)
    return f'<div class="caveat"><span class="ct">⚠ 采集提示</span><ul>{lis}</ul></div>\n'


def render_html_report(
    result,
    path: str | Path,
    *,
    pay_url: str | None = None,
    developer_wx: str = DEVELOPER_WX,
    repo_url: str = "",
) -> Path:
    """把一次会议结果写成自包含的 Single-File HTML，返回写入路径。"""
    c = result.constraint
    v = result.verdict
    q = v.quote
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 只接受看起来像 URL 的支付地址，避免把脏数据塞进 href
    if pay_url and not pay_url.startswith(("http://", "https://")):
        pay_url = None

    trials_html = _trials(result)
    receipt_html = _receipt(result, pay_url)
    won_html = _who_won(result)
    caveats_html = _caveats(result)

    repo_line = (
        f'<div>开源项目：<b>{escape(repo_url)}</b></div>'
        if repo_url
        else f"<div>开源项目：<b>{escape(REPO_NAME)}</b></div>"
    )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>麦门圆桌决议书 · {escape(c.raw_query or "本次会议")}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">

  <div class="hero">
    <div class="hero-top">
      <h1>🍔 麦门圆桌议会 <span class="chip">正式裁决</span></h1>
      <span class="stamp">{now}</span>
    </div>
    <div class="ask">
      <b>🎯 本期诉求：</b>{escape(c.raw_query or "（未指定需求）")}
      <div class="sub">📍 {escape(result.data.store_name or "麦当劳餐厅")}
        · {escape(v.headcount_label or f"{c.people} 人")}
        · 大脑：{escape(result.brain_label)}</div>
    </div>
  </div>

  {caveats_html}
  <div class="author">
    {repo_line}
    <div>👨‍💻 开发者微信：<span class="wx">{escape(developer_wx)}</span></div>
  </div>

  <div class="card">
    <div class="sec">💬 议会交锋纪要</div>
    {_debate(result)}
  </div>

  {trials_html}

  {won_html}

  {receipt_html}

  <footer>
    本项目为 <b>2026 麦当劳程序员创意开发大赛</b> 开源参赛作品，非麦当劳官方产品<br>
    餐品、价格、优惠、营养数据均来自麦当劳中国官方 MCP Server<br>
    金额以麦当劳官方渠道的最终结算为准，本页仅供参考<br>
    欢迎技术交流与协作探讨 · 微信 <span class="wx">{escape(developer_wx)}</span>
  </footer>

</div>
</body>
</html>
"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc, encoding="utf-8")
    return target
