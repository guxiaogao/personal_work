"""InstanceStore：双时间戳实例存储与反查索引。

双时间戳语义
------------
* **有效时间** `valid_from` / `valid_to` —— 报告期（2024Q3 即 2024-07-01..2024-09-30）
* **事务时间** `tx_from` / `tx_to` —— 披露日；`tx_to IS NULL` 表示"当前仍然相信"

同一 (实例, 属性, 报告期) 来了新披露时，**不覆盖旧行**：先把旧行的 `tx_to` 置为
新披露日以关闭其事务区间，再插一行新值。这是追溯调整能被记录下来的唯一方式，
也让"2024-03 时我们相信的 2023 年报营收是多少"成为可查询的问题。

反查索引
--------
`instance_attr(attribute_type_id)` 上的索引就是 L2「影响集可枚举」的实现依据：
给定一个 AttributeType，能在有界时间内列出所有受影响实例。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from doa.derive import ValueResolver


def _as_aware(dt: datetime | None) -> datetime | None:
    """把 naive datetime 视为 UTC。

    `tx_from` / `tx_to` 列是 TIMESTAMPTZ（事务时间是真实时刻，该带时区），
    于是 Postgres 返回 aware datetime，而披露日常以 naive 形式传入
    （财报接口给的是 '2024-10-30' 这类日期）。两者直接比较会抛
    TypeError: can't compare offset-naive and offset-aware datetimes。

    统一按 UTC 解释。披露日是日粒度，而同一属性的两次披露不会相隔数小时，
    因此时区偏移不影响先后判定。
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


class OutOfOrderDisclosure(RuntimeError):
    """新披露日早于现有行的事务起点。

    正确处理需要在历史中间插入事务区间，超出 MVP 范围；
    静默写入会产生 tx_from > tx_to 的倒置区间，破坏时点重建查询。
    """


@dataclass(frozen=True)
class AttrValue:
    """一条属性值（某实例、某属性、某报告期、某披露时点）。"""

    instance_id: int
    attribute_type_id: int
    attribute_name: str
    value: str | None
    valid_from: date | None
    valid_to: date | None
    tx_from: datetime
    tx_to: datetime | None

    @property
    def is_current(self) -> bool:
        """事务区间未关闭 ⇒ 这是当前相信的值。"""
        return self.tx_to is None


@dataclass(frozen=True)
class ImpactSet:
    """某个 schema 变更影响到的存量实例集合。L2 定级的核心产物。"""

    attribute_type_id: int
    instance_ids: tuple[int, ...]
    value_row_count: int

    @property
    def size(self) -> int:
        return len(self.instance_ids)

    @property
    def is_empty(self) -> bool:
        return not self.instance_ids


class InstanceStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # -------------------------------------------------------------- 实例

    def upsert_instance(
        self, entity_type_name: str, subject_key: str, schema_version: int
    ) -> int:
        """按 (类型, 业务主键) 取或建实例。业务主键如证券代码。"""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO instance (entity_type_name, subject_key, schema_version)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (entity_type_name, subject_key) DO UPDATE
                       SET entity_type_name = EXCLUDED.entity_type_name
                   RETURNING id""",
                (entity_type_name, subject_key, schema_version),
            )
            return cur.fetchone()["id"]

    def get_instance(self, entity_type_name: str, subject_key: str) -> int | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id FROM instance
                    WHERE entity_type_name = %s AND subject_key = %s""",
                (entity_type_name, subject_key),
            )
            row = cur.fetchone()
        return row["id"] if row else None

    def count_instances(self, entity_type_name: str | None = None) -> int:
        with self.conn.cursor(row_factory=dict_row) as cur:
            if entity_type_name is None:
                cur.execute("SELECT count(*) AS n FROM instance")
            else:
                cur.execute(
                    "SELECT count(*) AS n FROM instance WHERE entity_type_name = %s",
                    (entity_type_name,),
                )
            return cur.fetchone()["n"]

    # -------------------------------------------------------------- 属性值写入

    def put_attr(
        self,
        *,
        instance_id: int,
        attribute_type_id: int,
        value: str | None,
        valid_from: date | None,
        valid_to: date | None,
        disclosed_at: datetime | None = None,
        schema_version: int,
    ) -> int:
        """写入一个属性值，按双时间戳语义处理同期重复披露。

        同一 (实例, 属性, 报告期) 已有未关闭的行时：关闭旧行（`tx_to = disclosed_at`）
        再插新行。旧值仍可查，这是追溯调整的记录方式。

        值未变化时不写新行——否则每次重抓数据都会产生一条无意义的历史。
        """
        disclosed_at = _as_aware(disclosed_at)

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, value, tx_from FROM instance_attr
                    WHERE instance_id = %s AND attribute_type_id = %s
                      AND valid_from IS NOT DISTINCT FROM %s
                      AND valid_to   IS NOT DISTINCT FROM %s
                      AND tx_to IS NULL""",
                (instance_id, attribute_type_id, valid_from, valid_to),
            )
            existing = cur.fetchone()

            if existing is not None:
                if existing["value"] == value:
                    return existing["id"]      # 值没变，不制造历史

                # 乱序到达守门：新披露日早于现有行的 tx_from 时，关闭它会产生
                # tx_from > tx_to 的倒置区间，让 value_as_believed_at 的结果失去意义。
                # 乱序回填是真实场景（先抓新报告、后补旧披露），但正确处理需要
                # 在历史中间插入区间，超出 MVP 范围——故明确拒绝而非静默写坏数据。
                if disclosed_at is not None and disclosed_at < existing["tx_from"]:
                    raise OutOfOrderDisclosure(
                        f"披露日 {disclosed_at.isoformat()} 早于现有行的 "
                        f"{existing['tx_from'].isoformat()}（实例 {instance_id}、"
                        f"属性 {attribute_type_id}、报告期 {valid_to}）。"
                        " 乱序回填尚未支持，请按披露日先后顺序灌入。"
                    )

                cur.execute(
                    "UPDATE instance_attr SET tx_to = COALESCE(%s, now()) WHERE id = %s",
                    (disclosed_at, existing["id"]),
                )

            cur.execute(
                """INSERT INTO instance_attr
                       (instance_id, attribute_type_id, value,
                        valid_from, valid_to, tx_from, schema_version)
                   VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s)
                   RETURNING id""",
                (
                    instance_id,
                    attribute_type_id,
                    value,
                    valid_from,
                    valid_to,
                    disclosed_at,
                    schema_version,
                ),
            )
            return cur.fetchone()["id"]

    def commit(self) -> None:
        self.conn.commit()

    # -------------------------------------------------------------- 读取

    def current_attrs(
        self, instance_id: int, *, as_of_period: date | None = None
    ) -> list[AttrValue]:
        """某实例当前相信的属性值。as_of_period 限定报告期。"""
        sql = """
            SELECT ia.instance_id, ia.attribute_type_id, at.name AS attribute_name,
                   ia.value, ia.valid_from, ia.valid_to, ia.tx_from, ia.tx_to
              FROM instance_attr ia
              JOIN attribute_type at ON at.id = ia.attribute_type_id
             WHERE ia.instance_id = %(iid)s AND ia.tx_to IS NULL
        """
        params: dict[str, Any] = {"iid": instance_id}
        if as_of_period is not None:
            sql += " AND ia.valid_to = %(period)s"
            params["period"] = as_of_period
        sql += " ORDER BY at.name"

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [AttrValue(**dict(r)) for r in cur.fetchall()]

    def attr_history(
        self, instance_id: int, attribute_type_id: int, *, period: date | None = None
    ) -> list[AttrValue]:
        """某属性的全部披露历史，含已关闭的事务区间。追溯调整在此可见。"""
        sql = """
            SELECT ia.instance_id, ia.attribute_type_id, at.name AS attribute_name,
                   ia.value, ia.valid_from, ia.valid_to, ia.tx_from, ia.tx_to
              FROM instance_attr ia
              JOIN attribute_type at ON at.id = ia.attribute_type_id
             WHERE ia.instance_id = %(iid)s AND ia.attribute_type_id = %(aid)s
        """
        params: dict[str, Any] = {"iid": instance_id, "aid": attribute_type_id}
        if period is not None:
            sql += " AND ia.valid_to = %(period)s"
            params["period"] = period
        sql += " ORDER BY ia.valid_to, ia.tx_from"

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [AttrValue(**dict(r)) for r in cur.fetchall()]

    def value_as_believed_at(
        self,
        instance_id: int,
        attribute_type_id: int,
        period: date,
        believed_at: datetime,
    ) -> str | None:
        """「在 believed_at 时点，我们相信的该报告期值是多少」。

        这是双时间戳存在的意义：追溯调整后，历史认知仍可重建。
        """
        believed_at = _as_aware(believed_at)

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT value FROM instance_attr
                    WHERE instance_id = %s AND attribute_type_id = %s AND valid_to = %s
                      AND tx_from <= %s AND (tx_to IS NULL OR tx_to > %s)
                    ORDER BY tx_from DESC LIMIT 1""",
                (instance_id, attribute_type_id, period, believed_at, believed_at),
            )
            row = cur.fetchone()
        return row["value"] if row else None

    # -------------------------------------------------------------- 反查（L2 影响集）

    def impact_set(self, attribute_type_id: int) -> ImpactSet:
        """列出受某 AttributeType 影响的存量实例。

        **这就是 L2「影响集可枚举」的实现**：靠 `instance_attr(attribute_type_id)`
        索引，在有界时间内给出确定的实例清单。影响集可枚举 → L2；否则 → L3。
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT DISTINCT instance_id FROM instance_attr
                    WHERE attribute_type_id = %s AND tx_to IS NULL
                    ORDER BY instance_id""",
                (attribute_type_id,),
            )
            ids = tuple(r["instance_id"] for r in cur.fetchall())

            cur.execute(
                """SELECT count(*) AS n FROM instance_attr
                    WHERE attribute_type_id = %s AND tx_to IS NULL""",
                (attribute_type_id,),
            )
            n = cur.fetchone()["n"]

        return ImpactSet(
            attribute_type_id=attribute_type_id, instance_ids=ids, value_row_count=n
        )

    def instances_of_type(self, entity_type_name: str) -> tuple[int, ...]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM instance WHERE entity_type_name = %s ORDER BY id",
                (entity_type_name,),
            )
            return tuple(r["id"] for r in cur.fetchall())

    def missing_required(
        self, attribute_type_id: int, entity_type_name: str
    ) -> list[tuple[int, str]]:
        """该类型下缺少此属性值的实例，用于必填约束收紧时生成 L3 违规清单。

        返回 (instance_id, subject_key)，subject_key 比 id 可读。
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT i.id, i.subject_key
                     FROM instance i
                    WHERE i.entity_type_name = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM instance_attr ia
                           WHERE ia.instance_id = i.id
                             AND ia.attribute_type_id = %s
                             AND ia.tx_to IS NULL
                             AND ia.value IS NOT NULL AND ia.value <> ''
                      )
                    ORDER BY i.id""",
                (entity_type_name, attribute_type_id),
            )
            return [(r["id"], r["subject_key"]) for r in cur.fetchall()]

    def count_attr_rows(self, *, attribute_type_id: int | None = None) -> int:
        """属性值行数。L0/L1 的「零写入」断言靠比对此计数成立。"""
        with self.conn.cursor(row_factory=dict_row) as cur:
            if attribute_type_id is None:
                cur.execute("SELECT count(*) AS n FROM instance_attr")
            else:
                cur.execute(
                    "SELECT count(*) AS n FROM instance_attr WHERE attribute_type_id = %s",
                    (attribute_type_id,),
                )
            return cur.fetchone()["n"]


