"""reasoner-service 的 Python 客户端。

服务是无状态的：本体文本随请求传入，推理后即丢弃。本体的唯一来源是 SchemaRegistry。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_URL = "http://localhost:7070"


class ReasonerUnavailable(RuntimeError):
    """服务不可达。调用方需决定是降级还是中止。"""


@dataclass(frozen=True)
class ConsistencyResult:
    consistent: bool
    unsatisfiable_classes: list[str] = field(default_factory=list)
    axiom_count: int = 0
    class_count: int = 0
    reasoner_time_ms: int = 0

    @property
    def ok(self) -> bool:
        """既一致、又没有不可满足类。

        两者是不同的信号：本体可以是一致的，却含有永远不可能有实例的类
        （如 Weird ⊑ Company ⊓ Filing 而二者互斥）。约束收紧导致某类不可满足
        是真缺陷，但 isConsistent() 仍为 true——只查一致性会漏掉这种情况。
        """
        return self.consistent and not self.unsatisfiable_classes


class ReasonerClient:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or os.environ.get("DOA_REASONER_URL", DEFAULT_URL)).rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ 底层

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", body)
            except json.JSONDecodeError:
                detail = body
            raise ValueError(f"推理服务拒绝请求 ({e.code}): {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ReasonerUnavailable(
                f"推理服务不可达 {self.base_url}: {e}。"
                " 先启动：java -jar reasoner-service/target/reasoner-service.jar"
            ) from e

    # ------------------------------------------------------------------ 端点

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ReasonerUnavailable(f"推理服务不可达 {self.base_url}: {e}") from e

    def available(self) -> bool:
        try:
            self.health()
            return True
        except ReasonerUnavailable:
            return False

    def consistency(self, ontology: str) -> ConsistencyResult:
        """一致性检查 + 不可满足类。ontology 为 OWL 文本，格式由 OWLAPI 自动识别。"""
        raw = self._post("/consistency", {"ontology": ontology})
        return ConsistencyResult(
            consistent=raw["consistent"],
            unsatisfiable_classes=raw.get("unsatisfiableClasses", []),
            axiom_count=raw.get("axiomCount", 0),
            class_count=raw.get("classCount", 0),
            reasoner_time_ms=raw.get("reasonerTimeMs", 0),
        )
