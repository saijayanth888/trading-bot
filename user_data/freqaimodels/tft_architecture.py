"""
Temporal Fusion Transformer — simplified PyTorch implementation.

Faithful to the key components of Lim et al. 2019
("Temporal Fusion Transformers for Interpretable Multi-horizon Time Series
Forecasting") with a few practical simplifications appropriate for a
single-encoder classifier with quantile auxiliary loss:

  - Variable Selection Network (VSN) with per-variable Gated Residual Networks
  - Static enrichment GRN around the LSTM encoder
  - Interpretable multi-head self-attention (shared value projection)
  - Position-wise GRN after attention
  - Two output heads:
        classification  (logits over n_classes)
        quantile         (n_quantiles values per sample, default 10/50/90)

Skipped vs the paper (intentionally — keeps the surface area small for a v1):
  - Separate encoder/decoder LSTM stacks (we use a single encoder)
  - Static covariate inputs (the strategy already encodes regime as one-hot
    timeseries features; treating them as static is a future enhancement)
  - Variable-specific embedding tables for categoricals

`forward(x)` returns logits only so the module is drop-in compatible with
FreqAI's `BasePyTorchClassifier` predict path.  `forward_with_quantiles(x)`
returns both heads and is what the custom trainer calls.
"""

from __future__ import annotations

import math

import torch
from torch import nn

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


class GatedLinearUnit(nn.Module):
    """GLU(x) = (W₁x) ⊙ σ(W₂x)  —  a learnable gate."""

    def __init__(self, in_features: int, out_features: int, dropout: float = 0.0):
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.linear = nn.Linear(in_features, out_features * 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.dropout(x)
        x = self.linear(x)
        a, b = x.chunk(2, dim=-1)
        return a * torch.sigmoid(b)


class GatedResidualNetwork(nn.Module):
    """
    GRN(x, c) = LayerNorm( skip(x) + GLU( W₂ ELU( W₁x + W_c c ) ) )

    The optional context vector `c` lets static information modulate the
    transformation (used by VSN selection weights and post-attention GRNs).
    """

    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        output_size: int | None = None,
        dropout: float = 0.1,
        context_size: int | None = None,
    ):
        super().__init__()
        output_size = output_size or input_size
        self.skip = (
            nn.Linear(input_size, output_size)
            if input_size != output_size else nn.Identity()
        )
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.context = (
            nn.Linear(context_size, hidden_size, bias=False)
            if context_size is not None else None
        )
        self.elu = nn.ELU()
        self.fc2 = nn.Linear(hidden_size, output_size)
        self.glu = GatedLinearUnit(output_size, output_size, dropout=dropout)
        self.norm = nn.LayerNorm(output_size)

    def forward(
        self, x: torch.Tensor, context: torch.Tensor | None = None,
    ) -> torch.Tensor:
        residual = self.skip(x)
        h = self.fc1(x)
        if self.context is not None and context is not None:
            ctx = self.context(context)
            # broadcast static context across the time axis if needed
            while ctx.dim() < h.dim():
                ctx = ctx.unsqueeze(-2)
            h = h + ctx
        h = self.elu(h)
        h = self.fc2(h)
        h = self.glu(h)
        return self.norm(h + residual)


class VariableSelectionNetwork(nn.Module):
    """
    Per-variable transformation + softmax-weighted aggregation.

    Inputs are embedded per variable into `var_dim`, each variable gets its
    own GRN producing a `hidden_size` vector, and a separate selection GRN
    produces a softmax over variables. Output is the weighted sum.
    """

    def __init__(
        self,
        n_vars: int,
        var_dim: int,
        hidden_size: int,
        dropout: float = 0.1,
    ):
        super().__init__()
        self.n_vars = n_vars
        self.var_dim = var_dim
        self.var_grns = nn.ModuleList([
            GatedResidualNetwork(var_dim, hidden_size, hidden_size, dropout)
            for _ in range(n_vars)
        ])
        self.selection_grn = GatedResidualNetwork(
            n_vars * var_dim, hidden_size, n_vars, dropout=dropout,
        )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """
        x: (..., n_vars, var_dim)
        Returns (out: (..., hidden_size), weights: (..., n_vars))
        """
        flat = x.flatten(start_dim=-2)                       # (..., n_vars * var_dim)
        weights = torch.softmax(self.selection_grn(flat), dim=-1)
        var_outs = torch.stack(
            [grn(x[..., i, :]) for i, grn in enumerate(self.var_grns)],
            dim=-2,
        )                                                    # (..., n_vars, hidden_size)
        out = (var_outs * weights.unsqueeze(-1)).sum(dim=-2)
        return out, weights


