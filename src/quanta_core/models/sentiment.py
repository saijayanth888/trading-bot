"""Sentiment classifier — STUB.

Per ``docs/quanta-core-v4-rev2/01-RESEARCH-MULTI_MODEL_RESIDENCY.md``
§6.1 the v4 design routes sentiment through the always-warm ``hermes3:8b``
Ollama slot with a baked prompt, rather than maintaining a dedicated
DeBERTa-class model. This module defines the public interface only so
the rest of the v4 plumbing (registry, ledger writer, dashboard tile)
can be wired without blocking on the prompt-engineering work.

TODO(v4-build): replace this stub with the real implementation. The
real version will:
  - Accept a Markdown headline batch + optional symbol context.
  - Call ``OllamaClient.generate`` against ``hermes3:8b`` with a
    structured prompt (see ``docs/sentiment_prompts.md`` once ported).
  - Parse the JSON reply into ``{score: -1..+1, confidence: 0..1,
    headline_count: int}`` (see the feature spec in
    ``user_data/modules/sentiment_engine.py`` for the schema that the
    dashboard already consumes).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SentimentModel", "SentimentPrediction"]


@dataclass(frozen=True)
class SentimentPrediction:
    """Stub return type for :meth:`SentimentModel.predict`.

    Attributes
    ----------
    score:
        Aggregate directional sentiment in ``[-1, 1]``.
    confidence:
        Calibration in ``[0, 1]``. ``0`` indicates "the model declines
        to take a position on this batch."
    headline_count:
        Number of headlines actually consumed (may be less than the
        input if the model truncated).
    """

    score: float
    confidence: float
    headline_count: int


class SentimentModel:
    """Stub sentiment classifier — implementation deferred.

    The :meth:`predict` placeholder returns a deterministic neutral
    response. The real implementation will be filled in once the
    ``hermes3:8b`` prompt is locked.
    """

    def __init__(self, model_tag: str = "hermes3:8b") -> None:
        self.model_tag = model_tag

    def predict(self, text: str) -> SentimentPrediction:
        """Return a neutral sentiment prediction.

        Parameters
        ----------
        text:
            Headline or batched-headline string. Unused in the stub.

        Returns
        -------
        SentimentPrediction
            ``score=0.0``, ``confidence=0.0``, ``headline_count=N``
            where ``N`` is a simple newline-count proxy for headlines.
        """
        # Count newline-separated headlines as a placeholder for the
        # real tokeniser's batch size. The actual model will report the
        # number of headlines it managed to score (may be less than
        # the input if the prompt was truncated).
        headline_count = max(1, len([line for line in text.splitlines() if line.strip()]))
        return SentimentPrediction(
            score=0.0,
            confidence=0.0,
            headline_count=headline_count,
        )
