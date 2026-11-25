#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/container_agent.sh"
AGENT=""; AGENT_MODEL=""; AGENT_REASONING_EFFORT=""; OUTPUT_DIR=""; PROMPT_FILE=""; EXPECTED_OUTPUT=""
CONTAINERIZE=0; CONTAINER_IMAGE=""; CONTAINER_NETWORK=none; CONTAINER_AUTH_ROOT=""; CONTAINER_ENV_FILE=""; CONTAINER_SESSION_NAME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2;;
    --agent-model) AGENT_MODEL="$2"; shift 2;;
    --reasoning-effort) AGENT_REASONING_EFFORT="$2"; shift 2;;
    --output-dir) OUTPUT_DIR="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --expected-output) EXPECTED_OUTPUT="$2"; shift 2;;
    --containerize) CONTAINERIZE=1; shift;;
    --container-image) CONTAINER_IMAGE="$2"; shift 2;;
    --container-network) CONTAINER_NETWORK="$2"; shift 2;;
    --container-auth-root) CONTAINER_AUTH_ROOT="$2"; shift 2;;
    --container-env-file) CONTAINER_ENV_FILE="$2"; shift 2;;
    --container-session-name) CONTAINER_SESSION_NAME="$2"; shift 2;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done
[ -n "$AGENT" ] && [ -n "$OUTPUT_DIR" ] && [ -f "$PROMPT_FILE" ] || exit 2
case "$EXPECTED_OUTPUT" in candidate.json|review.json|verdict.json|feedback.json|plan.json|codebase.json|overview.json) ;; *) exit 2;; esac
# Each role mounts a different workspace; never reuse a campaign container.
unset AGENT_CONTAINER_SESSION_NAME
CONTAINER_SESSION_NAME=""
validate_agent_reasoning_effort "$AGENT" "$AGENT_REASONING_EFFORT"
export AGENT_REASONING_EFFORT
PYTHON_BIN="${PYTHON_BIN:-python3}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
PROMPT="$(cat "$PROMPT_FILE")"
write_agent_prompt_file "$OUTPUT_DIR/prompt.md" "$PROMPT"
LOG_FILE="$OUTPUT_DIR/agent.log"
AGENT_PID=""
on_interrupt() {
  trap - INT TERM
  if [ -n "$AGENT_PID" ]; then terminate_pid_tree "$AGENT_PID" 0.2 || true; fi
  if [ "$CONTAINERIZE" -eq 1 ] && [ -n "$CONTAINER_SESSION_NAME" ]; then stop_container_session_force "$CONTAINER_SESSION_NAME" || true; fi
  record_headless_cost_log "$PYTHON_BIN" "$LOG_FILE" ideation_role "$OUTPUT_DIR" interrupted
  exit 130
}
trap on_interrupt INT TERM
(
  if [ -n "${AGENTIC_TASK_FAILURE_FD:-}" ]; then
    exec {AGENTIC_TASK_FAILURE_FD}>&-
    unset AGENTIC_TASK_FAILURE_FD
  fi
  AGENT_PROMPT_FILE=prompt.md run_agent_prompt "$AGENT" "$PROMPT" "$AGENT_MODEL" "" "$OUTPUT_DIR" "$CONTAINERIZE" "$CONTAINER_IMAGE" "$CONTAINER_NETWORK" "$CONTAINER_AUTH_ROOT" "$CONTAINER_ENV_FILE" "$OUTPUT_DIR" "$CONTAINER_SESSION_NAME"
) > "$LOG_FILE" 2>&1 &
AGENT_PID=$!
status=0
wait "$AGENT_PID" || status=$?
AGENT_PID=""
record_headless_cost_log "$PYTHON_BIN" "$LOG_FILE" ideation_role "$OUTPUT_DIR" "$(headless_cost_status "$status" 0)"
[ "$status" -eq 0 ] || exit "$status"
"$PYTHON_BIN" - "$OUTPUT_DIR/$EXPECTED_OUTPUT" <<'PY'
import json, sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if isinstance(value, dict) else 1)
PY
