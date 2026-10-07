#!/usr/bin/env node
// validate-cards — R14 packet 2: the card validator (RULINGS.md R14.1, R14.4).
//
// R14.1: "A new trap filing is a card. A card is at most 15 lines and at most 2,000 bytes, with six
// fields ... Cards live in specs/<project>/cards.md, written only by that project." This tool checks
// the shape of those files. It reads; it never writes, moves or deletes any file, and it never judges
// whether a card is RIGHT (law 1: doctrine is data; a hub folds only what it verifies locally).
//
// THE CARD FORMAT (this header is the contract; examples/cards-v1.md is a valid sample)
//
//   specs/<project>/cards.md, where <project> is a fleet member (tools/fleet-membership.mjs over the
//   top-level specs/*.md files). The file is read line by line (CRLF is read as LF):
//
//   - A line beginning "## " opens a CARD. Its heading is exactly "## <project>/<id>", where <project>
//     is the directory the file sits in and <id> matches [A-Za-z0-9][A-Za-z0-9._-]{0,63}. The full id
//     "<project>/<id>" is what dispositions name (dispositions/README.md). It is unique by construction
//     across the bus, and a card filed in another project's file is refused.
//   - Every following line, up to the next "## " line or "no-card:" line, belongs to that card. Each
//     non-blank line is either a FIELD ("<name>: <value>", name at column 0) or a CONTINUATION of the
//     previous field (it begins with whitespace). The six fields, each at most once:
//         rule:        the rule                                                     (required)
//         mechanism:   why it happens                                               (required)
//         applies:     comma-separated fleet member ids; ABSENT means every board   (optional)
//         check:       a command, or a law-6 pointer to a harness already pushed under the
//                      filing project's own bus directory                            (required)
//         supersedes:  what it replaces, or "none"                                   (required)
//         evidence:    exactly "measured" or "reported"                              (required)
//     Any other field name is refused, so a typo cannot silently drop a field.
//   - The SAME six fields may instead be spelled as bold bullets, the form live boards already file in:
//         - **Rule:** ...   - **Mechanism:** ...   - **Applies to:** ... (or **Applies:**)
//         - **Check:** ...  - **Supersedes:** ...  - **Evidence:** measured|reported
//     Labels are case-insensitive; values, requirements and limits are identical, and one field in two
//     spellings is a repeat. A bold bullet with any OTHER label ("- **Where:** ...", "- **Falsifier:**")
//     is an extra line, not a field: it satisfies nothing, it counts toward the 15-line and 2,000-byte
//     limits like any line, and its continuations belong to it. A bold label within two edits of a
//     field label ("- **Aplies to:**") is refused as a likely misspelling, so a typo cannot silently
//     widen `applies` to every board. Plain "<name>: <value>" lines stay strict: any other name is refused.
//   - A card is its heading plus its lines, trailing blank lines excluded: at most 15 lines and at most
//     2,000 bytes (UTF-8, lines joined by LF).
//   - A line beginning "no-card: " is an R14.4 no-card line: "no-card: <index id> <reason>", where the
//     index id is a TRAPS-INDEX.md row id (T<line>) and the reason is not empty. It closes any open card.
//   - Lines before the first card or no-card line are a free preamble (a "# title", prose), except that
//     card-like text there is refused: a field line in either spelling, or a "###"-style heading (the
//     TRAPS.md filing form), would otherwise be silently accepted as prose. After that,
//     a line beginning "# " is refused: it would read as a card boundary to a human and not to a tool.
//
// Usage
//   node tools/validate-cards.mjs                       every specs/*/cards.md in this bus checkout
//   node tools/validate-cards.mjs --bus <path>          another bus checkout
//   node tools/validate-cards.mjs --file <cards.md> --project <id>   one file (a board's filing tool,
//                                                       before it commits); --bus supplies the roster
// Exit: 0 every file valid (including "no card files yet"); 1 invalid, with one reason per line,
// each naming file:line and card; 2 tool or environment failure.

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fleetMembers } from './fleet-membership.mjs';

