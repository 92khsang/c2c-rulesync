#!/usr/bin/env bun
// Generate tests/vectors/bun_yaml.json: how Bun.YAML.parse treats front matter.
//
// Claude Code parses rule front matter with Bun.YAML, and whether a document
// parses decides whether a rule is path-scoped or loads unconditionally.
// src/c2c_rulesync/bun_yaml.py predicts Bun.YAML.parse; this script records the
// real answers for three sets of documents:
//
// - hand:   hand-picked edge cases found while calibrating the parser;
// - layout: combinations of keys, values and layouts rule files use;
// - random: a seeded corpus of mostly malformed documents, held out from
//           calibration to catch overfitting.
//
// Usage: bun scripts/gen_yaml_vectors.mjs tests/vectors/bun_yaml.json

import { writeFileSync } from "node:fs";

const HAND_PICKED = [
  // Scalars and plain-scalar edge cases.
  "paths: src/**", "paths: *.ts", "paths: a, b", "paths: src/** # note", "paths: a #b", "paths: a#b",
  "paths: ,a", "paths: -a", "paths: - a", "paths: ?a", "paths: :a", "paths: a:b", "paths: a :b",
  "paths: @a", "paths: `a`", "paths: %a", "paths: !foo a", "paths: {a,b}.ts", "paths: a b c",
  "paths: -", "paths: --", "paths: .", "paths: +", "paths: ~a", "paths: \u{e9}", "key: a: b",
  // Type resolution.
  "paths: 0", "paths: 00", "paths: 09", "paths: 012", "paths: 0x1F", "paths: 0X1f", "paths: 0o17",
  "paths: 0O7", "paths: 0b101", "paths: -0x1", "paths: +1", "paths: -1", "paths: 1_000",
  "paths: 1,000", "paths: 1.", "paths: .5", "paths: +.5", "paths: 1e3", "paths: 1e+2", "paths: 1E-2",
  "paths: 1e", "paths: 1.2.3", "paths: .inf", "paths: -.inf", "paths: .nan", "paths: 2024-01-01",
  "paths: yes", "paths: no", "paths: on", "paths: True", "paths: FALSE", "paths: tRUE", "paths: Null",
  "paths: NULL", "paths: nulL", "paths: ~", "paths: null", "paths: true",
  // Empty values and comments.
  "paths:", "paths:  ", "paths: # comment only", "paths: ''", "paths: \"\"", "", "   ", "# only comment",
  "\npaths: a", "paths: a # c\n  # d", "paths: a\t# tab comment", "paths:\ta", "paths:  \t a",
  "a:   b   ", "a: b\t", "a: x #", "a: x#",
  // Quoted scalars.
  "paths: 'it''s'", "paths: \"a\\tb\"", "paths: \"unterminated", "paths: 'a''", "paths: \"a\\qb\"",
  "paths: \"a\\x41\\u0042\"", "a: \"\\ \"", "a: \"\\\n  b\"", "a: \"x\\\ty\"", "a: \"\\/\"",
  "a: \"\\e\\N\\_\\L\\P\"", "a: \"\\U0001F600\"", "a: '\\n'", "a: 'x' # c", "a: 'x'# c", "a: \"x\"#c",
  "paths: 'a' b", "paths: \"a\" # c", "paths: 'a\n  b'", "paths: \"a\n  b\"", "paths: 'a\nb'",
  "paths: \"a\nb\"", "paths: 'a\n b'",
  // Multi-line plain scalars.
  "paths: a\n  b", "paths: a\n\n  b", "paths:\n  a\n  b", "paths: a\nb", "paths: a\nb: c", "paths: a\n b",
  "paths: a\n\n b", "paths:\n  a: 1\n  b",
  // Block sequences and mappings.
  "paths:\n  - a", "paths:\n- a\n- b", "paths:\n  - **/*.ts", "paths:\n  - \"**/*.ts\"", "paths:\n  - {a,b}.ts",
  "paths:\n  - 1\n  - a\n  -\n  - ''", "paths:\n  - a\n   - b", "paths:\n  - a\n - b", "paths:\n\t- a",
  "a:\n  - x\nb: y", "a:\n- x\nb: y", "a:\n- x\n- y", "a:\n  -\n    b", "a:\n  - b: 1\n    c: 2",
  "a:\n  - b: 1\n   c: 2", "a:\n  - - x", "- - x", "a:\n  b:\n    c: d", "a: 1\n b: 2", "a:\n  b: 1\n c: 2",
  "- a\n- b", "- paths: a", "just a scalar", "paths: a\npaths: b", " a: b", " a: b\n c: d", "\ta: b",
  "a: b\n\t", "a: b\n  \t", "a:\n\n\n  b",
  // Flow collections.
  "paths: [**/*.ts]", "paths: [\"a\", b]", "paths: [a, [b, c]]", "paths: []", "paths: [1, 'a', true, null, ~]",
  "paths: [a, b,]", "paths: [a,,b]", "paths: {a: 1, b}", "paths: [a: 1]", "paths: [a,\n  b]",
  "paths: [\"a\",\n\"b\"]", "paths: [\n  a,\n  b\n]", "paths:\n- [a, b]\n- c", "paths: [a] b", "paths: {a}",
  "paths: {}", "paths: {a: 1}", "paths: [a", "paths: [\n\"a\"]", "paths: [\n a]", "paths: [a,\n b\n ]",
  "paths: [a,\nb]", "paths:\n  [a,\n  b]", "paths:\n  [a,\n b]", "a: [a#b]", "a: [a #b]", "a: {a: [b, c]}",
  "a: [{b: c}]", "a: [a, b]c", "paths: [*x]",
  // Block scalars.
  "paths: |\n  a\n  b", "paths: >\n  a\n  b", "paths: >-\n  a\n  b\n", "paths: |+\n  a\n\n", "paths: |2\n   a",
  "paths: >\n\n  a", "a: |\n  x\n y", "a: |\n   x\n  y", "a: |-\n  x\n\n", "a: >+\n  x\n  y\n\n  z\n",
  "a: >\n  x\n    y\n  z", "a: |\n\tx", "a: |\n  \tx",
  // Anchors, aliases and tags.
  "paths: &x a\nother: *x", "paths: *a\nx: &a b", "x: &x [a, b]\npaths: *x", "a: &x\n  - q\nb: *x",
  "a: *", "a: &", "a: & b", "a: &x", "a: !", "a: ! b", "a: !!", "paths: !!str 123", "paths: !!int 1",
  "a: !!float 1", "a: !!bool true", "a: !!null ''", "a: !!map {}", "a: !!seq []", "a: !!binary x",
  "a: !custom [x]", "a: !!str\n  - q", "paths: !<tag:yaml.org,2002:str> a",
  // Keys.
  "Paths: a", "key_with-dash: v", "123: v", "\"quoted key\": v", "a:b: c", "a:\tb", "a : b", "a  : b",
  "-a: b", "?a: b", ":a: b", "a b: c", "a,b: c", "a[: b", "a]: b", "a{: b", "a}: b", "a#b: c", "a #b: c",
  "@a: b", "`a: b", "? paths\n: a", "? a", "? a\n: b", "? [a]\n: b", "[a]: b", "{a: b}: c", "a: b: c",
  "\"a\": b: c",
  // Documents and line endings.
  "paths: a\n...", "a: b\n...\n", "a: b\n...\nc: d", "%YAML 1.2\n---\npaths: a", "paths: a\r\nother: b",
  "paths: a\r\n", "paths: \"a\"\r\n  - b", "paths:\r\n  - a\r\n  - b\r\n",
];

