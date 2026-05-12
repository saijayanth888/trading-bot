"""
Slack alerting via incoming webhook.

The webhook URL is read from the env var named in `SlackConfig.webhook_url_env`
(default `SLACK_WEBHOOK_URL`). All notifications are non-blocking: a failed
POST is logged at WARNING and never raises into the trading loop.

Public surface:

    slack = SlackAlerter.from_env()
    slack.notify_trade_entry(...)
    slack.notify_trade_exit(...)
    slack.notify_daily_summary(...)
    slack.notify_weekly_evolution(...)
    slack.notify_risk_warning("drawdown", value=0.06, threshold=0.05)
    slack.notify_risk_critical("drawdown", value=0.082, threshold=0.08)
    slack.notify_error("execution_engine", exc, context={"order_id": "..."})

Format: Slack Block Kit. Blocks render cleanly in both desktop + mobile.
Rate limiting: simple per-key dedup with a 60s cool-off so a tight loop
of identical errors doesn't paginate the channel.

Env vars:
    SLACK_WEBHOOK_URL                 — incoming webhook URL (required for live)
    SLACK_ALERTS_DRY_RUN=1            — log instead of POST (tests / staging)
    SLACK_DAILY_SUMMARY_HOUR_UTC=0    — when the daily summary should fire
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# Lazy import to keep the module loadable without `requests`
try:
    import requests
    _REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    _REQUESTS_AVAILABLE = False


EMOJI = {
    "entry":         ":green_circle:",
    "exit_win":      ":large_green_square:",
    "exit_loss":     ":red_circle:",
    "exit_breakeven": ":white_circle:",
    "warning":       ":warning:",
    "critical":      ":rotating_light:",
    "evolution":     ":robot_face:",
    "daily":         ":bar_chart:",
    "error":         ":x:",
    "info":          ":information_source:",
}


@dataclass
class SlackConfig:
    webhook_url_env: str = "SLACK_WEBHOOK_URL"
    timeout_sec: float = 5.0
    dry_run: bool = False
    daily_summary_hour_utc: int = 0
    dedup_window_sec: float = 60.0
    bot_name: str = "trading-bot"
    icon_emoji: str = ":chart_with_upwards_trend:"

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            webhook_url_env=os.environ.get("SLACK_WEBHOOK_URL_ENV_NAME", "SLACK_WEBHOOK_URL"),
            dry_run=os.environ.get("SLACK_ALERTS_DRY_RUN", "0") == "1",
            daily_summary_hour_utc=int(os.environ.get("SLACK_DAILY_SUMMARY_HOUR_UTC", "0")),
        )


class SlackAlerter:
    """Thread-safe Slack webhook poster with simple dedup."""

    def __init__(self, config: SlackConfig | None = None, *, http=None) -> None:
        self.cfg = config or SlackConfig()
        self._http = http or requests
        self._url = os.environ.get(self.cfg.webhook_url_env, "")
        self._dedup: dict[str, float] = {}
        self._lock = threading.Lock()

        if not self._url and not self.cfg.dry_run:
            logger.warning(
                "[slack] %s not set in env — alerts will silently no-op. "
                "Set SLACK_ALERTS_DRY_RUN=1 to confirm intent.",
                self.cfg.webhook_url_env,
            )

    @classmethod
    def from_env(cls) -> "SlackAlerter":
        return cls(SlackConfig.from_env())

    @property
    def enabled(self) -> bool:
        return bool(self._url) or self.cfg.dry_run

    # ------------------------------------------------------------------
    # Public alerts
    # ------------------------------------------------------------------

    def notify_trade_entry(
        self,
        pair: str,
        signal: str,                   # "long" / "short"
        entry_price: float,
        stake: float,
        confidence: float,
        tft_probs: Mapping[str, float] | None = None,
        drl_votes: Mapping[str, str | int] | None = None,
        regime: str | None = None,
        entry_tag: str | None = None,
    ) -> bool:
        fields: list[tuple[str, str]] = [
            ("Pair", f"`{pair}`"),
            ("Signal", signal.upper()),
            ("Entry", _fmt_price(entry_price)),
            ("Stake", _fmt_money(stake)),
            ("Confidence", f"{confidence:.1%}"),
        ]
        if regime:
            fields.append(("Regime", regime))
        if entry_tag:
            fields.append(("Tag", f"`{entry_tag}`"))
        if tft_probs:
            fields.append(("TFT", _fmt_tft_probs(tft_probs)))
        if drl_votes:
            fields.append(("DRL", _fmt_drl_votes(drl_votes)))

        blocks = _blocks(
            header=f"{EMOJI['entry']}  Trade entry — {pair}",
            fields=fields,
            delta=f"position opened at {_fmt_price(entry_price)} · "
                  f"stake {_fmt_money(stake)} · "
                  f"{signal.lower()} on {confidence:.0%} model confidence",
            action="monitor — bot will manage exits per strategy rules",
        )
        return self._post(
            text=f"Trade entry on {pair} @ {_fmt_price(entry_price)}",
            blocks=blocks,
            dedup_key=f"entry:{pair}:{int(time.time() // 5)}",
        )

    def notify_trade_exit(
        self,
        pair: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        duration_minutes: float,
        confidence: float | None = None,
    ) -> bool:
        if pnl > 0:
            emoji_key, headline = "exit_win", "Trade closed (win)"
        elif pnl < 0:
            emoji_key, headline = "exit_loss", "Trade closed (loss)"
        else:
            emoji_key, headline = "exit_breakeven", "Trade closed (flat)"

        fields = [
            ("Pair", f"`{pair}`"),
            ("Entry", _fmt_price(entry_price)),
            ("Exit", _fmt_price(exit_price)),
            ("P&L", _fmt_pnl(pnl)),
            ("P&L %", _fmt_pct(pnl_pct, signed=True)),
            ("Reason", f"`{exit_reason}`"),
            ("Duration", _fmt_duration(duration_minutes)),
        ]
        if confidence is not None:
            fields.append(("Conf @ entry", f"{confidence:.1%}"))

        ret_pct = (exit_price / entry_price - 1.0) if entry_price else 0.0
        delta = f"{_fmt_pct(ret_pct, signed=True)} on {pair} after {_fmt_duration(duration_minutes)}"
        action = (
            "no action — exit per rule"
            if exit_reason in ("trailing_stop_loss", "roi", "stop_loss") else
            f"review — exit reason `{exit_reason}` is unusual"
        )
        blocks = _blocks(
            header=f"{EMOJI[emoji_key]}  {headline} — {pair}",
            fields=fields,
            delta=delta,
            action=action,
        )
        return self._post(
            text=f"Trade closed {pair} P&L={_fmt_pnl(pnl)}",
            blocks=blocks,
            dedup_key=f"exit:{pair}:{exit_price}:{int(time.time() // 5)}",
        )

    def notify_daily_summary(
        self,
        date_utc: str,
        starting_equity: float,
        ending_equity: float,
        total_pnl: float,
        num_trades: int,
        wins: int,
        losses: int,
        sharpe_30d: float | None = None,
        max_drawdown: float | None = None,
    ) -> bool:
        win_rate = wins / max(num_trades, 1)
        fields = [
            ("Date (UTC)", f"`{date_utc}`"),
            ("Start equity", _fmt_money(starting_equity)),
            ("End equity", _fmt_money(ending_equity)),
            ("P&L", _fmt_pnl(total_pnl)),
            ("P&L %", _fmt_pct(total_pnl / max(starting_equity, 1), signed=True)),
            ("Trades", f"{num_trades} ({wins}W / {losses}L)"),
            ("Win rate", f"{win_rate:.1%}"),
        ]
        if sharpe_30d is not None:
            fields.append(("Sharpe (30d)", f"{sharpe_30d:.2f}"))
        if max_drawdown is not None:
            fields.append(("Max drawdown", _fmt_pct(max_drawdown)))

        delta = (
            f"{_fmt_pct(total_pnl / max(starting_equity, 1), signed=True)} day · "
            f"{num_trades} trades · {win_rate:.0%} win rate"
        )
        action = (
            "review — drawdown approaching limit"
            if max_drawdown is not None and max_drawdown < -0.05
            else "no action — within targets"
        )
        blocks = _blocks(
            header=f"{EMOJI['daily']}  Daily summary — {date_utc}",
            fields=fields,
            delta=delta,
            action=action,
        )
        return self._post(
            text=f"Daily summary {date_utc}: {_fmt_pnl(total_pnl)} ({num_trades} trades)",
            blocks=blocks,
            dedup_key=f"daily:{date_utc}",
        )

    def notify_weekly_evolution(
        self,
        generation: int,
        champion_id: str,
        champion_fitness: float,
        agent_fitness: Sequence[Mapping[str, Any]],
        runner_up_id: str | None = None,
        lineage: Sequence[str] | None = None,
    ) -> bool:
        # Sort agents by fitness desc for the report
        agents = sorted(
            agent_fitness, key=lambda a: float(a.get("fitness", 0.0)), reverse=True,
        )
        leaderboard_lines = [
            f"{i + 1}. `{a.get('member_id', '?')}` — fitness "
            f"*{float(a.get('fitness', 0.0)):.3f}*"
            + (
                f"  (sharpe={float(a['metrics'].get('sharpe_ratio', 0)):.2f}, "
                f"dd={float(a['metrics'].get('max_drawdown', 0)):.1%})"
                if a.get("metrics") else ""
            )
            for i, a in enumerate(agents)
        ]
        leaderboard = "\n".join(leaderboard_lines) or "_no agents_"

        fields = [
            ("Generation", str(generation)),
            ("Champion", f"`{champion_id}` — fitness *{champion_fitness:.3f}*"),
        ]
        if runner_up_id:
            fields.append(("Runner-up", f"`{runner_up_id}`"))
        if lineage:
            fields.append(("Lineage", " → ".join(f"`{m}`" for m in lineage[-5:])))

        delta = f"generation {generation} · champion fitness {champion_fitness:.3f}"
        blocks = _blocks(
            header=f"{EMOJI['evolution']}  Weekly evolution — gen {generation}",
            fields=fields,
            sections=[("Leaderboard", leaderboard)],
            delta=delta,
            action="no action — bot will use champion genome next cycle",
        )
        return self._post(
            text=f"Evolution gen {generation} — champion {champion_id} ({champion_fitness:.3f})",
            blocks=blocks,
            dedup_key=f"evolution:{generation}",
        )

    def notify_risk_warning(self, metric: str, value: float, threshold: float) -> bool:
        return self._risk_alert("warning", metric, value, threshold)

    def notify_risk_critical(self, metric: str, value: float, threshold: float) -> bool:
        return self._risk_alert("critical", metric, value, threshold)

    def notify_error(
        self, component: str, exc: BaseException | str,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        if isinstance(exc, BaseException):
            tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            details = "".join(traceback.format_tb(exc.__traceback__))[-1500:]
        else:
            tb = str(exc)
            details = ""
        fields = [("Component", f"`{component}`"), ("Error", f"`{tb}`")]
        if context:
            for k, v in context.items():
                fields.append((str(k), f"`{v}`"))
        sections = [("Traceback", f"```\n{details}\n```")] if details else []
        blocks = _blocks(
            header=f"{EMOJI['error']}  System error — {component}",
            fields=fields,
            sections=sections,
            delta=tb[:200],
            action=f"investigate `{component}` — check logs and traceback",
        )
        return self._post(
            text=f"ERROR in {component}: {tb}",
            blocks=blocks,
            dedup_key=f"error:{component}:{tb[:64]}",
        )

    def notify_info(
        self, component: str, message: str,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        fields = [("Component", f"`{component}`"), ("Status", f"`{message}`")]
        if context:
            for k, v in context.items():
                fields.append((str(k), f"`{v}`"))
        blocks = _blocks(
            header=f"{EMOJI['info']}  {component.replace('_', ' ').title()}",
            fields=fields,
        )
        return self._post(
            text=f"{component}: {message}",
            blocks=blocks,
            dedup_key=f"info:{component}:{message[:64]}",
        )

    def notify_training_stub(
        self, pair: str, size_bytes: int, files: int, tensor_blobs: int,
        path: str | None = None, detail: str | None = None,
    ) -> bool:
        """Stub-artifact alert for the TFT training pipeline.

        Fires from the TFT save() path when the validation gate rejects a
        freshly-written model.zip. Uses the :rotating_light: emoji to make
        the channel notification visually distinct from regular errors, and
        dedups on the pair name so a stuck pair won't repeatedly page.

        Dedup window is the SlackConfig.dedup_window_sec default (60s) when
        called from the in-process alerter. The 30-min/pair window required
        by spec is enforced by the caller via a state file in
        ~/.hermes/state-snapshots/ so the dedup survives even if the
        freqtrade process restarts between failures.
        """
        fields = [
            ("Pair", f"`{pair}`"),
            ("Size", f"`{size_bytes:,} B`"),
            ("Files in zip", f"`{files}`"),
            ("Tensor blobs", f"`{tensor_blobs}`"),
        ]
        if path:
            fields.append(("Path", f"`{path}`"))
        if detail:
            fields.append(("Detail", f"`{detail[:200]}`"))

        body = (
            f"STUB ARTIFACT for {pair} — size={size_bytes}B, files={files}, "
            f"tensor_blobs={tensor_blobs}. Pair quarantined from runtime. "
            f"Investigate /ops · TrainingHealth card."
        )
        blocks = _blocks(
            header=f"{EMOJI['critical']}  tft-training · stub artifact",
            fields=fields,
            delta=body[:200],
            action=(
                "open /ops · TrainingHealth card; if multiple pairs flagged "
                "in one cycle this is a serializer regression, restart "
                "freqtrade only after confirming the failure mode"
            ),
        )
        return self._post(
            text=body,
            blocks=blocks,
            dedup_key=f"tft_stub:{pair}",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _risk_alert(
        self, level: str, metric: str, value: float, threshold: float,
    ) -> bool:
        emoji_key = "warning" if level == "warning" else "critical"
        headline = f"Risk {level.upper()} — {metric}"
        fields = [
            ("Metric", f"`{metric}`"),
            ("Current", _fmt_pct(value) if abs(value) <= 1 else f"{value:.4f}"),
            ("Threshold", _fmt_pct(threshold) if abs(threshold) <= 1 else f"{threshold:.4f}"),
            ("Severity", level.upper()),
        ]
        delta = (
            f"{_fmt_pct(value, signed=True) if abs(value) <= 1 else f'{value:.4f}'}"
            f" vs limit "
            f"{_fmt_pct(threshold) if abs(threshold) <= 1 else f'{threshold:.4f}'}"
        )
        action = (
            "halt trading — kill switch armed"
            if level == "critical"
            else "review positions — approaching limit"
        )
        blocks = _blocks(
            header=f"{EMOJI[emoji_key]}  {headline}",
            fields=fields,
            delta=delta,
            action=action,
        )
        return self._post(
            text=f"RISK {level.upper()}: {metric}={value:.4f} (limit {threshold:.4f})",
            blocks=blocks,
            dedup_key=f"risk:{level}:{metric}",
        )

    def _post(self, text: str, blocks: list[dict], dedup_key: str | None = None) -> bool:
        if dedup_key and self._is_duplicate(dedup_key):
            return False
        if not self.enabled:
            return False
        payload = {
            "text": text,                   # fallback used by Slack mobile push
            "username": self.cfg.bot_name,
            "icon_emoji": self.cfg.icon_emoji,
            "blocks": blocks,
        }
        if self.cfg.dry_run or not self._url:
            logger.info("[slack:dry] %s", text)
            return True
        if not _REQUESTS_AVAILABLE:
            logger.warning("[slack] requests library missing — cannot POST")
            return False
        try:
            resp = self._http.post(
                self._url, json=payload, timeout=self.cfg.timeout_sec,
            )
            if resp.status_code >= 400:
                logger.warning("[slack] POST failed status=%s body=%s",
                               resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as exc:
            logger.warning("[slack] POST error: %s", exc)
            return False

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._dedup.get(key, 0.0)
            if now - last < self.cfg.dedup_window_sec:
                return True
            self._dedup[key] = now
            # Trim ageing entries
            cutoff = now - self.cfg.dedup_window_sec * 4
            for k in list(self._dedup.keys()):
                if self._dedup[k] < cutoff:
                    del self._dedup[k]
        return False


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------


def _strip_md(s: str) -> str:
    """Strip only the chars that would break a triple-backtick fence.

    Inside a code block, Slack does NOT parse mrkdwn — underscores,
    asterisks, etc. render literally. So we only need to neutralise
    backticks, which would otherwise close the fence prematurely.
    """
    return str(s).replace("`", "")


def _format_table(pairs: Iterable[tuple[str, str]]) -> str:
    """Render key/value pairs as a fixed-width monospaced table.

    Slack renders triple-backtick fenced blocks in monospace — perfect for
    tabular alerts that scan top-down at-a-glance, instead of the 2-column
    stacked grid that the old fields-array layout produced.
    """
    rows = [(_strip_md(k), _strip_md(v)) for k, v in pairs]
    if not rows:
        return ""
    key_w = max(len(k) for k, _ in rows)
    val_w = max(min(len(v), 60) for _, v in rows)   # cap value width for sanity
    lines = []
    for k, v in rows:
        # truncate insanely long values
        v_short = v if len(v) <= 60 else v[:57] + "..."
        lines.append(f"{k:<{key_w}}  {v_short:<{val_w}}")
    return "```\n" + "\n".join(lines) + "\n```"


def _blocks(
    header: str,
    fields: Iterable[tuple[str, str]] = (),
    sections: Iterable[tuple[str, str]] = (),
    *,
    delta: str | None = None,
    action: str | None = None,
) -> list[dict]:
    """Production Block-Kit shape.

    Every notification answers four questions in 2 seconds:
      header        WHAT happened
      fields        the numbers (rendered as a monospaced top-down table)
      delta         WHAT CHANGED since last time (vs yesterday / last hour)
      action        WHAT TO DO right now ("monitor", "no action", "investigate")
      context       when it fired (UTC clock)

    Layout decisions:
      * fields → monospaced fenced code block (table), NOT the
        2-column stacked grid Slack's `fields` array produces. The grid
        was visually noisy for >4 pairs and forced scanning in a Z pattern.
      * tables are scoped to the section block they live in so divider +
        delta + action read naturally afterwards.
    """
    out: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": header[:150]}}]
    pairs = list(fields)
    if pairs:
        tbl = _format_table(pairs)
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": tbl[:2900]}})
    if delta:
        out.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Δ*  {delta[:2900]}"},
        })
    if action:
        out.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Action*  {action[:2900]}"},
        })
    for title, body in sections:
        out.append({"type": "divider"})
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{body}"[:2900]}})
    out.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f":clock1: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        }],
    })
    return out


def _fmt_money(x: float) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def _fmt_pnl(x: float) -> str:
    if x is None:
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}${x:,.2f}"


def _fmt_pct(x: float, signed: bool = False) -> str:
    if x is None:
        return "—"
    if signed and x > 0:
        return f"+{x:.2%}"
    return f"{x:.2%}"


def _fmt_price(x: float) -> str:
    if x is None:
        return "—"
    if x >= 100:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.4f}"
    return f"${x:.6f}"


def _fmt_duration(minutes: float) -> str:
    if minutes < 1:
        return f"{int(minutes * 60)}s"
    if minutes < 60:
        return f"{minutes:.1f}m"
    hrs, m = divmod(minutes, 60)
    if hrs < 24:
        return f"{int(hrs)}h{int(m):02d}m"
    days, hrs = divmod(hrs, 24)
    return f"{int(days)}d{int(hrs)}h"


def _fmt_tft_probs(probs: Mapping[str, float]) -> str:
    parts = []
    for k in ("up", "flat", "down"):
        if k in probs:
            parts.append(f"{k}={float(probs[k]):.2f}")
    return " ".join(parts) or "—"


def _fmt_drl_votes(votes: Mapping[str, Any]) -> str:
    return ", ".join(f"{a}=`{v}`" for a, v in votes.items())
