"""离线演示数据。

存在的意义：让任何人**不申请 MCP Token**，clone 下来就能跑通完整流程。

━━ 关键设计：演示的是"假数据"，不是"假协议" ━━

本模块**不返回 Python 对象**，而是返回与麦当劳 MCP 真实响应**同构的原始文本**，
包括三种脏格式：

* 说明文字 + JSON（`query-nearby-stores` / `calculate-price`）
* Markdown（`available-coupons` / `query-my-coupons` / `campaign-calendar`）
* toon 紧凑表格（`list-nutrition-foods`）

好处：`--demo` 会真实地走一遍 `find_json` / `parse_coupon_markdown` /
`parse_nutrition_toon` / `to_yuan`。**解析层的 bug 在离线阶段就会暴露**，
不必等到配好 Token 才炸。金额单位也严格照抄真机：菜单是**元**，算价是**分**。

⚠️ 数据声明：本文件中的门店、餐品、价格、营养、券均为**演示用示例数据**，
不是麦当劳官方实时数据。真实模式（配置 `MCD_MCP_TOKEN`）下本文件完全不参与。
营养数值参考公开营养信息的量级，仅用于演示计算逻辑，**不构成营养建议**。
"""

from __future__ import annotations

import json

DEMO_STORE = {
    "storeCode": "DEMO001",
    "beCode": "DEMO-BE-001",
    "storeName": "麦当劳（示例·正弘城店）",
    "address": "示例地址，仅用于演示",
    "distance": "0.8km",
}

# 结构对齐 query-meals：分类 → 餐品。价格单位 **元**（与真机一致）
DEMO_MENU = {
    "汉堡 · 人气热卖": [
        {"code": "B001", "name": "板烧鸡腿堡", "price": 24.0, "tags": ["经典", "非油炸"]},
        {"code": "B002", "name": "双层吉士汉堡", "price": 13.5, "tags": ["经典", "高钠"]},
        {"code": "B003", "name": "麦辣鸡腿堡", "price": 23.5, "tags": ["辣", "油炸"]},
        {"code": "B004", "name": "巨无霸", "price": 26.5, "tags": ["经典", "招牌"]},
        {"code": "B005", "name": "麦香鸡腿堡", "price": 23.0, "tags": ["招牌"]},
    ],
    "套餐": [
        {"code": "C001", "name": "1+1 随心配", "price": 13.9, "tags": ["超值", "可选两件"]},
        {"code": "C002", "name": "板烧鸡腿堡套餐", "price": 39.0, "tags": ["含中薯+中可"]},
        {"code": "C003", "name": "巨无霸套餐", "price": 42.0, "tags": ["含中薯+中可"]},
    ],
    "小食": [
        {"code": "S001", "name": "中薯条", "price": 12.0, "tags": ["油炸"]},
        {"code": "S002", "name": "玉米杯", "price": 13.0, "tags": ["低钠", "非油炸"]},
        {"code": "S003", "name": "麦辣鸡翅", "price": 15.0, "tags": ["辣", "油炸"]},
    ],
    "饮品": [
        {"code": "D001", "name": "纯牛奶", "price": 9.0, "tags": ["高蛋白", "无糖"]},
        {"code": "D002", "name": "可口可乐（中）", "price": 9.5, "tags": ["含糖"]},
        {"code": "D003", "name": "无糖可乐（中）", "price": 9.5, "tags": ["无糖"]},
        {"code": "D004", "name": "鲜煮咖啡", "price": 11.0, "tags": ["无糖", "提神"]},
    ],
}

# list-nutrition-foods 的列顺序（真机同构）
_NUTRITION_HEADER = (
    "productName,nutritionDescription,energyKj,energyKcal,"
    "protein,fat,carbohydrate,sodium,calcium"
)

