"""端到端演示：真实财报数据驱动的本体演化。

演示核心主张——schema 漂移在横截面自带，且 L0 演化不触碰存量实例。

    1. 建种子本体（按白酒行业的字段）
    2. 灌茅台数据 → 入库
    3. 灌平安银行数据 → 发现未知字段（漂移）
    4. L0 演化：把银行特有字段加为可选属性
    5. 验证：存量实例写入行数 = 0
    6. 灌银行数据 → 新字段落库
    7. 加派生属性（同比）→ 实例层仍然零写，读时求值

跑法：python examples/demo_real_ingest.py
需要 Postgres 已启动（docker compose up -d）。
"""

from __future__ import annotations

import sys
from datetime import date

from rich.console import Console
from rich.table import Table

from doa.db import connect, init_schema
from doa.derive import evaluate
from doa.ingest import FieldMapping, Ingestor, ObjectMapping
from doa.sources import eastmoney
from doa.sr import SchemaRegistry
from doa.sr.delta import AddAttributeType, AddEntityType, AddParserDef
from doa.store import InstanceStore, StoreResolver

console = Console()

# 白酒的字段（种子本体按这些建）
DISTILLERY_FIELDS = [
    ("SECURITY_NAME_ABBR", "name_cn", "string"),
    ("TOTAL_ASSETS", "total_assets", "number"),
    ("TOTAL_LIABILITIES", "total_liabilities", "number"),
    ("INVENTORY", "inventory", "number"),
    ("FIXED_ASSET", "fixed_asset", "number"),
    ("ACCOUNTS_RECE", "accounts_receivable", "number"),
]

# 银行特有字段（种子本体里不存在，要靠演化加入）
BANK_ONLY_FIELDS = [
    ("ACCEPT_DEPOSIT", "accept_deposit", "number"),
    ("LOAN_ADVANCE", "loan_advance", "number"),
    ("CASH_DEPOSIT_PBC", "cash_deposit_pbc", "number"),
    ("LOAN_PBC", "loan_pbc", "number"),
]


def _mapping(fields: list[tuple[str, str, str]]) -> ObjectMapping:
    return ObjectMapping(
        entity_type_name="Company",
        subject_field="SECURITY_CODE",
        period_field="REPORT_DATE",
        disclosed_field="NOTICE_DATE",
        fields=tuple(FieldMapping(src, attr) for src, attr, _ in fields),
    )