export const MAX_CARD_LINES = 15;
export const MAX_CARD_BYTES = 2000;
export const CARD_FIELDS = ['rule', 'mechanism', 'applies', 'check', 'supersedes', 'evidence'];
const REQUIRED = ['rule', 'mechanism', 'check', 'supersedes', 'evidence'];
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const INDEX_ID_RE = /^T[1-9][0-9]*$/;
const EXIT_OK = 0, EXIT_INVALID = 1, EXIT_FAIL = 2;
export const FIELD_HINT = 'card fields are "<name>: <value>" or "- **<Name>:** <value>"; see examples/cards-v1.md';
// The bold-bullet spelling of the same six fields (labels compared case-insensitively).
export const BOLD_LABELS = {
  rule: 'rule', mechanism: 'mechanism', 'applies to': 'applies', applies: 'applies',
  check: 'check', supersedes: 'supersedes', evidence: 'evidence',
};
const NOTE = Symbol('note');

// One card line as a field, or null when it is neither spelling. Returns
// { label, name, value } (name null for an unknown plain field) or { label, note: true } for a
// bold-bullet line whose label is not one of the six.
export function parseFieldLine(l) {
  const b = /^- \*\*([^*:]+):\*\*(?:\s+(.*))?$/.exec(l);
  if (b) {
    const label = b[1].trim();
    const name = BOLD_LABELS[label.toLowerCase().replace(/\s+/g, ' ')];
    return name ? { label, name, value: (b[2] || '').trim() } : { label, note: true };
  }
  const m = /^([A-Za-z-]+):(?:\s(.*))?$/.exec(l);
  if (!m) return null;
  return { label: m[1], name: CARD_FIELDS.includes(m[1]) ? m[1] : null, value: (m[2] || '').trim() };
}

function distance(a, b) {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...new Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) {
    d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  }
  return d[a.length][b.length];
}

// A bold label within two edits of a field label is a likely misspelling, refused rather than kept
// as a note: otherwise a typo'd optional `applies` would silently widen a card to every board.
function nearField(label) {
  const l = label.toLowerCase().replace(/\s+/g, ' ');
  for (const k of Object.keys(BOLD_LABELS)) if (distance(l, k) <= 2) return k;
  return null;
}