DEMO_NUTRITION = {
    "板烧鸡腿堡": {"energyKj": 2049, "energyKcal": 490, "protein": 27.0, "fat": 22.0, "carbohydrate": 44.0, "sodium": 890, "calcium": 120},
    "双层吉士汉堡": {"energyKj": 1881, "energyKcal": 450, "protein": 24.0, "fat": 24.0, "carbohydrate": 34.0, "sodium": 1240, "calcium": 260},
    "麦辣鸡腿堡": {"energyKj": 2176, "energyKcal": 520, "protein": 25.0, "fat": 27.0, "carbohydrate": 44.0, "sodium": 1100, "calcium": 90},
    "巨无霸": {"energyKj": 2301, "energyKcal": 550, "protein": 27.0, "fat": 29.0, "carbohydrate": 45.0, "sodium": 1010, "calcium": 180},
    "麦香鸡腿堡": {"energyKj": 2008, "energyKcal": 480, "protein": 26.0, "fat": 23.0, "carbohydrate": 42.0, "sodium": 960, "calcium": 110},
    "1+1 随心配": {"energyKj": 2510, "energyKcal": 600, "protein": 26.0, "fat": 26.0, "carbohydrate": 62.0, "sodium": 1180, "calcium": 210},
    "板烧鸡腿堡套餐": {"energyKj": 3050, "energyKcal": 730, "protein": 32.0, "fat": 28.0, "carbohydrate": 82.0, "sodium": 1320, "calcium": 150},
    "巨无霸套餐": {"energyKj": 3300, "energyKcal": 790, "protein": 32.0, "fat": 35.0, "carbohydrate": 84.0, "sodium": 1350, "calcium": 200},
    "中薯条": {"energyKj": 1423, "energyKcal": 340, "protein": 4.0, "fat": 16.0, "carbohydrate": 44.0, "sodium": 270, "calcium": 20},
    "玉米杯": {"energyKj": 544, "energyKcal": 130, "protein": 4.0, "fat": 1.5, "carbohydrate": 28.0, "sodium": 5, "calcium": 4},
    "麦辣鸡翅": {"energyKj": 1046, "energyKcal": 250, "protein": 14.0, "fat": 16.0, "carbohydrate": 12.0, "sodium": 570, "calcium": 30},
    "纯牛奶": {"energyKj": 544, "energyKcal": 130, "protein": 7.0, "fat": 5.0, "carbohydrate": 10.0, "sodium": 120, "calcium": 240},
    "可口可乐（中）": {"energyKj": 879, "energyKcal": 210, "protein": 0.0, "fat": 0.0, "carbohydrate": 53.0, "sodium": 20, "calcium": 4},
    "无糖可乐（中）": {"energyKj": 4, "energyKcal": 1, "protein": 0.0, "fat": 0.0, "carbohydrate": 0.0, "sodium": 25, "calcium": 4},
    "鲜煮咖啡": {"energyKj": 21, "energyKcal": 5, "protein": 0.5, "fat": 0.0, "carbohydrate": 1.0, "sodium": 10, "calcium": 8},
}

# 演示券（阈值满减）。⚠️ 真机的券类接口只返回 Markdown 文本，
# 门槛与金额一律以 calculate-price 为准；这里为了让离线演示能展示"凑单"逻辑而内置。
DEMO_COUPONS = [
    {"couponId": "CP001", "couponCode": "MCD30", "name": "满 30 减 6", "threshold": 30.0, "discount": 6.0},
    {"couponId": "CP002", "couponCode": "MCD50", "name": "满 50 减 12", "threshold": 50.0, "discount": 12.0},
    {"couponId": "CP003", "couponCode": "MCD20", "name": "麦麦省券 · 满 20 减 5", "threshold": 20.0, "discount": 5.0},
]

DEMO_ACCOUNT = {
    "availablePoint": 1280.0,
    "totalPoint": 6420.0,
    "frozenPoint": 0.0,
    "expiringPoint": 320.0,
    "expiringDate": "下月末",
    "memberLevel": "麦麦会员",
}

DEMO_CAMPAIGN = [
    {"date": "今日", "name": "会员日 · 指定汉堡参与满减", "status": "进行中"},
    {"date": "本周五", "name": "麦麦省券加码", "status": "未开始"},
]

