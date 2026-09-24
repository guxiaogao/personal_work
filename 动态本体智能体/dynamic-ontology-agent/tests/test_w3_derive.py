"""W3 第一步：派生属性表达式语言。

边界（刻意窄）：纯算术 + 单个时间访问器，求值失败返 null，禁止引用其他派生属性。
这三条共同保证：依赖集可精确静态分析、影响集有界、依赖图恒为一层。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from doa.derive import (
    DictResolver,
    ExpressionError,
    dependencies,
    evaluate,
    parse_expression,
)
from doa.sr.delta import AddAttributeType, AddEntityType
from doa.sr.registry import SchemaError

# ------------------------------------------------------------------ 解析


def test_parse_arithmetic():
    for src in (
        "1",
        "revenue",
        "revenue - cost",
        "(revenue - cost) / revenue",
        "revenue / same_period_last_year(revenue) - 1",
        "-revenue",
        "total_assets - total_liabilities",
        "revenue * 0.85 + 100",
    ):
        assert parse_expression(src) is not None


@pytest.mark.parametrize(
    "src,msg",
    [
        ("", "为空"),
        ("revenue +", "意外结束"),
        ("revenue )", "多余内容"),
        ("(revenue", "期望"),
        ("revenue $ cost", "非法字符"),
        ("sum_over_industry(revenue)", "不支持函数"),
        ("max(a, b)", "不支持函数"),
        ("same_period_last_year(same_period_last_year(x))", "不能嵌套"),
        ("same_period_last_year(1)", "必须是属性名"),
    ],
)
def test_parse_rejects(src, msg):
    with pytest.raises(ExpressionError, match=msg):
        parse_expression(src)


def test_comma_gives_meaningful_error():
    """逗号在词法层要被接受，好让语法层报出真正的原因。

    回归测试：早先词法不认逗号，`max(a, b)` 报的是「非法字符 ','」——
    指向标点而非真正问题（不支持多参函数），用户无从下手。
    """
    with pytest.raises(ExpressionError, match="不支持函数"):
        parse_expression("max(a, b)")


def test_cross_instance_aggregation_rejected():
    """跨实例聚合被排除在语言之外——它会让影响集失去边界。

    若允许 sum_over(Company, revenue)，任一公司 revenue 变动都会让全体派生值失效，
    L2 的「影响集可枚举」不再成立。
    """
    for src in (
        "revenue / sum_over(Company, revenue)",
        "rank_over(industry, revenue)",
        "revenue / avg_over(industry, revenue)",
    ):
        with pytest.raises(ExpressionError, match="不支持函数"):
            parse_expression(src)


# ------------------------------------------------------------------ 依赖分析


def test_dependencies_current_and_prior_are_separate():
    """current 与 prior 必须分开。

    revenue 在报告期 P 上变化，会让两个期的 yoy 失效：
    期 P 的（经 current）与期 P+1 的（经 prior）。
    """
    d = dependencies("revenue / same_period_last_year(revenue) - 1")
    assert d.current == {"revenue"}
    assert d.prior == {"revenue"}
    assert d.all_names == {"revenue"}


def test_dependencies_multiple_attrs():
    d = dependencies("(revenue - cost) / same_period_last_year(total_assets)")
    assert d.current == {"revenue", "cost"}
    assert d.prior == {"total_assets"}


def test_dependencies_no_attrs():
    d = dependencies("1 + 2 * 3")
    assert d.all_names == frozenset()


def test_dependencies_is_static():
    """依赖提取不需要任何数据——这是分级能在演化时（无实例参与）完成的前提。"""
    assert dependencies("revenue / cost").current == {"revenue", "cost"}


# ------------------------------------------------------------------ 求值


def test_evaluate_basic():
    r = DictResolver(current_values={"revenue": 1000, "cost": 600})
    assert evaluate("(revenue - cost) / revenue", r) == Decimal("0.4")


def test_evaluate_yoy():
    r = DictResolver(current_values={"revenue": 120}, prior_values={"revenue": 100})
    assert evaluate("revenue / same_period_last_year(revenue) - 1", r) == Decimal("0.2")


def test_evaluate_missing_current_returns_null():
    r = DictResolver(current_values={})
    assert evaluate("revenue * 2", r) is None


def test_evaluate_missing_prior_returns_null():
    """新上市公司没有上年同期——这是常态，不是错误。"""
    r = DictResolver(current_values={"revenue": 100}, prior_values={})
    assert evaluate("revenue / same_period_last_year(revenue) - 1", r) is None


def test_evaluate_division_by_zero_returns_null():
    r = DictResolver(current_values={"revenue": 100, "cost": 0})
    assert evaluate("revenue / cost", r) is None


def test_evaluate_empty_string_is_missing():
    """空字符串按缺失处理——财报字段常以空串表示不适用。"""
    r = DictResolver(current_values={"revenue": ""})
    assert evaluate("revenue * 2", r) is None


def test_evaluate_non_numeric_returns_null():
    r = DictResolver(current_values={"revenue": "不适用"})
    assert evaluate("revenue * 2", r) is None


def test_evaluate_uses_decimal_not_float():
    """金额计算必须用 Decimal：float 会引入精度误差。"""
    r = DictResolver(current_values={"a": "0.1", "b": "0.2"})
    assert evaluate("a + b", r) == Decimal("0.3")


def test_evaluate_unary_minus():
    r = DictResolver(current_values={"x": 5})
    assert evaluate("-x + 10", r) == Decimal("5")


def test_evaluate_precedence():
    r = DictResolver(current_values={"a": 2, "b": 3, "c": 4})
    assert evaluate("a + b * c", r) == Decimal("14")
    assert evaluate("(a + b) * c", r) == Decimal("20")


# ------------------------------------------------------------------ 与 SR 集成


def test_sr_rejects_bad_expression(sr):
    sr.commit([AddEntityType(name="Company")])
    with pytest.raises(SchemaError, match="派生表达式非法"):
        sr.commit(
            [
                AddAttributeType(
                    entity_type_name="Company",
                    name="bad",
                    derivation_expr="sum_over_industry(revenue)",
                )
            ]
        )


def test_sr_rejects_unknown_dependency(sr):
    sr.commit([AddEntityType(name="Company")])
    with pytest.raises(SchemaError, match="不存在的属性"):
        sr.commit(
            [
                AddAttributeType(
                    entity_type_name="Company",
                    name="yoy",
                    derivation_expr="revenue / same_period_last_year(revenue) - 1",
                )
            ]
        )


def test_sr_rejects_self_reference(sr):
    sr.commit([AddEntityType(name="Company")])
    with pytest.raises(SchemaError, match="引用自身"):
        sr.commit(
            [
                AddAttributeType(
                    entity_type_name="Company", name="loop", derivation_expr="loop + 1"
                )
            ]
        )


def test_sr_rejects_derived_referencing_derived(sr):
    """禁止派生引用派生 ⇒ 依赖图恒为一层，无需拓扑排序与环检测。"""
    sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
            AddAttributeType(entity_type_name="Company", name="cost", datatype="number"),
            AddAttributeType(
                entity_type_name="Company",
                name="gross_margin",
                datatype="number",
                derivation_expr="(revenue - cost) / revenue",
            ),
        ]
    )
    with pytest.raises(SchemaError, match="不能引用其他派生属性"):
        sr.commit(
            [
                AddAttributeType(
                    entity_type_name="Company",
                    name="gross_margin_yoy",
                    datatype="number",
                    derivation_expr=(
                        "gross_margin / same_period_last_year(gross_margin) - 1"
                    ),
                )
            ]
        )


def test_sr_accepts_valid_derivation_same_batch(sr):
    """同一批次内先加 revenue 再加引用它的派生属性，应当成立。"""
    v = sr.commit(
        [
            AddEntityType(name="Company"),
            AddAttributeType(entity_type_name="Company", name="revenue", datatype="number"),
            AddAttributeType(
                entity_type_name="Company",
                name="revenue_yoy",
                datatype="number",
                derivation_expr="revenue / same_period_last_year(revenue) - 1",
                index_derived=True,
            ),
        ]
    )
    attrs = {
        a["name"]: a
        for e in sr.snapshot(v)["entity_types"]
        if e["name"] == "Company"
        for a in e["attributes"]
    }
    assert attrs["revenue_yoy"]["derivation_expr"] is not None
    assert attrs["revenue_yoy"]["index_derived"] is True
