"""Temporal Fusion Transformer — PyTorch implementation.

Ported verbatim from ``user_data/freqaimodels/tft_architecture.py``
(2026-05-12 worktree state). The architecture has no FreqAI dependency
and is reused unchanged; see :mod:`quanta_core.models.tft` for the
training/inference wrapper that replaces ``TFTModel.py``.

Reference: Lim et al. 2019, "Temporal Fusion Transformers for
Interpretable Multi-horizon Time Series Forecasting".
"""

from __future__ import annotations

import math
from typing import cast

import torch
from torch import nn

__all__ = [
    "GatedLinearUnit",
    "GatedResidualNetwork",
    "InterpretableMultiHeadAttention",
    "TemporalFusionTransformer",
    "VariableSelectionNetwork",
    "pinball_loss",
]


class GatedLinearUnit(nn.Module):
    """GLU(x) = (W_1 x) * sigmoid(W_2 x) — learnable gate."""

    def __init__(self, in_features: int, out_features: int, dropout: float = 0.0) -> None:
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.linear = nn.Linear(in_features, out_features * 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.dropout(x)
        x = self.linear(x)
        a, b = x.chunk(2, dim=-1)
        return a * torch.sigmoid(b)


class GatedResidualNetwork(nn.Module):
    """GRN(x, c) = LayerNorm(skip(x) + GLU(W_2 ELU(W_1 x + W_c c)))."""

    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        output_size: int | None = None,
        dropout: float = 0.1,
        context_size: int | None = None,
    ) -> None:
        super().__init__()
        output_size = output_size or input_size
        self.skip: nn.Module = (
            nn.Linear(input_size, output_size) if input_size != output_size else nn.Identity()
        )
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.context = (
            nn.Linear(context_size, hidden_size, bias=False) if context_size is not None else None
        )
        self.elu = nn.ELU()
        self.fc2 = nn.Linear(hidden_size, output_size)
        self.glu = GatedLinearUnit(output_size, output_size, dropout=dropout)
        self.norm = nn.LayerNorm(output_size)

    def forward(
        self,
        x: torch.Tensor,
        context: torch.Tensor | None = None,
    ) -> torch.Tensor:
        residual = self.skip(x)
        h = self.fc1(x)
        if self.context is not None and context is not None:
            ctx = self.context(context)
            while ctx.dim() < h.dim():
                ctx = ctx.unsqueeze(-2)
            h = h + ctx
        h = self.elu(h)
        h = self.fc2(h)
        h = self.glu(h)
        return cast(torch.Tensor, self.norm(h + residual))


