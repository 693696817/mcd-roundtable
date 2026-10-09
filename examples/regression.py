"""回归测试：纯函数单测 + 五种典型提问的端到端不变量。

两层，因为失败模式不同：

* ``check_units()`` —— 不起进程，直接测纯函数。跑得快，定位准。
  这里固化的是**踩过的坑**：金额单位、脏返回解析、忌口解析、标记注入。
  每一条都对应一个曾经真实出过错、并且修好了的地方。
* ``check_scenarios()`` —— 真起进程跑五种提问。慢，但能抓到"函数都对、
  拼起来不对"的问题（比如报价与明细对不上、方案里没有主食）。

用法::

    python examples/regression.py
"""

import json
import os
import subprocess
import sys

ENV_PY = r"C:\Users\Admin\.workbuddy\binaries\python\envs\mcd_rt\Scripts\python.exe"
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "..", "src"))

SCENARIOS = [
    ("省钱", ["--demo", "--no-color", "中午想吃饱，30 以内，把券用上"]),
    ("蛋白", ["--demo", "--no-color", "增肌，蛋白质 30g 以上", "--roles", "macro,light", "--objective", "protein"]),
    ("团队", ["--demo", "--no-color", "团队 6 人午餐，预算 300", "--people", "6"]),
    ("控卡", ["--demo", "--no-color", "减脂期，单餐 600kcal 以内"]),
    ("券最大化", ["--demo", "--no-color", "三个人，预算 60，把券用到极致"]),
]

_unit_failures: list[str] = []


def expect(name: str, got, want) -> None:
    if got != want:
        _unit_failures.append(f"{name}：期望 {want!r}，实得 {got!r}")


# --------------------------------------------------------------------------- #
# 纯函数单测
# --------------------------------------------------------------------------- #

