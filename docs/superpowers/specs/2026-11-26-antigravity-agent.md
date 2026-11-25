# Add Antigravity as an agent backend

The operator wants `antigravity` selectable everywhere an agent backend is chosen, with the same status as `gemini`. Examples: `--agent antigravity` on every `src/scripts/*.sh` entry point, `agent.name: antigravity` and `agent.pool` in configs, and `RoleAgent('antigravity')` for ideation, proposal and research-discovery roles.

## Facts from the pinned headless CLI

Agents run through headless-cli, pinned at commit `93cd9b06b85f848af1308c41e018991b33907c5e` in `src/scripts/lib/container_agent.sh`, `HEADLESS_NPX_SPEC`. That commit already supports `antigravity`; see `~/.npm/_npx/072efe7f50c6c5f6/node_modules/@roberttlange/headless/dist/agents.js`, `docker.js` and `cli.js`.

- **Binary.** `agy`, overridable by `ANTIGRAVITY_CLI_BIN` or `AGY_CLI_BIN`. Headless runs it with `-p <prompt>`, and with `--model` when a model is given.
- **Prompt delivery.** `promptFileMode: "argument"` (not stdin), the same as gemini. Check `agent_streams_prompt_file` and `prompt_file_reference` in `container_agent.sh`.
- **Config directory.** `.gemini/antigravity-cli`. The workspace config dir is `.agents`, and the seed paths are `.gemini/antigravity-cli` and `.gemini/config`.
- **Docker seed and credential files.** These live under `.gemini/antigravity-cli/`: `antigravity-oauth-token`, `settings.json`, `installation_id` and `jetski_state.pbtxt`.
- **Docker home variables.** `AGY_HOME` and `ANTIGRAVITY_HOME`.
- **Reasoning effort.** Not supported. Headless ignores it with a warning. Reject `--reasoning-effort` for antigravity in exactly the places that reject it for gemini.
- **Usage and cost.** Headless records antigravity usage (`antigravity-usage.js`). Make `src/investigation/cost_tracking.py` accept antigravity usage records wherever it handles gemini, if the shape is the same. If it isn't, record usage as unpriced rather than crashing.

## Where to change

Every place that lists or branches on agent names. Find them with:

```
grep -rn "opencode" src tests --include=*.py --include=*.sh
```

Skip `src/agents/skills/`. The list includes at least:
- `src/investigation/config.py` (`VALID_AGENTS`)
- `script_runners.py`, `cost_tracking.py`, `codex_subscription_auth.py`
- `src/pipeline/config.py`, `src/posthoc_pipeline/config.py`, `src/variants_pipeline/config.py`
- `src/orchestrator/paper_step.py`, `paper_evidence_audit.py`
- `src/agsci/doctor.py` (binary `agy`; auth check against the `.gemini/antigravity-cli/antigravity-oauth-token` file; no API-key env)
- every `src/scripts/*.sh` agent `case` statement, usage line and `AGENT_CONFIG_DIR` (`.gemini/antigravity-cli`)
- `src/scripts/lib/container_agent.sh`: allowed env keys, credential files to mount, effort validation, the agent list at around line 969, and the config-dir function at around line 803
- `src/scripts/lib/concept_figure.sh`, only if it lists agents for the same purpose
- docs and README agent lists

## Constraints

- **Leave gemini behaviour alone.** Antigravity is a separate agent, not an alias.
- **No live runs.** `agy` is not installed on this machine. Tests use fakes, the way the existing gemini tests do. Add tests for:
  - config validation that accepts `antigravity` and rejects reasoning effort for it;
  - the script argument parser that accepts `--agent antigravity`;
  - the container credential-file list;
  - the doctor mapping.
- **Verification.** Run `env -u HEADLESS_BILLING .venv/bin/python -m pytest -q tests`. The full suite must pass, or report pre-existing failures separately. Keep `ruff check src tests` and `bash -n` on the changed scripts clean.
- **Commits and push.** Commit on the current branch `feat/antigravity-agent` with focused commits. Do not push.
- **Report.** List the files changed, the tests added, and anything left unsupported, with the reason.
