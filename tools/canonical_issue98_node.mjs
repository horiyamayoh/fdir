/*
 * Independent Issue #98 canonicalization oracle.
 *
 * This file intentionally does not import the Python implementation or any
 * adapter.  It implements the small JSON wire contract directly so the
 * qualification runner can compare two language implementations on the same
 * authored bytes.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const DEFAULT_CONTRACT = path.join(ROOT, "machine", "canonicalization.json");
const FORBIDDEN_KEYS = new Set([
  "sourceBytes",
  "sourceByteStore",
  "contentAddressedSource",
  "semanticEquivalence",
  "EquivalenceCertificate",
  "LineageCertificate",
  "AccountingItem",
  "predicate",
]);

class OracleError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function fail(code, message) {
  throw new OracleError(code, message);
}

function skipWhitespace(text, index) {
  let cursor = index;
  while (cursor < text.length && /[\u0009\u000a\u000d\u0020]/u.test(text[cursor])) {
    cursor += 1;
  }
  return cursor;
}

class JsonKeyScanner {
  constructor(text) {
    this.text = text;
    this.length = text.length;
  }

  parseString(index) {
    if (this.text[index] !== '"') {
      fail("INVALID_JSON", `expected JSON string at offset ${index}`);
    }
    const start = index;
    let cursor = index + 1;
    while (cursor < this.length) {
      const character = this.text[cursor];
      if (character === '"') {
        const literal = this.text.slice(start, cursor + 1);
        try {
          return { value: JSON.parse(literal), next: cursor + 1 };
        } catch (error) {
          fail("INVALID_JSON", `invalid JSON string: ${error.message}`);
        }
      }
      if (character === "\\") {
        cursor += 1;
        if (cursor >= this.length) {
          fail("INVALID_JSON", "unterminated JSON escape");
        }
        if (this.text[cursor] === "u") {
          if (!/^[0-9a-fA-F]{4}$/u.test(this.text.slice(cursor + 1, cursor + 5))) {
            fail("INVALID_JSON", `invalid unicode escape at offset ${cursor}`);
          }
          cursor += 5;
        } else if ("\\\"/bfnrt".includes(this.text[cursor])) {
          cursor += 1;
        } else {
          fail("INVALID_JSON", `invalid JSON escape at offset ${cursor}`);
        }
        continue;
      }
      if (character < " ") {
        fail("INVALID_JSON", `control character in JSON string at offset ${cursor}`);
      }
      cursor += 1;
    }
    fail("INVALID_JSON", "unterminated JSON string");
  }

  parseNumber(index) {
    const match = this.text.slice(index).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/u);
    if (!match) {
      fail("INVALID_JSON", `invalid JSON number at offset ${index}`);
    }
    const token = match[0];
    if (token.includes(".") || token.includes("e") || token.includes("E")) {
      fail("FLOATING_POINT_NUMBER", `authoritative JSON number is not an integer: ${token}`);
    }
    return index + token.length;
  }

  parseValue(index) {
    const cursor = skipWhitespace(this.text, index);
    const character = this.text[cursor];
    if (character === '"') return this.parseString(cursor).next;
    if (character === "{") return this.parseObject(cursor);
    if (character === "[") return this.parseArray(cursor);
    if (this.text.startsWith("true", cursor)) return cursor + 4;
    if (this.text.startsWith("false", cursor)) return cursor + 5;
    if (this.text.startsWith("null", cursor)) return cursor + 4;
    if (character === "-" || /[0-9]/u.test(character || "")) return this.parseNumber(cursor);
    fail("INVALID_JSON", `unexpected JSON token at offset ${cursor}`);
  }

  parseObject(index) {
    let cursor = skipWhitespace(this.text, index + 1);
    const keys = new Set();
    if (this.text[cursor] === "}") return cursor + 1;
    while (cursor < this.length) {
      const parsed = this.parseString(cursor);
      if (keys.has(parsed.value)) {
        fail("DUPLICATE_OBJECT_KEY", `duplicate JSON object key: ${parsed.value}`);
      }
      keys.add(parsed.value);
      cursor = skipWhitespace(this.text, parsed.next);
      if (this.text[cursor] !== ":") {
        fail("INVALID_JSON", `expected colon at offset ${cursor}`);
      }
      cursor = this.parseValue(cursor + 1);
      cursor = skipWhitespace(this.text, cursor);
      if (this.text[cursor] === "}") return cursor + 1;
      if (this.text[cursor] !== ",") {
        fail("INVALID_JSON", `expected object separator at offset ${cursor}`);
      }
      cursor = skipWhitespace(this.text, cursor + 1);
    }
    fail("INVALID_JSON", "unterminated JSON object");
  }

  parseArray(index) {
    let cursor = skipWhitespace(this.text, index + 1);
    if (this.text[cursor] === "]") return cursor + 1;
    while (cursor < this.length) {
      cursor = this.parseValue(cursor);
      cursor = skipWhitespace(this.text, cursor);
      if (this.text[cursor] === "]") return cursor + 1;
      if (this.text[cursor] !== ",") {
        fail("INVALID_JSON", `expected array separator at offset ${cursor}`);
      }
      cursor = skipWhitespace(this.text, cursor + 1);
    }
    fail("INVALID_JSON", "unterminated JSON array");
  }

  scan() {
    const end = this.parseValue(0);
    if (skipWhitespace(this.text, end) !== this.length) {
      fail("INVALID_JSON", "trailing JSON data");
    }
  }
}

function parseAuthoredJson(raw) {
  if (raw.charCodeAt(0) === 0xfeff) {
    fail("BOM_NOT_ALLOWED", "UTF-8 BOM is outside the canonical input contract");
  }
  try {
    new JsonKeyScanner(raw).scan();
    return JSON.parse(raw);
  } catch (error) {
    if (error instanceof OracleError) throw error;
    fail("INVALID_JSON", error.message);
  }
}

function walk(value, pathText = "$") {
  if (Array.isArray(value)) {
    value.forEach((child, index) => walk(child, `${pathText}[${index}]`));
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      if (FORBIDDEN_KEYS.has(key)) fail("FORBIDDEN_KEY", `forbidden IR field at ${pathText}: ${key}`);
      walk(child, `${pathText}.${key}`);
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value) || !Number.isInteger(value)) {
      fail("FLOATING_POINT_NUMBER", `non-integer JSON number at ${pathText}`);
    }
  }
}

function utf16Compare(left, right) {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

function normalizeConfig(config) {
  const collections = new Map(Object.entries(config.entityCollections || {}));
  const projectionByName = new Map((config.projections || []).map((item) => [item.name, item]));
  return { collections, projectionByName };
}

function canonicalText(value, field, config) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value) || !Number.isInteger(value)) {
      fail("FLOATING_POINT_NUMBER", `non-integer JSON number in field ${field || "<root>"}`);
    }
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (Array.isArray(value)) {
    const normalized = value.map((item) => item);
    const idField = config.collections.get(field || "");
    if (idField && normalized.every((item) => item !== null && typeof item === "object" && !Array.isArray(item) && typeof item[idField] === "string")) {
      normalized.sort((left, right) => utf16Compare(left[idField], right[idField]));
    } else if (field === "items" && normalized.every((item) => item !== null && typeof item === "object" && Number.isInteger(item.ordinal) && item.ordinal >= 0)) {
      normalized.sort((left, right) => left.ordinal - right.ordinal);
    }
    return `[${normalized.map((item) => canonicalText(item, undefined, config)).join(",")}]`;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value).sort(utf16Compare);
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalText(value[key], key, config)}`).join(",")}}`;
  }
  fail("INVALID_JSON", `unsupported JSON value in field ${field || "<root>"}`);
}

function projectionValue(document, projection, config) {
  if (projection === "full") return document;
  const item = config.projectionByName.get(projection);
  let excludes = item?.excludes || [];
  if (item?.aliasOf) excludes = config.projectionByName.get(item.aliasOf)?.excludes || [];
  const copy = structuredClone(document);
  for (const key of excludes) delete copy[key];
  return copy;
}

export function canonicalizeRaw(raw, { projection = "full", config = null } = {}) {
  const contract = config || JSON.parse(fs.readFileSync(DEFAULT_CONTRACT, "utf8"));
  const normalizedConfig = normalizeConfig(contract);
  const value = parseAuthoredJson(raw);
  walk(value);
  const projected = projectionValue(value, projection, normalizedConfig);
  const bytes = Buffer.from(canonicalText(projected, undefined, normalizedConfig), "utf8");
  return {
    status: "accepted",
    canonicalBytesBase64: bytes.toString("base64"),
    canonicalUtf8: bytes.toString("utf8"),
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    hasTerminalLf: bytes.at(-1) === 0x0a,
  };
}

function parseArguments(argv) {
  const result = { input: null, stdin: false, projection: "full", contract: DEFAULT_CONTRACT };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--stdin") result.stdin = true;
    else if (argument === "--input") result.input = argv[++index];
    else if (argument === "--projection") result.projection = argv[++index];
    else if (argument === "--contract") result.contract = argv[++index];
    else if (argument === "--help") result.help = true;
    else fail("CLI_ARGUMENT", `unknown argument: ${argument}`);
  }
  return result;
}

function main() {
  try {
    const args = parseArguments(process.argv.slice(2));
    if (args.help || (!args.input && !args.stdin)) {
      process.stdout.write("usage: node canonical_issue98_node.mjs [--input FILE|--stdin] [--projection full|content|source-map-excluded]\n");
      return args.help ? 0 : 2;
    }
    const raw = args.stdin ? fs.readFileSync(0, "utf8") : fs.readFileSync(args.input, "utf8");
    const config = JSON.parse(fs.readFileSync(args.contract, "utf8"));
    process.stdout.write(`${JSON.stringify(canonicalizeRaw(raw, { projection: args.projection, config }))}\n`);
    return 0;
  } catch (error) {
    const payload = {
      status: "rejected",
      errorCode: error instanceof OracleError ? error.code : "ORACLE_ERROR",
      error: error instanceof Error ? error.message : String(error),
    };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    return 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}` || path.resolve(process.argv[1] || "") === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
