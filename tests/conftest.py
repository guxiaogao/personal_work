from __future__ import annotations

import psycopg
import pytest

from doa.db import connect, dsn, init_schema, reset_schema
from doa.sr import SchemaRegistry


def _server_reachable() -> bool:
    try:
        with psycopg.connect(dsn(test=True), connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_db() -> None:
    """测试库不可达就整体跳过，而不是每条用例各报一次连接错误。"""
    if not _server_reachable():
        pytest.skip(
            f"测试库不可达: {dsn(test=True)}。先 docker compose up -d，"
            "再 createdb doa_test（见 README）",
            allow_module_level=True,
        )


@pytest.fixture()
def conn():
    c = connect(test=True)
    init_schema(c)
    reset_schema(c)  # 每条用例独立空库
    yield c
    c.close()


@pytest.fixture()
def sr(conn) -> SchemaRegistry:
    r = SchemaRegistry(conn)
    r.bootstrap()
    return r
