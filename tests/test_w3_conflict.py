"""L3 冲突报告与人工裁决。

专利未涉及 validator 失败后的处置，这部分是自行设计：
动作三选一（改本体 / 改数据 / 放弃），理由必填，报告明细封顶。
"""

from __future__ import annotations

import pytest

from doa.evolve import MAX_VIOLATION_DETAIL, ConflictStore, Violation
from doa.sr.delta import AddAttributeType, AddEntityType


@pytest.fixture()
def store(conn) -> ConflictStore:
    return ConflictStore(conn)


def _viol(n: int = 1, entity: str = "Company", **kw) -> list[Violation]:
    return [
        Violation(
            kind="missing_required",
            entity_type_name=entity,
            attribute_name="INVENTORY",
            instance_id=1000 + i,
            subject_key=f"{600000 + i:06d}",
            current_value=None,
            constraint_desc="minCardinality=1 但存量为空",
            **kw,
        )
        for i in range(n)
    ]


# ------------------------------------------------------------------ 记录


def test_record_and_read_back(sr, store):
    base = sr.head()
    r = store.record(
        branch="proposal-a",
        base_version_id=base,
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(3),
    )
    assert r.violation_count == 3
    assert r.truncated is False
    assert r.status == "open"

    back = store.get(r.report_id)
    assert back is not None
    assert len(back.violations) == 3
    assert back.violations[0].subject_key == "600000"
    assert back.violations[0].constraint_desc == "minCardinality=1 但存量为空"


def test_detail_capped_but_count_is_truthful(sr, store):
    """明细封顶，总数如实——跨行业灌数据可能产生上万条违规。"""
    n = MAX_VIOLATION_DETAIL + 57
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(n),
    )
    assert r.violation_count == n                      # 总数如实
    assert len(r.violations) == MAX_VIOLATION_DETAIL   # 明细封顶
    assert r.truncated is True
    assert f"违规 {n} 条" in r.summary()

    back = store.get(r.report_id)
    assert back.violation_count == n
    assert len(back.violations) == MAX_VIOLATION_DETAIL


def test_by_entity_type_aggregation(sr, store):
    """按类型聚合：违规集中于某一类型，提示该走约束下推而非放宽全局约束。"""
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(5, entity="Bank") + _viol(2, entity="Distillery"),
    )
    assert r.by_entity_type() == {"Bank": 5, "Distillery": 2}


def test_aggregation_counts_full_set_not_truncated_detail(sr, store):
    """聚合必须基于全量违规，不能基于被截断的明细。

    回归测试：早先 by_entity_type() 在 self.violations（已封顶 200 条）上现场聚合，
    导致两个错误——受影响多的类型被低估为封顶值，排在后面的小众类型彻底消失。
    裁决的人因此会漏掉该类型，下次灌它的数据时再撞一次同样的冲突。
    """
    n_bank, n_broker = MAX_VIOLATION_DETAIL + 980, 23
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(n_bank, entity="Bank") + _viol(n_broker, entity="Broker"),
    )
    assert r.truncated is True
    assert len(r.violations) == MAX_VIOLATION_DETAIL      # 明细确实被截断
    # 但聚合是全量的：银行不是封顶值，券商也没消失
    assert r.by_entity_type() == {"Bank": n_bank, "Broker": n_broker}

    # 且能从库里读回——明细已丢，聚合不能只活在内存里
    back = store.get(r.report_id)
    assert back.by_entity_type() == {"Bank": n_bank, "Broker": n_broker}


def test_by_kind_aggregation(sr, store):
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=(
            _viol(3, entity="Bank")
            + [
                Violation(
                    kind="data_loss",
                    entity_type_name="Bank",
                    attribute_name="LOAN_PBC",
                    constraint_desc="删除属性会丢弃已有实例数据",
                )
            ]
        ),
    )
    assert r.by_kind() == {"missing_required": 3, "data_loss": 1}


