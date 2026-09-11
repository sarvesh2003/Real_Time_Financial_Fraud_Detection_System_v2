#!/bin/bash
# One-shot eval harness for a single fault: inject -> run the automated
# Kaggle legs (retrieval-on/retrieval-off) in parallel (or sequentially
# re-injected, for transient faults) -> pause for the manual Claude Desktop
# leg -> restore.
#
# Usage:
#   export KAGGLE_OLLAMA_URL="https://<current-ngrok-id>.ngrok-free.app/v1"
#   ./eval/run_fault_eval.sh <fault_number>
#
# Requires KAGGLE_OLLAMA_URL set in the environment before running (Kaggle's
# URL changes every session, so this is never hardcoded). Everything lands
# in eval/runs/fault<N>/.
set -uo pipefail
# Deliberately NOT `set -e`: with it, a nonzero exit from ANY command -
# including a backgrounded leg via `wait`, or `inject`/`restore` themselves -
# aborts the whole script immediately, skipping the restore call below and
# leaving the fault live for every subsequent fault in the sweep. Every
# command that matters for the eval's correctness is checked explicitly
# instead (see FAULT_LIVE below).

FAULT_NUM="${1:?Usage: $0 <fault_number>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

FAULT_LIVE=0
trap 'if [ "$FAULT_LIVE" = "1" ]; then echo "!! Script exiting with fault $FAULT_NUM still LIVE - run: bash $SCRIPT_PATH restore" >&2; fi' EXIT

: "${KAGGLE_OLLAMA_URL:?Set KAGGLE_OLLAMA_URL first - the current Kaggle ngrok URL, ending in /v1}"

KAGGLE_MODEL="${KAGGLE_MODEL:-qwen2.5:32b-instruct}"

# fault_number -> "family/script.sh|family|fault_name|liveness"
declare -A FAULTS=(
  [1]="redis/01_node_stopped.sh|redis|node_stopped|continuous"
  [2]="redis/02_eviction_storm.sh|redis|eviction_storm|continuous"
  [3]="redis/03_connection_pool_exhaustion.sh|redis|connection_pool_exhaustion|continuous"
  [4]="redis/04_slow_commands.sh|redis|slow_commands|transient"
  [6]="kafka/06_rebalance_churn.sh|kafka|rebalance_churn|transient"
  [7]="kafka/07_partition_imbalance.sh|kafka|partition_imbalance|transient"
  [8]="kafka/08_malformed_event.sh|kafka|malformed_event|transient"
  [9]="network/09_latency_injection.sh|network|latency_injection|continuous"
  [10]="network/10_packet_loss.sh|network|packet_loss|continuous"
  [11]="network/11_container_paused.sh|network|container_paused|continuous"
  [12]="network/12_cpu_throttle.sh|network|cpu_throttle|continuous"
  [13]="config/13_client_timeout_too_low.sh|config|client_timeout_too_low|continuous"
  [14]="config/14_pool_size_too_small.sh|config|pool_size_too_small|continuous"
  [15]="config/15_bad_retry_backoff.sh|config|bad_retry_backoff|continuous"
  [16]="config/16_wrong_maxmemory_policy.sh|config|wrong_maxmemory_policy|continuous"
)

ENTRY="${FAULTS[$FAULT_NUM]:-}"
if [ -z "$ENTRY" ]; then
  echo "Unknown fault number: $FAULT_NUM" >&2
  echo "Valid fault numbers: ${!FAULTS[*]}" >&2
  exit 1
fi
IFS='|' read -r SCRIPT_REL FAMILY NAME LIVENESS <<< "$ENTRY"
SCRIPT_PATH="fault-injection/$SCRIPT_REL"

RESULTS_DIR="$REPO_ROOT/eval/runs/fault$(printf '%02d' "$FAULT_NUM")"
mkdir -p "$RESULTS_DIR"

