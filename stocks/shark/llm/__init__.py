# shark.llm — Multi-provider LLM client abstraction

from shark.llm.structured import chat_structured, StructuredOutputError
from shark.llm.client import chat_json, chat_by_role, resolve_role_route

__all__ = [
    "chat_structured",
    "StructuredOutputError",
    "chat_json",
    "chat_by_role",
    "resolve_role_route",
]
