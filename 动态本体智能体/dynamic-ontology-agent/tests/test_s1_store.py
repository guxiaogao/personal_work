"""InstanceStore：双时间戳、反查影响集、零写入断言。

本文件覆盖 §1.1 的断言 1（L0/L1 零写入）与断言 2 的前半（影响集可枚举）。
断言 2 的后半（重解析数 == 影响集）待 S3 的 Assimilator。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from doa.derive import evaluate
from doa.ingest import FieldMapping, Ingestor, ObjectMapping
from doa.sr.delta import AddAttributeType, AddEntityType, AddParserDef, AddValidatorDef
from doa.store import InstanceStore, OutOfOrderDisclosure, StoreResolver

Q3_2024 = date(2024, 9, 30)
Q3_2023 = date(2023, 9, 30)

# 种子数据的首次披露日。必须显式给出：默认 now() 会晚于后续测试构造的
# 「追溯调整」披露日，触发乱序守门。
FIRST_DISCLOSURE = datetime(2024, 10, 30)
RESTATED_AT = datetime(2025, 3, 15)


@pytest.fixture()
def store(conn) -> InstanceStore:
    return InstanceStore(conn)


@pytest.fixture()
def seeded(sr, store):
    """一个最小的金融本体 + 两家公司的营收数据。"""
    v = sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
        ]
    )
    aid = _attr_id(sr, "Company", "revenue")
    for subject, amount in (("600519", "100"), ("000001", "200")):
        iid = store.upsert_instance("Company", subject, v)
        store.put_attr(
            instance_id=iid,
            attribute_type_id=aid,
            value=amount,
            valid_from=None,
            valid_to=Q3_2024,
            disclosed_at=FIRST_DISCLOSURE,
            schema_version=v,
        )
    store.commit()
    return v


def _attr_id(sr, entity: str, name: str) -> int:
    # 默认游标返回元组，取列要么用下标要么指定 row_factory；这里按下标取
    with sr.conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM attribute_type
                WHERE entity_type_name = %s AND name = %s AND removed_in IS NULL
                ORDER BY id DESC LIMIT 1""",
            (entity, name),
        )
        return cur.fetchone()[0]


# ------------------------------------------------------------------ 实例


def test_upsert_instance_is_idempotent(sr, store):
    v = sr.commit([AddEntityType(name="Company")])
    a = store.upsert_instance("Company", "600519", v)
    b = store.upsert_instance("Company", "600519", v)
    assert a == b
    assert store.count_instances("Company") == 1


def test_get_instance(sr, store, seeded):
    assert store.get_instance("Company", "600519") is not None
    assert store.get_instance("Company", "999999") is None


# ------------------------------------------------------------------ 双时间戳


def test_same_value_does_not_create_history(sr, store, seeded):
    """值没变就不写新行——否则每次重抓数据都制造一条无意义的历史。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")
    before = store.count_attr_rows(attribute_type_id=aid)

    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="100",
        valid_from=None, valid_to=Q3_2024, schema_version=seeded,
    )
    store.commit()
    assert store.count_attr_rows(attribute_type_id=aid) == before


def test_restatement_closes_old_row_and_adds_new(sr, store, seeded):
    """追溯调整：关闭旧行的事务区间，再插新行。旧值仍可查。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")

    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="105",
        valid_from=None, valid_to=Q3_2024,
        disclosed_at=RESTATED_AT, schema_version=seeded,
    )
    store.commit()

    hist = store.attr_history(iid, aid, period=Q3_2024)
    assert len(hist) == 2
    old, new = hist
    assert old.value == "100" and old.tx_to is not None    # 旧行被关闭
    assert new.value == "105" and new.tx_to is None        # 新行为当前值
    assert old.tx_from < old.tx_to                         # 区间方向正确

    current = store.current_attrs(iid, as_of_period=Q3_2024)
    assert [a.value for a in current if a.attribute_name == "revenue"] == ["105"]


def test_value_as_believed_at_reconstructs_history(sr, store, seeded):
    """双时间戳的意义：追溯调整后，历史认知仍可重建。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")

    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="105",
        valid_from=None, valid_to=Q3_2024,
        disclosed_at=RESTATED_AT, schema_version=seeded,
    )
    store.commit()

    # 调整之前相信 100，之后相信 105
    assert store.value_as_believed_at(iid, aid, Q3_2024, datetime(2024, 12, 1)) == "100"
    assert store.value_as_believed_at(iid, aid, Q3_2024, datetime(2025, 6, 1)) == "105"
    # 首次披露之前无值
    assert store.value_as_believed_at(iid, aid, Q3_2024, datetime(2024, 1, 1)) is None


def test_out_of_order_disclosure_rejected(sr, store, seeded):
    """新披露日早于现有行的事务起点 ⇒ 拒绝，而非写出倒置区间。

    乱序回填是真实场景（先抓新报告、后补旧披露），但正确处理需要在历史中间
    插入事务区间，超出 MVP 范围。静默写入会产生 tx_from > tx_to 的行，
    让 value_as_believed_at 的结果失去意义——宁可报错。
    """
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")

    with pytest.raises(OutOfOrderDisclosure, match="早于"):
        store.put_attr(
            instance_id=iid, attribute_type_id=aid, value="105",
            valid_from=None, valid_to=Q3_2024,
            disclosed_at=datetime(2024, 1, 1),   # 早于种子的 2024-10-30
            schema_version=seeded,
        )


def test_different_periods_coexist(sr, store, seeded):
    """不同报告期是并存的行，不互相关闭。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")

    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="90",
        valid_from=None, valid_to=Q3_2023, schema_version=seeded,
    )
    store.commit()

    assert len(store.attr_history(iid, aid)) == 2
    assert all(a.tx_to is None for a in store.attr_history(iid, aid))  # 两行都是当前值