DEMO_TAKE_WAY = [
    {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
    {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"},
]

DEMO_DELIVERY_FEE_CENTS = 900  # 9.00 元

# 演示用"字段说明"前缀：真机在 JSON 前真的会贴这么一段，这里照抄
_API_PREAMBLE = (
    "# API Response Information\n\n"
    "Below is the response from an API call. To help you understand the data, "
    "I've provided:\n\n1. A detailed description of all fields in the response structure\n"
    "2. The complete API response\n\n## Original Response\n\n"
)


def _envelope(data: object, *, ok: bool = True, code: int = 200, msg: str = "请求成功") -> str:
    return json.dumps(
        {
            "success": ok,
            "code": code,
            "message": msg,
            "datetime": "2026-10-09 12:10:00",
            "traceId": "demo0000000000000000000000000000",
            "data": data,
        },
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------- #
# 券类工具的 Markdown 原文
# --------------------------------------------------------------------------- #

def _coupons_markdown(title: str) -> str:
    lines = [f"# {title}", ""]
    for c in DEMO_COUPONS:
        lines += [
            f"- {c['name']}",
            f"  couponId：{c['couponId']}",
            f"  couponCode：{c['couponCode']}",
            "  有效期：2026-10-01 至 2026-10-31",
            "",
        ]
    return "\n".join(lines)


def _campaign_markdown() -> str:
    lines = ["# 近期活动日历", ""]
    for c in DEMO_CAMPAIGN:
        lines += [
            f"**活动标题**：{c['name']}",
            f"**活动时间**：{c['date']}",
            f"**活动状态**：{c['status']}",
            "",
        ]
    return "\n".join(lines)


def _nutrition_toon() -> str:
    rows = [f"[{len(DEMO_NUTRITION)}]{{{_NUTRITION_HEADER}}}:"]
    for name, v in DEMO_NUTRITION.items():
        rows.append(
            ",".join(
                [
                    name,
                    "null",
                    str(v["energyKj"]),
                    str(v["energyKcal"]),
                    str(v["protein"]),
                    str(v["fat"]),
                    str(v["carbohydrate"]),
                    str(v["sodium"]),
                    str(v["calcium"]),
                ]
            )
        )
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# 演示算价（真机同构：分单位 + productList + takeWayList + enjoyable）
# --------------------------------------------------------------------------- #

def price_of(code: str) -> float:
    """按餐品编码查菜单价（元）。模拟服务端行为：调用方只传编码，不传价格。"""
    for items in DEMO_MENU.values():
        for m in items:
            if m["code"] == code:
                return float(m["price"])
    return 0.0


def name_of(code: str) -> str:
    for items in DEMO_MENU.values():
        for m in items:
            if m["code"] == code:
                return str(m["name"])
    return code


def _demo_calculate_price(args: dict) -> str:
    """演示算价：与真机**同一套语义**（分单位、productCode、满减、取餐方式）。"""
    items = args.get("items") or []
    lines: list[dict] = []
    subtotal_cents = 0
    for it in items:
        code = str(it.get("productCode") or "")
        qty = int(it.get("quantity", 1) or 1)
        unit = int(round(price_of(code) * 100))
        subtotal_cents += unit * qty
        lines.append(
            {
                "productCode": code,
                "productName": name_of(code),
                "quantity": qty,
                "originalSubtotal": unit * qty,
                "subtotal": unit * qty,
            }
        )

    subtotal_yuan = subtotal_cents / 100.0
    dining = args.get("diningType") or (
        "delivery" if int(args.get("orderType", 1) or 1) == 2 else "takeout"
    )

    # 已享受：取门槛达标里优惠最大的一张
    enjoyed = None
    best = 0.0
    for c in DEMO_COUPONS:
        if subtotal_yuan >= c["threshold"] and c["discount"] > best:
            best = c["discount"]
            enjoyed = c
    discount_cents = int(round(best * 100))

    # 可享受但还没享受：最接近的一档，差额即"再凑多少"
    enjoyable = None
    gap = None
    for c in DEMO_COUPONS:
        delta = c["threshold"] - subtotal_yuan
        if 0 < delta <= 12 and (gap is None or delta < gap):
            gap = delta
            enjoyable = c

    delivery_cents = 0 if dining == "takeout" else DEMO_DELIVERY_FEE_CENTS
    payload = {
        "productOriginalPrice": subtotal_cents,
        "productPrice": subtotal_cents,
        "deliveryOriginalPrice": delivery_cents,
        "deliveryPrice": delivery_cents,
        "packingOriginalPrice": 0,
        "packingPrice": 0,
        "originalPrice": subtotal_cents + delivery_cents,
        "discount": discount_cents,
        "price": subtotal_cents + delivery_cents - discount_cents,
        "productList": lines,
        "takeWayList": DEMO_TAKE_WAY if dining == "takeout" else [],
    }
    if enjoyed:
        payload["enjoyed"] = {
            "amountType": "3",
            "realDiscount": float(enjoyed["discount"]),
            "balance": 0,
        }
    if enjoyable:
        payload["enjoyable"] = {
            "amountType": "3",
            "realDiscount": float(enjoyable["discount"]),
            "balance": int(round((enjoyable["threshold"] - subtotal_yuan) * 100)),
        }

    return _API_PREAMBLE + _envelope(payload)


# --------------------------------------------------------------------------- #
# 工具分发：返回**原始文本**（与真实服务器同构）
# --------------------------------------------------------------------------- #

def demo_tool_raw(tool: str, args: dict | None = None) -> str:
    """返回真机同构的原始响应文本。"""
    args = args or {}

    if tool == "now-time-info":
        return json.dumps({"datetime": "2026-10-09 12:10:00", "period": "午餐"}, ensure_ascii=False)

    if tool == "query-nearby-stores":
        return _API_PREAMBLE + _envelope([DEMO_STORE])

    if tool == "query-meals":
        # 与真机同构：categories[].meals[] 只带编码与标签，
        # 名称与价格在单独的 meals{code: {...}} 明细映射里
        categories = [
            {
                "name": cat,
                "meals": [{"code": m["code"], "tags": m["tags"]} for m in items],
            }
            for cat, items in DEMO_MENU.items()
        ]
        details = {
            m["code"]: {
                "name": m["name"],
                "currentPrice": str(m["price"]),
                "tags": m["tags"],
            }
            for items in DEMO_MENU.values()
            for m in items
        }
        return _envelope({"categories": categories, "meals": details, "store": DEMO_STORE})

    if tool == "query-meal-detail":
        code = str(args.get("mealCode") or args.get("productCode") or args.get("code") or "")
        return _envelope(
            {
                "meal": {
                    "code": code,
                    "name": name_of(code),
                    "currentPrice": str(price_of(code)),
                    "modification": {"items": []},
                }
            }
        )

    if tool == "list-nutrition-foods":
        return _nutrition_toon()

    if tool == "query-my-coupons":
        return _coupons_markdown("我的优惠券")

    if tool == "query-store-coupons":
        return _coupons_markdown(f"{DEMO_STORE['storeName']} · 门店可用券")

    if tool == "available-coupons":
        return _coupons_markdown("当前可领取优惠券")

    if tool == "auto-bind-coupons":
        return _envelope({"bindCount": len(DEMO_COUPONS)})

    if tool == "query-my-account":
        return json.dumps(DEMO_ACCOUNT, ensure_ascii=False)

    if tool == "campaign-calendar":
        return _campaign_markdown()

    if tool == "calculate-price":
        return _demo_calculate_price(args)

    if tool == "create-order":
        return _API_PREAMBLE + _envelope(
            {
                "orderId": "DEMO-ORDER-0001",
                "payId": "DEMO-PAY-0001",
                "payH5Url": "https://demo.invalid/该链接为演示占位，真实模式返回麦当劳官方支付链接",
                "orderDetail": {"storeName": DEMO_STORE["storeName"], "orderStatus": "1"},
            }
        )

    if tool == "cancel-order":
        return _envelope({"cancelResult": True, "orderId": args.get("orderId", "")})

    return _envelope({})
