# Single-turn role calls: take the JSON from the agent's reply, not from a written file

Work in the worktree `/weka/s224075134/agentic/scientist-agent/AI-Scientist-v3-singleturn` on branch `feat/single-turn-roles`. That branch already contains gap-distance, the antigravity backend and the E2BIG fix.

## Why

Every ideation, proposal and research-discovery role call goes through `src/ideation/role_runner.py` (`run_role`), which calls `src/scripts/ideation_role.sh`. That script tells the agent to write `candidate.json`, `review.json` and so on, then validates the file. So each call is an agent loop:
1. read the prompt;
2. call write_file with the JSON;
3. send a final message.

Each turn resends the whole context. In a measured Codex research-gap run (`/home/s224075134/scratch/agentic/scientist-agent/project/diffusion_policy/research_gap_codex/steps/*/agent.log`, `usage` lines), input tokens were 1.2–5.3× the prompt size: extraction 2.3–3.8×, targeted reads 5.3×. That is about 1.3M of the run's 2.2M input tokens. Three crashes also came from agents printing the JSON instead of writing the file.

## Goal

A role call is one model turn. The agent's final reply is the JSON object, and the runner parses it. The model sees the same instructions and data; only the return channel changes.

## Requirements

1. **Find how each backend exposes the final reply and structured output.**
   - Check this in the pinned headless CLI: `src/scripts/lib/container_agent.sh` `HEADLESS_NPX_SPEC`, installed at `~/.npm/_npx/072efe7f50c6c5f6/node_modules/@roberttlange/headless/dist/`.
   - Headless already prints `--- final message ---` followed by the reply into the log.
   - Check whether it can pass native flags or a JSON schema. Candidates: codex `exec --output-last-message`/`--output-schema`, `agy --print --output-format json --json-schema`, claude `--output-format json`/`--json-schema`. Prefer headless's own options. Pass native flags only through a mechanism headless supports.
   - Record what each backend supports in the spec's Findings section (append it).
2. **Reply mode in the role runner.**
   - `run_role` and `ideation_role.sh` take the role output from the final reply. A JSON object may be bare or in one fenced json block. Anything else is an error, not a guess.
   - Write the parsed object to the expected output file (`candidate.json`, etc.), so caching (`completed.json`, `invalid_output.json`), resume and every caller stay unchanged.
   - If the agent wrote the expected file anyway, accept it. Keep file mode as a fallback for one release. Make reply mode the default.
3. **Prompts.**
   - Replace "write the JSON object to <file> in your workspace" and "your only tool use is writing the output file" in role prompts with: reply with only the JSON object, and use no tools.
   - This covers `src/prompts/ideation/`, `src/prompts/proposal/`, `src/research_discovery/prompts/`, and the sentence `ideation_role.sh` or `run_role` appends.
   - For backends where headless gives the prompt as a pointer to `prompt.md` (antigravity, for prompts that cannot go in argv), the one allowed tool use is reading `prompt.md`. Say so in the pointer text (`prompt_file_reference`).
4. **Schemas.** Where a backend supports enforced structured output, pass a minimal schema that requires a JSON object (with the role's top-level keys, if cheap to derive). Never weaken the existing Python validators. They stay the source of truth.
5. **Keep fixed:**
   - role names, output file names and step cache layout;
   - `PYTHONHASHSEED=0` determinism. Prompt texts change, so existing run caches are expected to miss; that is fine;
   - execution-mode ideation behaviour, apart from the return channel.

## Tests

Use fakes, no network and no live agents. Run:

`env -u HEADLESS_BILLING /weka/s224075134/agentic/scientist-agent/AI-Scientist-v3/.venv/bin/python -m pytest -q tests/ideation tests/research_discovery tests/runtime`

Add tests for:
- reply parsing: bare JSON, fenced JSON, prose plus JSON, which is rejected, and a non-object, which is rejected;
- reply mode writing the expected file;
- the file-mode fallback;
- the backend flag mapping, if any.

Keep `ruff check src tests` and `bash -n` on the changed scripts clean.

## Delivery

- **Commits.** Make focused commits on `feat/single-turn-roles`. Do not push. Do not touch the other worktrees (`AI-Scientist-v3`, `AI-Scientist-v3-gapdist`, `AI-Scientist-v3-agyrun`).
- **Report.** List the files changed, the tests added, the per-backend findings, and anything not done.
- **Live smoke test.** Allowed: at most one antigravity call (`gemini-4-hydrogen-medium`) and one codex call. Use a tiny role prompt, to confirm a reply-mode call returns parsed JSON in one turn. Report the turn and tool counts you can observe.

## Findings

Inspected the pinned headless CLI at `HEADLESS_NPX_SPEC` commit `93cd9b06b85f848af1308c41e018991b33907c5e` and its installed `dist/agents.js` command builders. Headless prints `--- final message ---` followed by the extracted reply. Its CLI has no passthrough for native backend arguments and its command builders do not add structured-output schema flags, so this integration cannot enforce a schema without changing headless.

- **Codex:** headless builds `codex exec ... --json --skip-git-repo-check` and reads the prompt from stdin. It does not pass `--output-last-message` or `--output-schema`; headless extracts the final message from the Codex transcript.
- **Claude:** headless builds `claude -p ... --output-format stream-json --verbose` (and uses stdin for prompt files). It does not pass `--json-schema`.
- **Antigravity:** headless builds `agy -p <prompt>` with optional model, timeout, and approval flags. It does not pass `--print`, `--output-format json`, or `--json-schema`.

No backend therefore receives a native enforced JSON schema through this pinned headless interface. Existing Python validators remain authoritative.