# ------------------------------------------------------------------ L2 影响集


def test_impact_set_enumerates_affected_instances(sr, store, seeded):
    """L2「影响集可枚举」的实现依据：按 attribute_type_id 反查。"""
    aid = _attr_id(sr, "Company", "revenue")
    impact = store.impact_set(aid)
    assert impact.size == 2
    assert impact.value_row_count == 2
    assert not impact.is_empty


def test_impact_set_of_unused_attribute_is_empty(sr, store, seeded):
    """新加的属性还没有值 ⇒ 影响集为空 ⇒ 这正是 L0 的依据。"""
    sr.commit([AddAttributeType(entity_type_name="Company", name="sector")])
    aid = _attr_id(sr, "Company", "sector")
    assert store.impact_set(aid).is_empty


def test_impact_set_excludes_closed_rows(sr, store, seeded):
    """已关闭的事务区间不计入影响集——重解析只针对当前值。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")
    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="105",
        valid_from=None, valid_to=Q3_2024,
        disclosed_at=RESTATED_AT, schema_version=seeded,
    )
    store.commit()

    impact = store.impact_set(aid)
    assert impact.size == 2            # 仍是两个实例
    assert impact.value_row_count == 2  # 但只数当前行，不数被关闭的那条


def test_missing_required_finds_violators(sr, store, seeded):
    """必填约束收紧时的 L3 违规清单来源。"""
    sr.commit([AddAttributeType(entity_type_name="Company", name="inventory")])
    aid = _attr_id(sr, "Company", "inventory")

    violators = store.missing_required(aid, "Company")
    assert {sk for _, sk in violators} == {"600519", "000001"}

    # 给其中一家补值后，它不再违规
    iid = store.get_instance("Company", "600519")
    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="42",
        valid_from=None, valid_to=Q3_2024, schema_version=seeded,
    )
    store.commit()
    assert {sk for _, sk in store.missing_required(aid, "Company")} == {"000001"}


def test_empty_string_counts_as_missing(sr, store, seeded):
    """财报字段常以空串表示不适用，应算缺失。"""
    sr.commit([AddAttributeType(entity_type_name="Company", name="inventory")])
    aid = _attr_id(sr, "Company", "inventory")
    iid = store.get_instance("Company", "600519")
    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="",
        valid_from=None, valid_to=Q3_2024, schema_version=seeded,
    )
    store.commit()
    assert {sk for _, sk in store.missing_required(aid, "Company")} == {"600519", "000001"}


# ------------------------------------------------------------------ 断言 1：零写入


def test_l0_new_entity_type_zero_write(sr, store, seeded):
    """§1.1 断言 1：新增 EntityType 后，instance_attr 无新行。"""
    before = store.count_attr_rows()
    sr.commit([AddEntityType(name="Bank")])
    assert store.count_attr_rows() == before


def test_l0_optional_attribute_zero_write(sr, store, seeded):
    """§1.1 断言 1：加可选属性后存量零写。

    元数据与实例分离的直接体现——SR 里多一条记录，IS 一行不动。
    """
    before = store.count_attr_rows()
    sr.commit(
        [AddAttributeType(entity_type_name="Company", name="ACCEPT_DEPOSIT", datatype="number")]
    )
    assert store.count_attr_rows() == before


def test_l1_derived_attribute_zero_write(sr, store, seeded):
    """§1.1 断言 1：加派生属性，实例层零写。

    这是二维代价模型里 (L0, L2) 那一格的实例侧：惰求值意味着不写行。
    """
    before = store.count_attr_rows()
    sr.commit(
        [
            AddAttributeType(
                entity_type_name="Company",
                name="revenue_yoy",
                datatype="number",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
                index_derived=True,
            )
        ]
    )
    assert store.count_attr_rows() == before


def test_l0_parser_addition_zero_write(sr, store, seeded):
    """加 parser 定义只是元数据，不触碰存量。"""
    before = store.count_attr_rows()
    sr.commit(
        [
            AddParserDef(
                entity_type_name="Company", attribute_name="revenue",
                kind="code_module", pattern="cn_amount", priority=10,
            )
        ]
    )
    assert store.count_attr_rows() == before


# ------------------------------------------------------------------ 派生属性惰求值


def test_store_resolver_evaluates_yoy(sr, store, seeded):
    """派生属性读时求值，值来自库里的两个报告期。"""
    aid = _attr_id(sr, "Company", "revenue")
    iid = store.get_instance("Company", "600519")
    store.put_attr(
        instance_id=iid, attribute_type_id=aid, value="80",
        valid_from=None, valid_to=Q3_2023, schema_version=seeded,
    )
    store.commit()

    r = StoreResolver(store, iid, Q3_2024)
    assert r.current("revenue") == Decimal("100")
    assert r.prior("revenue") == Decimal("80")
    assert evaluate("revenue / same_period_last_year(revenue) - 1", r) == Decimal("0.25")


def test_store_resolver_missing_prior_yields_null(sr, store, seeded):
    """新上市公司没有上年同期 ⇒ 派生值为 null，不是错误。"""
    iid = store.get_instance("Company", "600519")
    r = StoreResolver(store, iid, Q3_2024)
    assert r.prior("revenue") is None
    assert evaluate("revenue / same_period_last_year(revenue) - 1", r) is None


def test_store_resolver_unknown_attribute_yields_null(sr, store, seeded):
    iid = store.get_instance("Company", "600519")
    r = StoreResolver(store, iid, Q3_2024)
    assert r.current("does_not_exist") is None


# ------------------------------------------------------------------ ingest 管线


def test_ingest_pipeline_end_to_end(sr, store):
    """一行输入 → mapping → parser 链 → validator → 入库。"""
    v = sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
            AddParserDef(
                entity_type_name="Company", attribute_name="revenue",
                kind="code_module", pattern="cn_amount", priority=10,
            ),
            AddValidatorDef(
                entity_type_name="Company", attribute_name="ticker",
                kind="regex", spec={"pattern": r"^\d{6}$"},
            ),
        ]
    )
    mapping = ObjectMapping(
        entity_type_name="Company",
        subject_field="SECURITY_CODE",
        period_field="REPORT_DATE",
        disclosed_field="NOTICE_DATE",
        fields=(
            FieldMapping("SECURITY_CODE", "ticker"),
            FieldMapping("TOTAL_OPERATE_INCOME", "revenue"),
        ),
    )
    rows = [
        {
            "SECURITY_CODE": "600519",
            "REPORT_DATE": "2024-09-30 00:00:00",   # 东财的写法
            "NOTICE_DATE": "2024-10-30 00:00:00",
            "TOTAL_OPERATE_INCOME": "1.23亿",
        }
    ]
    rep = Ingestor(sr.conn, store, v).ingest(rows, mapping, sr.ancestors(v))

    assert rep.ok, rep.errors
    assert rep.rows_read == 1
    assert rep.values_written == 2

    iid = store.get_instance("Company", "600519")
    vals = {a.attribute_name: a.value for a in store.current_attrs(iid)}
    assert vals["revenue"] == "123000000"     # 单位换算生效，无尾随零
    assert vals["ticker"] == "600519"

    attrs = store.current_attrs(iid, as_of_period=date(2024, 9, 30))
    assert len(attrs) == 2                     # 报告期被正确解析


def test_ingest_records_validation_failure(sr, store):
    v = sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="ticker"),
            AddValidatorDef(
                entity_type_name="Company", attribute_name="ticker",
                kind="regex", spec={"pattern": r"^\d{6}$"},
            ),
        ]
    )
    mapping = ObjectMapping(
        entity_type_name="Company",
        subject_field="code",
        fields=(FieldMapping("code", "ticker"),),
    )
    rep = Ingestor(sr.conn, store, v).ingest([{"code": "BAD"}], mapping, sr.ancestors(v))

    assert not rep.ok
    assert rep.errors[0].kind == "validation_failed"
    assert rep.values_written == 0


def test_ingest_skips_derived_attributes(sr, store):
    """派生属性不参与 ingest——它没有输入值。这是 L1 零触碰的基础。"""
    v = sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
            AddAttributeType(
                entity_type_name="Company", name="revenue_yoy", datatype="number",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
            ),
        ]
    )
    mapping = ObjectMapping(
        entity_type_name="Company",
        subject_field="code",
        fields=(
            FieldMapping("code", "revenue"),
            FieldMapping("yoy_from_source", "revenue_yoy"),   # 即便源里有值也该忽略
        ),
    )
    rep = Ingestor(sr.conn, store, v).ingest(
        [{"code": "100", "yoy_from_source": "0.25"}], mapping, sr.ancestors(v)
    )
    assert rep.values_written == 1      # 只写了 revenue

    iid = store.get_instance("Company", "100")
    assert {a.attribute_name for a in store.current_attrs(iid)} == {"revenue"}


def test_ingest_empty_subject_is_error(sr, store):
    v = sr.commit([AddEntityType(name="Company"), AddAttributeType(entity_type_name="Company", name="x")])
    mapping = ObjectMapping(
        entity_type_name="Company", subject_field="code",
        fields=(FieldMapping("code", "x"),),
    )
    rep = Ingestor(sr.conn, store, v).ingest([{"code": ""}], mapping, sr.ancestors(v))
    assert not rep.ok
    assert store.count_instances("Company") == 0