// Card-like text before the first card heading: a field line in either spelling, or a "###"-style
// heading (the TRAPS.md filing form). Plain field names are matched case-insensitively here.
function cardLike(l) {
  if (/^#{3,6} /.test(l)) return true;
  const b = /^- \*\*([^*:]+):\*\*/.exec(l);
  if (b) return Boolean(BOLD_LABELS[b[1].trim().toLowerCase().replace(/\s+/g, ' ')]);
  const m = /^([A-Za-z-]+):(\s|$)/.exec(l);
  return Boolean(m && CARD_FIELDS.includes(m[1].toLowerCase()));
}

export function busMembers(bus) {
  const specs = join(bus, 'specs');
  const names = readdirSync(specs, { withFileTypes: true }).filter((e) => e.isFile()).map((e) => e.name);
  return fleetMembers(names);
}

// Parse one cards file. Returns { cards, noCards, errors }; errors are { line, card, reason }.
// `members` (an array of member ids) is used to check the file's project and each `applies` value.
export function parseCardsFile(text, project, members) {
  const lines = String(text).replace(/\r\n/g, '\n').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const memberSet = new Set(members);
  const errors = [];
  const cards = [];
  const noCards = [];
  if (!memberSet.has(project)) errors.push({ line: 0, card: null, reason: `'${project}' is not a fleet member (no specs/${project}.md counted by tools/fleet-membership.mjs)` });

  let current = null;
  let seenBlock = false;
  const close = () => {
    if (!current) return;
    let end = current.body.length;
    while (end > 0 && current.body[end - 1].text.trim() === '') end--;
    const body = current.body.slice(0, end);
    const all = [current.headingText, ...body.map((b) => b.text)];
    const card = { id: current.id, line: current.line, endLine: current.line + body.length, lines: all.length, bytes: Buffer.byteLength(all.join('\n'), 'utf8'), fields: {} };
    const err = (line, reason) => errors.push({ line, card: card.id, reason });
    if (card.lines > MAX_CARD_LINES) err(card.line, `card is ${card.lines} lines; the limit is ${MAX_CARD_LINES}`);
    if (card.bytes > MAX_CARD_BYTES) err(card.line, `card is ${card.bytes} bytes; the limit is ${MAX_CARD_BYTES}`);
    let last = null;
    for (const { text: l, line } of body) {
      if (l.trim() === '') { last = null; continue; }
      if (/^\s/.test(l)) {
        if (!last) err(line, 'continuation line with no field above it');
        else if (last !== NOTE) card.fields[last] += ' ' + l.trim();
        continue;
      }
      const f = parseFieldLine(l);
      if (!f) { err(line, `not a field or a continuation: ${JSON.stringify(l.slice(0, 60))}; ${FIELD_HINT}`); last = null; continue; }
      if (f.note) {
        const near = nearField(f.label);
        if (near) { err(line, `bold label '${f.label}' is not a field but is close to '${near}'; ${FIELD_HINT}`); last = null; continue; }
        last = NOTE; // an extra bold-bullet line: kept, counted toward the limits, never a field
        continue;
      }
      if (!f.name) { err(line, `unknown field '${f.label}' (fields: ${CARD_FIELDS.join(', ')}); ${FIELD_HINT}`); last = null; continue; }
      const name = f.name;
      if (Object.prototype.hasOwnProperty.call(card.fields, name)) { err(line, `field '${name}' appears twice`); last = null; continue; }
      card.fields[name] = f.value;
      last = name;
    }
    for (const name of REQUIRED) {
      if (!Object.prototype.hasOwnProperty.call(card.fields, name)) err(card.line, `missing field '${name}'`);
    }
    for (const [name, value] of Object.entries(card.fields)) {
      if (value === '') err(card.line, `field '${name}' is empty`);
    }
    if (card.fields.evidence !== undefined && card.fields.evidence !== '' && !['measured', 'reported'].includes(card.fields.evidence)) {
      err(card.line, `evidence must be 'measured' or 'reported', not '${card.fields.evidence}'`);
    }
    if (card.fields.applies !== undefined && card.fields.applies !== '') {
      const list = card.fields.applies.split(',').map((s) => s.trim());
      if (list.some((s) => s === '')) err(card.line, 'applies has an empty entry');
      for (const a of list.filter(Boolean)) if (!memberSet.has(a)) err(card.line, `applies names '${a}', which is not a fleet member`);
      card.applies = [...new Set(list.filter(Boolean))].sort();
    } else {
      card.applies = null; // every board
    }
    cards.push(card);
    current = null;
  };

  lines.forEach((text, i) => {
    const line = i + 1;
    if (text.startsWith('## ')) {
      close();
      seenBlock = true;
      const heading = text.slice(3).trim();
      const slash = heading.indexOf('/');
      const owner = slash > 0 ? heading.slice(0, slash) : '';
      const local = slash > 0 ? heading.slice(slash + 1) : '';
      if (slash <= 0 || !ID_RE.test(local)) errors.push({ line, card: heading, reason: `card heading must be "## ${project}/<id>" with <id> matching ${ID_RE.source}` });
      else if (owner !== project) errors.push({ line, card: heading, reason: `card names project '${owner}' but sits in specs/${project}/cards.md; a project writes only its own cards` });
      current = { id: heading, line, headingText: text, body: [] };
      return;
    }
    if (text.startsWith('no-card:')) {
      close();
      seenBlock = true;
      const m = /^no-card:\s+(\S+)(?:\s+(.*))?$/.exec(text);
      if (!m || !INDEX_ID_RE.test(m[1])) errors.push({ line, card: null, reason: 'no-card line must read "no-card: T<line> <reason>"' });
      else if (!m[2] || !m[2].trim()) errors.push({ line, card: null, reason: `no-card ${m[1]} has no reason` });
      else noCards.push({ indexId: m[1], reason: m[2].trim(), line });
      return;
    }
    if (current) { current.body.push({ text, line }); return; }
    if (!seenBlock && cardLike(text)) {
      errors.push({ line, card: null, reason: `card-like text outside a card heading; a card starts with \`## <project>/<id>\` (here \`## ${project}/<id>\`)` });
      return;
    }
    if (seenBlock && text.trim() !== '') {
      errors.push({ line, card: null, reason: text.startsWith('# ')
        ? 'a "# " heading after the first card reads as a boundary to a human and not to this tool'
        : `text outside any card after the first card or no-card line; ${FIELD_HINT}` });
    }
  });
  close();

  const seen = new Map();
  for (const c of cards) {
    if (seen.has(c.id)) errors.push({ line: c.line, card: c.id, reason: `duplicate card id (first at line ${seen.get(c.id)})` });
    else seen.set(c.id, c.line);
  }
  const seenNo = new Map();
  for (const n of noCards) {
    if (seenNo.has(n.indexId)) errors.push({ line: n.line, card: null, reason: `duplicate no-card ${n.indexId} (first at line ${seenNo.get(n.indexId)})` });
    else seenNo.set(n.indexId, n.line);
  }
  errors.sort((a, b) => a.line - b.line);
  return { cards, noCards, errors };
}

// Every specs/<dir>/cards.md in a bus checkout, sorted by project.
export function findCardFiles(bus) {
  const specs = join(bus, 'specs');
  return readdirSync(specs, { withFileTypes: true })
    .filter((e) => e.isDirectory() && existsSync(join(specs, e.name, 'cards.md')))
    .map((e) => ({ project: e.name, path: `specs/${e.name}/cards.md`, abs: join(specs, e.name, 'cards.md') }))
    .sort((a, b) => (a.project < b.project ? -1 : a.project > b.project ? 1 : 0));
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) throw new Error(`unexpected argument '${a}'`);
    out[a.slice(2)] = (argv[i + 1] && !argv[i + 1].startsWith('--')) ? argv[++i] : true;
  }
  return out;
}

