"""派生属性表达式：纯算术 + 单个时间访问器。

语法（刻意窄，见项目计划的边界决策）
------------------------------------
    expr   := term (('+' | '-') term)*
    term   := factor (('*' | '/') factor)*
    factor := ('+' | '-') factor | atom
    atom   := NUMBER
            | IDENT                      本期属性引用
            | 'same_period_last_year' '(' IDENT ')'   上年同期属性引用
            | '(' expr ')'

允许：`revenue / same_period_last_year(revenue) - 1`、`(revenue - cost) / revenue`
不允许：跨实例聚合、条件分支、引用其他派生属性、任何函数调用（除唯一的时间访问器）

为什么手写解析器而不是 Python ast + 白名单
--------------------------------------------
"纯算术"这个边界本身很窄，自定义语法比给 Python 语法做减法更可控：
不必防 getattr、双下划线属性、推导式等逃逸路径，且依赖集能被**精确**静态提取——
这是代价分级的前提（影响集必须可枚举，否则 L2 退化为 L3）。

三条设计约束
------------
1. 依赖集可精确静态分析：`dependencies()` 不执行表达式即可给出依赖的属性名。
2. 影响集有界：依赖只涉及本实例的属性（本期或上年同期），永不扩散到其他实例。
3. 依赖图恒为一层：派生属性不得引用其他派生属性（由 SchemaRegistry 在提交时校验），
   因此无需拓扑排序、无需环检测。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Protocol

logger = logging.getLogger(__name__)

TIME_ACCESSOR = "same_period_last_year"


class ExpressionError(ValueError):
    """表达式语法或语义错误。这是 schema 定义的缺陷，提交时就该拒绝。"""


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"""
      (?P<NUMBER>\d+\.\d+|\d+)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<OP>[+\-*/(),])
    | (?P<WS>\s+)
    | (?P<BAD>.)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    pos: int