class InterpretableMultiHeadAttention(nn.Module):
    """
    TFT-style attention: heads share a single value projection so that head
    importances are directly comparable. Output is the head-averaged
    weighted sum.
    """

    def __init__(self, hidden_size: int, n_heads: int, dropout: float = 0.1):
        super().__init__()
        if hidden_size % n_heads != 0:
            raise ValueError(
                f"hidden_size {hidden_size} must be divisible by n_heads {n_heads}"
            )
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
        Q = self.q_proj(q).view(b, t, self.n_heads, self.head_dim).transpose(1, 2)
        K = self.k_proj(k).view(b, t, self.n_heads, self.head_dim).transpose(1, 2)
        V = self.v_proj(v).unsqueeze(1)                      # (b, 1, t, head_dim)

        scores = torch.matmul(Q, K.transpose(-2, -1)) / math.sqrt(self.head_dim)
        if mask is not None:
            scores = scores.masked_fill(~mask, float("-inf"))
        attn = torch.softmax(scores, dim=-1)
        attn = self.dropout(attn)

        out = torch.matmul(attn, V.expand(-1, self.n_heads, -1, -1))   # (b, h, t, d)
        out = out.mean(dim=1)                                          # (b, t, d)
        return self.out_proj(out), attn


# ---------------------------------------------------------------------------
# Top-level TFT
# ---------------------------------------------------------------------------