echo "=================================================================="
echo "Fault $FAULT_NUM: $FAMILY/$NAME  (liveness: $LIVENESS)"
echo "Results: $RESULTS_DIR"
echo "=================================================================="

# --- B4 fix: rotate the changelog before injecting, so list_recent_changes'
# 24h window can never see residue from a previous fault in this sweep. ---
CHANGELOG="$REPO_ROOT/fault-injection/changelog.jsonl"
if [ -f "$CHANGELOG" ]; then
  cp "$CHANGELOG" "$RESULTS_DIR/changelog_prior.jsonl"
  : > "$CHANGELOG"
fi

inject() {
  echo "--- injecting ($SCRIPT_PATH inject) ---"
  if bash "$SCRIPT_PATH" inject > "$RESULTS_DIR/inject.log" 2>&1; then
    FAULT_LIVE=1
  else
    echo "!! inject FAILED (exit $?) - see $RESULTS_DIR/inject.log - fault may be PARTIALLY applied" >&2
    FAULT_LIVE=1  # assume live/unknown rather than silently treating a failed inject as safe
  fi
}
restore() {
  echo "--- restoring ($SCRIPT_PATH restore) ---"
  if bash "$SCRIPT_PATH" restore > "$RESULTS_DIR/restore.log" 2>&1; then
    FAULT_LIVE=0
  else
    echo "!! restore FAILED (exit $?) - see $RESULTS_DIR/restore.log - fault $FAULT_NUM is STILL LIVE" >&2
  fi
}

# RAG ablation: every automated leg runs twice - once with retrieval on
# (the default), once with it fully disabled (graph.py skips the corpus
# lookup entirely, not just hides it) - so it's measurable whether the
# corpus actually changes the investigation or the model would reach the
# same place unaided. Doesn't apply to the Claude Desktop leg - it never
# had access to our Chroma corpus in the first place.
RETRIEVAL_MODES=(on off)

ENV_LABEL="kaggle-qwen32b"

run_leg() {
  local RAG_MODE="$1"
  local TAG="fault${FAULT_NUM}_${ENV_LABEL}_rag${RAG_MODE}"
  echo "  -> launching $ENV_LABEL retrieval=$RAG_MODE ($KAGGLE_MODEL)"
  if ! RUN_TAG="$TAG" RESULTS_DIR="$RESULTS_DIR" ENV_LABEL="$ENV_LABEL" RETRIEVAL_MODE="$RAG_MODE" \
    FAULT_ID="$FAULT_NUM" FAULT_FAMILY="$FAMILY" FAULT_NAME="$NAME" \
    OLLAMA_BASE_URL="$KAGGLE_OLLAMA_URL" OLLAMA_MODEL="$KAGGLE_MODEL" \
    "$REPO_ROOT/agent/venv/Scripts/python.exe" "$REPO_ROOT/agent/run.py" \
    > "$RESULTS_DIR/${ENV_LABEL}_rag${RAG_MODE}_stdout.log" 2>&1
  then
    echo "!! $ENV_LABEL (rag=$RAG_MODE) leg exited nonzero (crash outside run.py's own handling - check ${ENV_LABEL}_rag${RAG_MODE}_stdout.log)" >&2
  fi
}

