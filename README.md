# agent-ops-kit

Guardrails and operating conventions for running a fleet of AI coding agents (Claude Code and friends) with one
human in the loop. Every tool here started as a fix for a real failure: a leaked key, a pile of unreviewed PRs, a
poller that never stopped, a secret sitting in a memory database. Stdlib Python and plain Node, no dependencies.

Each tool reads a `settings_v1.json` next to it (or the file an env var names), so nothing is hardcoded.
Runtime state defaults to `~/.agent-ops/`.

| Tool | What it does | Check: |
|---|---|---|
| [`templates/CLAUDE.md.template`](templates/CLAUDE.md.template) | An "agent operating system": one global instruction file for response style, approvals, judgment rules, secrets and memory, with per-project knowledge homes. | Check: a new session in any folder follows the same rules and names the project home it read. |
| [`tools/codex-review`](tools/codex-review) | Cross-model review gate. A second model (Codex CLI) reviews each production unit once; findings go to a ledger and a SessionStart hook nags until each is fixed or dismissed with a reason. Secret-redacted before anything is written or posted. | Check: `python3 tools/codex-review/codex_review_v1.py review <path> --unit u --project p --dry-run` |
| [`tools/wip-gate`](tools/wip-gate) | Caps PRs awaiting human review per project. Agents run it before spawning PR work; over the limit = exit 3. GitHub read failures never block. | Check: `python3 tools/wip-gate/wip_gate_v1.py check <project>` |
| [`tools/model-routing`](tools/model-routing) | A delegation table (model + effort per task class, with an [evidence method](tools/model-routing/EVIDENCE_METHOD.md)) and a router that sends one-shot tasks to an API lane under a PII reroute and a daily spend ceiling. | Check: `python3 tools/model-routing/route_v1.py "summarize this" ` prints the lane decision without calling anything |
| [`tools/push-gate`](tools/push-gate) | Scans every pushed range (added lines, paths, messages, identities, ref) for protected terms and real key formats; prints labels only. CI runs the gate from main (never the pushed copy), blocks pushes that edit the gate or workflows, and deletes a blocked agent branch ([workflow](examples/push-gate-workflow.yml)). | Check: `node tools/push-gate/push_gate_v1.mjs <old> <new>` exits 0 clean, 3 block, 2 refused |
| [`hooks/gh_log_guard_v1.py`](hooks/gh_log_guard_v1.py) | PreToolUse(Bash): blocks GitHub CLI commands that can fetch Actions logs or tokens, including quoting, case, brace/glob and variable tricks. A tripwire on the command text, not a sandbox: pair it with a least-scope token. | Check: `echo '{"tool_input":{"command":"gh auth token"}}' \| python3 hooks/gh_log_guard_v1.py; echo $?` prints 2 |
| [`hooks/tab_guard_v1.py`](hooks/tab_guard_v1.py) | Stop hook: blocks the end of a turn while browser tabs the agent opened are still open. | Check: run the hook tests; an unclosed `tabs_create` returns `"decision": "block"` |
| [`hooks/mem_redact_v1.py`](hooks/mem_redact_v1.py) | Stop hook: scrubs known secret values out of a memory plugin's SQLite DB and logs. Never prints a value. | Check: `python3 hooks/mem_redact_v1.py --list` counts values per store |
| [`tools/key-entry`](tools/key-entry) | The paste box: a secret goes from a hidden macOS dialog through a pipe into the Keychain, never argv, env, a terminal or the chat. | Check: `python3 -I tools/key-entry/paste_to_keychain_v1.py my-service` opens a hidden dialog |
| [`conventions/return-rail.md`](conventions/return-rail.md) | How one agent session hands work to another and gets the result back without a watcher or poller. | Check: the requester's brief ends in `RETURN TO:` and the runner's last step is the send |
| [`tools/maid`](tools/maid) | Read-only cleanup scan: backup files, superseded `_vN` files, stale comments, skipped or assertion-free tests, merged branches, idle worktrees, orphan memory notes. Report-first, never deletes. | Check: `python3 -I tools/maid/maid_scan_v1.py <project> --stdout` |

## Security model

- The guards are tripwires for a cooperative-but-fallible agent, not a sandbox. An agent that can write and run
  arbitrary files can route around any text-matching hook. Keep the hooks, settings files and the env vars that
  point at settings outside what agent sessions may edit, and give agents least-scope tokens.
- The review gate trusts the agent to fix or dismiss a finding with a reason; every dismissal is in the ledger for
  a human to audit. If you need human-only dismissal of high/critical findings, gate `resolve` behind your own check.
- Reviewer output is model text: it is redacted and shown as data, never executed. A read-only reviewer can still
  read any file under the reviewed path, so keep secrets out of reviewed trees.
- `paste_to_keychain` replaces an existing item of the same service on purpose (key rotation). Check the service
  name in the dialog title before pasting.

## Install

```bash
git clone <this repo> ~/agent-ops-kit
```

Hooks go in `~/.claude/settings.json` (see [`examples/claude-settings-hooks.json`](examples/claude-settings-hooks.json)).
Edit each tool's `settings_v1.json` for your projects, repos and paths.

## Tests

```bash
./run_tests.sh
```

Python `unittest` for every Python tool, `node --test` for the push gate. Tests never touch the network, the
Keychain or your real config: each one points the tool at a temp settings file.

## Principles behind the kit

- **Fail closed on secrets, fail open on availability.** A redactor that cannot parse a store skips it; a gate that cannot reach GitHub reports UNKNOWN instead of blocking work. A gate that cannot load its terms refuses (exit 2) instead of passing.
- **Labels, never values.** Every scanner prints which rule fired and where in the pipeline, never the matched text.
- **One human approval per batch.** Tools produce one-screen lists, not per-item prompts.
- **Name the outside verifier.** Each tool's tests check behavior against something the tool does not control (a real git repo, a real SQLite DB, a stubbed reviewer).

## License

MIT, see [LICENSE](LICENSE).
