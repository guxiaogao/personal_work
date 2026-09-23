"""SchemaRegistry 快照 → OWL 函数式语法。

只导出**约束子集**，不追求完整 OWL 本体（见项目计划 §7 的退路条款）：
SR 是主干表示，OWL 只是喂给推理机的一个视图。

映射
----
| SR                          | OWL                                      |
|-----------------------------|------------------------------------------|
| EntityType                  | Declaration(Class(...))                  |
| EntityType.parent_name      | SubClassOf(child, parent)                |
| RelationType                | ObjectProperty + Domain + Range          |
| AttributeType               | DataProperty + Domain + Range(datatype)  |
| min_cardinality > 0         | SubClassOf(owner, min 1 prop)            |
| 同层兄弟类型                | DisjointClasses(...)                     |

**关于兄弟互斥**：默认不声明。声明它会让"某实体同属两个兄弟类型"变成不一致，
这在金融场景常常是误报（一家公司同时是发行人和投资者）。需要时由调用方显式开启，
这正是 L3 判据要测的场景之一。
"""

from __future__ import annotations

from typing import Any

BASE = "urn:doa:"

# SR datatype → XSD
_XSD = {
    "string": "xsd:string",
    "number": "xsd:decimal",
    "date": "xsd:date",
    "bool": "xsd:boolean",
}


def _iri(name: str) -> str:
    return f":{name}"


def snapshot_to_owl(
    snapshot: dict[str, Any],
    *,
    ontology_iri: str | None = None,
    disjoint_siblings: bool = False,
    include_derived: bool = False,
) -> str:
    """把 SchemaRegistry.snapshot() 的输出转为 OWL 函数式语法文本。

    参数
    ----
    disjoint_siblings
        是否为同一父类下的兄弟类型声明 DisjointClasses。默认关闭，见模块文档。
    include_derived
        是否导出派生属性。默认关闭——派生属性无存量值，纳入推理会让
        minCardinality 类的约束产生误报。
    """
    version = snapshot.get("version_id", "unknown")
    iri = ontology_iri or f"{BASE}schema/v{version}"

    lines: list[str] = [
        f"Prefix(:=<{BASE}>)",
        "Prefix(owl:=<http://www.w3.org/2002/07/owl#>)",
        "Prefix(xsd:=<http://www.w3.org/2001/XMLSchema#>)",
        "Prefix(rdfs:=<http://www.w3.org/2000/01/rdf-schema#>)",
        "",
        f"Ontology(<{iri}>",
    ]

    entities = snapshot.get("entity_types", [])
    entity_names = {e["name"] for e in entities}

    # ---------------------------------------------------------------- 类
    for e in entities:
        lines.append(f"  Declaration(Class({_iri(e['name'])}))")

    for e in entities:
        parent = e.get("parent_name")
        if parent:
            # 父类可能已被移除——此时跳过而非产出悬挂引用，
            # 否则 OWLAPI 会把它当成一个未声明的新类，静默改变推理结果
            if parent in entity_names:
                lines.append(f"  SubClassOf({_iri(e['name'])} {_iri(parent)})")

    if disjoint_siblings:
        by_parent: dict[str | None, list[str]] = {}
        for e in entities:
            by_parent.setdefault(e.get("parent_name"), []).append(e["name"])
        for siblings in by_parent.values():
            if len(siblings) >= 2:
                joined = " ".join(_iri(s) for s in sorted(siblings))
                lines.append(f"  DisjointClasses({joined})")

    # ---------------------------------------------------------------- 对象属性
    for r in snapshot.get("relation_types", []):
        p = _iri(r["name"])
        lines.append(f"  Declaration(ObjectProperty({p}))")
        if r["domain_name"] in entity_names:
            lines.append(f"  ObjectPropertyDomain({p} {_iri(r['domain_name'])})")
        if r["range_name"] in entity_names:
            lines.append(f"  ObjectPropertyRange({p} {_iri(r['range_name'])})")

    # ---------------------------------------------------------------- 数据属性
    # 同名属性可挂在多个 EntityType 上，OWL 里是同一个 DataProperty，
    # 声明去重、定义域取并集（OWL 的多个 Domain 公理语义是合取，故用 ObjectUnionOf）
    declared: set[str] = set()
    domains: dict[str, list[str]] = {}
    ranges: dict[str, set[str]] = {}
    required: list[tuple[str, str]] = []

    for e in entities:
        for a in e.get("attributes", []):
            if a.get("derivation_expr") and not include_derived:
                continue
            name = a["name"]
            if name not in declared:
                declared.add(name)
                lines.append(f"  Declaration(DataProperty({_iri(name)}))")
            domains.setdefault(name, []).append(e["name"])
            ranges.setdefault(name, set()).add(
                _XSD.get(a.get("datatype", "string"), "xsd:string")
            )
            if a.get("min_cardinality", 0) > 0:
                required.append((e["name"], name))

    for name in sorted(declared):
        owners = sorted(set(domains[name]))
        if len(owners) == 1:
            lines.append(f"  DataPropertyDomain({_iri(name)} {_iri(owners[0])})")
        else:
            union = " ".join(_iri(o) for o in owners)
            lines.append(f"  DataPropertyDomain({_iri(name)} ObjectUnionOf({union}))")

        # 同名属性在不同类型上声明了不同 datatype 时，每个都要导出。
        # OWL 中多条 DataPropertyRange 语义是合取，decimal ⊓ string 为空，
        # 冲突于是以「类不可满足」的形式被推理机报出。
        # 只导出第一个会把冲突静默吞掉，推理机根本无从发现。
        for dt in sorted(ranges[name]):
            lines.append(f"  DataPropertyRange({_iri(name)} {dt})")

    for owner, name in sorted(required):
        lines.append(
            f"  SubClassOf({_iri(owner)} DataMinCardinality(1 {_iri(name)}))"
        )

    lines.append(")")
    return "\n".join(lines) + "\n"
