# Return rail (cross-session results without pollers)

When one agent session asks another to run something (a test suite, a migration dry-run, a deploy check),
the result has to come back without anyone watching a log.

**Requester:** end the brief with one line:

```
RETURN TO: <the requester's session name as the agent list prints it>
```

and one `Fails if ...` line saying what counts as wrong.

**Runner:** as its last step, send the result to that name with the session-messaging tool:
pass/fail counts, failing test names and the commit hash, in 300 words or fewer, with no secrets or PII.

**If the send fails:** write the same text to `reports/returns/<requester>_<date>.md` on the requester's branch
(or in the project home) and say so in the final message.

**Neither side runs a watcher or a poller.** The requester's next turn starts from the message (or the file).

Why: pollers burn tokens, outlive their purpose and need a hard stop; a one-shot message is cheap, auditable and
fails visibly. The 300-word cap keeps the requester's context small, and the no-secrets rule keeps a relayed
message safe to land anywhere.
