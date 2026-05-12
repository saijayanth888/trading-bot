"""shark.memory — markdown SSOT + atomic state helpers.

The decisions log API is re-exported here so downstream callers can do:

    from shark.memory import append_decision, update_with_outcome, get_past_context
"""

from shark.memory.decisions import (
    append_decision,
    get_past_context,
    update_with_outcome,
)

__all__ = [
    "append_decision",
    "update_with_outcome",
    "get_past_context",
]
