# 动态本体智能体

增量式动态本体演化引擎。设计与进度见 [项目计划.md](./项目计划.md)（单一事实来源，勿在本文件重复）。
代码在 `dynamic-ontology-agent/`。用中文回复。

## 环境（每次新会话先确认）

```bash
cd dynamic-ontology-agent
docker compose up -d                                    # Postgres，宿主端口 5433
docker exec doa-postgres createdb -U doa doa_test        # 仅首次
bash reasoner-service/run.sh &                           # HermiT 服务 :7070
.venv/Scripts/python.exe -m pytest -q                    # 全量测试
.venv/Scripts/doa.exe schema check                       # CLI 入口
```

- **必须用 `.venv/Scripts/python.exe`**：全局 Python 有坏的 `dmPython.pth`（缺 `dpi` 模块）
- Postgres 用 **5433** 而非 5432：本机已有 6379 / 3307 / 7687 等容器
- Docker Desktop 在 `%LOCALAPPDATA%\Programs\DockerDesktop`，不在 Program Files
- Git Bash 的 `/tmp` = `C:\Users\Administrator\AppData\Local\Temp`；脚本里用 `tempfile.gettempdir()`，硬写 `/tmp` 会 glob 不到
- 推理服务未启动时 `tests/test_w2_reasoner.py` 整体跳过，不算失败

## 不可违反的约束

**术语**：代码、注释、文档一律不使用 Palantir 用词（Object Type / Link Type / Ontology Manager / Funnel / Action 等）。用自有命名：`EntityType` / `RelationType` / `SchemaRegistry` / `InstanceStore` / `Assimilator`。branch / merge / rollback 是通用 Git 词汇，可用。

**SchemaRegistry 三条不变式**：fast-forward only（版本图是树，不做三路合并）；追加式存储（类型定义从不 UPDATE，"修改" = remove + add）；唯一性不是数据库约束（同名类型可合法重复出现，只在"某版本可见集"内校验）。

**派生属性**：表达式只允许纯算术 + `same_period_last_year(attr)`；禁止跨实例聚合、条件分支、引用其他派生属性。放宽任何一条都会让影响集失去边界或依赖图不再是一层。

## 已踩过的坑（勿重犯）

- **导出器吞掉矛盾 = 推理机失明**。同名属性的多个 datatype 必须全部导出，OWL 合取语义才能暴露冲突。每次扩展 OWL 导出都要配一条"矛盾必须被推理机报出"的测试。
- **一致性 ≠ 无缺陷**。`isConsistent()` 对"有不可满足类"的本体返回 true，两个信号必须分开报。
- **聚合必须基于全量**，不能在截断后的明细上算——小众类型会彻底消失。
- **Decimal 尾随零**：`Decimal('1.23') * 100000000` = `123000000.00`，值存 TEXT 时会被误判为"值变了"。用 `_tidy()`，别用 `normalize()`（会变科学计数法）。
- **`TIMESTAMPTZ` 返回 aware datetime**，与 naive 比较抛 TypeError。store 层用 `_as_aware()` 统一。
- `psycopg` 默认游标返回元组，要列名得传 `row_factory=dict_row`。

## 数据源

东财 `datacenter.eastmoney.com/securities/api/data/v1/get`、巨潮 `cninfo.com.cn`，均无需 key，直接 urllib 调（**不用 akshare**：依赖重且多一层版本漂移）。同一报表接口不同行业的非空字段集差异很大（四类共有仅 22/51）——**schema 漂移靠按行业分批灌入即可触发，不必人为注入**。