# ------------------------------------------------------------------ 派生属性求值上下文

def _shift_back_one_year(d: date) -> date:
    """报告期回退一年。2024-02-29 → 2023-02-28（闰日回退取 2 月末）。"""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


class StoreResolver(ValueResolver):
    """从 InstanceStore 取值的求值上下文，供派生属性惰求值使用。

    `same_period_last_year(attr)` 在库里的含义：同一实例、同一属性、
    报告期（`valid_to`）回退一年的那条当前值。
    """

    def __init__(
        self,
        store: InstanceStore,
        instance_id: int,
        period: date,
        *,
        attr_ids: dict[str, int] | None = None,
    ) -> None:
        self.store = store
        self.instance_id = instance_id
        self.period = period
        self._attr_ids = attr_ids or {}
        self._cache: dict[tuple[str, date], Decimal | None] = {}

    def _attr_id(self, attr: str) -> int | None:
        if attr in self._attr_ids:
            return self._attr_ids[attr]
        with self.store.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id FROM attribute_type
                    WHERE name = %s AND removed_in IS NULL
                    ORDER BY id DESC LIMIT 1""",
                (attr,),
            )
            row = cur.fetchone()
        aid = row["id"] if row else None
        self._attr_ids[attr] = aid  # type: ignore[assignment]
        return aid

    def _fetch(self, attr: str, period: date) -> Decimal | None:
        key = (attr, period)
        if key in self._cache:
            return self._cache[key]

        aid = self._attr_id(attr)
        if aid is None:
            self._cache[key] = None
            return None

        with self.store.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT value FROM instance_attr
                    WHERE instance_id = %s AND attribute_type_id = %s
                      AND valid_to = %s AND tx_to IS NULL
                    ORDER BY tx_from DESC LIMIT 1""",
                (self.instance_id, aid, period),
            )
            row = cur.fetchone()

        out: Decimal | None = None
        if row is not None and row["value"] not in (None, ""):
            try:
                out = Decimal(str(row["value"]))
            except InvalidOperation:
                out = None      # 非数值按缺失处理，与表达式求值器的约定一致
        self._cache[key] = out
        return out

    def current(self, attr: str) -> Decimal | None:
        return self._fetch(attr, self.period)

    def prior(self, attr: str) -> Decimal | None:
        return self._fetch(attr, _shift_back_one_year(self.period))
