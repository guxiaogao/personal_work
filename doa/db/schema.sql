-- dynamic-ontology-agent  数据库模式
-- 分区说明：
--   W1  schema_version / branch / 类型定义表
--   W2  instance / instance_attr / relation_inst
--   W4  hyperedge / hypernode / edge_node
-- 版本化策略：类型定义不做快照复制，用 (introduced_in, removed_in) 区间表达；
--            某版本下可见的类型 = introduced_in 是其祖先或自身，且 removed_in 不是。

-- ---------------------------------------------------------------- 版本图

-- 版本树：fast-forward only ⇒ 每个版本单亲，整体是一棵树
CREATE TABLE IF NOT EXISTS schema_version (
    version_id   BIGSERIAL PRIMARY KEY,
    parent_id    BIGINT REFERENCES schema_version(version_id),
    branch       TEXT        NOT NULL,
    message      TEXT        NOT NULL DEFAULT '',
    delta_json   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- 二维代价等级；W1 尚无 Validator，一律 UNGRADED，W2 由定级器写入
    cost_tier_i  TEXT        NOT NULL DEFAULT 'UNGRADED',
    cost_tier_x  TEXT        NOT NULL DEFAULT 'UNGRADED',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cost_tier_i_valid CHECK (cost_tier_i IN ('L0','L1','L2','L3','UNGRADED')),
    CONSTRAINT cost_tier_x_valid CHECK (cost_tier_x IN ('L0','L1','L2','L3','UNGRADED'))
);

CREATE INDEX IF NOT EXISTS idx_schema_version_parent ON schema_version(parent_id);
CREATE INDEX IF NOT EXISTS idx_schema_version_branch ON schema_version(branch);

