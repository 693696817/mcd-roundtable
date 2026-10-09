"""命令行入口。

    mcd-roundtable --demo "中午想吃饱，30 以内，把券用上"
    mcd-roundtable "三个人，预算 60，把券用到极致" --city 郑州 --keyword 新华书店
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys

from .council import CouncilError, build_brain, convene, parse_constraint
from .mcp_client import (
    BE_TYPE_PICKUP,
    ORDER_TYPE_DELIVERY,
    ORDER_TYPE_INSTORE,
    DemoMCP,
    MCPError,
    RealMCP,
    load_data,
)
from .render import make_console, render
from .roles import ROLES, resolve_roles

EPILOG = """\
示例:
  mcd-roundtable --demo "中午想吃饱，30 以内，把券用上"
  mcd-roundtable "三个人，预算 60，把券用到极致" --roles saver,purist
  mcd-roundtable "减脂期，单餐 600kcal 以内" --roles macro,light --objective protein
  mcd-roundtable "团队 8 人午餐，预算 300" --people 8 --rounds 2
  mcd-roundtable "帮我点一份" --city 郑州 --keyword 新华书店 --order

委员: saver(省钱部长) macro(健身总监) purist(麦门老饕) light(养生专员) novelty(尝鲜委员)

真实模式需要 MCD_MCP_TOKEN（到 https://open.mcd.cn/mcp 申请）。
加 --demo 可零配置跑通完整流程。
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcd-roundtable",
        description="麦门圆桌 —— 一句话，五个 AI 人格吵出一个能下单的麦当劳方案。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", nargs="*", help="你的需求，一句话即可")
    p.add_argument("--demo", action="store_true", help="离线演示模式：不联网，用内置示例数据跑通全流程")
    p.add_argument("--roles", help="指定参与的委员，逗号分隔，默认全部")
    p.add_argument("--people", type=int, help="人数")
    p.add_argument("--budget", type=float, help="总预算（元）")
    p.add_argument("--per-person", type=float, help="人均预算（元）")
    p.add_argument("--rounds", type=int, default=1, help="质询轮数，1-3，默认 1")
    p.add_argument("--objective", choices=["cost", "protein", "balanced"], default="cost", help="优化目标")
    p.add_argument("--brain", choices=["auto", "llm", "heuristic"], default="auto", help="话术大脑，默认自动探测")
    p.add_argument("--city", help="门店所在城市（真实模式必填，如 郑州）")
    p.add_argument("--keyword", help="门店关键词（真实模式必填，如 新华书店）")
    p.add_argument("--delivery", action="store_true", help="按外送（麦乐送）而不是到店自取来试算")
    p.add_argument("--order", action="store_true", help="确认下单：调用 create-order 并返回麦当劳官方支付链接")
    p.add_argument("--share", metavar="PATH", help="把决议导出为一张可截图的 SVG 卡片，如 --share card.svg")
    p.add_argument(
        "--html",
        nargs="?",
        const="mcd-verdict.html",
        metavar="PATH",
        help="把决议导出为自包含的交互式网页（单文件、离线可看），"
        "如 --html report.html；省略路径则用 mcd-verdict.html。"
        "提示：请把需求写在 --html 之前，例如 mcd-roundtable \"中午吃饱\" --html",
    )
    p.add_argument("--json", action="store_true", dest="as_json", help="输出结构化 JSON")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    return p