# Claude Desktop manual leg covers 2 model tiers per fault (Sonnet 5 Max,
# Haiku 4.5 Extended - Opus 5 dropped from the roster for faults 6+, per
# user decision). One template + transcript path per model, sharing the
# env_label prefix "claude-desktop-" so grade.py/aggregate.py treat them as
# a family (see startswith("claude-desktop") checks in both files) without
# hardcoding these specific names anywhere else.
CLAUDE_MODELS=(sonnet5 haiku4.5)
for CM in "${CLAUDE_MODELS[@]}"; do
  CLAUDE_RESULT_JSON="$RESULTS_DIR/fault${FAULT_NUM}_claude-desktop-${CM}.result.json"
  if [ ! -f "$CLAUDE_RESULT_JSON" ]; then
    sed -e "s/REPLACE_faultNN_claude-desktop/fault${FAULT_NUM}_claude-desktop-${CM}/" \
        -e "s/\"env_label\": \"claude-desktop\"/\"env_label\": \"claude-desktop-${CM}\"/" \
        -e "s/\"fault_id\": \"REPLACE\"/\"fault_id\": \"$FAULT_NUM\"/" \
        -e "s/\"fault_family\": \"REPLACE\"/\"fault_family\": \"$FAMILY\"/" \
        -e "s/\"fault_name\": \"REPLACE\"/\"fault_name\": \"$NAME\"/" \
        "$REPO_ROOT/eval/claude_desktop_template.json" > "$CLAUDE_RESULT_JSON"
  fi
done

echo ""
echo "=================================================================="
echo "MANUAL STEP - Claude Desktop (2 models: Sonnet 5 Max, Haiku 4.5 Extended)"
echo "Once the fault is injected below, open Claude Desktop (fraud_pipeline"
echo "MCP connector on) and send this same message to EACH of the 2 models:"
echo "  \"Something seems off with the fraud pipeline, can you investigate?\""
echo "When each finishes:"
echo "  1. Save its full chat transcript into:"
echo "     $RESULTS_DIR/claude_desktop_transcript_sonnet5.md"
echo "     $RESULTS_DIR/claude_desktop_transcript_haiku4.5.md"
echo "  2. Fill in the matching pre-made template (see _mapping_rules inside) at:"
echo "     $RESULTS_DIR/fault${FAULT_NUM}_claude-desktop-sonnet5.result.json"
echo "     $RESULTS_DIR/fault${FAULT_NUM}_claude-desktop-haiku4.5.result.json"
echo "Do this AFTER inject finishes and BEFORE you confirm restore below."
echo "=================================================================="
echo ""

if [ "$LIVENESS" = "continuous" ]; then
  inject
  echo "Fault is live. Launching both automated legs in parallel (retrieval on/off)..."
  PIDS=()
  for RAG_MODE in "${RETRIEVAL_MODES[@]}"; do
    run_leg "$RAG_MODE" &
    PIDS+=("$!")
  done
  # `set -e` is off (see above), so a nonzero exit from any leg does NOT
  # abort the script - it's just reported, and we still proceed to the
  # manual leg and restore either way.
  for PID in "${PIDS[@]}"; do
    wait "$PID" || echo "!! leg (PID $PID) exited nonzero" >&2
  done
  echo "Both automated legs finished."
else
  echo "This fault is TRANSIENT (self-resolves in seconds) - re-injecting fresh"
  echo "before each automated leg instead of holding one shared live state."
  for RAG_MODE in "${RETRIEVAL_MODES[@]}"; do
    inject
    echo "Running Kaggle leg (retrieval=$RAG_MODE)..."
    run_leg "$RAG_MODE"
  done
  inject
  echo "Fault re-injected once more, live now for your manual Claude Desktop turn."
fi

read -r -p "Press Enter once you've also saved both Claude Desktop transcripts/templates ... "

restore
echo ""
echo "Fault $FAULT_NUM done. Results in $RESULTS_DIR/"
echo "  - kaggle-qwen32b       (rag=on):  ${RESULTS_DIR}/fault${FAULT_NUM}_kaggle-qwen32b_ragon.result.json"
echo "  - kaggle-qwen32b       (rag=off): ${RESULTS_DIR}/fault${FAULT_NUM}_kaggle-qwen32b_ragoff.result.json"
echo "  - claude-desktop-sonnet5:         ${RESULTS_DIR}/fault${FAULT_NUM}_claude-desktop-sonnet5.result.json (fill in manually if not done)"
echo "  - claude-desktop-haiku4.5:        ${RESULTS_DIR}/fault${FAULT_NUM}_claude-desktop-haiku4.5.result.json (fill in manually if not done)"
