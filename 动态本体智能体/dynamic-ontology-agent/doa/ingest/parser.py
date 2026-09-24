"""parser 链与 validator（源自专利 EP3835968A1）。

parser 链语义（[0036]-[0037]、FIG.3）
------------------------------------
每个 AttributeType 挂 ≥1 个 parser 定义，按 priority **依次套用直到命中**；
命中后产出 modified value，再过 validator。全不中时：
* `not_required` → 静默丢弃该属性（不阻断整条记录入库）
* 有 `default_value` → 填默认值
* 否则 → 该属性解析失败，记为 ingest 错误

validator 时机（[0032]-[0033]）
-------------------------------
**parse 之后、store 之前**。与 parser 不同，validator 没有回退链——
校验失败就是失败。专利未涉及失败后的处置动作，本项目的设计是：
产出 `ValidationFailed` 由调用方决定（ingest 侧记为错误并跳过该属性值）。

为何需要 code_module parser
---------------------------
正则能匹配 "1.23亿" 但没法做乘法。中文财报的单位换算（亿/万）、
会计负数记法 "(1,234)" 都需要代码参与——这正是专利里两类 parser 并存的理由。
代码模块走**白名单注册**，不用 eval。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Sequence

# ------------------------------------------------------------------ 代码模块白名单

CodeModule = Callable[[str], Any]

_MODULES: dict[str, CodeModule] = {}


def code_module(name: str) -> Callable[[CodeModule], CodeModule]:
    """注册一个代码模块。白名单机制——parser 只能按名字引用已注册的函数。"""

    def deco(fn: CodeModule) -> CodeModule:
        if name in _MODULES:
            raise ValueError(f"代码模块重名: {name}")
        _MODULES[name] = fn
        return fn

    return deco


def get_code_module(name: str) -> CodeModule:
    if name not in _MODULES:
        raise KeyError(
            f"未注册的代码模块: {name}。可用: {sorted(_MODULES)}"
        )
    return _MODULES[name]


def registered_modules() -> list[str]:
    return sorted(_MODULES)


class ParseReject(Exception):
    """代码模块表示"我不处理这个输入"，链条继续往下走。

    与真正的异常区分：抛 ParseReject 是正常的链式回退，
    抛其他异常说明模块本身有 bug，不应被静默吞掉。
    """


# ------------------------------------------------------------------ 内置代码模块

_CN_UNITS = {"亿": Decimal("100000000"), "万": Decimal("10000"), "千": Decimal("1000")}


def _tidy(d: Decimal) -> Decimal:
    """去掉无意义的尾随零，且避免科学计数法。

    为何必要：`Decimal('1.23') * 100000000` 得到 `Decimal('123000000.00')`——
    Decimal 保留有效位数。属性值在库里存为 TEXT，'123000000.00' 与 '123000000'
    是不同字符串，会让 put_attr 把格式差异误判为值变化，凭空制造一条追溯调整历史。

    不能直接用 normalize()：它会把 123000000.00 变成 '1.23E+8'。
    """
    if d == d.to_integral_value():
        # to_integral_value 而非 quantize：后者在整数位超过上下文精度时会抛 InvalidOperation
        return d.to_integral_value()
    return d.normalize()


@code_module("cn_amount")
def _cn_amount(raw: str) -> Decimal:
    """中文金额：'1.23亿' → 123000000，'4,567万' → 45670000，'1234.5' → 1234.5。

    正则做不到这件事——它能匹配单位但没法做乘法。
    """
    s = raw.strip().replace(",", "").replace("，", "")
    if not s:
        raise ParseReject("空值")

    if s.endswith("元"):
        s = s[:-1]

    mult = Decimal(1)
    for unit, factor in _CN_UNITS.items():
        if s.endswith(unit):
            mult = factor
            s = s[: -len(unit)]
            break

    try:
        return _tidy(Decimal(s) * mult)
    except InvalidOperation as e:
        raise ParseReject(f"非数值: {raw!r}") from e


@code_module("accounting_negative")
def _accounting_negative(raw: str) -> Decimal:
    """会计负数记法：'(1,234.5)' → -1234.5。括号表示负值。"""
    s = raw.strip().replace(",", "")
    if not (s.startswith("(") and s.endswith(")")):
        raise ParseReject("非括号记法")
    try:
        return _tidy(-Decimal(s[1:-1]))
    except InvalidOperation as e:
        raise ParseReject(f"非数值: {raw!r}") from e


@code_module("plain_number")
def _plain_number(raw: str) -> Decimal:
    """普通数字，允许千分位与前后空白。"""
    s = raw.strip().replace(",", "")
    if not s:
        raise ParseReject("空值")
    try:
        return _tidy(Decimal(s))
    except InvalidOperation as e:
        raise ParseReject(f"非数值: {raw!r}") from e


@code_module("iso_date")
def _iso_date(raw: str) -> str:
    """把常见日期写法归一为 ISO：'2024年9月30日' / '2024/09/30' → '2024-09-30'。"""
    s = raw.strip()
    m = re.match(r"^(\d{4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?$", s)
    if not m:
        raise ParseReject(f"非日期: {raw!r}")
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


# ------------------------------------------------------------------ 定义模型

@dataclass(frozen=True)
class ParserDef:
    """一条 parser 定义。对应 parser_def 表的一行。"""

    kind: Literal["regex", "code_module"]
    pattern: str            # regex 模式，或白名单里的模块名
    priority: int = 100
    not_required: bool = False
    default_value: str | None = None
    parser_id: int | None = None


@dataclass(frozen=True)
class ValidatorDef:
    """一条 validator 定义。对应 validator_def 表的一行。"""

    kind: Literal["regex", "enum", "code_module"]
    spec: dict[str, Any]
    validator_id: int | None = None


# ------------------------------------------------------------------ 结果模型

@dataclass(frozen=True)
class ParseHit:
    """命中：产出 modified value。"""

    value: Any
    parser_id: int | None = None
    used_default: bool = False


@dataclass(frozen=True)
class ParseSkipped:
    """全不中但标记了 not_required——静默丢弃该属性，不阻断整条记录。"""

    reason: str


@dataclass(frozen=True)
class ParseFailed:
    """全不中且要求匹配。"""

    reason: str
    attempts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationFailed:
    """parse 成功但 validator 拒绝。validator 无回退链，失败即失败。"""

    value: Any
    reason: str
    validator_kind: str = ""


ParseOutcome = ParseHit | ParseSkipped | ParseFailed | ValidationFailed


# ------------------------------------------------------------------ 执行

def _apply_regex(pattern: str, raw: str) -> Any:
    """正则 parser。有捕获组取 group(1)，否则取整个匹配。"""
    m = re.search(pattern, raw)
    if m is None:
        raise ParseReject(f"不匹配 {pattern!r}")
    return m.group(1) if m.groups() else m.group(0)


def _apply_one(pdef: ParserDef, raw: str) -> Any:
    if pdef.kind == "regex":
        return _apply_regex(pdef.pattern, raw)
    if pdef.kind == "code_module":
        fn = get_code_module(pdef.pattern)
        return fn(raw)
    raise ValueError(f"未知 parser 类型: {pdef.kind}")


def parse_value(
    raw: str | None,
    parsers: Sequence[ParserDef],
    validators: Sequence[ValidatorDef] = (),
) -> ParseOutcome:
    """按 priority 依次套用 parser 直到命中，再过 validator。

    返回四种结果之一，调用方据此决定是写入、跳过还是记错。
    """
    chain = sorted(parsers, key=lambda p: p.priority)

    # 没有 parser 定义时按原值透传——种子期的属性常常还没配 parser
    if not chain:
        if raw is None or raw == "":
            return ParseSkipped("空值且无 parser 定义")
        return _validate(raw, validators)

    attempts: list[str] = []
    for pdef in chain:
        if raw is None or raw == "":
            break
        try:
            value = _apply_one(pdef, raw)
        except ParseReject as e:
            attempts.append(f"{pdef.kind}:{pdef.pattern} — {e}")
            continue
        except KeyError:
            # 未注册的代码模块是配置错误，不是数据问题，必须暴露
            raise
        return _validate(value, validators, parser_id=pdef.parser_id)

    # 全不中：default → not_required → 失败
    # 顺序有讲究：default_value 优先于 not_required，因为填了默认值就不该丢属性
    for pdef in chain:
        if pdef.default_value is not None:
            return _validate(
                pdef.default_value, validators, parser_id=pdef.parser_id, used_default=True
            )

    if any(p.not_required for p in chain):
        return ParseSkipped(
            f"全部 parser 未命中，按 not_required 丢弃（原值 {raw!r}）"
        )

    return ParseFailed(
        reason=f"全部 parser 未命中（原值 {raw!r}）", attempts=tuple(attempts)
    )


def _validate(
    value: Any,
    validators: Sequence[ValidatorDef],
    *,
    parser_id: int | None = None,
    used_default: bool = False,
) -> ParseOutcome:
    """parse 之后、store 之前守门。任一 validator 不过即整体失败。"""
    for vdef in validators:
        ok, reason = _check_one(vdef, value)
        if not ok:
            return ValidationFailed(value=value, reason=reason, validator_kind=vdef.kind)
    return ParseHit(value=value, parser_id=parser_id, used_default=used_default)


def _check_one(vdef: ValidatorDef, value: Any) -> tuple[bool, str]:
    text = str(value)

    if vdef.kind == "regex":
        pattern = vdef.spec.get("pattern", "")
        if re.fullmatch(pattern, text) is None:
            return False, f"不满足正则 {pattern!r}"
        return True, ""

    if vdef.kind == "enum":
        # 专利的固定值集示例（US state），且明确说明集合可扩展
        allowed = vdef.spec.get("values", [])
        if text not in {str(v) for v in allowed}:
            preview = ", ".join(str(v) for v in list(allowed)[:5])
            return False, f"不在枚举集内（允许 {preview}{'...' if len(allowed) > 5 else ''}）"
        return True, ""

    if vdef.kind == "code_module":
        name = vdef.spec.get("name", "")
        fn = get_code_module(name)
        try:
            fn(text)
        except ParseReject as e:
            return False, f"代码模块 {name} 拒绝: {e}"
        return True, ""

    return False, f"未知 validator 类型: {vdef.kind}"