async def _run(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()

    if args.demo:
        mcp_client = DemoMCP()
    else:
        try:
            mcp_client = RealMCP()
        except MCPError as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 2

    order_type = ORDER_TYPE_DELIVERY if args.delivery else ORDER_TYPE_INSTORE
    be_type = 2 if args.delivery else BE_TYPE_PICKUP

    async with mcp_client as client:
        data = await load_data(
            client,
            city=args.city,
            keyword=args.keyword,
            order_type=order_type,
            be_type=be_type,
        )

        constraint = parse_constraint(query, people=args.people)
        if args.budget is not None:
            constraint.budget_total = args.budget
        if args.per_person is not None:
            constraint.budget_per_person = args.per_person
        constraint.takeout = not args.delivery

        try:
            roles = resolve_roles(args.roles)
        except ValueError as exc:
            print(f"✖ {exc}", file=sys.stderr)
            return 2

        brain = build_brain(args.brain)
        async with brain as active_brain:
            try:
                result = await convene(
                    data,
                    constraint,
                    roles,
                    brain=active_brain,
                    rounds=args.rounds,
                    objective=args.objective,
                )
            except CouncilError as exc:
                print(f"✖ {exc}", file=sys.stderr)
                return 3

        order_info: dict | None = None
        if args.order:
            order_info = await _create_order(data, result)

    # 导出与"怎么显示"无关：以前 --share / --html 被写在 `else`（非 JSON 分支）里，
    # 于是 `--json --html r.html` 会安静地什么都不生成 —— 参数被收下了，
    # 文件却不在，用户只会以为是自己路径写错。
    #
    # 现在两个分支都导出。区别只在提示语的去向：
    #   --json  → stderr（stdout 必须留给**干净可解析**的 JSON）
    #   普通模式 → stdout，且排在结论之后、下单提示之前，保持原来的阅读顺序
    def _export(emit) -> None:
        if args.share:
            from .card import render_card_svg

            written = render_card_svg(result, args.share)
            emit(f"  🎴 决议卡已导出：{written}")
            emit("")
        if args.html:
            from .html_report import render_html_report

            pay_url = (order_info or {}).get("payH5Url") or None
            written = render_html_report(result, args.html, pay_url=pay_url)
            # 打印用户给的那个路径（与 --share 一致）：短、好认、可直接双击打开。
            # 绝对路径动辄上百字符，会把终端截图和演示 GIF 撑得很宽。
            emit(f"  📄 决议网页已生成：{written}")
            emit("")

    if args.as_json:
        _export(lambda msg: print(msg, file=sys.stderr))
        payload = _to_json(result)
        if order_info is not None:
            payload["order"] = order_info
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render(result, make_console(no_color=args.no_color))
        _export(print)
        if order_info is not None:
            _print_order(order_info)
        else:
            _hint_order()
    return 0


def _hint_order() -> None:
    print("  （未下单。确认方案后加 --order，将由麦当劳官方页面完成支付）")
    print()


def _print_order(info: dict) -> None:
    print()
    if not info.get("ok"):
        print(f"  ✖ {info.get('message') or '创建订单失败'}")
        return
    print(f"  ✅ 订单已生成：{info.get('orderId') or '（未返回）'}")
    if info.get("payH5Url"):
        print(f"  👉 麦当劳官方支付链接：{info['payH5Url']}")
    print("  （未支付的订单会自动失效；如需撤销，可调用 cancel-order）")
    print()


async def _create_order(data, result) -> dict:
    """调用 create-order 生成订单与**官方**支付链接。

    本工具不接触任何支付凭证，只把用户交还给麦当劳官方支付页。
    返回结构化结果，由调用方决定何时展示（保证"先看决议，再下单"）。
    """
    quote = result.verdict.quote
    order_type = data.ctx.order_type

    args: dict = {
        "storeCode": data.ctx.store_code,
        "orderType": order_type,
        "beType": data.ctx.be_type,
        "items": [{"productCode": line.code, "quantity": line.qty} for line in quote.lines],
        "needTableware": False,
    }
    # 实测：orderType=1（到店/得来速）必传 takeWayCode，值只能取自 takeWayList
    if order_type == ORDER_TYPE_INSTORE:
        take_way = quote.take_way_code() or _first_take_way(data)
        if not take_way:
            return {"ok": False, "message": "未拿到 takeWayList，无法确定取餐方式（到店下单必传 takeWayCode）"}
        args["takeWayCode"] = take_way

    try:
        raw = await data.client.call("create-order", args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"创建订单失败：{exc}"}

    payload = raw if isinstance(raw, dict) else {}
    if payload.get("_error"):
        return {"ok": False, "message": payload["_error"]}

    return {
        "ok": True,
        "orderId": payload.get("orderId"),
        "payH5Url": payload.get("payH5Url") or payload.get("payUrl") or payload.get("payLink"),
        "takeWayCode": args.get("takeWayCode"),
    }


def _first_take_way(data) -> str:
    for entry in getattr(data, "take_way_list", []) or []:
        code = str(entry.get("code") or "")
        if code:
            return code
    return ""


# --------------------------------------------------------------------------- #
# JSON 输出
# --------------------------------------------------------------------------- #

def _line_to_json(line) -> dict:
    return {
        "code": line.code,
        "name": line.name,
        "qty": line.qty,
        "unitPrice": line.unit_price,
        "amount": line.amount,
    }


def _to_json(result) -> dict:
    quote = result.verdict.quote
    return {
        "query": result.constraint.raw_query,
        # 约束保留 Python 字段名，便于与其他脚本直接对接
        "constraint": dataclasses.asdict(result.constraint),
        "mode": "demo" if result.data.is_demo else "live",
        "brain": result.brain_label,
        "store": result.data.store,
        "proposals": [
            {
                "role": p.role_key,
                "roleName": p.role_name,
                "items": [_line_to_json(i) for i in p.line_items],
                "amount": p.amount,
                "pitch": p.pitch,
            }
            for p in result.proposals
        ],
        "rebuttals": [dataclasses.asdict(r) for r in result.rebuttals],
        "trials": [dataclasses.asdict(t) for t in result.verdict.trials],
        "verdict": {
            "lines": [_line_to_json(l) for l in quote.lines],
            "subtotal": quote.subtotal,
            "discount": quote.discount,
            "deliveryFee": quote.delivery_fee,
            "packingFee": quote.packing_fee,
            "payable": quote.payable,
            "perPerson": quote.per_person,
            "appliedCoupon": quote.applied_coupon,
            "priceSource": quote.source,
            "takeWayList": quote.take_way_list,
            "nextGap": quote.next_gap,
            "nextSaving": quote.next_saving,
            "adopted": result.verdict.adopted,
            "rejected": result.verdict.rejected,
            "summary": result.verdict.summary,
        },
        "toppedUp": result.topped_up,
        "warnings": result.warnings,
    }


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    os.environ.setdefault("PYTHONUTF8", "1")
    args = build_parser().parse_args(argv)

    if not args.demo and not os.environ.get("MCD_MCP_TOKEN"):
        print(
            "✖ 未检测到 MCD_MCP_TOKEN。\n"
            "  · 想零配置先看效果：加 --demo\n"
            "  · 想接真实数据：到 https://open.mcd.cn/mcp 申请后设置环境变量",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
