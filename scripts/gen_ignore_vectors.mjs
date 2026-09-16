#!/usr/bin/env node
// Generate tests/vectors/node_ignore_7_0_5.json from the real node-ignore 7.0.5.
//
// Claude Code 2.1.273 matches rule globs with node-ignore 7.0.5, so
// src/c2c_rulesync/ignore.py is tested against answers computed by that exact
// library. The script downloads node-ignore's index.js and test fixtures at a
// pinned commit, checks their SHA-256, evaluates them under Node.js, and writes:
//
// - fixtures:  node-ignore's own test/fixtures/cases.js, ignores() scope only;
// - curated:   Claude-shaped inputs and the places 7.0.5 differs from git;
// - generated: a seeded corpus of patterns and of paths derived from them.
//
// Every vector records, per pattern, whether node-ignore can evaluate it alone
// (Claude Code drops the ones it cannot) and whether the kept patterns ignore
// the path. The script needs network access and is not run in CI.
//
// Usage: node scripts/gen_ignore_vectors.mjs tests/vectors/node_ignore_7_0_5.json

import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const COMMIT = "84d052ddfe7c326b01b306154e06709d6e7e2ed8";
const PINNED_SHA256 = {
  "index.js": "56e047fefe8e36a8a1529b31e2992314006372bd271703d9d62ba4b5d5777340",
  "test/fixtures/cases.js": "fd3663f5370323bbbc1ebd3afa7094a7ad20fa0b876c5a02b30ce0897df539e7",
};
// Pattern files cases.js reads from disk.
const FIXTURE_FILES = [
  "test/fixtures/.aignore",
  "test/fixtures/.ignore-issue-2",
  "test/fixtures/.gitignore-with-BOM",
];
const GENERATED_COUNT = 4000;
const SEED = 0x5eed7005;