class VariableSelectionNetwork(nn.Module):
    """Per-variable transformation + softmax-weighted aggregation."""

    def __init__(
        self,
        n_vars: int,
        var_dim: int,
        hidden_size: int,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.n_vars = n_vars
        self.var_dim = var_dim
        self.var_grns = nn.ModuleList(
            [
                GatedResidualNetwork(var_dim, hidden_size, hidden_size, dropout)
                for _ in range(n_vars)
            ]
        )
        self.selection_grn = GatedResidualNetwork(
            n_vars * var_dim,
            hidden_size,
            n_vars,
            dropout=dropout,
        )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass.

        Parameters
        ----------
        x:
            Tensor of shape ``(..., n_vars, var_dim)``.

        Returns
        -------
        tuple[torch.Tensor, torch.Tensor]
            ``(out, weights)`` where ``out`` has shape ``(..., hidden_size)``
            and ``weights`` has shape ``(..., n_vars)`` (softmax row-wise).
        """
        flat = x.flatten(start_dim=-2)
        weights = torch.softmax(self.selection_grn(flat), dim=-1)
        var_outs = torch.stack(
            [grn(x[..., i, :]) for i, grn in enumerate(self.var_grns)],
            dim=-2,
        )
        out = (var_outs * weights.unsqueeze(-1)).sum(dim=-2)
        return out, weights


class InterpretableMultiHeadAttention(nn.Module):
    """TFT-style attention with shared value projection across heads."""

    def __init__(self, hidden_size: int, n_heads: int, dropout: float = 0.1) -> None:
        super().__init__()
        if hidden_size % n_heads != 0:
            raise ValueError(f"hidden_size {hidden_size} must be divisible by n_heads {n_heads}")
        self.n_heads = n_heads
        self.head_dim = hidden_size // n_heads
        self.q_proj = nn.Linear(hidden_size, hidden_size)
        self.k_proj = nn.Linear(hidden_size, hidden_size)
        self.v_proj = nn.Linear(hidden_size, self.head_dim, bias=False)
        self.out_proj = nn.Linear(self.head_dim, hidden_size)
        self.dropout = nn.Dropout(dropout)

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        b, t, _ = q.shape
        q_proj = self.q_proj(q).view(b, t, self.n_heads, self.head_dim).transpose(1, 2)
        k_proj = self.k_proj(k).view(b, t, self.n_heads, self.head_dim).transpose(1, 2)
        v_proj = self.v_proj(v).unsqueeze(1)

        scores = torch.matmul(q_proj, k_proj.transpose(-2, -1)) / math.sqrt(self.head_dim)
        if mask is not None:
            scores = scores.masked_fill(~mask, float("-inf"))
        attn = torch.softmax(scores, dim=-1)
        attn = self.dropout(attn)

        out = torch.matmul(attn, v_proj.expand(-1, self.n_heads, -1, -1))
        out = out.mean(dim=1)
        return self.out_proj(out), attn


class TemporalFusionTransformer(nn.Module):
    """TFT classifier with auxiliary quantile head.

    ``forward(x) -> logits`` is the inference path; ``forward_with_quantiles``
    is what the trainer calls.
    """

    def __init__(
        self,
        n_features: int,
        n_classes: int,
        hidden_size: int = 64,
        n_heads: int = 4,
        n_quantiles: int = 3,
        dropout: float = 0.1,
        var_dim: int = 8,
        sequence_length: int | None = None,
    ) -> None:
        super().__init__()
        self.n_features = n_features
        self.n_classes = n_classes
        self.hidden_size = hidden_size
        self.n_quantiles = n_quantiles
        self.var_dim = var_dim
        self.sequence_length = sequence_length

        self.feature_embed = nn.Linear(1, var_dim)
        self.feature_id_embed = nn.Embedding(n_features, var_dim)
        self.register_buffer(
            "_feature_ids",
            torch.arange(n_features).long(),
            persistent=False,
        )

        self.vsn = VariableSelectionNetwork(n_features, var_dim, hidden_size, dropout)

        self.lstm = nn.LSTM(
            input_size=hidden_size,
            hidden_size=hidden_size,
            num_layers=1,
            batch_first=True,
        )
        self.lstm_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.lstm_norm = nn.LayerNorm(hidden_size)

        self.static_grn = GatedResidualNetwork(
            hidden_size,
            hidden_size,
            hidden_size,
            dropout,
            context_size=hidden_size,
        )

        self.attention = InterpretableMultiHeadAttention(
            hidden_size,
            n_heads,
            dropout,
        )
        self.attn_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.attn_norm = nn.LayerNorm(hidden_size)

        self.position_grn = GatedResidualNetwork(
            hidden_size,
            hidden_size,
            hidden_size,
            dropout,
        )
        self.final_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.final_norm = nn.LayerNorm(hidden_size)

        self.classification_head = nn.Linear(hidden_size, n_classes)
        self.quantile_head = nn.Linear(hidden_size, n_quantiles)

    def _embed(self, x: torch.Tensor) -> torch.Tensor:
        val = self.feature_embed(x.unsqueeze(-1))
        ids = self.feature_id_embed(self._feature_ids)
        return cast(torch.Tensor, val + ids)

    def _encode(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        b, t, _ = x.shape
        embedded = self._embed(x)
        vsn_in = embedded.reshape(b * t, self.n_features, self.var_dim)
        vsn_out, vsn_w = self.vsn(vsn_in)
        vsn_out = vsn_out.reshape(b, t, self.hidden_size)

        lstm_out, _ = self.lstm(vsn_out)
        gated = self.lstm_glu(lstm_out)
        encoded = self.lstm_norm(gated + vsn_out)
        return encoded, vsn_w.reshape(b, t, self.n_features)

    def _decode(self, encoded: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        _, t, _ = encoded.shape
        static_ctx = encoded.mean(dim=1)
        enriched = self.static_grn(encoded, static_ctx)

        mask = torch.tril(
            torch.ones(t, t, device=encoded.device, dtype=torch.bool),
        )
        attn_out, attn_weights = self.attention(enriched, enriched, enriched, mask)
        gated = self.attn_glu(attn_out)
        post_attn = self.attn_norm(gated + enriched)

        positioned = self.position_grn(post_attn)
        gated2 = self.final_glu(positioned)
        out = self.final_norm(gated2 + post_attn)

        last = out[:, -1, :]
        attn_summary = attn_weights[:, :, -1, :].mean(dim=1)
        return last, attn_summary

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Logits only — fast inference path."""
        encoded, _ = self._encode(x)
        last, _ = self._decode(encoded)
        return cast(torch.Tensor, self.classification_head(last))

    def forward_with_quantiles(
        self,
        x: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Returns ``(logits, quantiles, attention_summary)`` for training."""
        encoded, _ = self._encode(x)
        last, attn_summary = self._decode(encoded)
        logits = self.classification_head(last)
        quantiles = self.quantile_head(last)
        return logits, quantiles, attn_summary


def pinball_loss(
    predictions: torch.Tensor,
    targets: torch.Tensor,
    quantile_levels: torch.Tensor,
) -> torch.Tensor:
    """Pinball / quantile loss.

    Parameters
    ----------
    predictions:
        Shape ``(batch, n_quantiles)``.
    targets:
        Shape ``(batch,)`` — scalar per sample.
    quantile_levels:
        Shape ``(n_quantiles,)``, e.g. ``tensor([0.1, 0.5, 0.9])``.
    """
    if targets.dim() == predictions.dim() - 1:
        targets = targets.unsqueeze(-1)
    targets = targets.expand_as(predictions)
    errors = targets - predictions
    losses = torch.maximum(quantile_levels * errors, (quantile_levels - 1.0) * errors)
    return losses.mean()
