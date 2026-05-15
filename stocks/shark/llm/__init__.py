# shark.llm — Multi-provider LLM client abstraction

from shark.llm.client import chat_by_role, chat_json, resolve_role_route
from shark.llm.structured import StructuredOutputError, chat_structured

__all__ = [
    "chat_structured",
    "StructuredOutputError",
    "chat_json",
    "chat_by_role",
    "resolve_role_route",
]