class TemporalFusionTransformer(nn.Module):
    """
    TFT classifier with a quantile auxiliary head.

    forward(x) -> logits  (compatible with FreqAI's BasePyTorchClassifier)
    forward_with_quantiles(x) -> (logits, quantiles, attention_weights)
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
    ):
        super().__init__()
        self.n_features = n_features
        self.n_classes = n_classes
        self.hidden_size = hidden_size
        self.n_quantiles = n_quantiles
        self.var_dim = var_dim
        self.sequence_length = sequence_length

        # Per-feature scalar → var_dim embedding (one shared linear, applied
        # per feature with a learnt feature-id embedding added).
        self.feature_embed = nn.Linear(1, var_dim)
        self.feature_id_embed = nn.Embedding(n_features, var_dim)
        self.register_buffer(
            "_feature_ids",
            torch.arange(n_features).long(),
            persistent=False,
        )

        self.vsn = VariableSelectionNetwork(n_features, var_dim, hidden_size, dropout)

        # Sequence encoder
        self.lstm = nn.LSTM(
            input_size=hidden_size,
            hidden_size=hidden_size,
            num_layers=1,
            batch_first=True,
        )
        # Skip-connection gate around the LSTM
        self.lstm_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.lstm_norm = nn.LayerNorm(hidden_size)

        # Static enrichment uses pooled context as static info
        self.static_grn = GatedResidualNetwork(
            hidden_size, hidden_size, hidden_size, dropout,
            context_size=hidden_size,
        )

        # Interpretable temporal attention
        self.attention = InterpretableMultiHeadAttention(
            hidden_size, n_heads, dropout,
        )
        self.attn_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.attn_norm = nn.LayerNorm(hidden_size)

        # Position-wise feed-forward
        self.position_grn = GatedResidualNetwork(
            hidden_size, hidden_size, hidden_size, dropout,
        )
        self.final_glu = GatedLinearUnit(hidden_size, hidden_size, dropout=dropout)
        self.final_norm = nn.LayerNorm(hidden_size)

        # Output heads
        self.classification_head = nn.Linear(hidden_size, n_classes)
        self.quantile_head = nn.Linear(hidden_size, n_quantiles)

    # ----- internals --------------------------------------------------

    def _embed(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (batch, time, n_features)
        Returns: (batch, time, n_features, var_dim) — value + feature-id embedding.
        """
        # value embedding via shared scalar→var_dim
        val = self.feature_embed(x.unsqueeze(-1))                  # (b, t, f, d)
        ids = self.feature_id_embed(self._feature_ids)             # (f, d)
        return val + ids                                           # broadcast over (b, t)

    def _encode(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """
        x: (batch, time, n_features) → (encoded: (batch, time, hidden), vsn_w)
        """
        b, t, _ = x.shape
        embedded = self._embed(x)                                  # (b, t, f, d)
        # VSN per timestep
        vsn_in = embedded.reshape(b * t, self.n_features, self.var_dim)
        vsn_out, vsn_w = self.vsn(vsn_in)                          # (b*t, h), (b*t, f)
        vsn_out = vsn_out.reshape(b, t, self.hidden_size)

        lstm_out, _ = self.lstm(vsn_out)                           # (b, t, h)
        gated = self.lstm_glu(lstm_out)
        encoded = self.lstm_norm(gated + vsn_out)
        return encoded, vsn_w.reshape(b, t, self.n_features)

    def _decode(self, encoded: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """encoded: (b, t, h) → (last_hidden: (b, h), attention_summary: (b, t))"""
        b, t, _ = encoded.shape
        # Pooled summary as static context (mean over time)
        static_ctx = encoded.mean(dim=1)                           # (b, h)
        enriched = self.static_grn(encoded, static_ctx)            # (b, t, h)

        # Causal mask: row i can attend to columns 0..i
        mask = torch.tril(
            torch.ones(t, t, device=encoded.device, dtype=torch.bool)
        )
        attn_out, attn_weights = self.attention(enriched, enriched, enriched, mask)
        gated = self.attn_glu(attn_out)
        post_attn = self.attn_norm(gated + enriched)

        positioned = self.position_grn(post_attn)
        gated2 = self.final_glu(positioned)
        out = self.final_norm(gated2 + post_attn)

        last = out[:, -1, :]                                       # (b, h)

        # Average attention across heads, take the last query position
        # (i.e. how much the final timestep attended to each historical step)
        attn_summary = attn_weights[:, :, -1, :].mean(dim=1)       # (b, t)
        return last, attn_summary

    # ----- public API -------------------------------------------------

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Logits only — drop-in with FreqAI's classifier predict path."""
        encoded, _ = self._encode(x)
        last, _ = self._decode(encoded)
        return self.classification_head(last)

    def forward_with_quantiles(
        self, x: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Returns (logits, quantiles, attention_summary) for training + analysis."""
        encoded, _ = self._encode(x)
        last, attn_summary = self._decode(encoded)
        logits = self.classification_head(last)
        quantiles = self.quantile_head(last)
        return logits, quantiles, attn_summary


# ---------------------------------------------------------------------------
# Loss helper — pinball / quantile loss
# ---------------------------------------------------------------------------


def pinball_loss(
    predictions: torch.Tensor,
    targets: torch.Tensor,
    quantile_levels: torch.Tensor,
) -> torch.Tensor:
    """
    predictions:    (batch, n_quantiles)
    targets:        (batch,)        — scalar per sample
    quantile_levels:(n_quantiles,)  — e.g. tensor([0.1, 0.5, 0.9])
    """
    if targets.dim() == predictions.dim() - 1:
        targets = targets.unsqueeze(-1)
    targets = targets.expand_as(predictions)
    errors = targets - predictions
    losses = torch.maximum(quantile_levels * errors, (quantile_levels - 1.0) * errors)
    return losses.mean()
