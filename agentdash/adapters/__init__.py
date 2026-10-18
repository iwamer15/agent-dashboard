"""アダプタの登録と生成。新しいエージェントはここに1行足す。"""
from __future__ import annotations

from .base import AgentAdapter, Event  # noqa: F401
from .local_llm import LocalLLMAdapter
from .mock import MockAdapter

REGISTRY = {
    LocalLLMAdapter.name: LocalLLMAdapter,
    MockAdapter.name: MockAdapter,
}


def create(name: str, settings: dict | None = None):
    """config の [adapters.<name>] の値をコンストラクタ引数として渡す。"""
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"不明なアダプタ: {name} (利用可能: {', '.join(REGISTRY)})")
    return cls(**(settings or {}))
