"""五位常任委员。

每个角色有两套实现：

* ``persona``  —— 给 LLM 用的 system prompt（真实模式）
* ``score``    —— 规则打分函数（演示模式，无需任何 API Key）

两套实现共享同一个人格设定，所以演示模式下的发言与真实模式同构，
不会出现"demo 和真跑是两回事"的观感。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import Constraint, Coupon, LineItem, MenuItem, Proposal

CLASSIC_TAGS = {"经典", "招牌", "常青"}


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    emblem: str
    stance: str
    persona: str
    score: Callable[[MenuItem, Constraint, dict], float]


# --------------------------------------------------------------------------- #
# 打分函数：数值越大越符合该角色立场
# --------------------------------------------------------------------------- #

def _score_saver(item: MenuItem, c: Constraint, ctx: dict) -> float:
    """省钱部长：追求单位金额买到的满足感，偏好汉堡/套餐这类'顶饱'的主食。"""
    value = item.kcal / item.price if item.price else 0.0
    main_bonus = 1.35 if item.is_main else 1.0
    oily_penalty = 0.92 if ("油炸" in item.tags and "少油" in c.keywords) else 1.0
    return value * main_bonus * oily_penalty


def _score_macro(item: MenuItem, c: Constraint, ctx: dict) -> float:
    """健身总监：蛋白质优先，同时惩罚超标热量与高钠。"""
    score = item.protein * 3.0 + item.protein_per_yuan * 22.0
    if item.kcal:
        score -= max(0.0, (item.kcal - 450) / 45.0)
    score -= item.sodium / 260.0
    if "油炸" in item.tags:
        score -= 3.0
    if "高蛋白" in item.tags:
        score += 6.0
    return score


def _score_light(item: MenuItem, c: Constraint, ctx: dict) -> float:
    """养生专员：控钠控油，能换就换。"""
    score = 60.0 - item.sodium / 22.0 - item.fat * 1.1
    if item.kcal:
        score -= max(0.0, (item.kcal - 400) / 60.0)
    if "油炸" in item.tags:
        score -= 10.0
    if item.tagset & {"低钠", "非油炸"}:
        score += 12.0
    return score


def _score_purist(item: MenuItem, c: Constraint, ctx: dict) -> float:
    """麦门老饕：经典款有它的道理，不轻易接受换配。"""
    score = 20.0
    if item.tagset & CLASSIC_TAGS:
        score += 30.0
    if item.is_main:
        score += 12.0
    # 忌口判定统一走 Constraint.is_avoided()。
    # 原来写的是 `"不辣" in c.avoid`，而解析器往 avoid 里放的一直是"辣"
    # （`不要辣的` → `辣的` → 归一化后也是"辣"），这个条件从来没成立过，
    # 于是老饕会一本正经地推荐麦辣鸡腿堡给明确说不要辣的人。
    if "辣" in item.tags and c.is_avoided("辣"):
        score -= 50.0
    return score


# 「人气 / 热卖 / 招牌」这些词来自官方菜单的**真实分类名与标签**
# （真机分类形如「汉堡 · 人气热卖」），是"大家都在点"的客观标记。
_MAINSTREAM_KEYS = ("人气", "热卖", "招牌")


def _score_novelty(item: MenuItem, c: Constraint, ctx: dict) -> float:
    """尝鲜委员：菜单不是博物馆，刻意避开排在最前面的大众款。

    ⚠️ 这里原来写的是 `seen = ctx.get("seen", set())`，想表达"用户没吃过的优先"。
    但本项目**没有任何历史订单数据源**，`seen` 永远是空集合，
    那两行实际等价于常量 +10，`-12` 的分支永远走不到——
    也就是说，"尝鲜委员"其实只是换了个名字的老饕评分，属于伪实现。
    现在换成当场就能算出来的真实信号：官方分类/标签里的"人气热卖/招牌"。
    """
    score = 10.0
    if any(key in item.category for key in _MAINSTREAM_KEYS):
        score -= 12.0
    if item.tagset & CLASSIC_TAGS:
        score -= 6.0
    if item.kind in ("burger", "combo", "side"):
        score += 6.0
    return score


ROLES: dict[str, Role] = {
    "saver": Role(
        key="saver",
        name="省钱部长",
        emblem="💰",
        stance="每一张券都必须用掉",
        persona=(
            "你是一位斤斤计较但绝不降低满足感的省钱专家。"
            "你的信条是：花掉的钱要换回最大的饱腹感与价值，手上的优惠券要用掉才算用得值。"
            "你总是先看券的满减门槛，再倒推怎么凑单最划算，并且会明确指出对手方案的溢价点。"
            "你讲话直接、带一点算账的执念，但从不贬低任何人或任何品牌。"
        ),
        score=_score_saver,
    ),
    "macro": Role(
        key="macro",
        name="健身总监",
        emblem="🏋️",
        stance="蛋白质优先，钠要管",
        persona=(
            "你是一位严谨的运动营养教练。你只关心蛋白质、总热量与钠含量这三个数字，"
            "所有主张都要有营养数据支撑，不接受'好吃就行'这种理由。"
            "你会主动指出对手方案的营养缺陷，也会在数据支持时坦然让步。"
            "你不做医疗建议，只做营养结构上的取舍，并以麦当劳官方营养数据为准。"
        ),
        score=_score_macro,
    ),
    "purist": Role(
        key="purist",
        name="麦门老饕",
        emblem="🍔",
        stance="经典款有它的道理",
        persona=(
            "你是一位对麦当劳菜单如数家珍的老顾客。你尊重经典款，认为配方之所以长期保留，"
            "是因为它经得起时间检验。你对为了所谓'更健康'而破坏搭配的做法持保留意见，"
            "但你会用菜单与活动事实来说理，而不是情绪。你从不贬低任何餐品。"
        ),
        score=_score_purist,
    ),
    "light": Role(
        key="light",
        name="养生专员",
        emblem="🌿",
        stance="少油少钠，能换就换",
        persona=(
            "你是一位温和但坚持原则的饮食平衡倡导者。你关注钠、脂肪与总热量，"
            "主张用换配（换饮品、换配菜、去酱）来达成平衡，而不是拒绝吃快餐。"
            "你擅长提出'只差几块钱但结构好很多'的折中方案，说话平和，从不制造对立。"
        ),
        score=_score_light,
    ),
    "novelty": Role(
        key="novelty",
        name="尝鲜委员",
        emblem="🎲",
        stance="你每次都点一样的",
        persona=(
            "你负责打破惯性。当其他人都推荐经典款时，你会指出用户已经吃过太多次，"
            "并推荐菜单里用户尚未尝试过的选项。你讲话轻快、爱开玩笑，"
            "但最终仍然尊重预算与用户的硬性约束。"
        ),
        score=_score_novelty,
    ),
}

DEFAULT_ROLE_ORDER = ["saver", "macro", "purist", "light", "novelty"]


def resolve_roles(spec: str | None) -> list[Role]:
    """把 ``saver,macro`` 这样的字符串解析成 Role 列表。"""
    if not spec:
        return [ROLES[k] for k in DEFAULT_ROLE_ORDER]
    if spec.strip().lower() in ("all", "*"):
        return [ROLES[k] for k in DEFAULT_ROLE_ORDER]

    resolved: list[Role] = []
    for raw in spec.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in ROLES:
            raise ValueError(f"未知角色 '{key}'，可选：{', '.join(ROLES)}")
        resolved.append(ROLES[key])
    return resolved or [ROLES[k] for k in DEFAULT_ROLE_ORDER]


# --------------------------------------------------------------------------- #
# 规则大脑：不需要 LLM 也能产出可信提案
# --------------------------------------------------------------------------- #

def propose_heuristically(
    role: Role,
    menu: list[MenuItem],
    coupons: list[Coupon],
    constraint: Constraint,
    ctx: dict,
) -> Proposal:
    """按角色立场从真实菜单里挑一份可下单的组合。"""
    candidates = [
        m for m in menu if m.price > 0 and not m.is_condiment and not _is_avoided(m, constraint)
    ]
    if not candidates:
        return Proposal(role.key, role.name, [], "菜单为空，无法提案。", "", 0.0)

    ranked = sorted(candidates, key=lambda m: role.score(m, constraint, ctx), reverse=True)
    mains = [m for m in ranked if m.is_main] or ranked
    sides = [m for m in ranked if m.kind in ("side", "drink")]
    drinks = [m for m in ranked if m.kind == "drink"] or sides

    chosen: list[MenuItem] = [mains[0]]
    if constraint.people > 1 and len(mains) > 1:
        chosen.append(mains[1])
    if sides:
        chosen.append(sides[0])
    if drinks and drinks[0] not in chosen and role.key in ("macro", "light", "saver"):
        chosen.append(drinks[0])

    chosen = _fit_budget(chosen, ranked, constraint)
    lines = [LineItem(m.code, m.name, constraint.people, m.price) for m in chosen]
    total = sum(l.amount for l in lines)

    pitch = _render_pitch(role, chosen, total, constraint, coupons)
    rationale = _render_rationale(role, chosen)
    return Proposal(role.key, role.name, lines, pitch, rationale, round(total, 2))


def _is_avoided(item: MenuItem, c: Constraint) -> bool:
    # 判据统一收在 Constraint.is_avoided()，避免提案侧和凑单侧两套规则跑偏
    return c.is_avoided(item.name, *item.tags)


def _fit_budget(
    chosen: list[MenuItem],
    ranked: list[MenuItem],
    constraint: Constraint,
) -> list[MenuItem]:
    """先把组合压进预算，再按该角色的偏好把余量花掉。

    注意这里只保证"提案本身不超预算"，最终金额仍由求解器的真实试算决定。
    """
    budget = constraint.effective_budget_total
    people = max(1, constraint.people)
    if budget is None:
        return chosen

    result = list(chosen)
    while len(result) > 1 and sum(m.price * people for m in result) > budget:
        result.pop()

    if not result:
        result = [min(ranked, key=lambda m: m.price)]

    # 单项就超预算时必须换掉，否则"提案"从第一步就违反用户约束
    if sum(m.price * people for m in result) > budget:
        affordable = [m for m in ranked if m.price * people <= budget]
        if affordable:
            result = [affordable[0]]
        else:
            result = [min(ranked, key=lambda m: m.price)]
        return result

    current = sum(m.price * people for m in result)
    for extra in ranked:
        if len(result) >= 3 or extra in result:
            continue
        if current + extra.price * people <= budget:
            result.append(extra)
            current += extra.price * people

    return result


def _render_pitch(
    role: Role,
    chosen: list[MenuItem],
    total: float,
    constraint: Constraint,
    coupons: list[Coupon],
) -> str:
    names = " + ".join(m.name for m in chosen)
    per = total / constraint.people if constraint.people else total

    # ⚠️ 券类接口只返回 Markdown 文本，**不含满减门槛**。
    # 所以这里只在本工具确实知道门槛时才敢声称"能用券"，否则一律交给官方试算。
    best: Coupon | None = None
    for c in coupons:
        if c.threshold and c.applies_to(total) and (best is None or c.discount > best.discount):
            best = c

    if role.key == "saver":
        tail = f"，已触发「{best.name}」" if best else "，能不能用券由官方试算说了算"
        return f"{names}。合计约 ¥{total:.1f}，人均 ¥{per:.1f}{tail} —— 券用掉才叫划算。"
    if role.key == "macro":
        protein = sum(m.protein * constraint.people for m in chosen)
        # 官方营养表并不覆盖全部商品（尤其是套餐），没有数据时绝不编一个 0
        if protein <= 0:
            return f"换成{names}，合计 ¥{total:.1f}。这几项在官方营养表里没有条目，结构我无法背书。"
        return f"换成{names}。蛋白质合计约 {protein:.0f}g，合计 ¥{total:.1f}，结构比堆主食更可控。"
    if role.key == "light":
        # 口径必须和上面的 macro 一致：chosen 里每样都是"每人一份"，
        # 报价也是 `price * people`。漏乘人数会把 4 人份的钠说成 1 人份的，
        # 于是"钠合计 2900mg"被显示成"725mg"——看起来清淡得离谱。
        sodium = sum(m.sodium * constraint.people for m in chosen)
        if sodium <= 0:
            return f"建议{names}，合计 ¥{total:.1f}。官方营养表里查不到它们的钠值，我不能说它清淡。"
        return f"建议{names}，钠合计约 {sodium:.0f}mg，合计 ¥{total:.1f} —— 只差几块钱，结构好很多。"
    if role.key == "purist":
        classics = [m.name for m in chosen if m.tagset & CLASSIC_TAGS]
        tail = ""
        if len(chosen) > 1 and 0 < len(classics) < len(chosen):
            tail = f"（其中{'、'.join(classics)}是常年保留的搭配）"
        return f"就{names}{tail}。合计 ¥{total:.1f}，人均 ¥{per:.1f}。经典之所以经典是有理由的。"
    return f"你们每次都点这几样。试试{names}，合计 ¥{total:.1f}，人均 ¥{per:.1f}，人生苦短。"


def _render_rationale(role: Role, chosen: list[MenuItem]) -> str:
    bits: list[str] = []
    for m in chosen:
        if role.key == "macro" and m.protein:
            bits.append(f"{m.name} 蛋白质 {m.protein:.0f}g")
        elif role.key == "light" and m.sodium:
            bits.append(f"{m.name} 钠 {m.sodium:.0f}mg")
        elif role.key == "saver" and m.kcal:
            bits.append(f"{m.name} {m.kcal:.0f}kcal/¥{m.price:.1f}")
        elif role.key == "purist" and (m.tagset & CLASSIC_TAGS):
            bits.append(f"{m.name} 属经典款")
    return "；".join(bits)
