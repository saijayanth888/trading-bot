#!/usr/bin/env bash
# fix_hermes_skill_layout.sh — install repo-tracked Hermes skills into the
# canonical loader layout under ~/.hermes/skills/<group>/<name>/SKILL.md.
#
# Why this exists
# ---------------
# Hermes' skill loader walks ~/.hermes/skills/<group>/<name>/SKILL.md trees
# (one SKILL.md per directory). The trading-bot group currently has three
# skills installed in that layout via symlinks (regime_shift_detector,
# flash_crash_defense, squeeze_survival), but the four newer skills that
# landed in the repo at .hermes/skills/ (slack_reporting, post_mortem,
# market_research, stocks_coordination) live as bare .md files. The loader
# silently ignores files that aren't at <group>/<name>/SKILL.md, so the
# 8 LLM-driven crons that depend on these skills run without their playbooks
# and produce no Slack alerts — operator has zero observability on
# overnight P&L. See REVIEW_2026-05-11 §P0-P.
#
# Expected directory layout after this script runs
# ------------------------------------------------
#   ~/.hermes/skills/trading-bot/
#     ├── _context.md                     (symlink to repo .hermes/context.md)
#     ├── regime_shift_detector/SKILL.md  (existing — left untouched)
#     ├── flash_crash_defense/SKILL.md    (existing — left untouched)
#     ├── squeeze_survival/SKILL.md       (existing — left untouched)
#     ├── slack_reporting/SKILL.md        (NEW — symlink to repo)
#     ├── post_mortem/SKILL.md            (NEW — symlink to repo)
#     ├── market_research/SKILL.md        (NEW — symlink to repo)
#     └── stocks_coordination/SKILL.md    (NEW — symlink to repo)
#
# We symlink rather than copy so the repo stays the source of truth — edits
# in .hermes/skills/<name>.md show up in Hermes the next time the loader
# refreshes its index, without re-running this script.
#
# Idempotency
# -----------
# This script is safe to re-run. It:
#   * never deletes user data
#   * skips skills that are already correctly installed (dir + SKILL.md)
#   * relocates a stray top-level .md (e.g. ~/.hermes/skills/trading-bot/foo.md)
#     into ~/.hermes/skills/trading-bot/foo/SKILL.md
#   * detects when a SKILL.md is already pointing at the same repo source
#     and is a no-op in that case
#
# Usage
# -----
#   bash scripts/fix_hermes_skill_layout.sh           # do the work
#   bash scripts/fix_hermes_skill_layout.sh --dry-run # print actions only
#
# Run on the host (not inside any container). Requires bash >= 4 and a
# writable ~/.hermes/skills/ tree.

set -euo pipefail

# ── Resolve repo root ─────────────────────────────────────────────────────────
# This script lives at <repo>/scripts/fix_hermes_skill_layout.sh. When invoked
# from a worktree (e.g. .claude/worktrees/agent-XXXX/scripts/fix_…), the
# worktree's path is ephemeral, so we resolve to the MAIN repo path so the
# symlinks survive worktree cleanup. git --git-common-dir points at the
# shared .git dir; its parent is the main repo root. Fall back to the
# script's parent dir if git isn't available.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v git >/dev/null 2>&1 && git -C "${SCRIPT_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_COMMON_DIR="$(git -C "${SCRIPT_DIR}" rev-parse --git-common-dir)"
  # --git-common-dir may be relative; resolve absolute then take parent.
  GIT_COMMON_DIR="$(cd "$(dirname "${GIT_COMMON_DIR}")/$(basename "${GIT_COMMON_DIR}")" && pwd)"
  REPO_ROOT="$(dirname "${GIT_COMMON_DIR}")"
else
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
REPO_SKILLS_DIR="${REPO_ROOT}/.hermes/skills"

# Where Hermes expects to find loaded skills.
HERMES_GROUP="trading-bot"
HERMES_SKILLS_DIR="${HOME}/.hermes/skills/${HERMES_GROUP}"

# Skills that ship as bare <name>.md files in the repo and need to be moved
# into <name>/SKILL.md form under ~/.hermes/skills/<group>/.
SKILLS=(
  "slack_reporting"
  "post_mortem"
  "market_research"
  "stocks_coordination"
)

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

log() { printf '[fix_hermes_skill_layout] %s\n' "$*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY: $*"
  else
    log "$*"
    eval "$@"
  fi
}

# ── Preflight ─────────────────────────────────────────────────────────────────
if [[ ! -d "${REPO_SKILLS_DIR}" ]]; then
  log "ERROR: repo skills dir not found: ${REPO_SKILLS_DIR}" >&2
  exit 2
fi

if [[ ! -d "${HOME}/.hermes/skills" ]]; then
  log "ERROR: ~/.hermes/skills does not exist — is Hermes installed?" >&2
  exit 2
fi

# Ensure the trading-bot group dir exists.
run "mkdir -p '${HERMES_SKILLS_DIR}'"

# ── Per-skill install ─────────────────────────────────────────────────────────
installed=0
skipped=0
for name in "${SKILLS[@]}"; do
  src="${REPO_SKILLS_DIR}/${name}.md"
  if [[ ! -f "${src}" ]]; then
    log "skip ${name}: source not found at ${src}"
    continue
  fi

  target_dir="${HERMES_SKILLS_DIR}/${name}"
  target_skill="${target_dir}/SKILL.md"
  stray_md="${HERMES_SKILLS_DIR}/${name}.md"

  # Case 1: properly installed already. If SKILL.md is a symlink pointing
  # at the same source, we're done. If it's a regular file, leave it
  # (operator may have hand-edited; surface the divergence below).
  if [[ -e "${target_skill}" ]]; then
    if [[ -L "${target_skill}" ]]; then
      current_target="$(readlink -f "${target_skill}")"
      desired_target="$(readlink -f "${src}")"
      if [[ "${current_target}" == "${desired_target}" ]]; then
        log "ok   ${name}: already linked → ${desired_target}"
        skipped=$((skipped + 1))
        continue
      fi
      log "warn ${name}: SKILL.md links elsewhere (${current_target}); replacing with ${desired_target}"
      run "rm '${target_skill}'"
    else
      log "warn ${name}: SKILL.md is a regular file (not a symlink); leaving alone"
      skipped=$((skipped + 1))
      continue
    fi
  fi

  # Case 2: stray top-level <name>.md at the group level — relocate it.
  if [[ -e "${stray_md}" && ! -L "${stray_md}" ]]; then
    log "info ${name}: stray ${stray_md} found, will move into ${target_dir}/SKILL.md"
    run "mkdir -p '${target_dir}'"
    run "mv '${stray_md}' '${target_skill}'"
    installed=$((installed + 1))
    continue
  elif [[ -L "${stray_md}" ]]; then
    # If it's a stray symlink, drop it — we own this layout.
    log "info ${name}: removing stray symlink ${stray_md}"
    run "rm '${stray_md}'"
  fi

  # Case 3: fresh install — create dir + symlink SKILL.md → repo source.
  run "mkdir -p '${target_dir}'"
  run "ln -s '${src}' '${target_skill}'"
  installed=$((installed + 1))
done

# ── Summary ──────────────────────────────────────────────────────────────────
log "done: ${installed} installed, ${skipped} already correct"

if [[ $DRY_RUN -eq 1 ]]; then
  log "(dry-run — no changes made)"
fi