def check_units() -> int:
    from mcd_roundtable.council import Constraint, parse_constraint
    from mcd_roundtable.mcp_client import (
        _load_campaigns,
        _to_float,
        extract_text,
        find_json,
        parse_calculate_price,
    )
    from mcd_roundtable.models import MenuItem
    from mcd_roundtable.roles import ROLES

    # ---- 金额与脏返回 ---------------------------------------------------- #
    # 分/元混用、千分位、字符串数字：这几处都出过"静默差三个数量级"的错
    expect("千分位 1,234.5", _to_float("1,234.5"), 1234.5)
    expect("字符串数字 598.2", _to_float("598.2"), 598.2)
    expect("空值归零", _to_float(None), 0.0)
    expect("券门槛『满 30 减 6』取门槛", _to_float("满 30 减 6"), 30.0)

    pb = parse_calculate_price({
        "price": 2350, "enjoyable": {"balance": 800, "realDiscount": "5.5"},
    })
    expect("realDiscount 是字符串也不抛", pb.next_saving_yuan, 5.5)
    expect("balance 分→元", pb.next_gap_yuan, 8.0)
    expect("price 分→元", pb.payable, 23.5)

    # 字段说明里出现不带引号的 {xxx} 是常态，不能只看第一个 "{"
    j = find_json('说明：结构为 {xxx} 见下\n\n{"datetime": "2026-10-09 10:00"}')
    expect("跳过无效花括号占位符", (j or {}).get("datetime"), "2026-10-09 10:00")
    # toon 紧凑表头不能被误判成 JSON
    expect("toon 表头不当 JSON", find_json("[160]{productName,energyKcal}:\n  猪柳麦满分,308"), None)

    # 业务失败必须冒泡，不能被静默降级成空 dict
    expect("success 字符串 false 也报错",
           extract_text('{"success": "false", "message": "门店关闭"}').get("_error"), "门店关闭")
    expect("success 布尔 false 报错",
           extract_text('{"success": false, "message": "参数缺失"}').get("_error"), "参数缺失")
    expect("成功响应正常剥壳", extract_text('{"success": true, "data": {"a": 1}}'), {"a": 1})
    # 活动列表既可能是 dict 列表也可能是字符串列表
    expect("活动列表支持字符串", _load_campaigns({"data": ["麦乐送日"]}), ["麦乐送日"])
    expect("活动列表支持 dict", _load_campaigns({"campaigns": [{"name": "大薯买一送一"}]}), ["大薯买一送一"])

    # ---- 需求解析 -------------------------------------------------------- #
    # 「不辣」说的是忌口，方向不能读反——读反是最危险的一类误解析
    c = parse_constraint("来点不辣的")
    expect("不辣进忌口", ("辣" in c.avoid, "辣" in c.keywords), (True, False))
    c = parse_constraint("想吃辣的")
    expect("想吃辣仍是需求", ("辣" in c.keywords, "辣" in c.avoid), (True, False))
    expect("不要可乐和薯条拆两项", sorted(parse_constraint("不要可乐和薯条").avoid), ["可乐", "薯条"])
    # 「和」既是连词也是词首（和风沙拉），切错会把品名切坏
    expect("不要和风沙拉不误切", parse_constraint("不要和风沙拉").avoid, ["和风沙拉"])
    expect("不要辣的去掉语气词", parse_constraint("不要辣的").avoid, ["辣"])
    # 多人时「100 块以内」与「100 块」必须是同一个口径（整单）
    c = parse_constraint("三个人 100 块以内")
    expect("多人以内=整单", (c.people, c.budget_total, c.budget_per_person), (3, 100.0, None))
    c = parse_constraint("三个人 100 块")
    expect("多人块=整单", (c.people, c.budget_total, c.budget_per_person), (3, 100.0, None))
    expect("单人以内=人均", parse_constraint("一个人 30 以内").budget_per_person, 30.0)
    expect("蛋白质目标", parse_constraint("增肌，蛋白质 30g 以上").min_protein_per_person, 30.0)

    # ---- 忌口必须真的作用到打分上 ---------------------------------------- #
    spicy = MenuItem(code="B003", name="麦辣鸡腿堡", price=23.5,
                     category="汉堡 · 人气热卖", tags=["辣", "油炸"], nutrition={})
    plain = MenuItem(code="B001", name="板烧鸡腿堡", price=24.0,
                     category="汉堡 · 人气热卖", tags=["经典", "非油炸"], nutrition={})
    c_spicy = parse_constraint("来点不辣的")
    if ROLES["purist"].score(spicy, c_spicy, {}) >= ROLES["purist"].score(plain, c_spicy, {}):
        _unit_failures.append("老饕仍会把辣味推给说『不辣』的人")
    # 尝鲜委员要看分类里的"人气热卖"来避开大众款，而不是靠一个永远为空的 seen 集合
    novel = MenuItem(code="B900", name="安格斯厚牛堡", price=30.0, category="汉堡", tags=[], nutrition={})
    c_any = Constraint(raw_query="随便")
    if ROLES["novelty"].score(plain, c_any, {}) >= ROLES["novelty"].score(novel, c_any, {}):
        _unit_failures.append("尝鲜委员没有避开『人气热卖』")

    # ---- 终端标记注入 ---------------------------------------------------- #
    # 品名来自官方菜单，出现 [ ] 完全可能；rich 会当标记解析，轻则丢字重则抛异常
    import io

    from rich.console import Console

    from mcd_roundtable.models import LineItem, PriceQuote, Trial, Verdict
    from mcd_roundtable.render import _render_trials

    class _Stub:
        pass

    stub = _Stub()
    stub.verdict = Verdict(
        quote=PriceQuote(lines=[LineItem("C1", "巨无霸", 1, 25.0)], subtotal=25.0,
                         discount=0.0, delivery_fee=0.0, payable=25.0, source="calculate-price"),
        trials=[Trial(label="巨无霸[red]套餐", payable=30.0, original=35.0,
                      discount=5.0, per_person=30.0, adopted=True)],
    )
    buf = io.StringIO()
    try:
        _render_trials(stub, Console(file=buf, width=100, no_color=True))
        if "巨无霸[red]套餐" not in buf.getvalue():
            _unit_failures.append("含方括号的品名在终端里被吃掉")
    except Exception as exc:  # noqa: BLE001
        _unit_failures.append(f"含方括号的品名让终端渲染抛异常：{type(exc).__name__}")

    # ---- 质询轮的净收益公式 ---------------------------------------------- #
    # 净降 = 跨档优惠 − 已得优惠 − 为跨档多花的钱。三项少减任何一项，
    # 都会把"多花 8 块换来 5 块"念成"净降 5 块"——而这句话是拿去做购买决策的。
    from mcd_roundtable.council import _heuristic_rebuttals
    from mcd_roundtable.models import Proposal

    def _saver_says(next_gap: float, next_saving: float, discount: float) -> str:
        li = [LineItem("C1", "巨无霸", 1, 25.0)]
        q = PriceQuote(lines=li, subtotal=25.0, discount=discount, delivery_fee=0.0,
                       payable=50.0, source="calculate-price",
                       next_gap=next_gap, next_saving=next_saving)
        props = [Proposal("saver", "省钱部长", [], "x", "r", 25.0),
                 Proposal("purist", "麦门老饕", [], "y", "r", 30.0)]
        out = _heuristic_rebuttals(props, Constraint(raw_query="再点一点"),
                                   {"quotes": [(None, q)], "chosen_quote": q})
        return " ".join(r.text for r in out)

    if "净降" in _saver_says(8.0, 5.0, 0.0):
        _unit_failures.append("净收益为负（5-0-8=-3）时仍在宣称『净降』")
    if "净降" in _saver_says(3.0, 6.0, 4.0):
        _unit_failures.append("已得优惠没参与抵扣（6-4-3=-1）时仍在宣称『净降』")
    if "实付净降 ¥3.0" not in _saver_says(3.0, 6.0, 0.0):
        _unit_failures.append("净收益为正（6-0-3=3）时没有报出『净降』")

    # ---- HTML 报告的转义 -------------------------------------------------- #
    # 门店名来自官方接口，是真机可变字符串；这里是全文件唯一一处
    # "看起来是常量、其实是外部输入"的插值，漏一次 escape 就能长出 <script>。
    from mcd_roundtable.html_report import _receipt

    payload = "<script>alert(1)</script> & <img src=x onerror=y>"

    class _Data:
        store_name = payload
        is_demo = False

        @staticmethod
        def by_code(_code):  # 营养表查不到 → _nutrition() 返回空，不参与本断言
            return None

    class _V:
        quote = PriceQuote(lines=[LineItem("C1", "巨无霸", 1, 25.0)], subtotal=25.0,
                           discount=0.0, delivery_fee=0.0, payable=25.0,
                           source="calculate-price")
        headcount_label = "1 人"
        why_not_cheapest = ""
        summary = ""

    class _Result:
        verdict = _V()
        data = _Data()
        constraint = Constraint(raw_query="来一份")

    html = _receipt(_Result(), None)
    if "<script>" in html:
        _unit_failures.append("HTML 小票里的门店名没有被转义")
    if "&lt;script&gt;" not in html:
        _unit_failures.append("HTML 小票把门店名整段丢了（应当转义后保留）")

    for msg in _unit_failures:
        print(f"❌ 单测失败  {msg}")
    total = len(_unit_failures)
    print(f"{'✅' if not total else '❌'} 纯函数单测  失败 {total} 项")
    return total


