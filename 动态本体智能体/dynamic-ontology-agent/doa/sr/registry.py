"""SchemaRegistry：类型定义 + 版本图 + 分支。

不变式
------
* **fast-forward only**：每个版本单亲，版本图是一棵树。合并仅在目标分支 head
  是源分支 head 的祖先时允许，此时合并退化为移动指针。
* **追加式存储**：类型定义从不 UPDATE，只插入（带 introduced_in）或写 removed_in。
  任何历史版本都可由区间过滤重建。
* **唯一性不是数据库约束**：同名类型可以合法地出现多次（删除后重新加入，
  或存在于两个分叉的提案分支上）。唯一性针对「某版本下的可见集」在应用层校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from doa.derive import ExpressionError, dependencies
from doa.sr.delta import (
    AddAttributeType,
    AddEntityType,
    AddParserDef,
    AddRelationType,
    AddValidatorDef,
    RemoveAttributeType,
    RemoveEntityType,
    RemoveParserDef,
    RemoveRelationType,
    SchemaDelta,
    dump_deltas,
)

MAIN = "main"


class SchemaError(RuntimeError):
    """演化被拒：违反唯一性、引用不存在的类型、非 fast-forward 合并等。"""


@dataclass(frozen=True)
class VersionInfo:
    version_id: int
    parent_id: int | None
    branch: str
    message: str
    cost_tier_i: str
    cost_tier_x: str
    delta_count: int


class SchemaRegistry:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # -------------------------------------------------------------- 初始化

    def bootstrap(self) -> int:
        """建根版本与 main 分支。已存在则返回现有 main head。"""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT head_version_id FROM branch WHERE name = %s", (MAIN,))
            row = cur.fetchone()
            if row:
                return row["head_version_id"]

            cur.execute(
                """INSERT INTO schema_version (parent_id, branch, message, delta_json,
                                               cost_tier_i, cost_tier_x)
                   VALUES (NULL, %s, %s, '[]'::jsonb, 'L0', 'L0')
                   RETURNING version_id""",
                (MAIN, "root"),
            )
            root = cur.fetchone()["version_id"]
            cur.execute(
                "INSERT INTO branch (name, head_version_id, forked_from) VALUES (%s, %s, NULL)",
                (MAIN, root),
            )
        self.conn.commit()
        return root

    # -------------------------------------------------------------- 版本图

    def head(self, branch: str = MAIN) -> int:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT head_version_id FROM branch WHERE name = %s", (branch,))
            row = cur.fetchone()
        if not row:
            raise SchemaError(f"分支不存在: {branch}")
        return row["head_version_id"]

    def ancestors(self, version_id: int) -> list[int]:
        """从 version_id 上溯到根，含自身。fast-forward-only ⇒ 单亲链。"""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """WITH RECURSIVE up AS (
                       SELECT version_id, parent_id FROM schema_version WHERE version_id = %s
                       UNION ALL
                       SELECT sv.version_id, sv.parent_id
                         FROM schema_version sv JOIN up ON sv.version_id = up.parent_id
                   )
                   SELECT version_id FROM up""",
                (version_id,),
            )
            return [r["version_id"] for r in cur.fetchall()]

    def is_ancestor_or_self(self, anc: int, descendant: int) -> bool:
        return anc in set(self.ancestors(descendant))

    def log(self, branch: str = MAIN, limit: int = 50) -> list[VersionInfo]:
        """分支历史，从 head 回溯到根。"""
        chain = self.ancestors(self.head(branch))
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT version_id, parent_id, branch, message, cost_tier_i, cost_tier_x,
                          jsonb_array_length(delta_json) AS delta_count
                     FROM schema_version WHERE version_id = ANY(%s)""",
                (chain,),
            )
            by_id = {r["version_id"]: r for r in cur.fetchall()}
        out: list[VersionInfo] = []
        for vid in chain[:limit]:
            r = by_id[vid]
            out.append(
                VersionInfo(
                    version_id=r["version_id"],
                    parent_id=r["parent_id"],
                    branch=r["branch"],
                    message=r["message"],
                    cost_tier_i=r["cost_tier_i"],
                    cost_tier_x=r["cost_tier_x"],
                    delta_count=r["delta_count"],
                )
            )
        return out

    # -------------------------------------------------------------- 分支

    def branches(self) -> dict[str, int]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT name, head_version_id FROM branch ORDER BY name")
            return {r["name"]: r["head_version_id"] for r in cur.fetchall()}

    def create_branch(self, name: str, from_branch: str = MAIN) -> int:
        """从 from_branch 的 head 开一个新分支。"""
        if name in self.branches():
            raise SchemaError(f"分支已存在: {name}")
        base = self.head(from_branch)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO branch (name, head_version_id, forked_from) VALUES (%s, %s, %s)",
                (name, base, base),
            )
        self.conn.commit()
        return base

    def merge(self, source: str, target: str = MAIN) -> int:
        """fast-forward 合并：仅当 target head 是 source head 的祖先时允许。

        拒绝的情形即「target 上有 source 没有的版本」——那需要三路合并，
        本项目明确不做（见项目计划 §3 版本模型决策）。
        """
        src_head = self.head(source)
        tgt_head = self.head(target)
        if src_head == tgt_head:
            return tgt_head
        if not self.is_ancestor_or_self(tgt_head, src_head):
            raise SchemaError(
                f"非 fast-forward 合并被拒: {target}@{tgt_head} 不是 {source}@{src_head} 的祖先。"
                " 本项目只支持 fast-forward，请基于最新 target 重开提案分支。"
            )
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE branch SET head_version_id = %s WHERE name = %s", (src_head, target)
            )
        self.conn.commit()
        return src_head

    def rollback(self, branch: str = MAIN, to_version: int | None = None) -> int:
        """把分支 head 指回某个祖先。默认回退一步。

        版本行本身不删除——被丢弃的版本仍在表里，只是不再被任何分支指向。
        """
        cur_head = self.head(branch)
        if to_version is None:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT parent_id FROM schema_version WHERE version_id = %s", (cur_head,)
                )
                parent = cur.fetchone()["parent_id"]
            if parent is None:
                raise SchemaError("已在根版本，无法继续回退")
            to_version = parent
        elif not self.is_ancestor_or_self(to_version, cur_head):
            raise SchemaError(f"{to_version} 不是 {branch}@{cur_head} 的祖先，不能回退到它")

        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE branch SET head_version_id = %s WHERE name = %s", (to_version, branch)
            )
        self.conn.commit()
        return to_version

    # -------------------------------------------------------------- 演化

    def commit(
        self,
        deltas: Sequence[SchemaDelta],
        message: str = "",
        branch: str = MAIN,
    ) -> int:
        """把一组 delta 作为一个新版本提交到分支。

        全程单事务：校验失败则整体回滚，不留半个版本。
        W1 不做定级，cost_tier 留 UNGRADED 由 W2 的 Validator 填。
        """
        if not deltas:
            raise SchemaError("空变更集")

        parent = self.head(branch)
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """INSERT INTO schema_version (parent_id, branch, message, delta_json)
                       VALUES (%s, %s, %s, %s) RETURNING version_id""",
                    (parent, branch, message, Jsonb(dump_deltas(list(deltas)))),
                )
                new_version = cur.fetchone()["version_id"]

                # 逐个应用。可见性针对 parent 版本判断——同一批次内先加后引用也能成立，
                # 因为 _apply 里对本批次新增的类型会查 new_version。
                for d in deltas:
                    self._apply(cur, d, parent, new_version)

                cur.execute(
                    "UPDATE branch SET head_version_id = %s WHERE name = %s",
                    (new_version, branch),
                )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return new_version

    def _apply(
        self,
        cur: psycopg.Cursor,
        d: SchemaDelta,
        parent: int,
        new_version: int,
    ) -> None:
        """把单个 delta 落到定义表。校验在此处做。"""
        # 可见集按 parent 算，再叠加本批次已插入的行（它们的 introduced_in = new_version）
        scope = self.ancestors(parent) + [new_version]

        match d:
            case AddEntityType():
                if self._entity_visible(cur, d.name, scope):
                    raise SchemaError(f"EntityType 已存在: {d.name}")
                if d.parent_name and not self._entity_visible(cur, d.parent_name, scope):
                    raise SchemaError(f"父类型不存在: {d.parent_name}")
                cur.execute(
                    """INSERT INTO entity_type (name, uri, parent_name, introduced_in)
                       VALUES (%s, %s, %s, %s)""",
                    (d.name, d.uri, d.parent_name, new_version),
                )

            case AddRelationType():
                if self._relation_visible(cur, d.name, scope):
                    raise SchemaError(f"RelationType 已存在: {d.name}")
                for role, tname in (("domain", d.domain_name), ("range", d.range_name)):
                    if not self._entity_visible(cur, tname, scope):
                        raise SchemaError(f"{role} 类型不存在: {tname}")
                cur.execute(
                    """INSERT INTO relation_type (name, domain_name, range_name, introduced_in)
                       VALUES (%s, %s, %s, %s)""",
                    (d.name, d.domain_name, d.range_name, new_version),
                )

            case AddAttributeType():
                if not self._entity_visible(cur, d.entity_type_name, scope):
                    raise SchemaError(f"EntityType 不存在: {d.entity_type_name}")
                if self._attr_row(cur, d.entity_type_name, d.name, scope):
                    raise SchemaError(f"AttributeType 已存在: {d.entity_type_name}.{d.name}")
                if d.derivation_expr and d.min_cardinality > 0:
                    raise SchemaError("派生属性不能为必填：它没有存量值可填")
                if d.derivation_expr:
                    self._check_derivation(
                        cur, d.entity_type_name, d.name, d.derivation_expr, scope
                    )
                cur.execute(
                    """INSERT INTO attribute_type
                           (entity_type_name, name, datatype, min_cardinality,
                            derivation_expr, index_derived, introduced_in)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        d.entity_type_name,
                        d.name,
                        d.datatype,
                        d.min_cardinality,
                        d.derivation_expr,
                        d.index_derived,
                        new_version,
                    ),
                )

            case AddParserDef():
                attr = self._attr_row(cur, d.entity_type_name, d.attribute_name, scope)
                if not attr:
                    raise SchemaError(
                        f"AttributeType 不存在: {d.entity_type_name}.{d.attribute_name}"
                    )
                cur.execute(
                    """INSERT INTO parser_def (attribute_type_id, kind, pattern, priority,
                                               not_required, default_value, introduced_in)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        attr["id"],
                        d.kind,
                        d.pattern,
                        d.priority,
                        d.not_required,
                        d.default_value,
                        new_version,
                    ),
                )

            case AddValidatorDef():
                attr = self._attr_row(cur, d.entity_type_name, d.attribute_name, scope)
                if not attr:
                    raise SchemaError(
                        f"AttributeType 不存在: {d.entity_type_name}.{d.attribute_name}"
                    )
                cur.execute(
                    """INSERT INTO validator_def (attribute_type_id, kind, spec_json,
                                                  introduced_in)
                       VALUES (%s, %s, %s, %s)""",
                    (attr["id"], d.kind, Jsonb(d.spec), new_version),
                )

            case RemoveEntityType():
                row = self._entity_row(cur, d.name, scope)
                if not row:
                    raise SchemaError(f"EntityType 不存在: {d.name}")
                # 级联：该类型的属性与以它为定义域/值域的关系一并下线
                cur.execute(
                    "UPDATE entity_type SET removed_in = %s WHERE id = %s",
                    (new_version, row["id"]),
                )
                cur.execute(
                    """UPDATE attribute_type SET removed_in = %s
                        WHERE entity_type_name = %s AND removed_in IS NULL
                          AND introduced_in = ANY(%s)""",
                    (new_version, d.name, scope),
                )
                cur.execute(
                    """UPDATE relation_type SET removed_in = %s
                        WHERE (domain_name = %s OR range_name = %s) AND removed_in IS NULL
                          AND introduced_in = ANY(%s)""",
                    (new_version, d.name, d.name, scope),
                )

            case RemoveAttributeType():
                row = self._attr_row(cur, d.entity_type_name, d.name, scope)
                if not row:
                    raise SchemaError(
                        f"AttributeType 不存在: {d.entity_type_name}.{d.name}"
                    )
                cur.execute(
                    "UPDATE attribute_type SET removed_in = %s WHERE id = %s",
                    (new_version, row["id"]),
                )
                for tbl in ("parser_def", "validator_def"):
                    cur.execute(
                        f"""UPDATE {tbl} SET removed_in = %s
                             WHERE attribute_type_id = %s AND removed_in IS NULL""",
                        (new_version, row["id"]),
                    )

            case RemoveRelationType():
                row = self._relation_row(cur, d.name, scope)
                if not row:
                    raise SchemaError(f"RelationType 不存在: {d.name}")
                cur.execute(
                    "UPDATE relation_type SET removed_in = %s WHERE id = %s",
                    (new_version, row["id"]),
                )

            case RemoveParserDef():
                attr = self._attr_row(cur, d.entity_type_name, d.attribute_name, scope)
                if not attr:
                    raise SchemaError(
                        f"AttributeType 不存在: {d.entity_type_name}.{d.attribute_name}"
                    )
                cur.execute(
                    """SELECT id FROM parser_def
                        WHERE attribute_type_id = %s AND pattern = %s
                          AND removed_in IS NULL AND introduced_in = ANY(%s)""",
                    (attr["id"], d.pattern, scope),
                )
                prow = cur.fetchone()
                if not prow:
                    raise SchemaError(f"parser 不存在: {d.pattern!r}")
                cur.execute(
                    "UPDATE parser_def SET removed_in = %s WHERE id = %s",
                    (new_version, prow["id"]),
                )

            case _:
                raise SchemaError(f"未知算子: {d!r}")

    def _check_derivation(
        self,
        cur: psycopg.Cursor,
        entity: str,
        attr_name: str,
        expr: str,
        scope: list[int],
    ) -> None:
        """校验派生表达式：语法、自引用、依赖存在性、禁止引用其他派生属性。

        在**提交时**校验而非求值时，是为了让「坏 schema 进不来」——
        求值阶段只该遇到数据问题，不该遇到定义问题。
        """
        try:
            deps = dependencies(expr)
        except ExpressionError as e:
            raise SchemaError(f"派生表达式非法 ({entity}.{attr_name}): {e}") from e

        if attr_name in deps.all_names:
            raise SchemaError(f"派生属性不能引用自身: {entity}.{attr_name}")

        for dep in sorted(deps.all_names):
            row = self._attr_row(cur, entity, dep, scope)
            if row is None:
                raise SchemaError(
                    f"派生表达式引用了不存在的属性: {entity}.{dep}"
                    f"（定义于 {entity}.{attr_name}）"
                )
            # 依赖图恒为一层：否则分级时要沿依赖链传递失效，且需环检测
            if row["derivation_expr"] is not None:
                raise SchemaError(
                    f"派生属性不能引用其他派生属性: {entity}.{attr_name} → {entity}.{dep}"
                )

    # -------------------------------------------------------------- 可见性查询

    # 可见 = introduced_in 在 scope 内，且 removed_in 不在 scope 内。
    # scope 是预先算好的祖先集合，避免逐行触发递归 CTE。
    _VISIBLE = "introduced_in = ANY(%(scope)s) AND (removed_in IS NULL OR NOT (removed_in = ANY(%(scope)s)))"

    def _entity_row(self, cur: psycopg.Cursor, name: str, scope: list[int]) -> dict[str, Any] | None:
        cur.execute(
            f"SELECT * FROM entity_type WHERE name = %(name)s AND {self._VISIBLE}",
            {"name": name, "scope": scope},
        )
        return cur.fetchone()

    def _entity_visible(self, cur: psycopg.Cursor, name: str, scope: list[int]) -> bool:
        return self._entity_row(cur, name, scope) is not None

    def _relation_row(self, cur: psycopg.Cursor, name: str, scope: list[int]) -> dict[str, Any] | None:
        cur.execute(
            f"SELECT * FROM relation_type WHERE name = %(name)s AND {self._VISIBLE}",
            {"name": name, "scope": scope},
        )
        return cur.fetchone()

    def _relation_visible(self, cur: psycopg.Cursor, name: str, scope: list[int]) -> bool:
        return self._relation_row(cur, name, scope) is not None

    def _attr_row(
        self, cur: psycopg.Cursor, entity: str, name: str, scope: list[int]
    ) -> dict[str, Any] | None:
        cur.execute(
            f"""SELECT * FROM attribute_type
                 WHERE entity_type_name = %(entity)s AND name = %(name)s AND {self._VISIBLE}""",
            {"entity": entity, "name": name, "scope": scope},
        )
        return cur.fetchone()

    # -------------------------------------------------------------- 快照读取

    def snapshot(self, version_id: int | None = None, branch: str = MAIN) -> dict[str, Any]:
        """重建某版本下可见的完整 schema。"""
        v = version_id if version_id is not None else self.head(branch)
        scope = self.ancestors(v)
        params = {"scope": scope}

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT name, uri, parent_name FROM entity_type WHERE {self._VISIBLE} ORDER BY name",
                params,
            )
            entities = cur.fetchall()

            cur.execute(
                f"""SELECT name, domain_name, range_name FROM relation_type
                     WHERE {self._VISIBLE} ORDER BY name""",
                params,
            )
            relations = cur.fetchall()

            cur.execute(
                f"""SELECT id, entity_type_name, name, datatype, min_cardinality,
                           derivation_expr, index_derived
                      FROM attribute_type WHERE {self._VISIBLE}
                     ORDER BY entity_type_name, name""",
                params,
            )
            attrs = cur.fetchall()

            attr_ids = [a["id"] for a in attrs]
            parsers: dict[int, list[dict[str, Any]]] = {}
            validators: dict[int, list[dict[str, Any]]] = {}
            if attr_ids:
                cur.execute(
                    f"""SELECT attribute_type_id, kind, pattern, priority, not_required,
                               default_value
                          FROM parser_def
                         WHERE attribute_type_id = ANY(%(ids)s) AND {self._VISIBLE}
                         ORDER BY attribute_type_id, priority""",
                    {"ids": attr_ids, **params},
                )
                for r in cur.fetchall():
                    parsers.setdefault(r["attribute_type_id"], []).append(r)

                cur.execute(
                    f"""SELECT attribute_type_id, kind, spec_json
                          FROM validator_def
                         WHERE attribute_type_id = ANY(%(ids)s) AND {self._VISIBLE}""",
                    {"ids": attr_ids, **params},
                )
                for r in cur.fetchall():
                    validators.setdefault(r["attribute_type_id"], []).append(r)

        by_entity: dict[str, list[dict[str, Any]]] = {}
        for a in attrs:
            by_entity.setdefault(a["entity_type_name"], []).append(
                {
                    "name": a["name"],
                    "datatype": a["datatype"],
                    "min_cardinality": a["min_cardinality"],
                    "derivation_expr": a["derivation_expr"],
                    "index_derived": a["index_derived"],
                    "parsers": parsers.get(a["id"], []),
                    "validators": validators.get(a["id"], []),
                }
            )

        return {
            "version_id": v,
            "entity_types": [
                {**e, "attributes": by_entity.get(e["name"], [])} for e in entities
            ],
            "relation_types": relations,
        }
