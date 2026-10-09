"""检查终端输出的列宽：面板内每行必须正好 76 显示列（CJK 记 2 列）。"""
import sys
import unicodedata


def w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


bad = 0
for path in sys.argv[1:]:
    print(f"=== {path} ===")
    with open(path, encoding="utf-8") as fh:
        for i, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            # 只看框内的行（以 │ 结尾），这类行必须严格 76 列
            if line.endswith("│") and line.lstrip().startswith("│"):
                got = w(line)
                if got != 76:
                    bad += 1
                    print(f"  L{i}: width={got}  {line!r}")
            # 行首禁则
            stripped = line.lstrip("│ ")
            if stripped and stripped[0] in "，。、；：！？）》」』】":
                bad += 1
                print(f"  L{i}: 行首禁则违规 {line!r}")
    print("  面板框线宽度与行首禁则：通过" if bad == 0 else f"  发现 {bad} 处问题")

print("\nRESULT:", "PASS" if bad == 0 else f"FAIL({bad})")