function main(argv) {
  const args = parseArgs(argv);
  const bus = resolve(args.bus || join(dirname(fileURLToPath(import.meta.url)), '..'));
  if (!existsSync(join(bus, 'RULINGS.md')) || !existsSync(join(bus, 'specs'))) throw new Error(`${bus} does not look like the doctrine bus`);
  const members = busMembers(bus);
  let files;
  if (args.file) {
    if (typeof args.project !== 'string') throw new Error('--file needs --project <id>');
    files = [{ project: args.project, path: String(args.file), abs: resolve(String(args.file)) }];
  } else {
    files = findCardFiles(bus);
  }
  let bad = 0, cards = 0, noCards = 0;
  const out = [];
  for (const f of files) {
    const result = parseCardsFile(readFileSync(f.abs, 'utf8'), f.project, members);
    cards += result.cards.length; noCards += result.noCards.length;
    for (const e of result.errors) {
      bad++;
      out.push(`INVALID ${f.path}:${e.line}${e.card ? ` [${e.card}]` : ''} ${e.reason}`);
    }
  }
  out.push(`[validate-cards] ${files.length} file(s), ${cards} card(s), ${noCards} no-card line(s), ${bad} problem(s).`);
  console.log(out.join('\n'));
  return bad ? EXIT_INVALID : EXIT_OK;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = main(process.argv.slice(2)); }
  catch (error) { console.error(`[validate-cards] ${error.message}`); process.exitCode = EXIT_FAIL; }
}
