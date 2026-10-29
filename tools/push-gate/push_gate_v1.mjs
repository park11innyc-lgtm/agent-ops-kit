#!/usr/bin/env node
// push_gate_v1.mjs: the second net behind a local commit gate. Scans everything a push publishes and blocks on a
// protected term or a real key format. Built for agent branches: CI runs it on every push to an agent branch and
// deletes the branch on a block, so a leak never sits on the remote.
//
//   node push_gate_v1.mjs <old-sha> <new-sha> [--ref <refname>] [--base <rev>]
//
// <old-sha> may be all zeros (a new branch): the range is then <base>..<new>, base from --base or settings default_base.
// Surfaces scanned per commit: the added lines of every changed file (content), every changed path and its parent
// directories (path), the commit message (message), author and committer name + email (identity); plus the ref name (ref).
// Each surface is read as written, with invisible format characters removed (zero-width, soft hyphen, BOM, bidi marks),
// and with control bytes removed. A key cut across two added lines (a line ending in a key prefix) is joined and re-read.
// Terms: one per line in the terms file (PUSH_GATE_TERMS_FILE, else settings terms_file); # comments allowed. The file is
// a secret in its own right, so the gate prints only term:#<line>, never the term, the matched text or a path.
// Keys in CONTENT under settings key_exempt_dirs (the gate's own tests are full of fake keys) are reported, not blocked;
// a key in a path, a message, an identity or the ref always blocks.
// Exit codes: 0 clean; 3 BLOCK; 2 refused (no terms file, bad range, git failed). Anything else is a crash, never a block:
// CI must delete a branch only on 3.
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ZERO = /^0{40}$|^0{64}$/;
const FORMAT_CHARS = /[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]/g;
const CONTROL = /[\u0000-\u0009\u000B-\u001F\u007F�]/g;
const SHA = /^[0-9a-f]{7,64}$/;

export function loadSettings(path) {
  return JSON.parse(readFileSync(path || process.env.PUSH_GATE_SETTINGS || join(HERE, "settings_v1.json"), "utf8"));
}