# --------------------------------------------------------------------------- #
# 端到端场景
# --------------------------------------------------------------------------- #

def run(args):
    proc = subprocess.run(
        [ENV_PY, "-m", "mcd_roundtable", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1", "NO_COLOR": "1"},
    )
    return proc


def check_json(args):
    proc = subprocess.run(
        [ENV_PY, "-m", "mcd_roundtable", *args, "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if proc.returncode != 0:
        return None, proc.stderr[-500:]
    return json.loads(proc.stdout), None


def check_scenarios() -> int:
    failures = 0
    for name, args in SCENARIOS:
        proc = run(args)
        ok = proc.returncode == 0 and "Traceback" not in proc.stderr
        data, err = check_json(args)
        invariants = []
        if data is None:
            invariants.append(f"JSON 失败：{err}")
        else:
            v = data["verdict"]
            q = v["lines"]
            if not q:
                invariants.append("决议为空")
            if v["payable"] <= 0:
                invariants.append("应付 <= 0")
            if data["mode"] != "demo":
                invariants.append("mode 不是 demo")
            if not data["proposals"]:
                invariants.append("没有提案")
            if abs(sum(l["amount"] for l in q) - v["subtotal"]) > 0.05:
                invariants.append(f"明细合计 {sum(l['amount'] for l in q)} != 小计 {v['subtotal']}")
            if v["priceSource"] != "calculate-price":
                invariants.append(f"价格来源异常：{v['priceSource']}")
            if len(data["trials"]) < 2:
                invariants.append(f"试算组数过少：{len(data['trials'])}")
            # 硬约束：方案里必须有主食
            mains = ("包", "堡", "套餐", "随心配", "卷", "巨无霸", "吉士")
            if not any(any(k in l["name"] for k in mains) for l in q):
                invariants.append("方案里没有主食")

        # 退出码非 0 和"不变量被破坏"是两种独立的失败。
        # 只数 invariants 的话，一个崩掉的场景会因为"没产出结果→没有不变量可查"
        # 而被记成通过，回归脚本自己就成了一个静默失败源。
        failed_here = bool(invariants) or not ok
        status = "❌" if failed_here else "✅"
        if failed_here:
            failures += 1
        print(f"{status} {name:<8} 退出码={proc.returncode} 不变量问题={invariants or '无'}")

    return failures


def main() -> int:
    unit = check_units()
    print()
    scen = check_scenarios()
    print()
    print("=" * 64)
    if unit or scen:
        print(f"❌ 单测失败 {unit} 项，场景失败 {scen} 个")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
