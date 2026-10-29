# Paste box (hidden-dialog key entry)

The rule: **a human types a secret into exactly one place, a hidden-answer dialog.** Never the chat, never a
terminal prompt (`read -s`, getpass), never argv or an env var. The dialog's answer reaches the script through
osascript's stdout pipe, and the script hands it to the target (Keychain via `security -i` stdin, or an API
over HTTPS) without printing it. Because the human only pastes, the agent can launch the script itself.

```bash
python3 -I tools/key-entry/paste_to_keychain_v1.py agent-ops-openrouter
```

Then tools read it back with `security find-generic-password -w -s <service>` (see `tools/model-routing/route_v1.py`).
The same shape works for pushing a value to a hosting provider's env API: dialog -> pipe -> HTTPS POST, then list the
variable *names* (never values) as the read-back.
