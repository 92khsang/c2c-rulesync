#!/usr/bin/env node
// Generate tests/vectors/marked_comments.json: which HTML comments Claude Code
// strips from a rule body.
//
// Claude Code tokenizes a rule body containing "<!--" with a marked lexer, GFM
// off, that behaves as marked 15.0.12 (observed: identical output on 23,031
// bodies; marked 16 and later differ, for example by keeping link reference
// definitions). For every top-level html token whose raw text, ignoring leading
// white space, starts with "<!--" and contains "-->", it deletes each
// "<!-- ... -->" from that raw text and keeps the rest only if trim() does not
// empty it; every other token's raw text is kept. This script applies that
// behavior with marked 15.0.12 to curated bodies and to a seeded random corpus,
// for src/c2c_rulesync/markdown_blocks.py to be tested against.
//
// Usage:
//   npm install --prefix <dir> marked@15.0.12
//   node scripts/gen_comment_vectors.mjs <dir>/node_modules/marked tests/vectors/marked_comments.json

import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const EXPECTED_VERSION = "15.0.12";
const RANDOM_COUNT = 3000;
const RANDOM_SEED = 1;

const CURATED = [
  "Rule\n<!-- note -->\nMore",
  "<!-- a -->\n\nText",
  "Text\n\n<!-- a --> trailing\n",
  "  <!-- indented -->\nx",
  "    <!-- code -->\nx",
  "- item\n  <!-- in list -->\n",
  "> quote <!-- inline -->\n",
  "```\n<!-- fenced -->\n```\n",
  "<!-- unclosed\nrest",
  "para <!-- inline --> text",
  "Line1\r\nLine2\r\n<!-- crlf -->\r\nLine3",
  "\tTabbed\n<!-- c -->",
  "<!-- one --><!-- two -->\nx",
  "<!--\nmulti\nline\n-->\nafter",
  "Title\n=====\n<!-- after setext -->",
  "# H\n<!-- after heading -->\nbody",
  "text\n<!-- a -->b\nc",
  "<!-- a -->\n<!-- b -->\n",
  "   <!-- three spaces -->",
  "<div>\n<!-- in html -->\n</div>",
  "1. one\n<!-- after ordered -->",
  "---\n<!-- after hr -->",
  "a  \n<!-- -->\n",
  "<!---->x\n",
  "<!-- a -- b -->\n",
  "* * *\n<!-- x -->",
  // Rule bodies as people write them.
  "# Frontend rules\n\n<!-- Added after the CSS regression in March. -->\n- Keep component styles in the matching stylesheet.\n",
  "- Validate input with the shared schema.\n  <!-- incident: missing validation on /upload -->\n- Return ApiError, never a bare string.\n",
  "Use `Vec<String>` in examples.\n\n<!--\nLonger rationale\nacross lines.\n-->\n\n```rust\nlet v: Vec<String> = Vec::new(); // <!-- not a comment -->\n```\n",
  "<!-- TODO: expand -->",
  "Intro paragraph\n<!-- note -->\n\n## Section\n\nText <!-- inline stays --> here.\n",
  // Link reference definitions: dropped unless they continue a paragraph.
  "Follow the [style guide][style].\n\n<!-- TODO -->\n[style]: https://example.com/style\n",
  "Intro text\n[style]: https://example.com/style\n<!-- note -->\n",
  "<!-- note -->\n[style]: <https://example.com/style> \"Style\"\n[style]: https://example.com/other\nSee [style].\n",
  "> [style]: https://example.com/style\n<!-- note -->\n",
  "- item\n\n  [style]: https://example.com/style\n<!-- note -->\n",
  "[]: https://example.com\n[style]:\n<!-- note -->\n",
  // A space or tab left at the end becomes a line break of the comment token.
  "# Rules\n\nUse X.\n<!-- note -->\n\t",
  "Use X.\n<!-- note -->\n ",
  // A comment right after a block quote, and comments in a declaration that
  // ends at its first ">".
  "> Note\n>\n  <!-- why -->\nText\n",
  "<!X\n<!-- a --><!-- b -->\nend -->",
  // The remainder is blank by JavaScript's trim(), not Python's str.strip().
  "Use X.\n\n<!-- note -->\u0085\nMore\n",
  "Use X.\n\n<!-- note -->\ufeff\u3000\nMore\n",
  "Use X.\n\n<!-- note -->\u001c\nMore\n",
  "Title\n<!-- note -->\n=====\n",
  "Line one\r<!-- note -->\rLine two\r",
  // Comments inside the other kinds of HTML block stay.
  "<script>\n<!-- kept -->\n</script>\n<!-- note -->\n",
  "<?php\n<!-- kept -->\n?>\n<!-- note -->\n",
  "<![CDATA[\n<!-- kept -->\n]]>\n<!-- note -->\n",
  "<custom-tag>\n<!-- kept -->\n\n<!-- note -->\n",
  "Text\n<DIV>\n<!-- note -->\n",
  "<\u017fcript>\n<!-- note -->\n",
  "- item\n<!-- note -->\n- item\n",
  "> - item\nlazy\n<!-- note -->\n",
  // A lazily continued block quote rebuilds its raw text from lengths in
  // UTF-16 code units.
  "> > <!-- note -->\n> \u{1F600} quoted\nlazy\n",
  "> - \u{1F600}\n> >\n> > <!-- note -->\nlazy\n",
  // JavaScript's `.` stops at U+2028, so this line is not an ATX heading.
  "<!-- note -->\n# Title\u2028\n\t",
  // trimEnd() removes U+FEFF from the end of a list.
  "- item\n\ufeff\n <!-- note -->\n",
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

const LINES = [
  "Paragraph text.", "more text", "", "", "# Heading", "Title", "=====", "-----", "---", "***", "- item",
  "* item", "+ item", "1. one", "2) two", "  - nested", "   continued", "    indented code", "\tTabbed",
  "> quote", "> <!-- q -->", "```", "~~~", "```js", "<div>", "</div>", "<script>", "</script>",
  "<custom-tag>", "<span>inline</span>", "<!-- c -->", "  <!-- c -->", "   <!-- c -->", "    <!-- c -->",
  "<!-- start", "end -->", "<!-- a --> tail", "text <!-- mid --> text", "<!-- a --><!-- b -->", "<!---->",
  "<!-->", "- <!-- in item -->", "  <!-- item indent -->", "[ref]: https://example.com", "<?php ?>",
  "<!DOCTYPE html>", "Line  ", "\\<!-- escaped -->", "> - item", "lazy", "[ref]: <https://x> \"t\"", " ", "\t",
  "<!-- c -->\u0085", "<!-- c -->\ufeff", "<DIV>", "<?php", "?>", "<![CDATA[", "]]>", "> > nested", "\u{1F600} emoji",
];

function randomBodies() {
  const next = seeded(RANDOM_SEED);
  const pick = (items) => items[Math.floor(next() * items.length)];
  const bodies = [];
  for (let index = 0; index < RANDOM_COUNT; index += 1) {
    const lines = Array.from({ length: 1 + Math.floor(next() * 8) }, () => pick(LINES));
    if (!lines.some((line) => line.includes("<!--"))) {
      lines.splice(Math.floor(next() * lines.length), 0, pick(["<!-- c -->", "  <!-- c -->", "<!-- a\nb -->"]));
    }
    let body = lines.join(pick(["\n", "\n", "\r\n"]));
    if (next() < 0.3) {
      body += "\n";
    }
    bodies.push(body);
  }
  return bodies;
}

function strip(Lexer, body) {
  if (!body.includes("<!--")) {
    return body;
  }
  let content = "";
  for (const token of new Lexer({ gfm: false }).lex(body)) {
    if (token.type === "html") {
      const trimmed = token.raw.trimStart();
      if (trimmed.startsWith("<!--") && trimmed.includes("-->")) {
        const rest = token.raw.replace(/<!--[\s\S]*?-->/g, "");
        if (rest.trim().length > 0) {
          content += rest;
        }
        continue;
      }
    }
    content += token.raw;
  }
  return content;
}

const [markedDir, output] = process.argv.slice(2);
if (!markedDir || !output) {
  throw new Error("usage: node scripts/gen_comment_vectors.mjs <marked package dir> <output.json>");
}
const packageJson = JSON.parse(readFileSync(join(resolve(markedDir), "package.json"), "utf8"));
if (packageJson.version !== EXPECTED_VERSION) {
  throw new Error(`expected marked ${EXPECTED_VERSION}, found ${packageJson.version}`);
}
const { Lexer } = createRequire(import.meta.url)(resolve(markedDir));
const vectors = [
  ...CURATED.map((body) => ({ set: "curated", body, content: strip(Lexer, body) })),
  ...randomBodies().map((body) => ({ set: "random", body, content: strip(Lexer, body) })),
];
const lines = [
  "{",
  `"source": ${JSON.stringify({ marked: EXPECTED_VERSION, generator: "scripts/gen_comment_vectors.mjs", random_seed: RANDOM_SEED })},`,
  '"vectors": [',
  ...vectors.map((vector, index) => JSON.stringify(vector) + (index === vectors.length - 1 ? "" : ",")),
  "]",
  "}",
];
writeFileSync(output, `${lines.join("\n")}\n`);
console.log(`wrote ${output}: ${vectors.length} vectors`);