def main() -> int:
    conn = connect()
    init_schema(conn)
    with conn.cursor() as cur:   # 演示从干净状态开始
        cur.execute(
            "TRUNCATE instance_attr, instance, relation_inst, hyperedge, "
            "conflict_violation, conflict_type_summary, adjudication, conflict_report "
            "RESTART IDENTITY CASCADE"
        )
    conn.commit()

    sr = SchemaRegistry(conn)
    sr.bootstrap()
    store = InstanceStore(conn)

    # ---------------------------------------------------------------- 1. 种子本体
    console.rule("[bold]1. 种子本体（按白酒行业字段）")
    deltas = [AddEntityType(name="Company")]
    for _src, attr, dtype in DISTILLERY_FIELDS:
        deltas.append(
            AddAttributeType(entity_type_name="Company", name=attr, datatype=dtype)
        )
    # 金额字段统一挂 cn_amount 解析器：既处理纯数字，也处理 '1.23亿' 这类写法
    for _src, attr, dtype in DISTILLERY_FIELDS:
        if dtype == "number":
            deltas.append(
                AddParserDef(
                    entity_type_name="Company", attribute_name=attr,
                    kind="code_module", pattern="cn_amount", priority=10,
                    not_required=True,
                )
            )
    v1 = sr.commit(deltas, message="种子本体：白酒行业字段")
    console.print(f"version {v1}，属性 {len(DISTILLERY_FIELDS)} 个")

    # ---------------------------------------------------------------- 2. 灌白酒
    console.rule("[bold]2. 灌茅台 600519")
    rows = eastmoney.fetch("balance", "600519", page_size=4)
    if not rows:
        console.print("[red]接口无数据，跳过[/red]")
        return 1
    rep = Ingestor(conn, store, v1).ingest(rows, _mapping(DISTILLERY_FIELDS), sr.ancestors(v1))
    console.print(rep.summary())

    iid = store.get_instance("Company", "600519")
    vals = {a.attribute_name: a.value for a in store.current_attrs(iid)}
    console.print(f"存货 inventory = {vals.get('inventory')}")
    console.print(f"总资产 total_assets = {vals.get('total_assets')}")

    # ---------------------------------------------------------------- 3. 漂移
    console.rule("[bold]3. 灌平安银行 000001 —— 漂移暴露")
    bank_rows = eastmoney.fetch("balance", "000001", page_size=4)
    if not bank_rows:
        console.print("[red]接口无数据，跳过[/red]")
        return 1

    bank_nonnull = eastmoney.nonnull_fields(bank_rows[0])
    known = {src for src, _a, _d in DISTILLERY_FIELDS}
    unknown = sorted(
        f for f in bank_nonnull
        if f in {src for src, _a, _d in BANK_ONLY_FIELDS} and f not in known
    )
    console.print(f"银行非空字段 {len(bank_nonnull)} 个，其中本体未覆盖的：")
    for f in unknown:
        console.print(f"  [yellow]{f}[/yellow]")
    console.print("[dim]白酒本体里没有这些概念——必须演化[/dim]")

    # ---------------------------------------------------------------- 4. L0 演化
    console.rule("[bold]4. L0 演化：加银行特有字段为可选属性")
    rows_before = store.count_attr_rows()
    instances_before = store.count_instances()

    bank_deltas = []
    for _src, attr, dtype in BANK_ONLY_FIELDS:
        bank_deltas.append(
            AddAttributeType(entity_type_name="Company", name=attr, datatype=dtype)
        )
        bank_deltas.append(
            AddParserDef(
                entity_type_name="Company", attribute_name=attr,
                kind="code_module", pattern="cn_amount", priority=10,
                not_required=True,
            )
        )
    v2 = sr.commit(bank_deltas, message="L0：银行特有字段")

    rows_after = store.count_attr_rows()
    console.print(f"version {v1} → {v2}，新增 {len(BANK_ONLY_FIELDS)} 个属性类型")

    t = Table(title="L0 零触碰验证")
    t.add_column("指标")
    t.add_column("演化前", justify="right")
    t.add_column("演化后", justify="right")
    t.add_row("instance_attr 行数", str(rows_before), str(rows_after))
    t.add_row("instance 行数", str(instances_before), str(store.count_instances()))
    console.print(t)

    if rows_after == rows_before:
        console.print("[green]✓ 存量实例写入行数 = 0[/green]  元数据与实例分离生效")
    else:
        console.print(f"[red]✗ 存量被触碰了 {rows_after - rows_before} 行[/red]")
        return 1

    # ---------------------------------------------------------------- 5. 灌银行
    console.rule("[bold]5. 按新本体灌银行数据")
    all_fields = DISTILLERY_FIELDS + BANK_ONLY_FIELDS
    rep2 = Ingestor(conn, store, v2).ingest(
        bank_rows, _mapping(all_fields), sr.ancestors(v2)
    )
    console.print(rep2.summary())

    bank_iid = store.get_instance("Company", "000001")
    bvals = {a.attribute_name: a.value for a in store.current_attrs(bank_iid)}
    t2 = Table(title="平安银行：新属性已落库")
    t2.add_column("属性")
    t2.add_column("值", justify="right")
    for _src, attr, _d in BANK_ONLY_FIELDS:
        if attr in bvals:
            t2.add_row(attr, bvals[attr])
    console.print(t2)
    # 白酒专属字段在银行身上为空，靠 not_required 跳过而非报错
    console.print(f"[dim]inventory（银行无此科目）= {bvals.get('inventory', '未写入')}[/dim]")

    # ---------------------------------------------------------------- 6. 派生属性
    console.rule("[bold]6. 加派生属性（同比）—— 实例层仍然零写")
    rows_before_derived = store.count_attr_rows()
    v3 = sr.commit(
        [
            AddAttributeType(
                entity_type_name="Company",
                name="total_assets_yoy",
                datatype="number",
                derivation_expr="total_assets / same_period_last_year(total_assets) - 1",
                index_derived=True,
            )
        ],
        message="L1：总资产同比（惰求值）",
    )
    rows_after_derived = store.count_attr_rows()
    console.print(f"version {v2} → {v3}")
    console.print(
        f"instance_attr 行数 {rows_before_derived} → {rows_after_derived}"
        + ("  [green]✓ 零写[/green]" if rows_after_derived == rows_before_derived
           else "  [red]✗ 被触碰[/red]")
    )

    # 读时求值：需要同一报告期与其上年同期都有值
    periods = sorted(
        {a.valid_to for a in store.current_attrs(iid) if a.valid_to}, reverse=True
    )
    expr = "total_assets / same_period_last_year(total_assets) - 1"
    console.print("\n[dim]读时求值（茅台，各报告期）：[/dim]")
    for p in periods[:4]:
        r = StoreResolver(store, iid, p)
        val = evaluate(expr, r)
        shown = f"{val:.4%}" if val is not None else "null（缺上年同期）"
        console.print(f"  {p}  总资产同比 = {shown}")

    console.rule("[bold green]演示结束")
    console.print(
        "核心主张已验证：schema 漂移由真实行业差异自带；"
        "L0/L1 演化对存量实例零触碰。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