const KEYS = ["paths", "description", "globs"];
const VALUES = [
  "src/**", "\"src/**\"", "'src/**'", "*.ts", "**/*.ts", "\"**/*.ts\"", "{a,b}.ts", "src/{a,b}/**",
  "@x", "`x`", "%x", "!x", "&x y", "a: b", "a:b", "a #c", "a#c", "[a]", "[a, b]", "[\"*.ts\"]",
  "{a: b}", "|", ">", "-", "- a", "? a", "a, b", "123", "true", "null", "~", "''", "\"\"",
  "\"a\\\"b\"", "'a''b'", "\"\\q\"", ".env*", "docs/*.md", "!!str 1", "*x", "a\tb", "a  ",
  "Use `Vec<String>` & friends", "Rules for: the API", "#hash", "src/**/*.{ts,tsx}",
];
const LAYOUTS = [
  (k, v, w) => `${k}: ${v}`,
  (k, v, w) => `${k}:\n  - ${v}\n  - ${w}`,
  (k, v, w) => `${k}:\n- ${v}`,
  (k, v, w) => `${k}: [${v}, ${w}]`,
  (k, v, w) => `description: ${w}\n${k}: ${v}`,
  (k, v, w) => `${k}: ${v}\r\nname: ${w}`,
  (k, v, w) => `${k}:\n    - ${v}\n    - ${w}`,
];

