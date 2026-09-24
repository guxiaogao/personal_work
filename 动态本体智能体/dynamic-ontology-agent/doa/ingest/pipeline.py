"""ingest 管线：一行输入数据 → 本体实例。

流程（专利 EP3835968A1 FIG.3）
-----------------------------
接收输入 → 按 object-attribute mapping 定「行→实体类型、字段→属性类型」
→ 取该属性类型的 parser 定义集 → 依次套用直到命中 → validator 守门
→ 创建属性实例存值 → 遍历所有字段与行 → 实例化对象并关联属性

mapping 可外部化（独立 JSON）或内嵌于输入数据（[0047]）。本实现取外部化，
因为财报接口的字段名是固定的英文代码，映射关系适合集中维护。

**派生属性不参与 ingest**：它们没有输入值，读时才求值。这是 L1「零触碰」的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import psycopg
from psycopg.rows import dict_row

from doa.ingest.parser import (
    ParseFailed,
    ParseHit,
    ParserDef,
    ParseSkipped,
    ValidationFailed,
    ValidatorDef,
    parse_value,
)
from doa.store import InstanceStore


@dataclass(frozen=True)
class FieldMapping:
    """输入字段 → 属性类型。"""

    source_field: str
    attribute_name: str


@dataclass(frozen=True)
class ObjectMapping:
    """一行输入 → 一个实体实例。

    subject_field 给出业务主键所在字段（如证券代码）；
    period_field / disclosed_field 给出双时间戳的来源字段。
    """

    entity_type_name: str
    subject_field: str
    fields: tuple[FieldMapping, ...]
    period_field: str | None = None
    disclosed_field: str | None = None


@dataclass
class IngestError:
    subject_key: str
    attribute_name: str
    raw_value: Any
    reason: str
    kind: str  # parse_failed | validation_failed | unknown_attribute


@dataclass
class IngestReport:
    rows_read: int = 0
    instances_written: int = 0
    values_written: int = 0
    values_skipped: int = 0       # not_required 丢弃
    defaults_applied: int = 0
    errors: list[IngestError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"读入 {self.rows_read} 行，实例 {self.instances_written}，"
            f"属性值 {self.values_written}，跳过 {self.values_skipped}，"
            f"默认值 {self.defaults_applied}，错误 {len(self.errors)}"
        )


def _parse_period(raw: Any) -> date | None:
    """报告期。接受 '2024-12-31' 与 '2024-12-31 00:00:00' 两种写法（东财返回后者）。"""
    if raw in (None, ""):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw).strip()
    return date.fromisoformat(text[:10])


def _parse_disclosed(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    text = str(raw).strip().replace("/", "-")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())


class Ingestor:
    """按当前 schema 把输入行落库。"""

    def __init__(
        self, conn: psycopg.Connection, store: InstanceStore, schema_version: int
    ) -> None:
        self.conn = conn
        self.store = store
        self.schema_version = schema_version
        self._defs: dict[str, tuple[int, list[ParserDef], list[ValidatorDef]] | None] = {}

    def _attr_defs(
        self, entity_type_name: str, attribute_name: str, scope: Sequence[int]
    ) -> tuple[int, list[ParserDef], list[ValidatorDef]] | None:
        """取属性类型 id 及其 parser / validator 定义。派生属性返回 None（不参与 ingest）。"""
        key = f"{entity_type_name}.{attribute_name}"
        if key in self._defs:
            return self._defs[key]

        visible = (
            "introduced_in = ANY(%(scope)s) "
            "AND (removed_in IS NULL OR NOT (removed_in = ANY(%(scope)s)))"
        )
        params = {"scope": list(scope), "entity": entity_type_name, "name": attribute_name}

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""SELECT id, derivation_expr FROM attribute_type
                     WHERE entity_type_name = %(entity)s AND name = %(name)s AND {visible}""",
                params,
            )
            row = cur.fetchone()
            if row is None or row["derivation_expr"] is not None:
                # 派生属性无输入值，读时求值——这是 L1 零触碰的基础
                self._defs[key] = None
                return None
            aid = row["id"]

            cur.execute(
                f"""SELECT id, kind, pattern, priority, not_required, default_value
                      FROM parser_def
                     WHERE attribute_type_id = %(aid)s AND {visible}
                     ORDER BY priority""",
                {**params, "aid": aid},
            )
            parsers = [
                ParserDef(
                    kind=r["kind"],
                    pattern=r["pattern"],
                    priority=r["priority"],
                    not_required=r["not_required"],
                    default_value=r["default_value"],
                    parser_id=r["id"],
                )
                for r in cur.fetchall()
            ]

            cur.execute(
                f"""SELECT id, kind, spec_json FROM validator_def
                     WHERE attribute_type_id = %(aid)s AND {visible}""",
                {**params, "aid": aid},
            )
            validators = [
                ValidatorDef(kind=r["kind"], spec=r["spec_json"], validator_id=r["id"])
                for r in cur.fetchall()
            ]

        out = (aid, parsers, validators)
        self._defs[key] = out
        return out

    def ingest(
        self,
        rows: Iterable[Mapping[str, Any]],
        mapping: ObjectMapping,
        scope: Sequence[int],
    ) -> IngestReport:
        """把若干行按 mapping 落库。"""
        report = IngestReport()

        for row in rows:
            report.rows_read += 1
            subject = str(row.get(mapping.subject_field, "")).strip()
            if not subject:
                report.errors.append(
                    IngestError(
                        subject_key="",
                        attribute_name=mapping.subject_field,
                        raw_value=row.get(mapping.subject_field),
                        reason="业务主键为空",
                        kind="parse_failed",
                    )
                )
                continue

            period = (
                _parse_period(row.get(mapping.period_field))
                if mapping.period_field
                else None
            )
            disclosed = (
                _parse_disclosed(row.get(mapping.disclosed_field))
                if mapping.disclosed_field
                else None
            )

            iid = self.store.upsert_instance(
                mapping.entity_type_name, subject, self.schema_version
            )
            report.instances_written += 1

            for fm in mapping.fields:
                defs = self._attr_defs(
                    mapping.entity_type_name, fm.attribute_name, scope
                )
                if defs is None:
                    continue    # 属性不存在或是派生属性，跳过
                aid, parsers, validators = defs

                raw = row.get(fm.source_field)
                raw_text = None if raw is None else str(raw)
                outcome = parse_value(raw_text, parsers, validators)

                match outcome:
                    case ParseHit(value=v, used_default=used_default):
                        self.store.put_attr(
                            instance_id=iid,
                            attribute_type_id=aid,
                            value=str(v),
                            valid_from=None,
                            valid_to=period,
                            disclosed_at=disclosed,
                            schema_version=self.schema_version,
                        )
                        report.values_written += 1
                        if used_default:
                            report.defaults_applied += 1

                    case ParseSkipped():
                        report.values_skipped += 1

                    case ParseFailed(reason=reason):
                        report.errors.append(
                            IngestError(
                                subject_key=subject,
                                attribute_name=fm.attribute_name,
                                raw_value=raw,
                                reason=reason,
                                kind="parse_failed",
                            )
                        )

                    case ValidationFailed(reason=reason):
                        # validator 无回退链，失败即记错并跳过该值
                        report.errors.append(
                            IngestError(
                                subject_key=subject,
                                attribute_name=fm.attribute_name,
                                raw_value=raw,
                                reason=reason,
                                kind="validation_failed",
                            )
                        )

        self.store.commit()
        return report
