"""W1 验收：版本图、分支、fast-forward 合并、回滚、区间可见性。

§6.1 的十条断言里有三条属于 W2/W3/W4（定级、增量写入、超边），W1 覆盖其余部分
再加上版本模型自身的不变式。
"""

from __future__ import annotations

import pytest

from doa.sr.delta import (
    AddAttributeType,
    AddEntityType,
    AddParserDef,
    AddRelationType,
    RemoveAttributeType,
    RemoveEntityType,
)
from doa.sr.registry import MAIN, SchemaError


def _names(snap) -> set[str]:
    return {e["name"] for e in snap["entity_types"]}


def _attrs(snap, entity: str) -> set[str]:
    for e in snap["entity_types"]:
        if e["name"] == entity:
            return {a["name"] for a in e["attributes"]}
    return set()


# ------------------------------------------------------------------ 基础

def test_bootstrap_creates_root(sr):
    head = sr.head()
    assert sr.log()[0].message == "root"
    assert sr.branches() == {MAIN: head}


def test_commit_advances_head(sr):
    before = sr.head()
    v = sr.commit([AddEntityType(name="Company")], message="加公司")
    assert v != before
    assert sr.head() == v
    assert _names(sr.snapshot()) == {"Company"}


def test_empty_changeset_rejected(sr):
    with pytest.raises(SchemaError, match="空变更集"):
        sr.commit([])


def test_duplicate_type_rejected(sr):
    sr.commit([AddEntityType(name="Company")])
    with pytest.raises(SchemaError, match="已存在"):
        sr.commit([AddEntityType(name="Company")])


def test_dangling_reference_rejected(sr):
    with pytest.raises(SchemaError, match="不存在"):
        sr.commit([AddAttributeType(entity_type_name="Ghost", name="x")])


def test_failed_commit_leaves_no_version(sr):
    """校验失败必须整体回滚，不留半个版本。"""
    head_before = sr.head()
    n_before = len(sr.log())
    with pytest.raises(SchemaError):
        sr.commit(
            [
                AddEntityType(name="Company"),          # 合法
                AddAttributeType(entity_type_name="Ghost", name="x"),  # 非法
            ]
        )
    assert sr.head() == head_before
    assert len(sr.log()) == n_before
    assert _names(sr.snapshot()) == set()


def test_same_batch_forward_reference_ok(sr):
    """同一批次内先建类型、后引用它，应当成立。"""
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddEntityType(name="Filing"),
            AddRelationType(name="files", domain_name="Company", range_name="Filing"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
        ]
    )
    snap = sr.snapshot()
    assert _names(snap) == {"Company", "Filing"}
    assert {r["name"] for r in snap["relation_types"]} == {"files"}


# ------------------------------------------------------------------ 区间可见性

def test_removed_type_invisible_at_head_but_visible_in_history(sr):
    v1 = sr.commit([AddEntityType(name="Company")])
    v2 = sr.commit([RemoveEntityType(name="Company")])

    assert _names(sr.snapshot(v2)) == set()      # head 处不可见
    assert _names(sr.snapshot(v1)) == {"Company"}  # 历史版本仍可见


def test_remove_entity_cascades_to_attributes_and_relations(sr):
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddEntityType(name="Filing"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
            AddRelationType(name="files", domain_name="Company", range_name="Filing"),
        ]
    )
    sr.commit([RemoveEntityType(name="Company")])
    snap = sr.snapshot()
    assert _names(snap) == {"Filing"}
    assert snap["relation_types"] == []      # 定义域消失，关系一并下线


def test_readd_after_remove_is_allowed(sr):
    """同名类型可以删除后重新加入——这正是唯一性不能做成数据库约束的原因。"""
    sr.commit([AddEntityType(name="Company")])
    sr.commit([RemoveEntityType(name="Company")])
    v3 = sr.commit([AddEntityType(name="Company", uri="urn:v2")])
    assert _names(sr.snapshot(v3)) == {"Company"}


def test_remove_attribute_keeps_entity(sr):
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
            AddAttributeType(entity_type_name="Company", name="sector"),
        ]
    )
    sr.commit([RemoveAttributeType(entity_type_name="Company", name="sector")])
    snap = sr.snapshot()
    assert _attrs(snap, "Company") == {"ticker"}


# ------------------------------------------------------------------ 分支与合并

def test_branch_isolation(sr):
    sr.commit([AddEntityType(name="Company")])
    sr.create_branch("proposal-a")
    sr.commit([AddEntityType(name="Segment")], branch="proposal-a")

    assert _names(sr.snapshot(branch="proposal-a")) == {"Company", "Segment"}
    assert _names(sr.snapshot(branch=MAIN)) == {"Company"}  # main 不受影响


def test_fast_forward_merge(sr):
    sr.commit([AddEntityType(name="Company")])
    sr.create_branch("proposal-a")
    v = sr.commit([AddEntityType(name="Segment")], branch="proposal-a")

    assert sr.merge("proposal-a") == v
    assert sr.head(MAIN) == v
    assert _names(sr.snapshot(branch=MAIN)) == {"Company", "Segment"}