function generated() {
  const docs = [];
  VALUES.forEach((value, index) => {
    const other = VALUES[(index * 7 + 3) % VALUES.length];
    for (const layout of LAYOUTS) {
      for (const key of KEYS.slice(0, index % 3 === 0 ? 3 : 1)) {
        docs.push(layout(key, value, other));
      }
    }
  });
  return docs;
}

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

const RANDOM_COUNT = 3000;
const RANDOM_SEED = 11;
const SCALAR_BITS = [
  "src", "**", "*", "/", ".ts", "{a,b}", "[x]", "a b", ":", ": ", "#", " #c", "'", "\"", "\\", "-",
  "? ", "!", "&", "@", "`", "%", "|", ">", ",", "1", "true", "~", "null", "\t", "\u{E9}", "0x1F", ".inf",
];
const RANDOM_KEYS = ["paths", "description", "globs", "name", "Paths", "a b", "\"q\""];

function randomDocuments() {
  const next = seeded(RANDOM_SEED);
  const pick = (items) => items[Math.floor(next() * items.length)];
  const scalar = () => Array.from({ length: 1 + Math.floor(next() * 3) }, () => pick(SCALAR_BITS)).join("");
  const quoted = () => pick([
    `"${scalar().replace(/["\\]/g, "")}"`,
    `'${scalar().replace(/'/g, "''")}'`,
    scalar(),
  ]);
  const flow = () =>
    `[${Array.from({ length: Math.floor(next() * 3) }, quoted).join(pick([", ", ",", " , "]))}${pick(["", ","])}]`;
  const value = () => pick([
    quoted, quoted, flow, () => "", () => `|\n  ${scalar()}\n  ${scalar()}`, () => `>-\n  ${scalar()}`,
    () => `&a ${quoted()}`, () => "*a", () => `!!str ${scalar()}`,
  ])();
  const documents = [];
  for (let index = 0; index < RANDOM_COUNT; index += 1) {
    const entries = [];
    for (let entry = 0, count = 1 + Math.floor(next() * 3); entry < count; entry += 1) {
      const key = pick(RANDOM_KEYS);
      const layout = Math.floor(next() * 5);
      if (layout === 0) {
        entries.push(`${key}: ${value()}`);
      } else if (layout === 1) {
        const items = Array.from({ length: 1 + Math.floor(next() * 2) }, () => `${pick(["  ", "", "    ", " "])}- ${value()}`);
        entries.push(`${key}:\n${items.join("\n")}`);
      } else if (layout === 2) {
        entries.push(`${key}: ${flow()}`);
      } else if (layout === 3) {
        entries.push(`${key}:${pick(["", " ", " # c"])}`);
      } else {
        entries.push(`${pick(["", " ", "\t"])}${key}: ${scalar()}${pick(["", `\n  ${scalar()}`, `\n${scalar()}`])}`);
      }
    }
    documents.push(entries.join(pick(["\n", "\r\n", "\n\n"])));
  }
  return documents;
}

function record(set, doc) {
  let value;
  try {
    value = Bun.YAML.parse(doc);
  } catch (error) {
    return { set, doc, ok: false, error: String(error.message) };
  }
  return { set, doc, ok: true, value };
}

// JSON has no NaN or Infinity; encode them as tagged strings.
function replacer(key, value) {
  if (typeof value === "number" && !Number.isFinite(value)) {
    return { number: String(value) };
  }
  return value;
}

const output = process.argv[2];
if (!output) {
  throw new Error("usage: bun scripts/gen_yaml_vectors.mjs <output.json>");
}
if (typeof Bun === "undefined") {
  throw new Error("run this script with Bun, the runtime Claude Code uses");
}
const seen = new Set();
const vectors = [];
for (const [set, documents] of [["hand", HAND_PICKED], ["layout", generated()], ["random", randomDocuments()]]) {
  for (const doc of documents) {
    if (seen.has(doc)) {
      continue;
    }
    seen.add(doc);
    const vector = record(set, doc);
    try {
      JSON.stringify(vector, replacer);
    } catch {
      // Aliases can make a value cyclic, which JSON cannot hold.
      continue;
    }
    vectors.push(vector);
  }
}
const lines = [
  "{",
  `"source": ${JSON.stringify({ runtime: `Bun ${Bun.version}`, generator: "scripts/gen_yaml_vectors.mjs", random_seed: RANDOM_SEED })},`,
  '"vectors": [',
  ...vectors.map((vector, index) => JSON.stringify(vector, replacer) + (index === vectors.length - 1 ? "" : ",")),
  "]",
  "}",
];
writeFileSync(output, `${lines.join("\n")}\n`);
console.log(`wrote ${output}: ${vectors.length} vectors (${vectors.filter((v) => !v.ok).length} errors)`);
