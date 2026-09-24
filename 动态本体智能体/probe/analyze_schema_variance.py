"""探测东财资产负债表接口在不同公司类型间的 schema 差异。

目的：验证「同一个报表接口，不同行业返回的字段集是否不同」——
若不同，说明 schema 漂移在真实数据里是横截面就存在的，不必等时间推移。
"""

import glob
import json
import os
import sys
import tempfile

LABELS = {"000001": "银行", "600519": "白酒", "600030": "券商", "601318": "保险"}

# Git Bash 的 /tmp 与 Windows Python 看到的 /tmp 不是同一个目录，
# 故用 tempfile.gettempdir() 而非硬编码 /tmp。
TMP = sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir()


def main() -> None:
    sets: dict[str, set[str]] = {}
    pattern = os.path.join(TMP, "cols_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"没找到探测数据: {pattern}")
        return
    for f in files:
        code = os.path.basename(f)[5:-5]
        label = LABELS.get(code, "")
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"{code} {label}: 读取失败 {e}")
            continue
        rows = (d.get("result") or {}).get("data") or []
        if not rows:
            print(f"{code} {label}: 空 success={d.get('success')} msg={d.get('message')}")
            continue
        row = rows[0]
        nonnull = {k for k, v in row.items() if v not in (None, "")}
        sets[code] = nonnull
        print(f"{code} {label}: 字段 {len(row)}, 非空 {len(nonnull)}")

    if len(sets) < 2:
        return

    common = set.intersection(*sets.values())
    union = set.union(*sets.values())
    print()
    print(f"并集 {len(union)}  共有 {len(common)}")
    for code, s in sets.items():
        only = s - common
        print(f"  {code} {LABELS.get(code, '')} 独有 {len(only)}: {sorted(only)[:10]}")


if __name__ == "__main__":
    main()