def test_record_with_no_violations(sr, store):
    """无明细也能记录——某些 L3（如删类型）的理由是语义而非逐条违规。"""
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=[],
    )
    assert r.violation_count == 0
    assert r.truncated is False
    assert store.get(r.report_id).violations == []


# ------------------------------------------------------------------ 裁决


def test_adjudicate_amend_ontology(sr, store):
    """改本体：放宽约束后重新提交，产出新版本。"""
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(3),
    )
    # 实际的放宽动作走 SchemaRegistry，裁决只登记结果
    new_v = sr.commit([AddEntityType(name="Bank")], message="改为可选属性后重提")

    store.adjudicate(
        r.report_id,
        action="amend_ontology",
        actor="analyst@doa",
        rationale="银行无存货科目，INVENTORY 必填约束应降为可选",
        result_version_id=new_v,
    )
    assert store.get(r.report_id).status == "resolved"
    adj = store.adjudications(r.report_id)
    assert len(adj) == 1
    assert adj[0]["action"] == "amend_ontology"
    assert adj[0]["result_version_id"] == new_v


def test_adjudicate_amend_data(sr, store):
    """改数据：本体不变，触碰存量实例。"""
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(12),
    )
    store.adjudicate(
        r.report_id,
        action="amend_data",
        actor="analyst@doa",
        rationale="12 家公司确实漏报存货，按公告补值",
        affected_instances=12,
    )
    adj = store.adjudications(r.report_id)[0]
    assert adj["action"] == "amend_data"
    assert adj["affected_instances"] == 12
    assert adj["result_version_id"] is None    # 本体未变


def test_adjudicate_abandon(sr, store):
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(1),
    )
    store.adjudicate(
        r.report_id,
        action="abandon",
        actor="analyst@doa",
        rationale="该约束本身建模有误，提案作废",
    )
    adj = store.adjudications(r.report_id)[0]
    assert adj["action"] == "abandon"
    assert adj["result_version_id"] is None
    assert adj["affected_instances"] == 0


def test_rationale_is_mandatory(sr, store):
    """理由必填：这是审计与 L3 拦截准确率核验的依据。"""
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(1),
    )
    for bad in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="必须填写理由"):
            store.adjudicate(
                r.report_id, action="abandon", actor="a", rationale=bad
            )
    assert store.get(r.report_id).status == "open"   # 失败不改状态


def test_double_adjudication_rejected(sr, store):
    r = store.record(
        branch="proposal-a",
        base_version_id=sr.head(),
        deltas=[AddEntityType(name="Bank")],
        violations=_viol(1),
    )
    store.adjudicate(r.report_id, action="abandon", actor="a", rationale="作废")
    with pytest.raises(ValueError, match="已裁决"):
        store.adjudicate(r.report_id, action="abandon", actor="a", rationale="再来一次")


def test_adjudicate_missing_report(store):
    with pytest.raises(ValueError, match="不存在"):
        store.adjudicate(99999, action="abandon", actor="a", rationale="x")


# ------------------------------------------------------------------ 队列


def test_open_reports_excludes_resolved(sr, store):
    base = sr.head()
    r1 = store.record(
        branch="p1", base_version_id=base, deltas=[AddEntityType(name="A")],
        violations=_viol(1),
    )
    r2 = store.record(
        branch="p2", base_version_id=base, deltas=[AddEntityType(name="B")],
        violations=_viol(1),
    )
    assert {r.report_id for r in store.open_reports()} == {r1.report_id, r2.report_id}

    store.adjudicate(r1.report_id, action="abandon", actor="a", rationale="作废")
    assert {r.report_id for r in store.open_reports()} == {r2.report_id}


def test_violation_describe_is_readable(sr, store):
    v = _viol(1)[0]
    s = v.describe()
    assert "missing_required" in s
    assert "Company.INVENTORY" in s
    assert "600000" in s
