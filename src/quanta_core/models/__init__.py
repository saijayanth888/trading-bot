"""Model layer — registry, Ollama client, TFT, sentiment + microstructure stubs.

See ``docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md`` for the
load-on-demand residency policy this module implements and
``docs/quanta-core-v4/10-CODE_PATTERNS.md`` §4 for the TFT port contract
(safetensors weights + JSON metadata — never Python's stdlib serialiser).
"""

from __future__ import annotations

from quanta_core.models.microstructure import MicrostructureModel
from quanta_core.models.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaResponse,
)
from quanta_core.models.registry import (
    ModelHandle,
    ModelRegistry,
    RegistryError,
)
from quanta_core.models.sentiment import SentimentModel
from quanta_core.models.tft import (
    TFTConfig,
    TFTModel,
    TFTValidationError,
    validate_artifact,
)

__all__ = [
    "MicrostructureModel",
    "ModelHandle",
    "ModelRegistry",
    "OllamaClient",
    "OllamaError",
    "OllamaResponse",
    "RegistryError",
    "SentimentModel",
    "TFTConfig",
    "TFTModel",
    "TFTValidationError",
    "validate_artifact",
]
