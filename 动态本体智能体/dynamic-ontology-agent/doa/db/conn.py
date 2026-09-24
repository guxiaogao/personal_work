"""数据库连接与模式初始化。"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")

DEFAULT_DSN = "postgresql://doa:doa@localhost:5433/doa"


def dsn(test: bool = False) -> str:
    """连接串。优先读环境变量，其次用默认值（宿主端口 5433）。"""
    if test:
        # 只替换末段库名。不能用 replace("/doa", ...)，那会先命中 "//doa:doa@" 里的用户名
        base, _, _dbname = DEFAULT_DSN.rpartition("/")
        return os.environ.get("DOA_DSN_TEST", f"{base}/doa_test")
    return os.environ.get("DOA_DSN", DEFAULT_DSN)


def connect(test: bool = False, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn(test), autocommit=autocommit)


def init_schema(conn: psycopg.Connection) -> None:
    """建表。schema.sql 全程 IF NOT EXISTS / OR REPLACE，可重复执行。"""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()


def reset_schema(conn: psycopg.Connection) -> None:
    """清库重建。测试夹具用，生产路径不调用。"""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    init_schema(conn)
