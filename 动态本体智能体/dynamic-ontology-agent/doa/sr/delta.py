"""SchemaDelta：一次本体演化的最小变更单元。

设计要点
--------
1. 变更是**追加式**的：新增 = 插一行带 introduced_in；删除 = 给已有行写 removed_in。
   没有 UPDATE 语义，因此任何历史版本都能被重建。
2. "修改"（如 parser 模式变更、约束收紧）在本层表达为 remove + add 的组合，
   不提供原地改写的算子——否则历史版本会被破坏。
3. 每个算子自报 `affects_existing_instances`，这是 W2 二维定级的输入之一；
   W1 尚无 Validator，仅记录不使用。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class _Base(BaseModel):
    """所有 delta 的公共行为。"""

    def describe(self) -> str:
        raise NotImplementedError

    @property
    def affects_existing_instances(self) -> bool:
        """是否可能触碰存量实例。W2 定级的输入，W1 仅记录。"""
        return False


# ------------------------------------------------------------------ 新增类

class AddEntityType(_Base):
    op: Literal["add_entity_type"] = "add_entity_type"
    name: str
    uri: str | None = None
    parent_name: str | None = None

    def describe(self) -> str:
        suffix = f" ⊑ {self.parent_name}" if self.parent_name else ""
        return f"+EntityType {self.name}{suffix}"


class AddRelationType(_Base):
    op: Literal["add_relation_type"] = "add_relation_type"
    name: str
    domain_name: str
    range_name: str

    def describe(self) -> str:
        return f"+RelationType {self.name}: {self.domain_name} → {self.range_name}"


class AddAttributeType(_Base):
    op: Literal["add_attribute_type"] = "add_attribute_type"
    entity_type_name: str
    name: str
    datatype: Literal["string", "number", "date", "bool"] = "string"
    min_cardinality: int = 0
    derivation_expr: str | None = None
    index_derived: bool = False

    def describe(self) -> str:
        if self.derivation_expr:
            flag = "→idx" if self.index_derived else "lazy"
            return f"+AttributeType {self.entity_type_name}.{self.name} = {self.derivation_expr} [{flag}]"
        req = "required" if self.min_cardinality > 0 else "optional"
        return f"+AttributeType {self.entity_type_name}.{self.name}:{self.datatype} [{req}]"

    @property
    def affects_existing_instances(self) -> bool:
        # 必填属性要求存量实例补值，因此触碰存量；可选属性与派生属性不触碰
        return self.min_cardinality > 0


class AddParserDef(_Base):
    op: Literal["add_parser_def"] = "add_parser_def"
    entity_type_name: str
    attribute_name: str
    kind: Literal["regex", "code_module"] = "regex"
    pattern: str
    priority: int = 100
    not_required: bool = False
    default_value: str | None = None

    def describe(self) -> str:
        return (
            f"+Parser {self.entity_type_name}.{self.attribute_name} "
            f"[{self.kind} p={self.priority}] {self.pattern!r}"
        )


class AddValidatorDef(_Base):
    op: Literal["add_validator_def"] = "add_validator_def"
    entity_type_name: str
    attribute_name: str
    kind: Literal["regex", "enum", "code_module"] = "regex"
    spec: dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        return f"+Validator {self.entity_type_name}.{self.attribute_name} [{self.kind}]"


# ------------------------------------------------------------------ 移除类

class RemoveEntityType(_Base):
    op: Literal["remove_entity_type"] = "remove_entity_type"
    name: str

    def describe(self) -> str:
        return f"-EntityType {self.name}"

    @property
    def affects_existing_instances(self) -> bool:
        return True


class RemoveAttributeType(_Base):
    op: Literal["remove_attribute_type"] = "remove_attribute_type"
    entity_type_name: str
    name: str

    def describe(self) -> str:
        return f"-AttributeType {self.entity_type_name}.{self.name}"

    @property
    def affects_existing_instances(self) -> bool:
        return True


class RemoveRelationType(_Base):
    op: Literal["remove_relation_type"] = "remove_relation_type"
    name: str

    def describe(self) -> str:
        return f"-RelationType {self.name}"

    @property
    def affects_existing_instances(self) -> bool:
        return True


class RemoveParserDef(_Base):
    op: Literal["remove_parser_def"] = "remove_parser_def"
    entity_type_name: str
    attribute_name: str
    pattern: str

    def describe(self) -> str:
        return f"-Parser {self.entity_type_name}.{self.attribute_name} {self.pattern!r}"

    @property
    def affects_existing_instances(self) -> bool:
        # parser 变更意味着已入库的值可能需要按新规则重解析
        return True


SchemaDelta = Annotated[
    Union[
        AddEntityType,
        AddRelationType,
        AddAttributeType,
        AddParserDef,
        AddValidatorDef,
        RemoveEntityType,
        RemoveRelationType,
        RemoveAttributeType,
        RemoveParserDef,
    ],
    Field(discriminator="op"),
]

_ADAPTER: TypeAdapter[SchemaDelta] = TypeAdapter(SchemaDelta)
_LIST_ADAPTER: TypeAdapter[list[SchemaDelta]] = TypeAdapter(list[SchemaDelta])


def parse_delta(raw: dict[str, Any]) -> SchemaDelta:
    return _ADAPTER.validate_python(raw)


def parse_deltas(raw: list[dict[str, Any]]) -> list[SchemaDelta]:
    return _LIST_ADAPTER.validate_python(raw)


def dump_deltas(deltas: list[SchemaDelta]) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json") for d in deltas]
