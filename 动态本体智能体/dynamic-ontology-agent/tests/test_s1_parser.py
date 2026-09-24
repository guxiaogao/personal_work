"""parser 链与 validator（专利 EP3835968A1 的核心机制）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from doa.ingest import (
    ParseFailed,
    ParseHit,
    ParserDef,
    ParseReject,
    ParseSkipped,
    ValidationFailed,
    ValidatorDef,
    code_module,
    get_code_module,
    parse_value,
    registered_modules,
)

# ------------------------------------------------------------------ 链式回退


def test_first_matching_parser_wins():
    """按 priority 依次套用，命中即停。"""
    parsers = [
        ParserDef(kind="regex", pattern=r"^([\d.]+)亿$", priority=10, parser_id=1),
        ParserDef(kind="regex", pattern=r"^([\d,]+)$", priority=20, parser_id=2),
    ]
    r = parse_value("1.23亿", parsers)
    assert isinstance(r, ParseHit)
    assert r.value == "1.23"
    assert r.parser_id == 1          # 高优先级那条命中


def test_falls_back_to_lower_priority():
    parsers = [
        ParserDef(kind="regex", pattern=r"^([\d.]+)亿$", priority=10, parser_id=1),
        ParserDef(kind="regex", pattern=r"^([\d,]+)$", priority=20, parser_id=2),
    ]
    r = parse_value("456789", parsers)
    assert isinstance(r, ParseHit)
    assert r.parser_id == 2          # 第一条不中，回退到第二条


def test_priority_order_not_insertion_order():
    """排序按 priority，不是定义顺序。"""
    parsers = [
        ParserDef(kind="regex", pattern=r"^(\d+)$", priority=99, parser_id=99),
        ParserDef(kind="regex", pattern=r"^(\d{2})$", priority=1, parser_id=1),
    ]
    r = parse_value("42", parsers)
    assert isinstance(r, ParseHit)
    assert r.parser_id == 1


def test_all_parsers_miss_fails():
    parsers = [ParserDef(kind="regex", pattern=r"^\d+$", parser_id=1)]
    r = parse_value("不是数字", parsers)
    assert isinstance(r, ParseFailed)
    assert len(r.attempts) == 1


def test_not_required_skips_instead_of_failing():
    """标记 not_required ⇒ 全不中时静默丢弃该属性，不阻断整条记录。"""
    parsers = [ParserDef(kind="regex", pattern=r"^\d+$", not_required=True)]
    r = parse_value("不适用", parsers)
    assert isinstance(r, ParseSkipped)


def test_default_value_takes_precedence_over_not_required():
    """填了默认值就不该丢属性——default 优先于 not_required。"""
    parsers = [
        ParserDef(
            kind="regex", pattern=r"^\d+$", not_required=True,
            default_value="0", parser_id=7,
        )
    ]
    r = parse_value("缺失", parsers)
    assert isinstance(r, ParseHit)
    assert r.value == "0"
    assert r.used_default is True


def test_no_parser_defs_passes_through():
    """种子期的属性常还没配 parser，此时按原值透传。"""
    r = parse_value("原样", [])
    assert isinstance(r, ParseHit)
    assert r.value == "原样"


def test_empty_value_with_no_parsers_is_skipped():
    assert isinstance(parse_value("", []), ParseSkipped)
    assert isinstance(parse_value(None, []), ParseSkipped)


def test_regex_without_group_returns_whole_match():
    parsers = [ParserDef(kind="regex", pattern=r"\d+")]
    r = parse_value("abc123def", parsers)
    assert isinstance(r, ParseHit)
    assert r.value == "123"


# ------------------------------------------------------------------ 代码模块


def test_cn_amount_unit_conversion():
    """正则能匹配 '1.23亿' 但没法做乘法——这是 code_module 存在的理由。"""
    fn = get_code_module("cn_amount")
    assert fn("1.23亿") == Decimal("123000000")
    assert fn("4,567万") == Decimal("45670000")
    assert fn("1234.5") == Decimal("1234.5")
    assert fn("8.5亿元") == Decimal("850000000")


def test_numeric_output_has_no_trailing_zeros():
    """数值输出不能带无意义的尾随零。

    回归测试：`Decimal('1.23') * 100000000` 得到 `Decimal('123000000.00')`。
    属性值在库里存为 TEXT，'123000000.00' 与 '123000000' 是不同字符串，
    会让 put_attr 把纯格式差异误判为值变化，凭空制造一条追溯调整历史。
    """
    fn = get_code_module("cn_amount")
    assert str(fn("1.23亿")) == "123000000"
    assert str(fn("4,567万")) == "45670000"
    assert str(fn("8.5亿元")) == "850000000"
    # 真正的小数要保留
    assert str(fn("1234.56")) == "1234.56"
    # 且不能变成科学计数法
    assert "E" not in str(fn("1.23亿"))


def test_accounting_negative():
    fn = get_code_module("accounting_negative")
    assert fn("(1,234.5)") == Decimal("-1234.5")
    with pytest.raises(ParseReject):
        fn("1234")              # 非括号记法，交给链条下一条


def test_iso_date_normalisation():
    fn = get_code_module("iso_date")
    assert fn("2024年9月30日") == "2024-09-30"
    assert fn("2024/09/30") == "2024-09-30"
    assert fn("2024-9-3") == "2024-09-03"


def test_code_module_in_chain():
    parsers = [
        ParserDef(kind="code_module", pattern="accounting_negative", priority=10, parser_id=1),
        ParserDef(kind="code_module", pattern="cn_amount", priority=20, parser_id=2),
    ]
    neg = parse_value("(1,234)", parsers)
    assert isinstance(neg, ParseHit) and neg.value == Decimal("-1234")

    amt = parse_value("2.5亿", parsers)
    assert isinstance(amt, ParseHit) and amt.value == Decimal("250000000")
    assert amt.parser_id == 2      # 第一条 reject 后回退


def test_mixed_regex_and_code_module_chain():
    parsers = [
        ParserDef(kind="regex", pattern=r"^N/?A$", priority=5, default_value=None, parser_id=1),
        ParserDef(kind="code_module", pattern="cn_amount", priority=10, parser_id=2),
    ]
    r = parse_value("3.3万", parsers)
    assert isinstance(r, ParseHit) and r.value == Decimal("33000")


def test_unregistered_module_raises_not_rejects():
    """未注册的模块是配置错误，必须暴露，不能当成链式回退静默吞掉。"""
    parsers = [ParserDef(kind="code_module", pattern="does_not_exist")]
    with pytest.raises(KeyError, match="未注册"):
        parse_value("x", parsers)


def test_builtin_modules_registered():
    mods = registered_modules()
    for name in ("cn_amount", "accounting_negative", "plain_number", "iso_date"):
        assert name in mods


def test_duplicate_module_name_rejected():
    with pytest.raises(ValueError, match="重名"):
        code_module("cn_amount")(lambda s: s)


# ------------------------------------------------------------------ validator


def test_validator_regex_pass_and_fail():
    parsers = [ParserDef(kind="regex", pattern=r"^(\d+)$")]
    vs = [ValidatorDef(kind="regex", spec={"pattern": r"^\d{6}$"})]

    assert isinstance(parse_value("600519", parsers, vs), ParseHit)

    bad = parse_value("60051", parsers, vs)
    assert isinstance(bad, ValidationFailed)
    assert bad.validator_kind == "regex"


def test_validator_enum():
    vs = [ValidatorDef(kind="enum", spec={"values": ["SH", "SZ", "BJ"]})]
    assert isinstance(parse_value("SH", [], vs), ParseHit)
    assert isinstance(parse_value("HK", [], vs), ValidationFailed)


def test_validator_code_module():
    vs = [ValidatorDef(kind="code_module", spec={"name": "plain_number"})]
    assert isinstance(parse_value("123", [], vs), ParseHit)
    assert isinstance(parse_value("abc", [], vs), ValidationFailed)


def test_validator_runs_after_parse():
    """时机：parse 之后、store 之前。validator 看到的是 modified value，不是原值。

    原值 '1.23亿' 不满足纯数字正则，但 parser 产出的 123000000 满足。
    """
    parsers = [ParserDef(kind="code_module", pattern="cn_amount")]
    vs = [ValidatorDef(kind="regex", spec={"pattern": r"^\d+$"})]
    r = parse_value("1.23亿", parsers, vs)
    assert isinstance(r, ParseHit)
    assert r.value == Decimal("123000000")


def test_validator_has_no_fallback_chain():
    """与 parser 不同，validator 无回退：任一不过即整体失败。"""
    vs = [
        ValidatorDef(kind="regex", spec={"pattern": r"^\d+$"}),
        ValidatorDef(kind="enum", spec={"values": ["1", "2"]}),
    ]
    assert isinstance(parse_value("3", [], vs), ValidationFailed)   # 过了正则，栽在枚举