def test_non_fast_forward_merge_rejected(sr):
    """main 上有提案分支没有的版本 ⇒ 需要三路合并 ⇒ 拒绝。"""
    sr.commit([AddEntityType(name="Company")])
    sr.create_branch("proposal-a")
    sr.commit([AddEntityType(name="Segment")], branch="proposal-a")
    sr.commit([AddEntityType(name="Filing")], branch=MAIN)  # main 前进，产生分叉

    with pytest.raises(SchemaError, match="非 fast-forward"):
        sr.merge("proposal-a")


def test_duplicate_branch_rejected(sr):
    sr.create_branch("proposal-a")
    with pytest.raises(SchemaError, match="分支已存在"):
        sr.create_branch("proposal-a")


def test_two_branches_may_hold_same_type_name(sr):
    """两个分支各自加同名类型都应成立——唯一性只在单一版本线内生效。"""
    sr.commit([AddEntityType(name="Company")])
    sr.create_branch("proposal-a")
    sr.create_branch("proposal-b")
    sr.commit([AddAttributeType(entity_type_name="Company", name="ticker")], branch="proposal-a")
    sr.commit([AddAttributeType(entity_type_name="Company", name="ticker")], branch="proposal-b")

    assert _attrs(sr.snapshot(branch="proposal-a"), "Company") == {"ticker"}
    assert _attrs(sr.snapshot(branch="proposal-b"), "Company") == {"ticker"}
    # 先合并的能进，后合并的因分叉被拒
    sr.merge("proposal-a")
    with pytest.raises(SchemaError, match="非 fast-forward"):
        sr.merge("proposal-b")


# ------------------------------------------------------------------ 回滚

def test_rollback_one_step(sr):
    v1 = sr.commit([AddEntityType(name="Company")])
    sr.commit([AddEntityType(name="Segment")])

    assert sr.rollback() == v1
    assert sr.head() == v1
    assert _names(sr.snapshot()) == {"Company"}


def test_rollback_to_specific_version(sr):
    root = sr.head()
    sr.commit([AddEntityType(name="Company")])
    sr.commit([AddEntityType(name="Segment")])

    sr.rollback(to_version=root)
    assert _names(sr.snapshot()) == set()


def test_rollback_to_non_ancestor_rejected(sr):
    sr.commit([AddEntityType(name="Company")])
    sr.create_branch("proposal-a")
    other = sr.commit([AddEntityType(name="Segment")], branch="proposal-a")

    with pytest.raises(SchemaError, match="不是"):
        sr.rollback(MAIN, to_version=other)  # 旁支版本，不是 main 的祖先


def test_rollback_at_root_rejected(sr):
    with pytest.raises(SchemaError, match="根版本"):
        sr.rollback()


def test_rollback_then_commit_forks(sr):
    """回滚后再提交会产生分叉：被丢弃的版本仍在表里，只是无人指向。"""
    v1 = sr.commit([AddEntityType(name="Company")])
    v2 = sr.commit([AddEntityType(name="Segment")])
    sr.rollback(to_version=v1)
    v3 = sr.commit([AddEntityType(name="Filing")])

    assert _names(sr.snapshot()) == {"Company", "Filing"}
    assert v2 not in [v.version_id for v in sr.log()]   # 不在当前分支线上
    assert _names(sr.snapshot(v2)) == {"Company", "Segment"}  # 但仍可读


# ------------------------------------------------------------------ 派生属性与 parser

def test_derived_attribute_cannot_be_required(sr):
    sr.commit([AddEntityType(name="Company")])
    with pytest.raises(SchemaError, match="派生属性"):
        sr.commit(
            [
                AddAttributeType(
                    entity_type_name="Company",
                    name="yoy",
                    derivation_expr="revenue / same_period_last_year(revenue) - 1",
                    min_cardinality=1,
                )
            ]
        )


def test_derived_attribute_index_flag_roundtrip(sr):
    """派生属性入不入索引是二维代价的开关，必须能读回。"""
    sr.commit(
        [
            AddEntityType(name="Company"),
            # 派生表达式引用的属性必须先存在——提交时即校验
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
        ]
    )
    sr.commit(
        [
            AddAttributeType(
                entity_type_name="Company",
                name="yoy_indexed",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
                index_derived=True,
            ),
            AddAttributeType(
                entity_type_name="Company",
                name="yoy_lazy",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
                index_derived=False,
            ),
        ]
    )
    attrs = {
        a["name"]: a
        for e in sr.snapshot()["entity_types"]
        if e["name"] == "Company"
        for a in e["attributes"]
    }
    assert attrs["yoy_indexed"]["index_derived"] is True
    assert attrs["yoy_lazy"]["index_derived"] is False


def test_parser_chain_ordered_by_priority(sr):
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
            AddParserDef(
                entity_type_name="Company", attribute_name="revenue",
                pattern=r"^([\d.]+)亿$", priority=10,
            ),
            AddParserDef(
                entity_type_name="Company", attribute_name="revenue",
                pattern=r"^([\d,]+)$", priority=20,
            ),
        ]
    )
    for e in sr.snapshot()["entity_types"]:
        if e["name"] == "Company":
            parsers = e["attributes"][0]["parsers"]
    assert [p["priority"] for p in parsers] == [10, 20]  # 按 priority 升序，链式回退的顺序


def test_delta_json_persisted(sr):
    """delta 原文要存下来——W2 定级器与审计都要读它。"""
    sr.commit([AddEntityType(name="Company")], message="加公司")
    assert sr.log()[0].delta_count == 1
