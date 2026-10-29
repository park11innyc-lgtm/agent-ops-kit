#!/usr/bin/env python3
"""Task router v1: send a task to the cheapest lane that may see it, with a PII reroute and a daily spend ceiling.
route_v1.py "<task>" [--kind K] [--files F ...] [--run] [--out FILE]
Prints the decision as one JSON line. Lanes, keywords, prices and ceilings live in settings_v1.json (or ROUTE_SETTINGS).
Lane "plan" means: do it in the current agent session, no API call. Keys come from the macOS Keychain, never
argv/env, never printed; store them with tools/key-entry/paste_to_keychain_v1.py. Stdlib only."""
import argparse, fcntl, json, os, re, subprocess, sys, urllib.request, urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.environ.get("ROUTE_SETTINGS") or os.path.join(HERE, "settings_v1.json")


def load_settings(path=None):
    with open(path or SETTINGS) as fh:
        return json.load(fh)


def keychain_get(service, account=None):
    p = subprocess.run(["security", "find-generic-password", "-w", "-s", service] + (["-a", account] if account else []),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def http_post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(65536))
        except Exception:
            return e.code, {}


def pii_hits(text, cfg):
    sc = cfg["lanes"]["pii_scanner"]
    hits = [n for n, rx in sc["regexes"].items() if re.search(rx, text)]
    low = text.lower()
    hits += ["term:" + t for t in sc.get("terms", []) if t and t.lower() in low]
    return hits


def infer_kind(task, cfg):
    low = task.lower()
    # Kinds that stay off the third-party lane are matched first, so a generic word ("draft", "summarize") never outranks a local-only or secrets word.
    order = sorted(cfg["lanes"]["kind_keywords"].items(), key=lambda kv: cfg["lanes"]["kind_to_lane"].get(kv[0]) == "openrouter")
    for kind, words in order:
        if any(w in low for w in words):
            return kind
    return cfg["lanes"]["default_kind"]


def decide(task, text, kind, cfg):
    L = cfg["lanes"]
    kind = kind or infer_kind(task, cfg)
    lane = L["kind_to_lane"].get(kind, L["kind_to_lane"].get(L["default_kind"], "plan"))
    d = {"kind": kind, "lane": lane, "model": None, "rerouted": False, "pii": []}
    if lane != "plan":
        # attached files go out with the prompt, so the local-only rules see the whole payload, not just the task line
        low = text.lower()
        hit = next((k for k, words in L["kind_keywords"].items() if L["kind_to_lane"].get(k) == "plan" and any(w in low for w in words)), None)
        if hit is None and any(re.search(p, text) for p in L.get("local_only_patterns", [])):
            hit = "secret-shaped content"
        if hit:
            d.update(lane="plan", rerouted=True, reason="payload matches local-only rule (%s): run it here" % hit)
            lane = "plan"
    if lane == "openrouter":
        hits = pii_hits(text, cfg)
        if hits:
            d.update(pii=hits, rerouted=True, lane=L["pii_reroute_lane"],
                     reason="PII scanner hit (%s): not sent to openrouter, rerouted to %s" % (",".join(hits), L["pii_reroute_lane"]))
            lane = d["lane"]
    if lane != "plan":
        lc = L["lanes"][lane]
        d["model"] = lc["strong_model"] if kind in L["strong_kinds"] else lc["default_model"]
    else:
        d["reason"] = d.get("reason") or "run it here"
    return d


def today_spend(ledger, lane, now=None):
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    total = 0.0
    try:
        with open(ledger) as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("lane") == lane and str(r.get("at", "")).startswith(day):
                    total += float(r.get("usd", 0))
    except OSError:
        pass
    return total


def cost(lc, model, tin, tout):
    pi, po = lc.get("prices_per_mtok", {}).get(model, lc["default_price_per_mtok"])
    return round((tin * pi + tout * po) / 1e6, 6)


