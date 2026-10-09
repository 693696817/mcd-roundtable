"""券约束下的凑单求解器。

职责边界（这点很重要）：

* 求解器负责**生成候选组合**并**调用 calculate-price 真实试算**；
* LLM 只负责"吵"，**不负责算钱**。

流程：
    候选池 → 枚举组合 → 预算/硬约束过滤 → 本地打分取 Top-K
      → 对 Top-K 逐个调用 calculate-price 真实试算
      → 用**真实金额**重新打分排序 → 取实际最优

⚠️ 实测坑（已在此处理）：
  * items 元素的字段名是 `productCode`，写成 `code`/`mealCode` 不报错，
    只会静默返回 `price=0`；
  * 返回金额单位是**分**。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .mcp_client import parse_calculate_price
from .models import Constraint, Coupon, LineItem, MenuItem, PriceQuote, Proposal, Trial

MAX_POOL = 16
MAX_COMBOS = 900
TOP_K = 4
# 「委员提案加分」：让圆桌的讨论真的能影响结果，
# 否则求解器会给出一个谁都没提过的组合，"会议"就只是个装饰。
PROPOSAL_BONUS = 7.0
# 官方营养表没覆盖该商品时的**排序中性值**（只影响排序，绝不作为营养数据展示）
UNKNOWN_DENSITY = 8.0


@dataclass
class Plan:
    items: list[tuple[MenuItem, int]]
    local_total: float
    score: float

    @property
    def codes(self) -> list[str]:
        return [i.code for i, _ in self.items]

    @property
    def label(self) -> str:
        """紧凑的组合描述。

        用中文顿号连接、数量统一后置，是为了让"N 人 × M 种"也能塞进一行——
        表格里换行的组合名看起来像排版事故。
        """
        names = "、".join(item.name for item, _ in self.items)
        qtys = {q for _, q in self.items}
        if len(qtys) == 1:
            n = next(iter(qtys))
            return f"{names} ×{n}" if n > 1 else names
        return names + "（数量不等）"


class Optimizer:
    def __init__(self, data, constraint: Constraint, objective: str = "cost") -> None:
        self.data = data
        self.constraint = constraint
        self.objective = objective
        self._proposal_codes: set[str] = set()

    # ---------------------------------------------------------------- 候选生成

    def build_pool(self, proposals: list[Proposal]) -> list[MenuItem]:
        """候选池 = 各委员提案里出现过的餐品 ∪ 菜单里"性价比密度"最高的餐品。

        ⚠️ 委员提案的餐品**必须无条件保送进池**。
        早期版本把它们和全菜单一起按密度排序再截断，而官方营养表没覆盖的商品
        密度为 0，会被直接挤出候选池 —— 结果是"五个委员吵了半天，
        求解器给出了一个谁都没提过的汉堡"。
        """
        from_proposals: dict[str, MenuItem] = {}
        for proposal in proposals:
            for line in proposal.line_items:
                item = self.data.by_code(line.code)
                if item is not None and not item.is_condiment:
                    from_proposals[item.code] = item
        self._proposal_codes = set(from_proposals)

        others: list[MenuItem] = []
        for item in self.data.menu:
            if item.price <= 0 or item.is_condiment or item.code in from_proposals:
                continue
            others.append(item)
        others.sort(key=self._value_density, reverse=True)

        room = max(0, MAX_POOL - len(from_proposals))
        pool = list(from_proposals.values()) + others[:room]
        if not pool:  # 极端情况：菜单全是调味料，宁可不筛也不要空手而归
            pool = [m for m in self.data.menu if m.price > 0][:MAX_POOL]
        return pool[:MAX_POOL]

    def _value_density(self, item: MenuItem) -> float:
        # 官方营养表没覆盖这个商品。排序必须给个中性值，
        # 否则它会被挤到队尾彻底出局。注意：这只是**排序键**，不是营养数据。
        if item.kcal <= 0:
            return UNKNOWN_DENSITY
        if self.objective == "protein":
            return item.protein_per_yuan * 10 + item.protein
        if self.objective == "balanced":
            return (item.kcal / max(item.price, 1)) - item.sodium / 300.0
        return item.kcal / max(item.price, 1)

    def enumerate_plans(self, pool: list[MenuItem]) -> list[Plan]:
        people = max(1, self.constraint.people)
        budget = self.constraint.effective_budget_total
        plans: list[Plan] = []

        for size in (1, 2, 3, 4):
            if size > len(pool):
                break
            for combo in itertools.combinations(pool, size):
                total = sum(item.price * people for item in combo)
                if budget is not None and total > budget * 1.08:
                    continue
                score = self._score(combo, total)
                if score <= -1e9:
                    continue
                plans.append(Plan([(m, people) for m in combo], round(total, 2), score))
                if len(plans) > MAX_COMBOS:
                    break
            if len(plans) > MAX_COMBOS:
                break

        plans.sort(key=lambda p: p.score, reverse=True)
        return plans[:TOP_K]

    def _score(self, combo: tuple[MenuItem, ...], total: float) -> float:
        people = max(1, self.constraint.people)
        c = self.constraint

        if any(not m.code for m in combo):
            return -1e10

        # 官方营养表**不覆盖全部商品**（套餐常常查不到）。
        # 数据不全时绝不能拿 0 当成"真实热量"去做硬性淘汰，
        # 否则这些商品会被整类误杀 —— 这是实测踩到的最隐蔽的坑。
        has_kcal = all(m.kcal > 0 for m in combo)

        kcal = sum(m.kcal * people for m in combo)
        protein = sum(m.protein * people for m in combo)
        sodium = sum(m.sodium * people for m in combo)
        per_kcal = kcal / people
        per_protein = protein / people
        budget = c.effective_budget_total

        # ---- 硬约束 ----
        # 必须含主食：否则求解器会退化成"只点一杯可乐"这种看起来最省钱的废解。
        # 用归一化 kind，真机的分类名是中文（"人气热卖"），拿类目名判断会永远失败。
        if not any(m.is_main for m in combo):
            return -1e10
        if budget is not None and total > budget:
            return -1e10
        if has_kcal:
            if c.max_kcal_per_person is not None and per_kcal > c.max_kcal_per_person:
                return -1e10
            if per_kcal < 200:
                return -1e10

        score = 0.0

        # ---- 0. 委员提案加分：让圆桌的讨论真的影响结果 ----
        score += PROPOSAL_BONUS * sum(1 for m in combo if m.code in self._proposal_codes)

        # ---- 1. 营养项（数据缺失时只做轻微扣分，不做淘汰） ----
        if not has_kcal:
            score -= 3.0
        else:
            # 蛋白质缺口：软约束。不硬性淘汰（可能无解），但强烈扣分
            if c.min_protein_per_person is not None:
                gap = c.min_protein_per_person - per_protein
                if gap > 0:
                    score -= gap * 3.2
            # 饱腹度：人均 350-750 kcal 是舒适区，低于 300 明显不够吃
            if per_kcal < 300:
                score -= (300 - per_kcal) * 0.14
            score += min(per_kcal, 750.0) * 0.035

        # ---- 2. 预算利用：给了预算就尽量吃满，而不是越便宜越好 ----
        if budget:
            score -= abs(1.0 - total / budget) * 22.0
        elif has_kcal:
            score += (kcal / max(total, 1.0)) * 0.9

        # ---- 3. 券的边际收益（本地估算，最终以真实试算为准） ----
        for coupon in self.data.coupons:
            if not coupon.threshold:
                continue
            if total >= coupon.threshold:
                score += coupon.discount * 1.2
            elif 0 < coupon.threshold - total <= 12:
                score += 0.8

        # ---- 4. 目标加权 ----
        if self.objective == "protein":
            score += per_protein * 2.6
        elif self.objective == "balanced":
            score += per_protein * 1.5 - sodium / 220.0
        elif has_kcal:
            score -= sodium / 700.0

        return score

    # ------------------------------------------------------------------ 真实试算

    async def price_plan(self, plan: Plan) -> PriceQuote:
        """调用官方 calculate-price。失败时返回**显式标注**的本地兜底。"""
        args = {
            **self.data.ctx.args(),
            "items": [{"productCode": item.code, "quantity": qty} for item, qty in plan.items],
            "needTableware": False,
        }
        raw = None
        try:
            raw = await self.data.client.call("calculate-price", args)
            self.data.price_calls += 1
        except Exception as exc:  # noqa: BLE001
            self.data.warnings.append(f"calculate-price 调用失败：{str(exc)[:60]}")

        # 解析失败必须**显式降级**，绝不能往外抛：
        # 调用方（optimize / optimize_topup）是在循环里逐方案试算的，
        # 一条脏数据让整场会议崩掉，用户看到的是 Traceback 而不是结论。
        # 降级后 source 会标成 local-fallback，报告里明确写"仅本地推算"。
        try:
            breakdown = parse_calculate_price(raw)
        except Exception as exc:  # noqa: BLE001
            self.data.warnings.append(f"calculate-price 返回值无法解析：{str(exc)[:60]}")
            return self.local_quote(plan)

        if breakdown.payable > 0 and breakdown.lines:
            if breakdown.take_way_list:
                self.data.take_way_list = breakdown.take_way_list
            # 用官方返回的真实品名与真实小计，覆盖本地菜单价
            lines = [LineItem(l.code, l.name, l.qty, round(l.amount / max(l.qty, 1), 2))
                     for l in breakdown.lines]
            return PriceQuote(
                lines=lines,
                subtotal=round(breakdown.line_amount_sum, 2),
                discount=breakdown.discount,
                delivery_fee=breakdown.delivery_fee,
                packing_fee=breakdown.packing_fee,
                payable=breakdown.payable,
                people=self.constraint.people,
                applied_coupon="官方自动择优" if breakdown.discount > 0 else None,
                source="calculate-price",
                take_way_list=breakdown.take_way_list,
                next_gap=breakdown.next_gap_yuan,
                next_saving=breakdown.next_saving_yuan,
                note=breakdown.coupon_note,
            )
        return self.local_quote(plan)

    async def verify(self, plans: list[Plan]) -> list[tuple[Plan, PriceQuote]]:
        """对候选逐个真实试算，并按**真实金额**重新排序。"""
        out: list[tuple[Plan, PriceQuote]] = []
        for plan in plans:
            out.append((plan, await self.price_plan(plan)))
        # 真实金额才是最终裁判：本地分 + 真实省下的钱
        out.sort(key=lambda pq: pq[0].score + pq[1].discount * 3.0, reverse=True)
        return out

    def to_trials(self, verified: list[tuple[Plan, PriceQuote]], winner: int = 0) -> list[Trial]:
        trials: list[Trial] = []
        for index, (plan, quote) in enumerate(verified):
            trials.append(
                Trial(
                    label=plan.label,
                    payable=quote.payable,
                    original=round(quote.subtotal + quote.delivery_fee + quote.packing_fee, 2),
                    discount=quote.discount,
                    per_person=quote.per_person,
                    adopted=index == winner,
                    note="" if quote.is_official else "本地兜底价",
                )
            )
        return trials

    # ------------------------------------------------------------------ 精算追加

    async def optimize_topup(
        self, plan: Plan, quote: PriceQuote, *, max_try: int = 4
    ) -> tuple[Plan, PriceQuote, Trial | None, str]:
        """「再多花一点反而更便宜」——主动去找这种反直觉的解。

        原理：官方 `calculate-price` 会告诉我们距离下一档优惠还差多少钱
        （`enjoyable.balance`）。如果补一个便宜小食就能跨过门槛，
        那么 **加东西 == 减总价**。这是人手点单几乎不可能算出来的，
        也正是这个项目存在的理由。

        返回 `(方案, 报价, 追加记录, 说明文字)`；没找到就原样返回。
        """
        if quote.next_gap <= 0 or quote.next_saving <= 0:
            return plan, quote, None, ""

        planned = {item.code for item, _ in plan.items}
        # 只考虑「单价 <= 门槛差额 + 一点余量」的候选，避免为了省 6 块多花 20 块
        ceilings = quote.next_gap * 1.6
        # 三条硬性排除，缺一条就会推荐出用户明确不要的东西：
        #   ① 调料/加购项（蘸酱、纸袋）不能单独作为跨档商品，官方试算会直接拒；
        #   ② 用户说了「不要/不吃」的品名（忌口是安全项，不是偏好项）；
        #   ③ 已经点过的，避免同一件重复堆量冒充"跨档"。
        adds = [
            item
            for item in self.data.menu
            if item.code not in planned
            and not item.is_condiment
            and not self.constraint.is_avoided(item.name, *item.tags)
            and 0 < item.price <= ceilings
        ]
        adds.sort(key=lambda m: (m.is_main, m.price))  # 优先用便宜的非主食去补门槛
        if not adds:
            return plan, quote, None, ""

        best: tuple[Plan, PriceQuote] | None = None
        for item in adds[:max_try]:
            candidate = Plan(
                list(plan.items) + [(item, 1)],
                round(plan.local_total + item.price, 2),
                plan.score,
            )
            try:
                cand_quote = await self.price_plan(candidate)
            except Exception:  # noqa: BLE001
                continue
            if not cand_quote.is_official:
                continue
            # 必须是"加了东西，实付反而更低"
            if cand_quote.payable < quote.payable - 0.01:
                if best is None or cand_quote.payable < best[1].payable:
                    best = (candidate, cand_quote)

        if best is None:
            return plan, quote, None, ""

        new_plan, new_quote = best
        added = new_plan.items[-1][0]
        saved = round(quote.payable - new_quote.payable, 2)
        trial = Trial(
            label=f"{new_plan.label}（精算追加）",
            payable=new_quote.payable,
            original=round(new_quote.subtotal + new_quote.delivery_fee + new_quote.packing_fee, 2),
            discount=new_quote.discount,
            per_person=new_quote.per_person,
            adopted=True,
            note=f"加 {added.name} 反而少花 ¥{saved:.1f}",
        )
        note = (
            f"⚠️ 精算发现反直觉解：加一份「{added.name}」（¥{added.price:.1f}）跨过优惠门槛，"
            f"实付反而从 ¥{quote.payable:.1f} 降到 ¥{new_quote.payable:.1f}，少花 ¥{saved:.1f}。"
        )
        return new_plan, new_quote, trial, note

    def local_quote(self, plan: Plan) -> PriceQuote:
        """真实试算不可用时的本地兜底。

        结果会被标记为 `local-fallback`，展示时必须显式标注 —— **绝不冒充官方价格**。
        """
        subtotal = plan.local_total
        best: Coupon | None = None
        for coupon in self.data.coupons:
            if coupon.threshold and coupon.applies_to(subtotal):
                if best is None or coupon.discount > best.discount:
                    best = coupon
        discount = best.discount if best else 0.0
        fee = 0.0 if self.constraint.takeout else 9.0
        return PriceQuote(
            lines=[LineItem(i.code, i.name, q, i.price) for i, q in plan.items],
            subtotal=round(subtotal, 2),
            discount=round(discount, 2),
            delivery_fee=fee,
            payable=round(subtotal - discount + fee, 2),
            people=self.constraint.people,
            applied_coupon=best.name if best else None,
            source="local-fallback",
        )
