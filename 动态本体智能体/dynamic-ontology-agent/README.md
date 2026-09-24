# dynamic-ontology-agent

增量式动态本体演化引擎。元数据与实例分离，Git 式版本管理，演化代价二维自动分级。

设计与里程碑见 [../项目计划.md](../项目计划.md)。

## 环境准备

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # Linux/macOS

docker compose up -d                                  # Postgres 16，宿主端口 5433
docker exec doa-postgres createdb -U doa doa_test      # 测试库，仅首次
```

连接串默认 `postgresql://doa:doa@localhost:5433/doa`，可用 `DOA_DSN` / `DOA_DSN_TEST` 覆盖（见 `.env.example`）。
宿主端口用 5433 而非 5432，避免与本机已装的 Postgres 冲突。

## 快速上手

```bash
doa init                                              # 建表 + 根版本 + main 分支

doa schema commit examples/01_seed_finance.json -m "金融种子本体"
doa schema show                                       # 当前 head 下可见的 schema
doa schema log                                        # 版本历史

doa schema branch proposal-yoy                        # 开提案分支
doa schema commit examples/02_add_derived_yoy.json -b proposal-yoy -m "加同比派生指标"
doa schema merge proposal-yoy                         # fast-forward 合并
doa schema rollback                                   # head 回退一步
```

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q
```

测试库不可达时整体跳过而非逐条报错。W1 共 25 条断言，覆盖版本图、分支隔离、
fast-forward 约束、回滚、区间可见性、级联下线、派生属性开关。

## 当前进度

| 周 | 范围 | 状态 |
|---|---|---|
| W1 | SR 类型模型 + 版本图 + 分支/合并/回滚 + CLI | **完成**，25 tests |
| W2 | reasoner-service（owlapi + HermiT）+ 二维定级 + IS + parser 链 | 未开始 |
| W3 | 派生属性求值器 + Assimilator 增量应用 | 未开始 |
| W4 | 展平 + 超图 + Qdrant 双索引 + 问答链路 | 未开始 |

## 设计约定

**fast-forward only**：每个版本单亲，版本图是一棵树。合并仅在目标分支 head 是源分支
head 的祖先时允许。分叉后必须基于最新 target 重开提案分支——不做三路合并。

**追加式存储**：类型定义从不 UPDATE。新增 = 插一行带 `introduced_in`；删除 = 写
`removed_in`。「修改」表达为 remove + add 的组合，因此任何历史版本都能被重建。

**唯一性不是数据库约束**：同名类型可以合法地出现多次（删除后重新加入，或存在于两个
分叉的提案分支上）。唯一性针对「某版本下的可见集」在应用层校验。

**回滚不删数据**：被丢弃的版本仍在 `schema_version` 表里，只是不再被任何分支指向，
仍可用 `doa schema show -v <id>` 读取。
