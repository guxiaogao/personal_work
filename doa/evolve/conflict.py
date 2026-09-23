"""L3 冲突报告与人工裁决。

专利 EP3835968A1 明确未涉及 validator 失败后的处置动作，这部分是自行设计。

裁决动作三选一，语义正交：
* `amend_ontology` —— 改本体：放宽约束后重新提交（如必填降为可选）。
  产出一个新版本，原提案被替代。
* `amend_data`     —— 改数据：给违规实例补值或标记例外，再重新提交原变更。
  本体不变，触碰存量实例。
* `abandon`        —— 放弃：丢弃提案分支，什么都不改。

三者都要求填写 rationale：「为何这么裁」是审计的核心，也是论文里
「L3 拦截准确率」这项指标的人工核验依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from doa.sr.delta import SchemaDelta, dump_deltas

# 明细封顶：一次跨行业灌数据可能产生上万条违规，全量入库既慢又无助于裁决。
# 总数如实记录在 conflict_report.violation_count，只截断明细。
MAX_VIOLATION_DETAIL = 200

Action = Literal["amend_ontology", "amend_data", "abandon"]

ViolationKind = Literal[
    "missing_required",  # 必填约束下存量为空
    "data_loss",         # 删除类型/属性会丢弃已有实例数据
    "type_mismatch",     # 现值不满足新 datatype
    "orphan_instance",   # 实例的类型被移除
]


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    entity_type_name: str | None = None
    attribute_name: str | None = None
    instance_id: int | None = None
    subject_key: str | None = None
    current_value: str | None = None
    constraint_desc: str | None = None

    def describe(self) -> str:
        loc = ".".join(x for x in (self.entity_type_name, self.attribute_name) if x)
        who = f" [{self.subject_key}]" if self.subject_key else ""
        val = f" 当前值={self.current_value!r}" if self.current_value is not None else ""
        return f"{self.kind} {loc}{who}{val} — {self.constraint_desc or ''}".rstrip(" —")


@dataclass
class ConflictReport:
    """一次被拒的 L3 变更。"""

    report_id: int
    branch: str
    base_version_id: int
    violations: list[Violation] = field(default_factory=list)
    violation_count: int = 0      # 真实总数，可能大于 len(violations)
    truncated: bool = False
    status: str = "open"
    # 按 (类型, 违规种类) 的全量聚合。**不能**由 violations 现场算出：
    # 明细封顶后那样算会既低估受影响类型的数量、又让小众类型彻底消失。
    type_summary: dict[tuple[str, str], int] = field(default_factory=dict)

    def summary(self) -> str:
        shown = len(self.violations)
        tail = f"（明细仅前 {shown} 条）" if self.truncated else ""
        return f"报告 #{self.report_id} [{self.branch}] 违规 {self.violation_count} 条{tail}"

    def by_entity_type(self) -> dict[str, int]:
        """按实体类型聚合全量违规。

        违规集中于某一类型，通常提示该走「约束下推到子类型」而非放宽全局约束。
        本方法读的是入库时对**全量**违规算好的 type_summary，不是被截断的明细——
        否则小众类型会因截断而不可见，裁决的人会漏掉它，下次灌该类型数据时再撞一次。
        """
        out: dict[str, int] = {}
        for (entity, _kind), n in self.type_summary.items():
            out[entity] = out.get(entity, 0) + n
        return out

    def by_kind(self) -> dict[str, int]:
        """按违规种类聚合全量违规。"""
        out: dict[str, int] = {}
        for (_entity, kind), n in self.type_summary.items():
            out[kind] = out.get(kind, 0) + n
        return out


class ConflictStore:
    """冲突报告与裁决记录的持久化。"""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # -------------------------------------------------------------- 写入

    def record(
        self,
        *,
        branch: str,
        base_version_id: int,
        deltas: Sequence[SchemaDelta],
        violations: Sequence[Violation],
        cost_tier_i: str = "L3",
        cost_tier_x: str = "UNGRADED",
    ) -> ConflictReport:
        """记录一次被拒的变更。明细封顶，总数如实。"""
        total = len(violations)
        detail = list(violations[:MAX_VIOLATION_DETAIL])
        truncated = total > len(detail)

        # 聚合必须在全量违规上算，且在截断**之前**——这是明细封顶后唯一的信息来源
        summary: dict[tuple[str, str], int] = {}
        for v in violations:
            key = (v.entity_type_name or "(未知)", v.kind)
            summary[key] = summary.get(key, 0) + 1

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO conflict_report
                       (branch, base_version_id, delta_json, cost_tier_i, cost_tier_x,
                        violation_count, truncated)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    branch,
                    base_version_id,
                    Jsonb(dump_deltas(list(deltas))),
                    cost_tier_i,
                    cost_tier_x,
                    total,
                    truncated,
                ),
            )
            rid = cur.fetchone()["id"]

            if detail:
                cur.executemany(
                    """INSERT INTO conflict_violation
                           (report_id, kind, entity_type_name, attribute_name,
                            instance_id, subject_key, current_value, constraint_desc)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (
                            rid,
                            v.kind,
                            v.entity_type_name,
                            v.attribute_name,
                            v.instance_id,
                            v.subject_key,
                            v.current_value,
                            v.constraint_desc,
                        )
                        for v in detail
                    ],
                )

            if summary:
                cur.executemany(
                    """INSERT INTO conflict_type_summary
                           (report_id, entity_type_name, kind, violation_count)
                       VALUES (%s, %s, %s, %s)""",
                    [(rid, ent, kind, n) for (ent, kind), n in summary.items()],
                )
        self.conn.commit()

        return ConflictReport(
            report_id=rid,
            branch=branch,
            base_version_id=base_version_id,
            violations=detail,
            violation_count=total,
            truncated=truncated,
            type_summary=summary,
        )

    def adjudicate(
        self,
        report_id: int,
        *,
        action: Action,
        actor: str,
        rationale: str,
        result_version_id: int | None = None,
        affected_instances: int = 0,
    ) -> int:
        """登记裁决结果并关闭报告。

        本方法只**记录**裁决，不执行动作——改本体要走 SchemaRegistry.commit，
        改数据要走 Assimilator。职责分离，便于审计：裁决记录与实际变更各有出处。
        """
        if not rationale.strip():
            raise ValueError("裁决必须填写理由：这是审计与 L3 拦截准确率核验的依据")

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status FROM conflict_report WHERE id = %s", (report_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"冲突报告不存在: #{report_id}")
            if row["status"] == "resolved":
                raise ValueError(f"报告 #{report_id} 已裁决，不可重复裁决")

            cur.execute(
                """INSERT INTO adjudication
                       (report_id, action, actor, rationale, result_version_id,
                        affected_instances)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (report_id, action, actor, rationale, result_version_id, affected_instances),
            )
            aid = cur.fetchone()["id"]
            cur.execute(
                "UPDATE conflict_report SET status = 'resolved' WHERE id = %s", (report_id,)
            )
        self.conn.commit()
        return aid

    # -------------------------------------------------------------- 读取

    def get(self, report_id: int) -> ConflictReport | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM conflict_report WHERE id = %s", (report_id,))
            r = cur.fetchone()
            if r is None:
                return None
            cur.execute(
                """SELECT kind, entity_type_name, attribute_name, instance_id,
                          subject_key, current_value, constraint_desc
                     FROM conflict_violation WHERE report_id = %s ORDER BY id""",
                (report_id,),
            )
            vs = [Violation(**dict(x)) for x in cur.fetchall()]

            cur.execute(
                """SELECT entity_type_name, kind, violation_count
                     FROM conflict_type_summary WHERE report_id = %s""",
                (report_id,),
            )
            summary = {
                (x["entity_type_name"], x["kind"]): x["violation_count"]
                for x in cur.fetchall()
            }

        return ConflictReport(
            report_id=r["id"],
            branch=r["branch"],
            base_version_id=r["base_version_id"],
            violations=vs,
            violation_count=r["violation_count"],
            truncated=r["truncated"],
            status=r["status"],
            type_summary=summary,
        )

    def open_reports(self) -> list[ConflictReport]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM conflict_report WHERE status = 'open' ORDER BY id"
            )
            ids = [x["id"] for x in cur.fetchall()]
        return [r for r in (self.get(i) for i in ids) if r is not None]

    def adjudications(self, report_id: int) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT action, actor, rationale, result_version_id,
                          affected_instances, created_at
                     FROM adjudication WHERE report_id = %s ORDER BY id""",
                (report_id,),
            )
            return [dict(x) for x in cur.fetchall()]
