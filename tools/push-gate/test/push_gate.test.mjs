// node --test tools/push-gate/test/   (fake keys are built at runtime, so this file holds no key-shaped literal)
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { gate, loadSettings } from "../push_gate_v1.mjs";

const ZERO = "0".repeat(40);
const KEY = "sk-" + "ant-" + "A1b2C3d4".repeat(4);
const MAIL = "dev" + "@" + "example.invalid";

function repo() {
  const dir = mkdtempSync(join(tmpdir(), "pg-"));
  const env = { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1",
    GIT_AUTHOR_NAME: "Dev", GIT_AUTHOR_EMAIL: MAIL, GIT_COMMITTER_NAME: "Dev", GIT_COMMITTER_EMAIL: MAIL };
  const git = (...a) => execFileSync("git", a, { cwd: dir, env, encoding: "utf8" }).trim();
  git("init", "-q", "-b", "main");
  const commit = (files, msg = "change", extraEnv = {}) => {
    for (const [p, body] of Object.entries(files)) { mkdirSync(join(dir, dirname(p)), { recursive: true }); writeFileSync(join(dir, p), body); }
    execFileSync("git", ["add", "-A"], { cwd: dir, env });
    execFileSync("git", ["commit", "-q", "-m", msg], { cwd: dir, env: { ...env, ...extraEnv } });
    return git("rev-parse", "HEAD");
  };
  const base = commit({ "README.md": "hello\n" }, "init");
  const terms = join(dir, "..", `terms-${Date.now()}-${Math.random()}.txt`);
  writeFileSync(terms, "# comment\nexample-corp\nzed\n");
  return { dir, commit, base, terms, git };
}

function run(r, newSha, extra = {}) {
  const lines = [];
  const S = { ...loadSettings(), terms_file: r.terms, key_exempt_dirs: ["test/"], ...extra };
  const rc = gate(r.base, newSha, { cwd: r.dir, S, log: (l) => lines.push(l), ...extra.opts });
  return { rc, out: lines.join("\n") };
}

test("clean push passes", () => {
  const r = repo();
  assert.equal(run(r, r.commit({ "src/a.js": "const x = 1;\n" })).rc, 0);
});

test("term in content, path, message and identity blocks without printing it", () => {
  const cases = [
    () => { const r = repo(); return [r, r.commit({ "src/a.js": "// Example-Corp internal\n" })]; },
    () => { const r = repo(); return [r, r.commit({ "example-corp/a.js": "x\n" })]; },
    () => { const r = repo(); return [r, r.commit({ "a.js": "x\n" }, "fix for example-corp")]; },
    () => { const r = repo(); return [r, r.commit({ "a.js": "x\n" }, "m", { GIT_AUTHOR_NAME: "example-corp bot" })]; },
  ];
  for (const mk of cases) {
    const [r, sha] = mk();
    const { rc, out } = run(r, sha);
    assert.equal(rc, 3, out);
    assert.match(out, /BLOCK term:#2/);
    assert.doesNotMatch(out, /example-corp/i);
  }
});

test("short terms match whole words only", () => {
  const r = repo();
  assert.equal(run(r, r.commit({ "a.txt": "zedonk zeds\n" })).rc, 0);
  const r2 = repo();
  assert.equal(run(r2, r2.commit({ "a.txt": "ask zed now\n" })).rc, 3);
});

test("zero-width character inside a term still blocks", () => {
  const r = repo();
  assert.equal(run(r, r.commit({ "a.txt": "example​-corp\n" })).rc, 3);
});

test("key in content blocks, never printed", () => {
  const r = repo();
  const { rc, out } = run(r, r.commit({ "src/cfg.js": `const k = "${KEY}";\n` }));
  assert.equal(rc, 3); assert.match(out, /key:anthropic_key/); assert.ok(!out.includes(KEY));
});

test("key cut across two lines is joined", () => {
  const r = repo();
  const { rc, out } = run(r, r.commit({ "src/cfg.js": `const k = "${KEY.slice(0, 15)}" +\n  "${KEY.slice(15)}";\n` }));
  assert.equal(rc, 3, out); assert.match(out, /joined/);
});

test("key in an exempt test dir warns, but not in a near-miss dir or a message", () => {
  const r = repo();
  assert.equal(run(r, r.commit({ "test/fixture.js": `k = "${KEY}"\n` })).rc, 0);
  const r2 = repo();
  assert.equal(run(r2, r2.commit({ "tests/fixture.js": `k = "${KEY}"\n` })).rc, 3);
  const r3 = repo();
  assert.equal(run(r3, r3.commit({ "test/f.js": "x\n" }, `oops ${KEY}`)).rc, 3);
});

test("added content that looks like a diff header cannot hide or exempt later lines", () => {
  const r = repo();
  assert.equal(run(r, r.commit({ "notes.txt": `++ /dev/null\nexample-corp\n${KEY}\n` })).rc, 3);
  const r2 = repo();
  const { rc, out } = run(r2, r2.commit({ "notes.txt": `++ b/test/x\n${KEY}\n` }));
  assert.equal(rc, 3, out); assert.match(out, /BLOCK key:anthropic_key/);
});

test("a path added only by a merge commit is scanned", () => {
  const r = repo();
  r.git("checkout", "-q", "-b", "side"); r.commit({ "side.txt": "s\n" });
  r.git("checkout", "-q", "main"); r.commit({ "main.txt": "m\n" });
  r.git("merge", "-q", "--no-commit", "side");
  mkdirSync(join(r.dir, "example-corp"), { recursive: true }); writeFileSync(join(r.dir, "example-corp", "r.txt"), "hello\n");
  r.git("add", "-A"); r.git("commit", "-q", "-m", "merge");
  const { rc, out } = run(r, r.git("rev-parse", "HEAD"));
  assert.equal(rc, 3, out); assert.match(out, /BLOCK term:#2 surface=path/);
});

test("new branch (zero old sha) uses the base", () => {
  const r = repo();
  const sha = r.commit({ "a.txt": "example-corp\n" });
  const lines = [];
  const S = { ...loadSettings(), terms_file: r.terms };
  assert.equal(gate(ZERO, sha, { cwd: r.dir, S, base: r.base, log: (l) => lines.push(l) }), 3);
});

test("refusals: no terms file, bad sha, option-looking base", () => {
  const r = repo();
  const sha = r.commit({ "a.txt": "x\n" });
  const S = { ...loadSettings(), terms_file: join(r.dir, "missing.txt") };
  const saved = process.env.PUSH_GATE_TERMS_FILE; delete process.env.PUSH_GATE_TERMS_FILE;
  assert.equal(gate(r.base, sha, { cwd: r.dir, S, log: () => {} }), 2);
  const S2 = { ...loadSettings(), terms_file: r.terms };
  assert.equal(gate(r.base, "not-a-sha", { cwd: r.dir, S: S2, log: () => {} }), 2);
  assert.equal(gate(ZERO, sha, { cwd: r.dir, S: S2, base: "--output=/tmp/x", log: () => {} }), 2);
  if (saved !== undefined) process.env.PUSH_GATE_TERMS_FILE = saved;
});
