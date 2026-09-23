"""W2 第一步：reasoner-service 连通性 + SR → OWL → HermiT 链路。

服务未启动时整体跳过，不让 W1 的测试被连带拖红。
"""

from __future__ import annotations

import pytest

from doa.reasoner import ReasonerClient
from doa.sr.delta import AddAttributeType, AddEntityType, AddRelationType
from doa.sr.owl_export import snapshot_to_owl


@pytest.fixture(scope="module")
def client() -> ReasonerClient:
    c = ReasonerClient()
    if not c.available():
        pytest.skip(
            "reasoner-service 未启动。先跑："
            " java -jar reasoner-service/target/reasoner-service.jar",
            allow_module_level=True,
        )
    return c


# ------------------------------------------------------------------ 服务本身

def test_health(client):
    h = client.health()
    assert h["status"] == "ok"
    assert h["reasoner"] == "HermiT"


def test_consistent_ontology(client):
    owl = """
    Prefix(:=<urn:doa:>)
    Ontology(<urn:doa:t>
      Declaration(Class(:Company))
      Declaration(Class(:Filing))
      DisjointClasses(:Company :Filing)
    )
    """
    r = client.consistency(owl)
    assert r.consistent is True
    assert r.unsatisfiable_classes == []
    assert r.ok is True


def test_inconsistent_ontology(client):
    """个体同属两个互斥类 ⇒ 本体不一致。"""
    owl = """
    Prefix(:=<urn:doa:>)
    Ontology(<urn:doa:t>
      Declaration(Class(:Company))
      Declaration(Class(:Filing))
      DisjointClasses(:Company :Filing)
      Declaration(NamedIndividual(:x))
      ClassAssertion(:Company :x)
      ClassAssertion(:Filing :x)
    )
    """
    r = client.consistency(owl)
    assert r.consistent is False
    assert r.ok is False


def test_consistent_but_unsatisfiable_class(client):
    """一致 ≠ 无缺陷。

    Weird ⊑ Company ⊓ Filing 而二者互斥 ⇒ Weird 永远不可能有实例，
    但本体本身是一致的。两个信号必须分开报，否则约束收紧引入的这类缺陷会漏掉。
    """
    owl = """
    Prefix(:=<urn:doa:>)
    Ontology(<urn:doa:t>
      Declaration(Class(:Company))
      Declaration(Class(:Filing))
      Declaration(Class(:Weird))
      DisjointClasses(:Company :Filing)
      SubClassOf(:Weird :Company)
      SubClassOf(:Weird :Filing)
    )
    """
    r = client.consistency(owl)
    assert r.consistent is True             # 一致
    assert r.unsatisfiable_classes == ["urn:doa:Weird"]
    assert r.ok is False                    # 但不合格


def test_malformed_ontology_rejected(client):
    with pytest.raises(ValueError, match="解析失败"):
        client.consistency("this is not owl at all")


# ------------------------------------------------------------------ SR → OWL

def test_snapshot_exports_and_passes_reasoner(sr, client):
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddEntityType(name="Filing"),
            AddEntityType(name="AnnualFiling", parent_name="Filing"),
            AddRelationType(name="files", domain_name="Company", range_name="Filing"),
            AddAttributeType(
                entity_type_name="Company", name="ticker", min_cardinality=1
            ),
            AddAttributeType(
                entity_type_name="Company", name="revenue", datatype="number"
            ),
        ]
    )
    owl = snapshot_to_owl(sr.snapshot())

    assert "Declaration(Class(:Company))" in owl
    assert "SubClassOf(:AnnualFiling :Filing)" in owl
    assert "ObjectPropertyDomain(:files :Company)" in owl
    assert "DataPropertyRange(:revenue xsd:decimal)" in owl
    assert "SubClassOf(:Company DataMinCardinality(1 :ticker))" in owl

    r = client.consistency(owl)
    assert r.ok is True
    assert r.class_count == 3


