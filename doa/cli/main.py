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
from doa.sr import SchemaRegistry
from doa.sr.delta import parse_deltas
from doa.sr.registry import MAIN, SchemaError

app = typer.Typer(help="动态本体演化引擎", no_args_is_help=True)
schema_app = typer.Typer(help="本体模式与版本管理", no_args_is_help=True)
app.add_typer(schema_app, name="schema")

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


if __name__ == "__main__":
    app()
