"""Smoke tests for the sentiment + microstructure stubs.

These modules ship as placeholders; the tests exist to (a) lock in their
return shapes so downstream consumers can be wired in parallel, and
(b) keep the package-level coverage gate honest.
"""

from __future__ import annotations

from quanta_core.models.microstructure import (
    MicrostructureModel,
    MicrostructurePrediction,
)
from quanta_core.models.sentiment import SentimentModel, SentimentPrediction


def test_sentiment_returns_neutral_prediction() -> None:
    model = SentimentModel()
    result = model.predict("AAPL beats earnings\nMSFT misses guidance")
    assert isinstance(result, SentimentPrediction)
    assert result.score == 0.0
    assert result.confidence == 0.0
    assert result.headline_count == 2


def test_sentiment_blank_input_counts_one_headline() -> None:
    model = SentimentModel(model_tag="hermes3:8b-sentiment-current")
    result = model.predict("")
    assert result.headline_count == 1
    assert model.model_tag == "hermes3:8b-sentiment-current"


def test_microstructure_returns_neutral_imbalance() -> None:
    model = MicrostructureModel()
    result = model.predict({"bids": [], "asks": []})
    assert isinstance(result, MicrostructurePrediction)
    assert result.imbalance == 0.0
    assert result.confidence == 0.0


def test_models_package_exports() -> None:
    """The public API stays explicit — guard against accidental removals."""
    from quanta_core import models

    expected = {
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
    }
    assert expected.issubset(set(models.__all__))