def test_derived_attribute_excluded_by_default(sr, client):
    """派生属性无存量值，默认不入 OWL——否则 minCardinality 类约束会误报。"""
    sr.commit([AddEntityType(name="Company")])
    sr.commit(
        [
            AddAttributeType(
                entity_type_name="Company",
                name="revenue_yoy",
                datatype="number",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
            )
        ]
    )
    snap = sr.snapshot()
    assert "revenue_yoy" not in snapshot_to_owl(snap)
    assert "revenue_yoy" in snapshot_to_owl(snap, include_derived=True)


def test_removed_parent_does_not_dangle(sr, client):
    """父类被移除后，子类不应产出悬挂的 SubClassOf。

    否则 OWLAPI 会把未声明的父类当成一个新类，静默改变推理结果。
    """
    sr.commit([AddEntityType(name="Filing")])
    sr.commit([AddEntityType(name="AnnualFiling", parent_name="Filing")])
    # 直接改快照模拟父类缺失的情形（删除 Filing 会级联，这里要的是单独缺父类）
    snap = sr.snapshot()
    snap["entity_types"] = [e for e in snap["entity_types"] if e["name"] != "Filing"]

    owl = snapshot_to_owl(snap)
    assert "SubClassOf(:AnnualFiling :Filing)" not in owl
    assert client.consistency(owl).ok is True


def test_shared_attribute_domain_is_union(sr, client):
    """同名属性挂在多个类型上 ⇒ 定义域取并集。

    OWL 里多条 DataPropertyDomain 公理的语义是合取，直接各写一条会推出
    「两类的交集」，反而可能让两个互斥类的共有属性变成不可满足。
    """
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddEntityType(name="Fund"),
            AddAttributeType(entity_type_name="Company", name="name_cn"),
            AddAttributeType(entity_type_name="Fund", name="name_cn"),
        ]
    )
    owl = snapshot_to_owl(sr.snapshot())
    assert "ObjectUnionOf(:Company :Fund)" in owl
    assert client.consistency(owl).ok is True


def test_conflicting_datatype_is_detected(sr, client):
    """同名属性在两个类型上声明了不同 datatype ⇒ 必须被推理机发现。

    回归测试：早先的实现用 setdefault 只保留第一个 datatype，冲突被静默吞掉，
    推理机根本收不到矛盾的公理。每个 range 都要导出，OWL 的合取语义才能让
    decimal ⊓ string = ∅ 暴露出来。
    """
    sr.commit(
        [
            AddEntityType(name="Corp"),
            AddEntityType(name="Fund"),
            AddAttributeType(
                entity_type_name="Corp", name="size", datatype="number", min_cardinality=1
            ),
            AddAttributeType(
                entity_type_name="Fund", name="size", datatype="string", min_cardinality=1
            ),
        ]
    )
    owl = snapshot_to_owl(sr.snapshot())
    assert "DataPropertyRange(:size xsd:decimal)" in owl
    assert "DataPropertyRange(:size xsd:string)" in owl

    res = client.consistency(owl)
    assert res.ok is False
    assert set(res.unsatisfiable_classes) == {"urn:doa:Corp", "urn:doa:Fund"}


def test_same_datatype_shared_attribute_is_fine(sr, client):
    """同名同类型的共享属性不应误报。"""
    sr.commit(
        [
            AddEntityType(name="Corp"),
            AddEntityType(name="Fund"),
            AddAttributeType(entity_type_name="Corp", name="size", datatype="number"),
            AddAttributeType(entity_type_name="Fund", name="size", datatype="number"),
        ]
    )
    owl = snapshot_to_owl(sr.snapshot())
    assert owl.count("DataPropertyRange(:size") == 1
    assert client.consistency(owl).ok is True


def test_disjoint_siblings_opt_in(sr, client):
    """兄弟互斥默认关闭：金融场景下一个实体常合法地同属多个兄弟类型。"""
    sr.commit(
        [
            AddEntityType(name="Party"),
            AddEntityType(name="Issuer", parent_name="Party"),
            AddEntityType(name="Investor", parent_name="Party"),
        ]
    )
    snap = sr.snapshot()
    assert "DisjointClasses" not in snapshot_to_owl(snap)
    assert "DisjointClasses(:Investor :Issuer)" in snapshot_to_owl(
        snap, disjoint_siblings=True
    )
