"""领域模型。

刻意保持扁平：所有跨模块流转的数据都用这里的 dataclass，
这样 LLM 大脑与规则大脑可以输出同构结果，求解器与渲染层不必区分来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 真机 `query-meals` 返回的分类名是**中文且不稳定**（"人气热卖" / "超值套餐" / …），
# 所以不能拿分类名当品类判断依据。这里按优先级做关键词归一化。
# ⚠️ 顺序有意义：套餐必须排在汉堡前面，否则"板烧鸡腿堡套餐"会被判成汉堡。
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("combo", ("套餐", "随心配", "组合", "四件套", "双人餐", "分享桶")),
    ("drink", ("饮品", "饮料", "可乐", "咖啡", "牛奶", "茶", "果汁", "矿泉水", "气泡水")),
    ("side", ("小食", "配餐", "薯", "鸡翅", "鸡块", "鸡排", "玉米", "派", "沙拉", "冰淇淋", "甜品", "麦旋风")),
    ("burger", ("汉堡", "堡", "巨无霸", "双吉", "吉士", "卷")),
)

# 调味料/耗材：它们也是合法商品，但拿来凑一份"餐"没有意义，会在提案与求解时排除
_CONDIMENT_KEYS = ("酱", "调味", "蘸料", "纸巾", "餐具", "打包袋", "手提袋", "购物袋")


@dataclass
class MenuItem:
    code: str
    name: str
    price: float
    category: str = ""
    tags: list[str] = field(default_factory=list)
    nutrition: dict[str, float] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """归一化品类：burger / combo / side / drink / other。

        实测踩坑：真机分类名是中文（"人气热卖"、"超值套餐"），
        早期代码用 `category in ("burger", "combo")` 判断，**在真实模式下永远不成立**，
        导致求解器认为"菜单里没有主食"，退化成只点一杯饮料。
        """
        haystack = f"{self.category} {self.name} {' '.join(self.tags)}"
        for kind, keys in _KIND_RULES:
            if any(key in haystack for key in keys):
                return kind
        return "other"

    @property
    def is_main(self) -> bool:
        """是不是主食（求解器的硬约束：一份方案必须含主食）。"""
        return self.kind in ("burger", "combo")

    @property
    def is_condiment(self) -> bool:
        """调味料 / 耗材。真机菜单里确实有"韩式烟熏芝士风味酱"这类商品，
        但它们凑不出一顿饭，必须从提案与求解的候选里剔除。"""
        return any(key in self.name for key in _CONDIMENT_KEYS)

    @property
    def kcal(self) -> float:
        return float(self.nutrition.get("energyKcal", 0) or 0)

    @property
    def tagset(self) -> set[str]:
        return set(self.tags)

    @property
    def protein(self) -> float:
        return float(self.nutrition.get("protein", 0) or 0)

    @property
    def sodium(self) -> float:
        return float(self.nutrition.get("sodium", 0) or 0)

    @property
    def fat(self) -> float:
        return float(self.nutrition.get("fat", 0) or 0)

    @property
    def carbohydrate(self) -> float:
        return float(self.nutrition.get("carbohydrate", 0) or 0)

    @property
    def calcium(self) -> float:
        return float(self.nutrition.get("calcium", 0) or 0)

    @property
    def protein_per_yuan(self) -> float:
        return round(self.protein / self.price, 3) if self.price else 0.0


@dataclass
class Coupon:
    """一张券。

    ⚠️ 实测：`query-my-coupons` 返回的是一份**纯人类可读清单**，
    只有标题（如「9.9元中杯冰美式」），**完全没有 couponId / couponCode**；
    只有 `query-store-coupons` 才带标识。

    所以这里分两类：
      * ``kind="mcp"``     —— 拿到了真实 couponId，可以传回官方接口
      * ``kind="catalog"`` —— 只有名字，只能用于展示与话术，**不能当作参数传回去**
    """

    coupon_id: str
    name: str
    threshold: float = 0.0
    discount: float = 0.0
    kind: str = "mcp"

    @property
    def has_id(self) -> bool:
        return self.kind == "mcp" and bool(self.coupon_id) and not self.coupon_id.startswith("name:")

    def applies_to(self, subtotal: float) -> bool:
        return bool(self.threshold) and subtotal >= self.threshold


@dataclass
class Constraint:
    """从自然语言里解析出来的结构化约束。"""

    raw_query: str
    people: int = 1
    budget_total: float | None = None
    budget_per_person: float | None = None
    keywords: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    max_kcal_per_person: float | None = None
    min_protein_per_person: float | None = None
    takeout: bool = True  # 默认到店自取；用户明确要外送时才置为 False

    @property
    def effective_budget_total(self) -> float | None:
        if self.budget_total is not None:
            return self.budget_total
        if self.budget_per_person is not None:
            return self.budget_per_person * self.people
        return None

    def is_avoided(self, *texts: str) -> bool:
        """判断某件商品是否命中用户明确说出的忌口。

        统一收在这里，是因为"提案筛选"和"凑单候选筛选"必须用同一套判据：
        两边只要有一边漏了，用户就会在最后的小票上看到自己说不要的东西。
        匹配用子串（`"辣" in "麦辣鸡腿堡"`），因为中文忌口词本身就是片段。
        """
        blob = "".join(t for t in texts if t)
        return any(word and word in blob for word in self.avoid)


@dataclass
class LineItem:
    code: str
    name: str
    qty: int
    unit_price: float

    @property
    def amount(self) -> float:
        return round(self.unit_price * self.qty, 2)


@dataclass
class PriceQuote:
    """真实试算结果。**金额一律以这里为准，不接受模型估算。**

    source 字段是诚信标记：
      - `calculate-price` : 来自麦当劳官方接口，可信
      - `local-fallback`  : 接口不可用时的本地推算，展示时必须显式标注
    """

    lines: list[LineItem]
    subtotal: float
    discount: float
    delivery_fee: float
    payable: float
    people: int = 1
    applied_coupon: str | None = None
    source: str = "mcp"
    packing_fee: float = 0.0
    # "到店/得来速"下单必传 takeWayCode，取值只能从这份列表里挑
    take_way_list: list[dict] = field(default_factory=list)
    # 凑单提示：再买 next_gap 元，可再省 next_saving 元
    next_gap: float = 0.0
    next_saving: float = 0.0
    note: str = ""

    @property
    def per_person(self) -> float:
        return round(self.payable / self.people, 2) if self.people else self.payable

    @property
    def is_official(self) -> bool:
        return self.source.startswith("calculate-price")

    def take_way_code(self, prefer: str = "take-in-store") -> str:
        """挑一个 takeWayCode：优先外带，退化为第一个可选值。"""
        codes = [str(t.get("code") or "") for t in self.take_way_list]
        if prefer in codes:
            return prefer
        return codes[0] if codes else ""


@dataclass
class Trial:
    """一次真实试算的记录，用于把"我们真的算过多少钱"摊开给用户看。"""

    label: str
    payable: float
    original: float
    discount: float
    per_person: float = 0.0
    adopted: bool = False
    note: str = ""


@dataclass
class Proposal:
    """某位委员的提案。"""

    role_key: str
    role_name: str
    line_items: list[LineItem]
    pitch: str
    rationale: str = ""
    amount: float = 0.0


@dataclass
class Rebuttal:
    """质询轮里的一次发言。"""

    role_key: str
    role_name: str
    target_key: str | None
    target_name: str | None
    text: str


@dataclass
class Verdict:
    quote: PriceQuote
    adopted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    summary: str = ""
    per_person: float = 0.0
    trials: list[Trial] = field(default_factory=list)
    headcount_label: str = ""
    why_not_cheapest: str = ""