-- 分支：名字 → 头指针。回滚 = 把 head 指回祖先；fast-forward 合并 = 把 head 前移
CREATE TABLE IF NOT EXISTS branch (
    name            TEXT PRIMARY KEY,
    head_version_id BIGINT NOT NULL REFERENCES schema_version(version_id),
    forked_from     BIGINT REFERENCES schema_version(version_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 祖先判定：anc 是否为 desc 的祖先或其自身。fast-forward-only ⇒ 单亲上溯即可
CREATE OR REPLACE FUNCTION is_ancestor_or_self(anc BIGINT, descendant BIGINT)
RETURNS BOOLEAN AS $$
DECLARE
    found BOOLEAN;
BEGIN
    IF anc IS NULL OR descendant IS NULL THEN
        RETURN FALSE;
    END IF;
    WITH RECURSIVE up AS (
        SELECT version_id, parent_id FROM schema_version WHERE version_id = descendant
        UNION ALL
        SELECT sv.version_id, sv.parent_id
          FROM schema_version sv JOIN up ON sv.version_id = up.parent_id
    )
    SELECT EXISTS (SELECT 1 FROM up WHERE up.version_id = anc) INTO found;
    RETURN found;
END;
$$ LANGUAGE plpgsql STABLE;

-- 某类型定义在版本 v 下是否可见
CREATE OR REPLACE FUNCTION visible_at(introduced BIGINT, removed BIGINT, v BIGINT)
RETURNS BOOLEAN AS $$
    SELECT is_ancestor_or_self(introduced, v)
       AND (removed IS NULL OR NOT is_ancestor_or_self(removed, v));
$$ LANGUAGE sql STABLE;

-- ---------------------------------------------------------------- 类型定义

CREATE TABLE IF NOT EXISTS entity_type (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT   NOT NULL,
    uri           TEXT,
    parent_name   TEXT,                -- 类型层级；按名字引用，避免跨版本的 id 纠缠
    introduced_in BIGINT NOT NULL REFERENCES schema_version(version_id),
    removed_in    BIGINT REFERENCES schema_version(version_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_type_name ON entity_type(name);

CREATE TABLE IF NOT EXISTS relation_type (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT   NOT NULL,
    domain_name   TEXT   NOT NULL,     -- 定义域 EntityType 名
    range_name    TEXT   NOT NULL,     -- 值域 EntityType 名
    introduced_in BIGINT NOT NULL REFERENCES schema_version(version_id),
    removed_in    BIGINT REFERENCES schema_version(version_id)
);

CREATE INDEX IF NOT EXISTS idx_relation_type_name ON relation_type(name);

CREATE TABLE IF NOT EXISTS attribute_type (
    id               BIGSERIAL PRIMARY KEY,
    entity_type_name TEXT   NOT NULL,
    name             TEXT   NOT NULL,
    datatype         TEXT   NOT NULL,  -- string | number | date | bool
    min_cardinality  INT    NOT NULL DEFAULT 0,   -- 0 ⇒ 可选，L0/L1 零触碰的前提
    -- 派生属性：只存规则不存值，读时求值
    derivation_expr  TEXT,
    index_derived    BOOLEAN NOT NULL DEFAULT FALSE,  -- 派生属性是否入检索索引（二维代价的开关）
    introduced_in    BIGINT NOT NULL REFERENCES schema_version(version_id),
    removed_in       BIGINT REFERENCES schema_version(version_id),
    CONSTRAINT datatype_valid CHECK (datatype IN ('string','number','date','bool')),
    -- 派生属性不可为必填：它没有存量值可填
    CONSTRAINT derived_is_optional CHECK (derivation_expr IS NULL OR min_cardinality = 0)
);

CREATE INDEX IF NOT EXISTS idx_attribute_type_owner ON attribute_type(entity_type_name);
CREATE INDEX IF NOT EXISTS idx_attribute_type_name  ON attribute_type(name);

-- parser 链：按 priority 依次套用直到命中（专利 EP3835968A1 的核心机制）
CREATE TABLE IF NOT EXISTS parser_def (
    id                BIGSERIAL PRIMARY KEY,
    attribute_type_id BIGINT NOT NULL REFERENCES attribute_type(id),
    kind              TEXT   NOT NULL,   -- regex | code_module
    pattern           TEXT   NOT NULL,   -- regex 模式，或白名单里的可调用对象名
    priority          INT    NOT NULL DEFAULT 100,
    not_required      BOOLEAN NOT NULL DEFAULT FALSE,
    default_value     TEXT,
    introduced_in     BIGINT NOT NULL REFERENCES schema_version(version_id),
    removed_in        BIGINT REFERENCES schema_version(version_id),
    CONSTRAINT parser_kind_valid CHECK (kind IN ('regex','code_module'))
);

CREATE INDEX IF NOT EXISTS idx_parser_def_attr ON parser_def(attribute_type_id, priority);

-- validator：parse 之后、写入之前守门
CREATE TABLE IF NOT EXISTS validator_def (
    id                BIGSERIAL PRIMARY KEY,
    attribute_type_id BIGINT NOT NULL REFERENCES attribute_type(id),
    kind              TEXT   NOT NULL,   -- regex | enum | code_module
    spec_json         JSONB  NOT NULL,
    introduced_in     BIGINT NOT NULL REFERENCES schema_version(version_id),
    removed_in        BIGINT REFERENCES schema_version(version_id),
    CONSTRAINT validator_kind_valid CHECK (kind IN ('regex','enum','code_module'))
);

CREATE INDEX IF NOT EXISTS idx_validator_def_attr ON validator_def(attribute_type_id);

-- ---------------------------------------------------------------- 实例层（W2）

CREATE TABLE IF NOT EXISTS instance (
    id               BIGSERIAL PRIMARY KEY,
    entity_type_name TEXT   NOT NULL,
    subject_key      TEXT   NOT NULL,   -- 业务主键，如证券代码
    schema_version   BIGINT NOT NULL REFERENCES schema_version(version_id),
    UNIQUE (entity_type_name, subject_key)
);

CREATE INDEX IF NOT EXISTS idx_instance_type ON instance(entity_type_name);

-- 双时间戳：有效时间（报告期）与事务时间（披露日）。金融场景下二者真实分离
CREATE TABLE IF NOT EXISTS instance_attr (
    id                BIGSERIAL PRIMARY KEY,
    instance_id       BIGINT NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    attribute_type_id BIGINT NOT NULL REFERENCES attribute_type(id),
    value             TEXT,
    valid_from        DATE,           -- 报告期起
    valid_to          DATE,           -- 报告期止
    tx_from           TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 披露日 / 入库
    tx_to             TIMESTAMPTZ,
    schema_version    BIGINT NOT NULL REFERENCES schema_version(version_id)
);

-- 这两个索引就是 SR 反查影响集的实现，L2「影响集可枚举」靠它成立
CREATE INDEX IF NOT EXISTS idx_instance_attr_attrtype ON instance_attr(attribute_type_id);
CREATE INDEX IF NOT EXISTS idx_instance_attr_instance ON instance_attr(instance_id);

CREATE TABLE IF NOT EXISTS relation_inst (
    id               BIGSERIAL PRIMARY KEY,
    relation_type_id BIGINT NOT NULL REFERENCES relation_type(id),
    src_id           BIGINT NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    dst_id           BIGINT NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    valid_from       DATE,
    valid_to         DATE,
    tx_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    tx_to            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_relation_inst_type ON relation_inst(relation_type_id);
CREATE INDEX IF NOT EXISTS idx_relation_inst_src  ON relation_inst(src_id);

-- ---------------------------------------------------------------- 检索层（W4）

CREATE TABLE IF NOT EXISTS hyperedge (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    BIGINT NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    block_json     JSONB  NOT NULL,
    schema_version BIGINT NOT NULL REFERENCES schema_version(version_id)
);

-- instance → edge 反查，超边增量维护的关键
CREATE INDEX IF NOT EXISTS idx_hyperedge_instance ON hyperedge(instance_id);

CREATE TABLE IF NOT EXISTS hypernode (
    id         BIGSERIAL PRIMARY KEY,
    key_text   TEXT NOT NULL,   -- s⊕a，编码为 key 向量
    value_text TEXT,            -- v，编码为 value 向量
    UNIQUE (key_text, value_text)
);

CREATE TABLE IF NOT EXISTS edge_node (
    edge_id BIGINT NOT NULL REFERENCES hyperedge(id) ON DELETE CASCADE,
    node_id BIGINT NOT NULL REFERENCES hypernode(id) ON DELETE CASCADE,
    PRIMARY KEY (edge_id, node_id)
);

-- 倒排：node → edges
CREATE INDEX IF NOT EXISTS idx_edge_node_node ON edge_node(node_id);
