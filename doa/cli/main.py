"""doa 命令行入口。

W1 覆盖：初始化、提交 delta、查看版本图、分支、fast-forward 合并、回滚、快照。
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from doa.db import connect, init_schema
from doa.evolve import ConflictStore
from doa.reasoner import ReasonerClient, ReasonerUnavailable
from doa.sr import SchemaRegistry
from doa.sr.delta import parse_deltas
from doa.sr.owl_export import snapshot_to_owl
from doa.sr.registry import MAIN, SchemaError

app = typer.Typer(help="动态本体演化引擎", no_args_is_help=True)
schema_app = typer.Typer(help="本体模式与版本管理", no_args_is_help=True)
app.add_typer(schema_app, name="schema")
conflict_app = typer.Typer(help="L3 冲突报告与人工裁决", no_args_is_help=True)
app.add_typer(conflict_app, name="conflict")

console = Console()


def _registry() -> SchemaRegistry:
    conn = connect()
    return SchemaRegistry(conn)


@app.command("init")
def init() -> None:
    """建表并创建根版本与 main 分支。可重复执行。"""
    conn = connect()
    init_schema(conn)
    sr = SchemaRegistry(conn)
    root = sr.bootstrap()
    console.print(f"[green]初始化完成[/green]  root version = {root}, branch = {MAIN}")


@schema_app.command("commit")
def commit(
    file: Path = typer.Argument(..., help="SchemaDelta 列表的 JSON 文件"),
    message: str = typer.Option("", "-m", "--message"),
    branch: str = typer.Option(MAIN, "-b", "--branch"),
) -> None:
    """提交一组 delta 作为新版本。"""
    raw = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = [raw]
    deltas = parse_deltas(raw)
    sr = _registry()
    try:
        v = sr.commit(deltas, message=message, branch=branch)
    except SchemaError as e:
        console.print(f"[red]拒绝[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]已提交[/green] version {v} → {branch}")
    for d in deltas:
        console.print(f"  {d.describe()}")


@schema_app.command("log")
def log(branch: str = typer.Option(MAIN, "-b", "--branch")) -> None:
    """版本历史，从 head 回溯到根。"""
    sr = _registry()
    rows = sr.log(branch)
    t = Table(title=f"版本历史 [{branch}]")
    for col in ("version", "parent", "代价 (I,X)", "deltas", "message"):
        t.add_column(col)
    for i, v in enumerate(rows):
        marker = " ← head" if i == 0 else ""
        t.add_row(
            f"{v.version_id}{marker}",
            str(v.parent_id or "—"),
            f"({v.cost_tier_i}, {v.cost_tier_x})",
            str(v.delta_count),
            v.message or "—",
        )
    console.print(t)


@schema_app.command("branch")
def branch_cmd(
    name: str | None = typer.Argument(None, help="新分支名；省略则列出所有分支"),
    from_branch: str = typer.Option(MAIN, "--from"),
) -> None:
    """列出分支，或从某分支 head 新开一个分支。"""
    sr = _registry()
    if name is None:
        heads = sr.branches()
        t = Table(title="分支")
        t.add_column("name")
        t.add_column("head")
        for n, h in heads.items():
            t.add_row(n, str(h))
        console.print(t)
        return
    try:
        base = sr.create_branch(name, from_branch)
    except SchemaError as e:
        console.print(f"[red]拒绝[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]已创建分支[/green] {name} @ {base}（源自 {from_branch}）")


@schema_app.command("merge")
def merge(
    source: str = typer.Argument(..., help="源分支"),
    target: str = typer.Option(MAIN, "--into"),
) -> None:
    """fast-forward 合并。非 fast-forward 会被拒绝。"""
    sr = _registry()
    try:
        v = sr.merge(source, target)
    except SchemaError as e:
        console.print(f"[red]拒绝[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]已合并[/green] {source} → {target}, head = {v}")


@schema_app.command("rollback")
def rollback(
    to: int | None = typer.Option(None, "--to", help="目标版本；省略则回退一步"),
    branch: str = typer.Option(MAIN, "-b", "--branch"),
) -> None:
    """把分支 head 指回祖先版本。版本行本身保留。"""
    sr = _registry()
    try:
        v = sr.rollback(branch, to)
    except SchemaError as e:
        console.print(f"[red]拒绝[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]已回滚[/green] {branch} head → {v}")


@schema_app.command("show")
def show(
    version: int | None = typer.Option(None, "-v", "--version"),
    branch: str = typer.Option(MAIN, "-b", "--branch"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """展示某版本下可见的完整 schema。"""
    sr = _registry()
    snap = sr.snapshot(version, branch)
    if as_json:
        console.print_json(json.dumps(snap, ensure_ascii=False, default=str))
        return

    tree = Tree(f"schema @ version {snap['version_id']}")
    for e in snap["entity_types"]:
        label = e["name"] + (f" ⊑ {e['parent_name']}" if e["parent_name"] else "")
        node = tree.add(f"[bold]{label}[/bold]")
        for a in e["attributes"]:
            if a["derivation_expr"]:
                flag = "→idx" if a["index_derived"] else "lazy"
                node.add(f"{a['name']} = {a['derivation_expr']} [dim][{flag}][/dim]")
            else:
                req = "" if a["min_cardinality"] == 0 else " [red]required[/red]"
                np = len(a["parsers"])
                nv = len(a["validators"])
                extra = f" [dim]{np}p/{nv}v[/dim]" if (np or nv) else ""
                node.add(f"{a['name']}:{a['datatype']}{req}{extra}")
    if snap["relation_types"]:
        rnode = tree.add("[bold]关系[/bold]")
        for r in snap["relation_types"]:
            rnode.add(f"{r['name']}: {r['domain_name']} → {r['range_name']}")
    console.print(tree)


@schema_app.command("owl")
def owl(
    version: int | None = typer.Option(None, "-v", "--version"),
    branch: str = typer.Option(MAIN, "-b", "--branch"),
    disjoint_siblings: bool = typer.Option(False, "--disjoint-siblings"),
    include_derived: bool = typer.Option(False, "--include-derived"),
    out: Path | None = typer.Option(None, "-o", "--out"),
) -> None:
    """导出某版本的 OWL 视图（喂给推理机用）。"""
    sr = _registry()
    text = snapshot_to_owl(
        sr.snapshot(version, branch),
        disjoint_siblings=disjoint_siblings,
        include_derived=include_derived,
    )
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]已写入[/green] {out}")
    else:
        console.print(text, markup=False, highlight=False)


@schema_app.command("check")
def check(
    version: int | None = typer.Option(None, "-v", "--version"),
    branch: str = typer.Option(MAIN, "-b", "--branch"),
    disjoint_siblings: bool = typer.Option(False, "--disjoint-siblings"),
) -> None:
    """用 HermiT 检查某版本的逻辑一致性与不可满足类。"""
    sr = _registry()
    snap = sr.snapshot(version, branch)
    text = snapshot_to_owl(snap, disjoint_siblings=disjoint_siblings)

    try:
        res = ReasonerClient().consistency(text)
    except ReasonerUnavailable as e:
        console.print(f"[red]推理服务不可用[/red] {e}")
        raise typer.Exit(2)

    console.print(f"version {snap['version_id']}  "
                  f"[dim]{res.axiom_count} 公理 / {res.class_count} 类 / "
                  f"{res.reasoner_time_ms}ms[/dim]")
    if res.consistent:
        console.print("一致性  [green]通过[/green]")
    else:
        console.print("一致性  [red]不通过[/red]（存在逻辑矛盾）")

    if res.unsatisfiable_classes:
        # 一致但有不可满足类：本体没矛盾，但这些类永远不可能有实例
        console.print(f"不可满足类  [red]{len(res.unsatisfiable_classes)} 个[/red]")
        for c in res.unsatisfiable_classes:
            console.print(f"  {c}")
    elif res.consistent:
        console.print("不可满足类  [green]无[/green]")

    if not res.ok:
        raise typer.Exit(1)


@conflict_app.command("list")
def conflict_list() -> None:
    """列出待裁决的 L3 冲突报告。"""
    store = ConflictStore(connect())
    reports = store.open_reports()
    if not reports:
        console.print("没有待裁决的冲突报告")
        return
    t = Table(title="待裁决冲突")
    for col in ("报告", "分支", "基版本", "违规数", "明细截断"):
        t.add_column(col)
    for r in reports:
        t.add_row(
            f"#{r.report_id}",
            r.branch,
            str(r.base_version_id),
            str(r.violation_count),
            "是" if r.truncated else "否",
        )
    console.print(t)


@conflict_app.command("show")
def conflict_show(
    report_id: int = typer.Argument(..., help="冲突报告 id"),
    limit: int = typer.Option(20, "-n", "--limit", help="展示前 N 条明细"),
) -> None:
    """展示冲突报告明细与按类型的聚合。"""
    store = ConflictStore(connect())
    r = store.get(report_id)
    if r is None:
        console.print(f"[red]报告不存在[/red] #{report_id}")
        raise typer.Exit(1)

    console.print(r.summary() + f"  状态={r.status}")

    agg = r.by_entity_type()
    if agg:
        # 违规集中于单一类型 ⇒ 通常该走「约束下推到子类型」而非放宽全局约束
        t = Table(title="按实体类型聚合")
        t.add_column("类型")
        t.add_column("违规数")
        for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
            t.add_row(k, str(v))
        console.print(t)

    if r.violations:
        console.print(f"\n明细（前 {min(limit, len(r.violations))} 条）：")
        for v in r.violations[:limit]:
            console.print(f"  {v.describe()}")

    for a in store.adjudications(report_id):
        console.print(
            f"\n[dim]裁决[/dim] {a['action']} by {a['actor']}: {a['rationale']}"
        )


@conflict_app.command("adjudicate")
def conflict_adjudicate(
    report_id: int = typer.Argument(...),
    action: str = typer.Option(
        ..., "-a", "--action",
        help="amend_ontology（改本体）| amend_data（改数据）| abandon（放弃）",
    ),
    rationale: str = typer.Option(..., "-r", "--rationale", help="裁决理由，必填"),
    actor: str = typer.Option("human", "--actor"),
    result_version: int | None = typer.Option(None, "--result-version"),
    affected: int = typer.Option(0, "--affected", help="改数据触碰的实例数"),
) -> None:
    """登记裁决结果。

    本命令只记录裁决，不执行动作——改本体走 doa schema commit，改数据走 Assimilator。
    职责分离便于审计：裁决记录与实际变更各有出处。
    """
    if action not in ("amend_ontology", "amend_data", "abandon"):
        console.print(f"[red]未知动作[/red] {action}")
        raise typer.Exit(1)

    store = ConflictStore(connect())
    try:
        aid = store.adjudicate(
            report_id,
            action=action,  # type: ignore[arg-type]
            actor=actor,
            rationale=rationale,
            result_version_id=result_version,
            affected_instances=affected,
        )
    except ValueError as e:
        console.print(f"[red]拒绝[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]已裁决[/green] 报告 #{report_id} → {action}（记录 #{aid}）")


if __name__ == "__main__":
    app()