export function loadTerms(S) {
  const f = process.env.PUSH_GATE_TERMS_FILE || S.terms_file;
  if (!f) return null;
  const p = f.replace(/^~(?=$|\/)/, process.env.HOME || "~");
  if (!existsSync(p)) return null;
  return readFileSync(p, "utf8").split(/\r?\n/).map((t, i) => [t.trim(), i + 1]).filter(([t]) => t && !t.startsWith("#"));
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function compile(S, terms) {
  const maxWb = S.term_word_boundary_max_len ?? 4;
  return {
    terms: terms.map(([t, n]) => [n, new RegExp(t.length <= maxWb ? `(?<![A-Za-z0-9])${escapeRe(t)}(?![A-Za-z0-9])` : escapeRe(t), "i")]),
    keys: Object.entries(S.key_patterns).map(([label, rx]) => [label, new RegExp(rx)]),
    keyTail: new RegExp(`${S.key_prefix_pattern}[A-Za-z0-9_-]*$`),
  };
}

// every reading of one text: as written, format characters removed, control bytes removed
function readings(text) {
  const a = text.replace(FORMAT_CHARS, "");
  return [...new Set([text, a, a.replace(CONTROL, "")])];
}

export function scanText(text, C, { keysBlock = true } = {}) {
  const hits = [];
  for (const r of readings(text)) {
    for (const [n, rx] of C.terms) if (rx.test(r)) hits.push({ label: `term:#${n}`, block: true });
    for (const [label, rx] of C.keys) if (rx.test(r)) hits.push({ label: `key:${label}`, block: keysBlock });
  }
  return hits;
}

// a key cut across lines: a line ending in a key prefix (plus any body) is joined to the next line's first word
export function joinedKeyHits(lines, C, opts) {
  const hits = [];
  const strip = (l) => l.replace(FORMAT_CHARS, "").replace(/[\s\\'"`+|,]*$/, "");
  for (let i = 0; i + 1 < lines.length; i++) {
    const a = strip(lines[i]);
    if (!C.keyTail.test(a)) continue;
    const b = lines[i + 1].replace(FORMAT_CHARS, "").replace(/^[\s#/*;>|'"`+-]*/, "");
    for (const [label, rx] of C.keys) if (rx.test(a + b) && !rx.test(a)) hits.push({ label: `key:${label}:joined`, block: opts.keysBlock });
  }
  return hits;
}

function git(args, cwd) {
  return execFileSync("git", args, { cwd, encoding: "utf8", maxBuffer: 256 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] });
}

function exempt(path, S) {
  return (S.key_exempt_dirs || []).some((d) => path.startsWith(d.endsWith("/") ? d : d + "/"));
}

// added lines per file of one commit: Map(path -> [lines]). Header state is tracked so pushed content can never pose
// as diff metadata (an added line "++ /dev/null" prints as "+++ /dev/null" inside a hunk). Merges diff against every parent (-m).
function addedLines(sha, cwd) {
  const out = git(["show", "--format=", "-m", "-U0", "--no-color", "--no-ext-diff", "--text", "--no-renames", sha], cwd);
  const files = new Map();
  let cur = null, inHeader = false;
  for (const line of out.split("\n")) {
    if (line.startsWith("diff --git ")) { inHeader = true; cur = null; continue; }
    if (inHeader) {
      if (line.startsWith("+++ ")) {
        cur = line === "+++ /dev/null" ? null : line.replace(/^\+\+\+ (b\/)?/, "");
        if (cur !== null && !files.has(cur)) files.set(cur, []);
      } else if (line.startsWith("@@")) inHeader = false;
      continue;
    }
    if (cur !== null && line.startsWith("+")) files.get(cur).push(line.slice(1));
  }
  return files;
}

export function gate(oldSha, newSha, { ref = "", base, cwd = process.cwd(), S = loadSettings(), log = console.log } = {}) {
  const terms = loadTerms(S);
  if (!terms) { log("REFUSED: no terms file (set PUSH_GATE_TERMS_FILE or settings terms_file)"); return 2; }
  if (!SHA.test(newSha) || !(ZERO.test(oldSha) || SHA.test(oldSha))) { log("REFUSED: old/new must be commit SHAs"); return 2; }
  if ((base || S.default_base || "").startsWith("-")) { log("REFUSED: base must be a revision, not an option"); return 2; }
  const C = compile(S, terms);
  let commits;
  try {
    const from = ZERO.test(oldSha) ? (base || S.default_base) : oldSha;
    const range = from ? [`${from}..${newSha}`] : [newSha];
    commits = git(["rev-list", "--reverse", ...range], cwd).split("\n").filter(Boolean);
  } catch {
    log("REFUSED: git could not read the range"); return 2;
  }
  if (commits.length > (S.max_commits ?? 5000)) { log(`REFUSED: ${commits.length} commits is over max_commits`); return 2; }
  const found = [];
  const add = (surface, commit, hs) => hs.forEach((h) => found.push({ ...h, surface, commit: commit.slice(0, 7) }));
  if (ref) add("ref", "-------", scanText(ref, C));
  for (const c of commits) {
    try {
      add("message", c, scanText(git(["log", "-1", "--format=%B", c], cwd), C));
      add("identity", c, scanText(git(["log", "-1", "--format=%an <%ae>%n%cn <%ce>", c], cwd), C));
      const files = addedLines(c, cwd);
      const paths = new Set();
      for (const p of git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-m", c], cwd).split("\n").filter(Boolean)) {
        const parts = p.split("/");
        for (let i = 1; i <= parts.length; i++) paths.add(parts.slice(0, i).join("/"));
      }
      for (const p of paths) add("path", c, scanText(p, C));
      for (const [p, lines] of files) {
        const opts = { keysBlock: !exempt(p, S) };
        lines.forEach((l) => add("content", c, scanText(l, C, opts)));
        add("content", c, joinedKeyHits(lines, C, opts));
      }
    } catch {
      log(`REFUSED: git could not read commit ${c.slice(0, 7)}`); return 2;
    }
  }
  const seen = new Set();
  const uniq = found.filter((h) => { const k = `${h.label}|${h.surface}|${h.commit}|${h.block}`; return seen.has(k) ? false : seen.add(k); });
  for (const h of uniq) log(`${h.block ? "BLOCK" : "warn "} ${h.label} surface=${h.surface} commit=${h.commit}`);
  const blocked = uniq.some((h) => h.block);
  log(blocked ? `PUSH GATE: BLOCK (${commits.length} commits scanned)` : `PUSH GATE: clean (${commits.length} commits scanned)`);
  return blocked ? 3 : 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const a = process.argv.slice(2);
  const opt = (k) => { const i = a.indexOf(k); return i >= 0 ? a.splice(i, 2)[1] : undefined; };
  const ref = opt("--ref"), base = opt("--base");
  if (a.length !== 2) { console.log("usage: push_gate_v1.mjs <old-sha> <new-sha> [--ref <refname>] [--base <rev>]"); process.exit(2); }
  process.exit(gate(a[0], a[1], { ref, base }));
}