async function download(root, relative, expectedSha256) {
  const url = `https://raw.githubusercontent.com/kaelzhang/node-ignore/${COMMIT}/${relative}`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`failed to fetch ${url}: HTTP ${response.status}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (expectedSha256 && actual !== expectedSha256) {
    throw new Error(`SHA-256 mismatch for ${relative}: ${actual}`);
  }
  const target = join(root, relative);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, bytes);
}

async function loadNodeIgnore(root) {
  for (const [relative, sha256] of Object.entries(PINNED_SHA256)) {
    await download(root, relative, sha256);
  }
  for (const relative of FIXTURE_FILES) {
    await download(root, relative, null);
  }
  // cases.js requires `debug` for logging only.
  mkdirSync(join(root, "node_modules/debug"), { recursive: true });
  writeFileSync(join(root, "node_modules/debug/index.js"), "module.exports = () => () => {}\n");
  writeFileSync(join(root, "package.json"), '{"name": "ignore", "main": "index.js"}\n');
  const require = createRequire(join(root, "package.json"));
  return { require, ignore: require("./index.js") };
}

function canEvaluate(ignore, pattern) {
  try {
    ignore().add([pattern]).test("probe");
    return true;
  } catch {
    return false;
  }
}

function evaluate(ignore, patterns, path) {
  const valid = patterns.map((pattern) => canEvaluate(ignore, pattern));
  const kept = patterns.filter((_, index) => valid[index]);
  return { patterns, path, valid, ignored: ignore().add(kept).ignores(path) };
}

function fixtureVectors(require, ignore) {
  const vectors = [];
  require("./test/fixtures/cases.js").cases(({ description, patterns, paths_object, scopes }) => {
    if (scopes !== false && !scopes.includes("ignores")) {
      return;
    }
    const source = typeof patterns === "string"
      ? { text: patterns }
      : { patterns: patterns.map((item) => (typeof item === "string" ? item : item.pattern)) };
    const instance = ignore().add(patterns);
    for (const [path, expected] of Object.entries(paths_object)) {
      const ignored = instance.ignores(path);
      if (ignored !== Boolean(expected)) {
        throw new Error(`node-ignore disagrees with its own fixture: ${description}: ${path}`);
      }
      vectors.push({ description, ...source, path, ignored });
    }
  });
  return vectors;
}

const CURATED = [
  // Claude-shaped globs.
  [["*.md"], "docs/a.md"],
  [["*.md"], ".hidden.md"],
  [["src"], "packages/x/src/a.ts"],
  [["src/*.ts"], "x/src/a.ts"],
  [["/src"], "src/a.ts"],
  [["**/*.TS"], "a/b.ts"],
  [["src/**/*.ts"], "src/a.ts"],
  [["a/**/b"], "a/b"],
  [["docs/"], "docs/a.md"],
  [["probe.txt"], "pkg/probe.txt"],
  [[".env*"], ".env.local"],
  [["src/[slug]/page.tsx"], "src/[slug]/page.tsx"],
  [["src/\\[slug\\]/page.tsx"], "src/[slug]/page.tsx"],
  [["src/*/page.tsx"], "src/[slug]/page.tsx"],
  [["**/*.ts"], "..foo/a.ts"],
  [["docs/\u{E4}b.md"], "Docs/\u{C4}B.MD"],
  // Negation.
  [["*", "!*.md"], "a.md"],
  [["src", "!src/a.ts"], "src/a.ts"],
  [["b/*", "!b/c.ts"], "b/c.ts"],
  [["*", "!"], "x"],
  // Where 7.0.5 differs from git.
  [["**/**/foo"], "foo"],
  [["[!a].ts"], "!.ts"],
  [["[!a].ts"], "b.ts"],
  [["[^a].ts"], "^.ts"],
  [["abc\\*"], "abcd"],
  [["a\\?c"], "a?c"],
  [["\\d.ts"], "1.ts"],
  [["\\d.ts"], "d.ts"],
  [["abc/**/"], "abc/x"],
  [["[a/b]"], "a"],
  [["\u{FEFF}#x"], "#x"],
  [["foo/ "], "x/foo/y"],
  [["foo/"], "x/foo/y"],
  [["a\tb"], "a b"],
  [["a\\\tb"], "a b"],
  [["abc\t"], "abc"],
  // Invalid and degenerate brackets.
  [["[~-!]"], "a"],
  [["*", "[~-!]"], "a"],
  [["[a-9].ts"], "a.ts"],
  [["src/[q"], "src/[q"],
  [["[]"], "a"],
  // Regex syntax reachable through backslash escapes: brace quantifiers,
  // class escapes beside a hyphen, capturing groups and backreferences.
  [["\\\\{2}"], "\\\\"],
  [["\\\\{2}"], "\\{2}"],
  [["\\\\{2,1}"], "\\{2,1}"],
  [["a\\{2}"], "aa"],
  [["[!\\D-~]"], "5"],
  [["[\\d-!-~]"], "$"],
  [["[\\d--a]"], "\\"],
  [["[[\\W-$]"], "["],
  [["[[-\\D|-z]"], "P"],
  [["\\\\(\\\\)\\1"], "\\\\\\"],
  [["\\\\(a\\\\)\\1"], "\\a\\A\\"],
  // UTF-16 code units and case folding.
  [["a?b"], "a\u{1F600}b"],
  [["a??b"], "a\u{1F600}b"],
  [["s"], "\u{17F}"],
  [["k"], "\u{212A}"],
];

// mulberry32: small, seedable, and identical on every platform.
function seeded(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// [pattern token, text a matching path could contain there].
const TOKENS = [
  ["a", "a"], ["b", "b"], ["A", "a"], ["x", "X"], ["src", "src"], ["docs", "Docs"], [".md", ".md"],
  [".ts", ".TS"], [".", "."], ["-", "-"], ["_", "_"], ["/", "/"], ["/", "/"], ["*", "ab"], ["*", ""],
  ["**", "p/q"], ["**", ""], ["/**/", "/"], ["/**/", "/m/n/"], ["?", "z"], ["?", "\u{1F600}"],
  ["[ab]", "b"], ["[a-c]", "C"], ["[!a]", "!"], ["[^a]", "^"], ["[z-a]", ""], ["[~-!]", "~"],
  ["[", "["], ["]", "]"], ["\\*", "*"], ["\\?", "?"], ["\\[", "["], ["\\ ", " "], ["\\!", "!"],
  ["\\#", "#"], ["!", ""], ["#", "#"], [" ", ""], ["\t", ""], ["\u{E9}", "\u{C9}"],
  ["\u{1F600}", "\u{1F600}"], ["$", "$"], ["^", "^"], ["+", "+"], ["(", "("], [")", ")"],
  ["|", "|"], ["{", "{"], ["}", "}"], ["\\d", "7"], ["\\w", "w"], ["\\s", " "], ["\\b", ""],
  ["\\q", "q"], ["\\1", "\u{1}"], ["\\x41", "A"], ["\\u0041", "A"], ["\\c", "\\c"], ["\\\\", "\\"],
];
const RANDOM_SEGMENTS = [
  "a", "b", "src", "docs", "a.md", "b.ts", ".env", "[ab]", "!", "#", "a b", "\u{E9}", "\u{1F600}",
  "SRC", "Docs", "*", "?", "\u{17F}", "\u{212A}", "..foo", "x.y.z",
];

function derivedPath(next, parts) {
  const path = parts.join("").replace(/^\/+/, "").replace(/\/{2,}/g, "/");
  if (path === "" || path === "." || path === ".." || path.startsWith("./") || path.startsWith("../")) {
    return "a";
  }
  // Sometimes descend below the derived path, to exercise parent matching.
  return next() < 0.25 ? `${path.replace(/\/$/, "")}/child.ts` : path;
}

function generatedVectors(ignore) {
  const next = seeded(SEED);
  const pick = (items) => items[Math.floor(next() * items.length)];
  const vectors = [];
  while (vectors.length < GENERATED_COUNT) {
    const patternCount = next() < 0.8 ? 1 : 2 + Math.floor(next() * 2);
    const patterns = [];
    let derived = null;
    for (let p = 0; p < patternCount; p += 1) {
      const tokens = Array.from({ length: 1 + Math.floor(next() * 5) }, () => pick(TOKENS));
      patterns.push(tokens.map(([token]) => token).join(""));
      derived ??= tokens.map(([, text]) => text);
    }
    let path;
    if (next() < 0.6) {
      path = derivedPath(next, derived);
    } else {
      path = Array.from({ length: 1 + Math.floor(next() * 4) }, () => pick(RANDOM_SEGMENTS)).join("/");
    }
    if (path.endsWith("/")) {
      path = path.slice(0, -1) || "a";
    }
    vectors.push(evaluate(ignore, patterns, path));
  }
  return vectors;
}

// One vector per line keeps the file reviewable in diffs.
function serialize(document) {
  const lines = ["{", `"source": ${JSON.stringify(document.source)},`];
  const sets = ["fixtures", "curated", "generated"];
  sets.forEach((name, index) => {
    lines.push(`"${name}": [`);
    document[name].forEach((vector, position) => {
      const comma = position === document[name].length - 1 ? "" : ",";
      lines.push(`${JSON.stringify(vector)}${comma}`);
    });
    lines.push(index === sets.length - 1 ? "]" : "],");
  });
  lines.push("}");
  return `${lines.join("\n")}\n`;
}

async function main() {
  const output = process.argv[2];
  if (!output) {
    throw new Error("usage: node scripts/gen_ignore_vectors.mjs <output.json>");
  }
  const root = mkdtempSync(join(tmpdir(), "node-ignore-7.0.5-"));
  try {
    const { require, ignore } = await loadNodeIgnore(root);
    const document = {
      source: {
        project: "https://github.com/kaelzhang/node-ignore",
        version: "7.0.5",
        commit: COMMIT,
        sha256: PINNED_SHA256,
        license: "MIT; see THIRD_PARTY_NOTICES.md",
        generator: "scripts/gen_ignore_vectors.mjs",
        node: process.version,
        semantics: "ignore() defaults (ignorecase); patterns that cannot be evaluated alone are dropped",
      },
      fixtures: fixtureVectors(require, ignore),
      curated: CURATED.map(([patterns, path]) => evaluate(ignore, patterns, path)),
      generated: generatedVectors(ignore),
    };
    writeFileSync(output, serialize(document));
    const matched = document.generated.filter((vector) => vector.ignored).length;
    console.log(
      `wrote ${output}: ${document.fixtures.length} fixture, ${document.curated.length} curated, ` +
        `${document.generated.length} generated (${matched} ignored) vectors`,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

await main();
