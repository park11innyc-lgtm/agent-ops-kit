#!/bin/sh
# Runs every test in the kit. Stops with a non-zero exit at the first failing suite.
cd "$(dirname -- "${0}")" || exit 2
for d in tools/codex-review tools/wip-gate tools/model-routing tools/key-entry tools/maid hooks; do
  out=$(cd "$d" && python3 -m unittest 2>&1) || { echo "== $d FAILED"; echo "$out"; exit 1; }
  echo "== $d: $(echo "$out" | grep -E '^Ran ')"
done
out=$(node --test tools/push-gate/test/ 2>&1) || { echo "== tools/push-gate FAILED"; echo "$out"; exit 1; }
echo "== tools/push-gate: $(echo "$out" | grep -E '^ℹ tests') tests"
echo "ALL PASS"