def call_lane(lane, model, prompt, cfg, key, post=http_post):
    L = cfg["lanes"]; lc = L["lanes"][lane]; mx = L["max_output_tokens"]
    if lane == "openrouter":
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        st, r = post(lc["endpoint"], h, {"model": model, "max_tokens": mx, "messages": [{"role": "user", "content": prompt}]}, L["timeout_s"])
        u = r.get("usage", {}) if isinstance(r, dict) else {}
        txt = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "") if st == 200 else ""
        return st, txt, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    h = {"x-api-key": key, "anthropic-version": lc["anthropic_version"], "content-type": "application/json"}
    st, r = post(lc["endpoint"], h, {"model": model, "max_tokens": mx, "messages": [{"role": "user", "content": prompt}]}, L["timeout_s"])
    u = r.get("usage", {}) if isinstance(r, dict) else {}
    txt = "".join(b.get("text", "") for b in r.get("content", [])) if st == 200 else ""
    return st, txt, u.get("input_tokens", 0), u.get("output_tokens", 0)


_LOCKS = []


def main(*args, **kw):
    try:
        return _main(*args, **kw)
    finally:
        while _LOCKS:
            _LOCKS.pop().close()  # closing the file releases the flock


def _main(argv=None, cfg=None, keyfn=keychain_get, post=http_post, now=None, out=sys.stdout):
    ap = argparse.ArgumentParser()
    ap.add_argument("task"); ap.add_argument("--kind"); ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--run", action="store_true"); ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = cfg or load_settings()
    text = a.task
    for f in a.files:
        with open(os.path.expanduser(f), errors="replace") as fh:
            text += "\n\n--- FILE %s ---\n%s" % (f, fh.read())
    d = decide(a.task, text, a.kind, cfg)
    if a.run and d["lane"] != "plan":
        L = cfg["lanes"]; lc = L["lanes"][d["lane"]]
        ledger = os.path.expanduser(L["ledger_path"])
        # One call at a time per ledger: the check, the paid call and the ledger line run under one exclusive lock, and the call is refused when its
        # worst-case cost (whole prompt in, max_tokens out) would pass the ceiling, so concurrent runs cannot overshoot it.
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        lockf = open(ledger + ".lock", "a"); _LOCKS.append(lockf); fcntl.flock(lockf, fcntl.LOCK_EX)
        spent = today_spend(ledger, d["lane"], now)
        worst = cost(lc, d["model"], len(text.encode("utf-8")) + 64, L["max_output_tokens"])  # UTF-8 bytes bound tokens from above for byte-level BPE tokenizers; +64 for message framing
        if spent + worst > lc["daily_usd"]:
            d.update(refused=True, reason="daily ceiling reached: $%.2f of $%s on %s" % (spent, lc["daily_usd"], d["lane"]))
            print(json.dumps(d), file=out); return 4
        key = keyfn(lc["keychain_service"], lc.get("keychain_account"))
        if not key:
            d.update(refused=True, reason="no key in Keychain for service %s (store it with tools/key-entry/paste_to_keychain_v1.py)" % lc["keychain_service"])
            print(json.dumps(d), file=out); return 3
        try:
            st, txt, tin, tout = call_lane(d["lane"], d["model"], text, cfg, key, post)
            usd = cost(lc, d["model"], tin, tout) if st == 200 else 0
        except Exception as e:
            # timeout, reset or undecodable body: the provider may still bill it, so book the worst case against the ceiling
            st, txt, tin, tout, usd = "error:" + type(e).__name__, "", None, None, worst
        rec = {"at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"), "lane": d["lane"], "model": d["model"],
               "in": tin, "out": tout, "usd": usd, "status": st}
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        with open(ledger, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        d.update(status=st, tokens_in=tin, tokens_out=tout, usd=usd)
        if st != 200:
            print(json.dumps(d), file=out); return 1
        if a.out:
            with open(os.path.expanduser(a.out), "w") as fh:
                fh.write(txt)
            d["out"] = a.out
        else:
            d["output"] = txt
    print(json.dumps(d), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
