#!/usr/bin/env python3
"""Paste box: store a secret in the macOS Keychain without it ever touching argv, env, a file, a terminal or the chat.

  python3 -I paste_to_keychain_v1.py <keychain-service> [account] [--prompt "Paste the X API key"]

Path of the value: macOS hidden-answer dialog -> osascript stdout pipe -> this process's memory -> `security -i` stdin.
Why a dialog and not a terminal prompt: pasting into a tty fails in some embedded terminals (agent apps, IDE panes),
and a lost paste can land at the shell prompt and run. An agent may launch this script itself, because the human
only pastes into the box. Existing entries are replaced (-U). Nothing is printed except saved / skipped / failed.
"""
import argparse, re, subprocess, sys

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # no leading dash: it would read as a flag


def valid_name(s):
    return bool(NAME.fullmatch(s or ""))


def applescript_str(s):
    """quote text for an AppleScript string literal"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dialog_script(prompt, title):
    return ('set r to display dialog %s default answer "" with hidden answer with title %s '
            'buttons {"Skip", "Save"} default button "Save"\n'
            'if button returned of r is "Skip" then return ""\n'
            'return text returned of r') % (applescript_str(prompt), applescript_str(title))


def clean_value(raw):
    """strip whitespace a paste picks up; refuse characters that would break the `security -i` command line"""
    v = "".join((raw or "").split())
    if not v or any(c in v for c in "\"'\\") or any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    return v


def security_command(service, account, value):
    return 'add-generic-password -U -s %s -a %s -w "%s"\n' % (service, account, value)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("service"); ap.add_argument("account", nargs="?", default="agent-ops")
    ap.add_argument("--prompt", default=None); ap.add_argument("--title", default="Paste box")
    a = ap.parse_args(argv)
    if not (valid_name(a.service) and valid_name(a.account)):
        print("bad service/account name: letters, digits, . _ - only"); return 2
    if sys.platform != "darwin":
        print("macOS only (osascript + security)"); return 2
    prompt = a.prompt or "Paste the value for %s. The field stays hidden." % a.service
    p = subprocess.run(["/usr/bin/osascript", "-e", dialog_script(prompt, a.title)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        print("%s: cancelled, nothing changed" % a.service); return 1
    value = clean_value(p.stdout)
    if value is None:
        print("%s: skipped, empty or odd characters, nothing changed" % a.service); return 1
    # `security -i` reads commands from stdin, so the value never appears in any process argv.
    cmd = security_command(a.service, a.account, value)
    r = subprocess.run(["/usr/bin/security", "-i"], input=cmd, capture_output=True, text=True)
    del value, cmd
    print("%s: %s" % (a.service, "saved to Keychain" if r.returncode == 0 else "FAILED (nothing printed to protect the value)"))
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