def _tokenize(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    for m in _TOKEN_RE.finditer(src):
        kind = m.lastgroup
        if kind == "WS":
            continue
        if kind == "BAD":
            raise ExpressionError(f"非法字符 {m.group()!r}（位置 {m.start()}）")
        out.append(_Tok(kind, m.group(), m.start()))
    return out


# ---------------------------------------------------------------- 语法树

class Node:
    pass


@dataclass(frozen=True)
class Num(Node):
    value: Decimal


@dataclass(frozen=True)
class AttrRef(Node):
    """本期属性引用。"""
    name: str


@dataclass(frozen=True)
class PriorRef(Node):
    """上年同期属性引用，即 same_period_last_year(name)。"""
    name: str


@dataclass(frozen=True)
class BinOp(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class UnaryOp(Node):
    op: str
    operand: Node


# ---------------------------------------------------------------- 语法分析

class _Parser:
    def __init__(self, toks: list[_Tok], src: str) -> None:
        self.toks = toks
        self.src = src
        self.i = 0

    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, text: str) -> _Tok:
        t = self._peek()
        if t is None or t.text != text:
            got = t.text if t else "表达式结尾"
            raise ExpressionError(f"期望 {text!r}，实际是 {got!r}")
        self.i += 1
        return t

    def parse(self) -> Node:
        if not self.toks:
            raise ExpressionError("表达式为空")
        node = self._expr()
        if self.i != len(self.toks):
            raise ExpressionError(f"表达式尾部有多余内容: {self.toks[self.i].text!r}")
        return node

    def _expr(self) -> Node:
        node = self._term()
        while (t := self._peek()) and t.text in ("+", "-"):
            self.i += 1
            node = BinOp(t.text, node, self._term())
        return node

    def _term(self) -> Node:
        node = self._factor()
        while (t := self._peek()) and t.text in ("*", "/"):
            self.i += 1
            node = BinOp(t.text, node, self._factor())
        return node

    def _factor(self) -> Node:
        t = self._peek()
        if t and t.text in ("+", "-"):
            self.i += 1
            return UnaryOp(t.text, self._factor())
        return self._atom()

    def _atom(self) -> Node:
        t = self._peek()
        if t is None:
            raise ExpressionError("表达式意外结束")

        if t.kind == "NUMBER":
            self.i += 1
            try:
                return Num(Decimal(t.text))
            except InvalidOperation as e:
                raise ExpressionError(f"非法数字 {t.text!r}") from e

        if t.kind == "IDENT":
            self.i += 1
            nxt = self._peek()
            if nxt and nxt.text == "(":
                # 唯一允许的函数就是时间访问器
                if t.text != TIME_ACCESSOR:
                    raise ExpressionError(
                        f"不支持函数 {t.text!r}；只允许 {TIME_ACCESSOR}(属性名)"
                    )
                self._eat("(")
                inner = self._peek()
                if inner is None or inner.kind != "IDENT":
                    raise ExpressionError(f"{TIME_ACCESSOR} 的参数必须是属性名")
                self.i += 1
                # 嵌套时间访问器会让依赖跨越两个期，超出「上年同期」语义
                if inner.text == TIME_ACCESSOR:
                    raise ExpressionError(f"{TIME_ACCESSOR} 不能嵌套")
                self._eat(")")
                return PriorRef(inner.text)
            return AttrRef(t.text)

        if t.text == "(":
            self.i += 1
            node = self._expr()
            self._eat(")")
            return node

        raise ExpressionError(f"无法解析的记号 {t.text!r}（位置 {t.pos}）")


def parse_expression(src: str) -> Node:
    """解析表达式，失败抛 ExpressionError。"""
    return _Parser(_tokenize(src), src).parse()


# ---------------------------------------------------------------- 依赖分析

@dataclass(frozen=True)
class Dependencies:
    """表达式的静态依赖。

    current 与 prior 要分开：某个属性在报告期 P 上变化，会让**两个**期的派生值失效
    —— 期 P 的（经 current 引用）与期 P+1 的（经 prior 引用）。
    W3 计算影响集时必须同时考虑这两条。
    """

    current: frozenset[str] = field(default_factory=frozenset)
    prior: frozenset[str] = field(default_factory=frozenset)

    @property
    def all_names(self) -> frozenset[str]:
        return self.current | self.prior


def dependencies(node: Node | str) -> Dependencies:
    """静态提取依赖的属性名，不执行表达式。"""
    if isinstance(node, str):
        node = parse_expression(node)

    cur: set[str] = set()
    pri: set[str] = set()

    def walk(n: Node) -> None:
        match n:
            case Num():
                pass
            case AttrRef(name):
                cur.add(name)
            case PriorRef(name):
                pri.add(name)
            case UnaryOp(_, operand):
                walk(operand)
            case BinOp(_, left, right):
                walk(left)
                walk(right)
            case _:
                raise ExpressionError(f"未知节点: {n!r}")

    walk(node)
    return Dependencies(frozenset(cur), frozenset(pri))


# ---------------------------------------------------------------- 求值

class ValueResolver(Protocol):
    """求值所需的数据访问接口。W3 由 InstanceStore 实现。"""

    def current(self, attr: str) -> Decimal | None: ...
    def prior(self, attr: str) -> Decimal | None: ...


@dataclass
class DictResolver:
    """基于字典的求值上下文，测试与轻量场景用。"""

    current_values: dict[str, object] = field(default_factory=dict)
    prior_values: dict[str, object] = field(default_factory=dict)

    @staticmethod
    def _coerce(v: object) -> Decimal | None:
        if v is None or v == "":
            return None
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None

    def current(self, attr: str) -> Decimal | None:
        return self._coerce(self.current_values.get(attr))

    def prior(self, attr: str) -> Decimal | None:
        return self._coerce(self.prior_values.get(attr))


class _Missing(Exception):
    """求值中途遇到缺失值或除零，整个表达式判为 null。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def evaluate(
    expr: Node | str,
    resolver: ValueResolver,
    *,
    context: str = "",
) -> Decimal | None:
    """求值。数据缺失或除零返回 None 并记日志，不抛异常。

    区分两类失败：
    * **数据缺失 / 除零** —— 正常情况（新上市公司没有上年同期、分母为零），
      返回 None。检索侧 null 值不入超图。
    * **表达式错误** —— schema 定义的缺陷，抛 ExpressionError。
      正常路径下不会发生：SchemaRegistry 在提交时已解析过一遍。
    """
    node = parse_expression(expr) if isinstance(expr, str) else expr

    def ev(n: Node) -> Decimal:
        match n:
            case Num(value):
                return value
            case AttrRef(name):
                v = resolver.current(name)
                if v is None:
                    raise _Missing(f"缺少本期值 {name}")
                return v
            case PriorRef(name):
                v = resolver.prior(name)
                if v is None:
                    raise _Missing(f"缺少上年同期值 {name}")
                return v
            case UnaryOp(op, operand):
                v = ev(operand)
                return -v if op == "-" else v
            case BinOp(op, left, right):
                a, b = ev(left), ev(right)
                if op == "+":
                    return a + b
                if op == "-":
                    return a - b
                if op == "*":
                    return a * b
                if op == "/":
                    if b == 0:
                        raise _Missing("除数为零")
                    return a / b
                raise ExpressionError(f"未知运算符 {op!r}")
            case _:
                raise ExpressionError(f"未知节点: {n!r}")

    try:
        return ev(node)
    except _Missing as m:
        logger.info("派生属性求值为 null%s: %s", f"[{context}]" if context else "", m.reason)
        return None
    except (DivisionByZero, InvalidOperation) as e:
        # Decimal 的溢出/无效运算也按缺失处理，不让单条脏数据中断整批
        logger.info(
            "派生属性求值为 null%s: 数值异常 %s", f"[{context}]" if context else "", e
        )
        return None
