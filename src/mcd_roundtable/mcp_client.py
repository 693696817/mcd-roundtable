"""麦当劳 MCP 接入层。

本文件按**对 https://mcp.mcd.cn 的实测抓包结果**适配，不按文档猜测。

━━ 一、实测发现：返回格式有四种，不能直接 json.loads ━━

1. 纯 JSON                 —— `query-my-account`
2. 说明文字 + JSON          —— `query-nearby-stores` / `calculate-price`
                              （开头是一大段 "# API Response Information" 字段说明）
3. Markdown                —— `available-coupons` / `campaign-calendar` / `query-my-coupons`
4. toon 紧凑表格            —— `list-nutrition-foods`（`[160]{表头}:` 开头）

━━ 二、实测发现：三个致命的静默错误 ━━

* **字段名**：`calculate-price` / `create-order` 的 items 元素字段是 **`productCode`**，
  不是 `code`，也不是 `mealCode`。传错**不报错**，只是静默返回 `price=0`。
* **单位不统一**：`calculate-price` 返回的金额是**分**，而 `query-meals` 的
  `currentPrice` 是**元**。混用会让「29 元套餐」变成「0.29 元」。
* **场景耦合**：`create-order` 在 `orderType=1`（到店）下**必传 `takeWayCode`**，
  值只能从 `calculate-price` 返回的 `takeWayList[].code` 里取（实测值：`eat-in` 堂食 /
  `take-in-store` 外带）。漏传报 `600042`。

━━ 三、设计取舍：demo 与 live 共用一条解析路径 ━━

`DemoMCP` **不返回 Python 对象**，而是返回与真实服务器**同构的原始文本**
（含说明文字、Markdown、toon 表格三种脏格式）。这样：

* 离线演示会真实地走过 `find_json` / `parse_coupon_markdown` / `parse_nutrition_toon`；
* 解析层的 bug 在 `--demo` 下就会暴露，不会等到配好 Token 才炸；
* README 里的演示输出与真实模式输出结构完全一致，不存在"演示画大饼"。

━━ 四、金额口径 ━━

内部一律以 **元** 流转（`PriceBreakdown` 已换算）。所有对外展示的金额，
要么来自 `calculate-price`，要么被显式标注为 `local-fallback`，**绝不估算**。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import demo_data
from .models import Coupon, MenuItem

DEFAULT_BASE_URL = "https://mcp.mcd.cn"
CACHE_TTL_SECONDS = 300
MAX_RETRY = 3

# 写操作（非幂等）工具：**强制绕过缓存**。
# 缓存它们会制造"第二次调用拿到第一次结果"的幽灵行为——
# 连续两轮点单，第二轮直接返回上一单的 orderId 与支付链接，
# 用户以为下了新单，实际什么都没发生，而这是要付钱的动作。
#
# 这里只列真机确认存在的工具名。查不到名字的工具默认按只读处理（会被缓存），
# 所以将来**新增写操作工具时必须同步加到这里**。
_MUTATING_TOOLS = frozenset(
    {
        "create-order",
        "cancel-order",
        "auto-bind-coupons",
    }
)

# orderType：1-到店（含到店自取 + 得来速），2-外送（含麦乐送 + 企业团餐）
ORDER_TYPE_INSTORE = 1
ORDER_TYPE_DELIVERY = 2

# beType：1-到店取餐，2-麦乐送到家，5-得来速，6-企业团餐
BE_TYPE_PICKUP = 1
BE_TYPE_DELIVERY = 2
BE_TYPE_DRIVE = 5
BE_TYPE_CATERING = 6

_DECODER = json.JSONDecoder()


class MCPError(RuntimeError):
    """MCP 调用失败。"""


# --------------------------------------------------------------------------- #
# 一、脏文本 → 结构
# --------------------------------------------------------------------------- #

def _json_anchors(text: str) -> list[int]:
    """按可信度给出所有可能的 JSON 起点。

    ⚠️ 关键点：**每个锚点的所有出现位置都要试，不能只试第一个**。
    真机的字段说明里会出现形如 `结构为 {xxx} 见下` 的占位花括号，
    它排在真正的 JSON 前面；只试第一个 `{` 会解析失败并直接放弃，
    把一份好数据降级成 `_markdown`。
    """
    out: list[int] = []
    for anchor in ('{"success"', '{"code"'):
        start = 0
        while True:
            idx = text.find(anchor, start)
            if idx < 0:
                break
            out.append(idx)
            start = idx + 1
    start = 0
    while True:
        idx = text.find("{", start)
        if idx < 0:
            break
        if idx not in out:
            out.append(idx)
        start = idx + 1
    return out


def find_json(text: Any) -> Any | None:
    """从夹带说明文字的返回里抓出第一个完整 JSON 对象。

    麦当劳 MCP 习惯在 JSON 前面贴一整段"字段说明"，所以不能 `json.loads` 整串。
    `raw_decode` 从 `{` 开始解析，天然容忍尾部的多余文本。
    """
    if not isinstance(text, str):
        return None
    for idx in _json_anchors(text):
        try:
            return _DECODER.raw_decode(text, idx)[0]
        except json.JSONDecodeError:
            continue
    return None


def unwrap(payload: Any) -> Any:
    """剥掉 `{"success":..,"data":..}` 外壳。"""
    if isinstance(payload, dict) and "data" in payload and (
        "success" in payload or "code" in payload or "message" in payload
    ):
        return payload["data"]
    return payload


def extract_text(text: str) -> Any:
    """原始文本 → 结构化值；不是 JSON 就包成 `{"_markdown": ...}` 交给上层。"""
    payload = find_json(text)
    if payload is None:
        return {"_markdown": text}
    # 业务失败（success=false）也要能被上层看到，不能静默变成空 dict。
    # 真机两种写法都出现过：布尔 `false` 和字符串 `"false"`。
    # 只认布尔 `False` 时，字符串版会一路走到 unwrap，把 error 悄悄丢掉，
    # 上层看到的是一个空 dict —— 看起来像"接口没数据"，实际是"接口报错了"。
    if isinstance(payload, dict):
        flag = payload.get("success")
        failed = flag is False or (
            isinstance(flag, str) and flag.strip().lower() in ("false", "0", "no", "fail")
        )
        if failed:
            return {
                "_error": payload.get("message") or "未知错误",
                "_code": payload.get("code"),
            }
    return unwrap(payload)


def parse_nutrition_toon(raw: Any) -> dict[str, dict[str, float]]:
    """解析 `list-nutrition-foods` 的 toon 紧凑表格。

    实测原文形如::

        [160]{productName,nutritionDescription,energyKj,energyKcal,...}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          ...

    开头的 `[160]` 是条数标记；表头行不以 `{` 打头，必须单独处理。

    ⚠️ 本函数的入参可能是 `{"_markdown": toon 原文}` —— 因为 toon 表头里那个 `{`
    会让 `find_json` 误以为后面是 JSON，解析失败后整段被归入 `_markdown`。
    """
    if isinstance(raw, dict):
        if "_markdown" in raw:
            raw = raw["_markdown"]
        else:
            inner = raw.get("nutrition") or raw.get("data")
            if isinstance(inner, dict):
                return {k: v for k, v in inner.items() if isinstance(v, dict)}
            raw = inner if isinstance(inner, str) else None

    if not isinstance(raw, str) or not raw.strip():
        return {}

    out: dict[str, dict[str, float]] = {}
    header: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        brace = line.find("{")
        if brace >= 0 and "}:" in line:
            header = [h.strip() for h in line[brace + 1 : line.index("}")].split(",")]
            continue
        if not header:
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        row = dict(zip(header, cells))
        name = row.get("productName")
        if not name:
            continue
        values: dict[str, float] = {}
        for key, val in row.items():
            if key == "productName":
                continue
            try:
                values[key] = float(val)
            except (TypeError, ValueError):
                continue
        out[name] = values
    return out


_COUPON_ID_RE = re.compile(r"couponId[：:]\s*([A-Za-z0-9_\-]+)")
_COUPON_CODE_RE = re.compile(r"couponCode[：:]\s*([A-Za-z0-9_\-]+)")
_HEADING_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+([^：:\n]{2,40})$")
# 说明文字里的章节标题，不是券名
_SKIP_HEADINGS = {"response structure", "original response", "api response information"}


def parse_coupon_markdown(text: Any) -> list[dict]:
    """从券类工具的 Markdown 原文里抽取券信息。

    实测存在**两种格式**，必须都认：

    格式 A（`query-store-coupons`）—— 带标识::

        - 满 30 减 6
          couponId：CP001
          couponCode：MCD30

    格式 B（`query-my-coupons`）—— **纯人类可读清单，完全没有 couponId**::

        ## 9.9元中杯冰美式
        - **优惠**: ¥9.9 (用券价格)
        - **有效期**: 2026-10-09 00:00-2026-10-15 23:59
        - **标签**: 到店专用、外送专用

    格式 B 的券**不能**当参数传回官方接口，只能用于展示与话术。

    ⚠️ 文本里**不含满减门槛与金额**，真实优惠额一律以 `calculate-price` 为准。
    """
    if not isinstance(text, str):
        return []

    coupons: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            name = heading.group(1).strip()
            if name.lower() in _SKIP_HEADINGS or not name:
                current = None
                continue
            # 券名一般为中文；纯 ASCII 的标题（如 "Response Structure"）一律跳过
            if all(ord(ch) < 128 for ch in name):
                current = None
                continue
            current = {"name": name}
            coupons.append(current)
            continue

        # 每个 "- 券名" 也都是一条新券（格式 A）
        bullet = _BULLET_RE.match(line)
        if bullet:
            current = {"name": bullet.group(1).strip()}
            coupons.append(current)
            continue

        id_match = _COUPON_ID_RE.search(line)
        if id_match:
            if current is None or current.get("couponId"):
                current = {}
                coupons.append(current)
            current["couponId"] = id_match.group(1)
            continue

        code_match = _COUPON_CODE_RE.search(line)
        if code_match and current is not None:
            current.setdefault("couponCode", code_match.group(1))
            continue

    return [c for c in coupons if c.get("couponId") or c.get("name")]


def to_yuan(value: Any) -> float:
    """`calculate-price` 系列的金额单位是**分**，这里换算成元。"""
    if value is None:
        return 0.0
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    # 真机出现过的写法：`"598.2"`、`"1,234.5"`、`"满 30 减 6"`。
    # 必须先去掉千分位再匹配，否则 `"1,234.5"` 会被读成 1.0 —— 金额差三个数量级，
    # 而且不会报错，属于最危险的那种静默错误。
    # 保留"取第一个数字"的宽松行为：券门槛字段常常夹在「满 30 减 6」里。
    text = str(value).replace(",", "").replace("，", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else 0.0


# --------------------------------------------------------------------------- #
# 二、报价规范化
# --------------------------------------------------------------------------- #

@dataclass
class QuoteLine:
    code: str
    name: str
    qty: int
    amount: float  # 元


@dataclass
class PriceBreakdown:
    """`calculate-price` 真实响应的规范化视图（金额已统一为**元**）。"""

    payable: float = 0.0
    original: float = 0.0
    discount: float = 0.0
    delivery_fee: float = 0.0
    packing_fee: float = 0.0
    lines: list[QuoteLine] = field(default_factory=list)
    take_way_list: list[dict] = field(default_factory=list)
    # 「再凑 X 元就能再省 Y 元」——来自响应里的 enjoyable.balance / realDiscount
    next_gap_yuan: float = 0.0
    next_saving_yuan: float = 0.0
    coupon_note: str = ""
    source: str = "mcp"

    @property
    def line_amount_sum(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)


def parse_calculate_price(raw: Any, *, source: str = "calculate-price") -> PriceBreakdown:
    """把 `calculate-price` 的返回规范成 `PriceBreakdown`。

    实测字段（全部为**分**，除 `realDiscount` 为元）：:

        price / originalPrice / discount / productPrice / productOriginalPrice
        deliveryPrice / packingPrice / tablewarePrice
        productList[]: productCode, productName, quantity, originalSubtotal, subtotal
        takeWayList[]: code, title, subtitle
        enjoyed  : amountType(1特价/2折扣/3立减), realDiscount(元), balance(分)
        enjoyable: 同上（"可享受但还没享受"的优惠，balance 就是门槛差额）

    ⚠️ 不要依赖任何字段存在：响应会按场景裁剪。
    """
    if not isinstance(raw, dict):
        return PriceBreakdown(source=source)

    if "_error" in raw:
        return PriceBreakdown(source=f"{source}:error", coupon_note=str(raw["_error"]))

    payload = raw.get("data") if "price" not in raw and "data" in raw else raw
    if not isinstance(payload, dict):
        return PriceBreakdown(source=source)

    lines: list[QuoteLine] = []
    for entry in payload.get("productList") or []:
        if not isinstance(entry, dict):
            continue
        lines.append(
            QuoteLine(
                code=str(entry.get("productCode") or ""),
                name=str(entry.get("productName") or entry.get("productCode") or ""),
                qty=int(_to_float(entry.get("quantity")) or 1),
                amount=to_yuan(entry.get("subtotal")),
            )
        )

    enjoyable = payload.get("enjoyable") if isinstance(payload.get("enjoyable"), dict) else {}
    next_gap = to_yuan(enjoyable.get("balance")) if enjoyable else 0.0
    # 不能写 float(...)：真机这个字段偶尔是字符串或带单位的文本，
    # 裸 float() 会抛 ValueError，而它的调用方在 try 之外，会把整场会议带崩。
    next_saving = _to_float(enjoyable.get("realDiscount")) if enjoyable else 0.0
    if next_gap <= 0:
        next_gap, next_saving = 0.0, 0.0

    return PriceBreakdown(
        payable=to_yuan(payload.get("price")),
        original=to_yuan(payload.get("originalPrice") or payload.get("productOriginalPrice")),
        discount=to_yuan(payload.get("discount")),
        delivery_fee=to_yuan(payload.get("deliveryPrice")),
        packing_fee=to_yuan(payload.get("packingPrice")),
        lines=lines,
        take_way_list=[
            t for t in (payload.get("takeWayList") or []) if isinstance(t, dict)
        ],
        next_gap_yuan=next_gap,
        next_saving_yuan=next_saving,
        source=source,
    )


# --------------------------------------------------------------------------- #
# 三、门店上下文
# --------------------------------------------------------------------------- #

@dataclass
class StoreContext:
    """点餐类工具都要的"三件套"：storeCode + orderType + beType。"""

    store_code: str = ""
    be_code: str = ""
    order_type: int = ORDER_TYPE_INSTORE
    be_type: int = BE_TYPE_PICKUP

    def args(self) -> dict:
        base: dict[str, Any] = {
            "storeCode": self.store_code,
            "orderType": self.order_type,
            "beType": self.be_type,
        }
        # 到店自取（beType=1）**不能**传 beCode，传了会报错
        if self.be_code and self.order_type != ORDER_TYPE_INSTORE:
            base["beCode"] = self.be_code
        return base


# --------------------------------------------------------------------------- #
# 四、客户端
# --------------------------------------------------------------------------- #

class DemoMCP:
    """离线演示客户端。

    返回的是**原始文本**（与真实服务器同构），而不是现成的对象——
    这样 demo 模式会真实地走一遍解析层。
    """

    mode = "demo"

    async def __aenter__(self) -> "DemoMCP":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(self, tool: str, args: dict | None = None, **_kw: Any) -> Any:
        return extract_text(demo_data.demo_tool_raw(tool, args))


class RealMCP:
    """真实麦当劳 MCP 客户端（Streamable HTTP）。"""

    mode = "live"

    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.token = (token or os.environ.get("MCD_MCP_TOKEN", "")).strip()
        if not self.token:
            raise MCPError(
                "缺少 MCD_MCP_TOKEN。请到 https://open.mcd.cn/mcp 申请后设置环境变量，"
                "或加 --demo 零配置体验完整流程。"
            )
        self.base_url = base_url
        self.timeout = timeout
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self.calls = 0

    async def __aenter__(self) -> "RealMCP":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        # 新会话 = 新的门店/券/账号上下文。旧缓存必须清掉，
        # 否则重连后（例如换了城市或门店）会一直吃到上一次会话的价格。
        self._cache.clear()
        try:
            from mcp import ClientSession
        except ImportError as exc:  # pragma: no cover
            raise MCPError("未安装 mcp 包，请先执行 pip install -r requirements.txt") from exc

        headers = {"Authorization": f"Bearer {self.token}"}
        self._stack = contextlib.AsyncExitStack()
        try:
            transport = await self._build_transport(headers)
            streams = await self._stack.enter_async_context(transport)
            self._session = await self._stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await self._session.initialize()
        except MCPError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise MCPError(f"连接麦当劳 MCP 失败：{_explain(exc)}") from exc

    async def _build_transport(self, headers: dict[str, str]):
        """兼容 mcp 2.x 与 1.x 的 Streamable HTTP 传输层。

        mcp 2.3.0 把 `streamablehttp_client` 拆成了
        `create_mcp_http_client` + `streamable_http_client`，必须双版本兼容。
        """
        try:
            from mcp.client.streamable_http import (  # type: ignore[attr-defined]
                create_mcp_http_client,
                streamable_http_client,
            )
        except ImportError:
            from mcp.client.streamable_http import streamablehttp_client

            return streamablehttp_client(self.base_url, headers=headers)

        http_client = create_mcp_http_client(headers=headers)
        await self._stack.enter_async_context(http_client)  # type: ignore[union-attr]
        return streamable_http_client(self.base_url, http_client=http_client)

    async def close(self) -> None:
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call(self, tool: str, args: dict | None = None, *, use_cache: bool = True) -> Any:
        args = args or {}
        # 写操作一律绕过缓存，不看调用方传了什么
        if tool in _MUTATING_TOOLS:
            use_cache = False
        cache_key = f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

        if use_cache:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
                # 回深拷贝，不回缓存本体：调用方（解包、规范化、渲染）经常就地
                # 改写拿到的 dict。回本体等于把它们的手脚写进缓存，
                # 后续所有命中都带着被污染的数据，而且完全没有痕迹。
                return copy.deepcopy(cached[1])

        if self._session is None:
            raise MCPError("MCP 会话未建立，请先 await connect()")

        last_error: Exception | None = None
        for attempt in range(MAX_RETRY):
            try:
                self.calls += 1
                result = await self._session.call_tool(tool, args)
                text = "".join(
                    part.text
                    for part in (getattr(result, "content", None) or [])
                    if getattr(part, "text", None)
                )
                if not text:
                    raise MCPError(f"{tool} 返回空内容")

                value = extract_text(text)
                if use_cache:
                    self._cache[cache_key] = (time.time(), copy.deepcopy(value))
                return value
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < MAX_RETRY - 1:
                    # 429 限流退避久一点，其余快速重试
                    await _sleep(2.0 * (attempt + 1) if "429" in str(exc) else 0.6 * (attempt + 1))
                    continue
                break

        raise MCPError(f"调用 {tool} 失败：{last_error}")


def _explain(exc: BaseException) -> str:
    """把底层连接异常翻译成人话。"""
    text = str(exc) or type(exc).__name__
    if "401" in text or "403" in text:
        return "Token 被拒绝（401/403）。请确认 MCD_MCP_TOKEN 是 MCP Token 而非 LLM API Key。"
    if "429" in text:
        return "请求过于频繁（429），请稍后重试。"
    if "Timeout" in text or "timeout" in text:
        return "连接超时，请检查网络能否访问 mcp.mcd.cn。"
    return text[:180]


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- #
# 五、一次会话的完整快照
# --------------------------------------------------------------------------- #

class McdData:
    """一次会话所需的全部麦当劳数据快照。"""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.ctx = StoreContext()
        self.store: dict = {}
        self.menu: list[MenuItem] = []
        self.coupons: list[Coupon] = []
        self.account: dict = {}
        self.campaigns: list[str] = []
        self.now: dict = {}
        self.warnings: list[str] = []
        self.take_way_list: list[dict] = []
        self.price_calls = 0

    @property
    def is_demo(self) -> bool:
        return getattr(self.client, "mode", "live") == "demo"

    @property
    def store_name(self) -> str:
        return str(self.store.get("storeName") or self.store.get("name") or "")

    def by_code(self, code: str) -> MenuItem | None:
        for item in self.menu:
            if item.code == code:
                return item
        return None


async def load_data(
    client: Any,
    *,
    city: str | None = None,
    keyword: str | None = None,
    order_type: int = ORDER_TYPE_INSTORE,
    be_type: int = BE_TYPE_PICKUP,
) -> McdData:
    """按依赖顺序采集一次完整快照。单点失败不拖垮全局。"""
    data = McdData(client)
    data.ctx.order_type = order_type
    data.ctx.be_type = be_type
    data.now = await _safe(client, "now-time-info", {}, data, {}) or {}

    # 实测：query-nearby-stores 必须**同时**给 city 与 keyword，
    # 否则报 600058「城市名或者关键词不能为空」。
    store_args: dict[str, Any] = {"beType": be_type, "searchType": 2}
    if city and keyword:
        store_args["city"] = city
        store_args["keyword"] = keyword
    else:
        # 缺一不可，退化成"我的收藏餐厅"，让用户至少能跑起来
        store_args["searchType"] = 1
        if city:
            store_args["city"] = city
        if keyword:
            store_args["keyword"] = keyword

    stores = await _safe(client, "query-nearby-stores", store_args, data, None)
    store_list = stores if isinstance(stores, list) else (stores or {}).get("stores") or []
    if not store_list and isinstance(stores, dict):
        store_list = stores.get("data") or []
    if isinstance(store_list, list) and store_list:
        data.store = store_list[0]
        data.ctx.store_code = str(data.store.get("storeCode") or "")
        data.ctx.be_code = str(data.store.get("beCode") or "")

    if not data.ctx.store_code and data.is_demo:
        data.store = demo_data.DEMO_STORE
        data.ctx.store_code = str(data.store.get("storeCode") or "")

    if data.ctx.store_code:
        _load_menu(data, await _safe(client, "query-meals", data.ctx.args(), data, None))
    else:
        data.warnings.append("未取到门店：请用 --city 与 --keyword 指定（两者都要给）")

    nutrition = parse_nutrition_toon(await _safe(client, "list-nutrition-foods", {}, data, None))
    for item in data.menu:
        item.nutrition = nutrition.get(item.name, {})

    if not data.is_demo and data.ctx.store_code:
        await _safe(client, "auto-bind-coupons", {}, data, None)

    _load_coupons(data, await _safe(client, "query-my-coupons", {}, data, None))
    if data.ctx.store_code:
        _load_coupons(
            data, await _safe(client, "query-store-coupons", data.ctx.args(), data, None)
        )
    _load_coupons(data, await _safe(client, "available-coupons", {}, data, None))

    data.account = await _safe(client, "query-my-account", {}, data, {}) or {}
    data.campaigns = _load_campaigns(await _safe(client, "campaign-calendar", {}, data, None))
    return data


def _load_menu(data: McdData, meals: Any) -> None:
    """解析 `query-meals`。

    实测结构::

        {"categories": [{"name": "人气热卖", "meals": [{"code": "..", "tags": [..]}]}],
         "meals": {"9900005456": {"name": "..", "currentPrice": "38", ...}}}

    注意 `currentPrice` 是**元**（与 calculate-price 的分不同）。
    """
    if not isinstance(meals, dict):
        return
    payload = meals.get("data") if "categories" not in meals else meals
    if not isinstance(payload, dict):
        return

    detail_map = payload.get("meals") or payload.get("mealMap") or {}
    categories = payload.get("categories") or []
    if isinstance(categories, dict):
        categories = [{"name": k, "meals": v} for k, v in categories.items()]

    seen: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            continue
        cat_name = str(category.get("name") or "").replace("\n", " ").strip()
        for raw in category.get("meals") or []:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or raw.get("productCode") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            detail = detail_map.get(code) if isinstance(detail_map, dict) else None
            detail = detail if isinstance(detail, dict) else {}
            data.menu.append(
                MenuItem(
                    code=code,
                    name=str(detail.get("name") or raw.get("name") or code),
                    # currentPrice 是**元**（与 calculate-price 的分不同）；
                    # 真机把价格放在 meals 明细映射里，但分类项自带价格时也要认
                    price=_to_float(
                        detail.get("currentPrice")
                        or detail.get("price")
                        or raw.get("currentPrice")
                        or raw.get("price")
                    ),
                    category=cat_name,
                    tags=[str(t) for t in (raw.get("tags") or detail.get("tags") or []) if t is not None],
                )
            )


def _load_coupons(data: McdData, raw: Any) -> None:
    """券可能来自 JSON 数组，也可能来自 Markdown 文本。"""
    items: list[dict] = []

    if isinstance(raw, dict) and "_markdown" in raw:
        items = parse_coupon_markdown(raw["_markdown"])
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict):
        nested = raw.get("coupons") or raw.get("data") or raw.get("list")
        if isinstance(nested, list):
            items = [x for x in nested if isinstance(x, dict)]

    seen = {c.coupon_id for c in data.coupons}
    for entry in items:
        name = str(entry.get("name") or "").strip()
        coupon_id = str(entry.get("couponId") or entry.get("id") or "").strip()
        # 没有 id 的券（真机 query-my-coupons 就是这样）只能展示，不能传参
        kind = "mcp" if coupon_id else "catalog"
        if not coupon_id:
            if not name:
                continue
            coupon_id = f"name:{name}"
        if coupon_id in seen:
            continue
        seen.add(coupon_id)
        # Markdown 里没有满减门槛/金额，这里只登记标识；真实优惠额以 calculate-price 为准
        data.coupons.append(
            Coupon(
                coupon_id=coupon_id,
                name=name or str(entry.get("couponCode") or coupon_id),
                threshold=_to_float(entry.get("threshold")),
                discount=_to_float(entry.get("discount")),
                kind=kind,
            )
        )


def _load_campaigns(raw: Any) -> list[str]:
    if isinstance(raw, dict) and "_markdown" in raw:
        titles = re.findall(r"\*\*活动标题\*\*[：:]\s*(.+)", raw["_markdown"])
        if not titles:
            titles = re.findall(r"^#{1,4}\s*(.+)$", raw["_markdown"], flags=re.M)
        return [t.strip() for t in titles[:6]]
    if isinstance(raw, dict):
        items = raw.get("campaigns") or raw.get("data") or []
        if isinstance(items, list):
            # 这里的守卫曾经写成 `isinstance(x, (dict, str))`，放行了 str，
            # 但下面按 dict 调 `x.get(...)` —— 字符串列表会直接 AttributeError。
            return [
                str(x.get("name") or x) if isinstance(x, dict) else str(x)
                for x in items
                if x
            ][:6]
    return []


async def _safe(client: Any, tool: str, args: dict, data: McdData, fallback: Any) -> Any:
    """单点失败不拖垮全局：记录警告并返回兜底值（但绝不伪造数据）。"""
    try:
        result = await client.call(tool, args)
    except Exception as exc:  # noqa: BLE001
        data.warnings.append(f"{tool}: {str(exc)[:70]}")
        return fallback
    if isinstance(result, dict) and "_error" in result:
        data.warnings.append(f"{tool}: {result['_error']}")
        return fallback
    return fallback if result is None else result
