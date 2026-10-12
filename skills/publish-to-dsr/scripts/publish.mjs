#!/usr/bin/env node
/**
 * publish.mjs — publish to the NSLS Digital Sales Room (public page, Library
 * master, or an institution's room) with a person-bound publishing token.
 * Plain HTTPS: no AWS, no internal key, no deps. The token is never printed.
 */
import { readFileSync, statSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  parseArgs, validateArgs, normalize, validateHtml, validatePdf, validateSize, resolveToken,
  buildBody, checkPayload, validateEffectiveDeckRole, describeResponse, resolveStage, UsageError, ENDPOINT_PATH,
} from "./lib.mjs";

const TOKEN_FILE = join(homedir(), ".config", "nsls", "publish-token");

const USAGE = `Usage: node publish.mjs --target public|library|room (required) --kind html|pdf|link|built --title <t> [options]

  public : --kind html --slug <slug> --file <page.html>
  library: --kind pdf --file <doc.pdf>   |   --kind link --url <https://...>
  room   : (--institution <name> | --group-id <uuid>) --visibility room|staff
           --kind pdf --file <f.pdf> | link --url <u> | html --file <f.html>
           | built --manifest-id <id> --config <cfg.json> [--manifest-version <v>]
                   [--notes <notes.json>] [--artifact <path> --artifact-content-type html|pdf
                   [--deck-role prospect|presenter] [--target-doc-id <uuid>] [--deck-pair-id <uuid>]]
           [--also-library]
  common : [--description <text>] [--token <t>] [--stage staging|production] [--allow-production]

Defaults to STAGING. Production requires BOTH --stage production and --allow-production.
Token: --token, else $NSLS_PUBLISH_TOKEN, else ${TOKEN_FILE}
Slug:  lowercase letters/digits/single hyphens, max 80, "ph" reserved.
Size:  ~6MB per request in practice (base64 inflates ~33%): a PDF over ~4MB is too big.
--target is REQUIRED (never defaulted).
Library does not take html/built: use --target room ... --also-library.`;

function fail(msg, code = 1) {
  console.error(`\n${msg}\n`);
  process.exit(code);
}

function readBytes(path, flag) {
  try {
    const size = statSync(path).size;
    const sizeErr = validateSize(size);
    if (sizeErr) fail(`${flag}: ${sizeErr}`);
    return readFileSync(path);
  } catch (e) {
    fail(`Could not read ${flag} "${path}": ${e.message}`);
  }
}

function readJsonObject(path, flag) {
  const raw = readBytes(path, flag).toString("utf8");
  let v;
  try { v = JSON.parse(raw); } catch (e) { fail(`${flag} "${path}" is not valid JSON: ${e.message}`); }
  if (v === null || typeof v !== "object" || Array.isArray(v)) fail(`${flag} "${path}" must contain a JSON object.`);
  return v;
}

function readHtml(path, flag) {
  const buf = readBytes(path, flag);
  const err = validateHtml(buf.toString("utf8"), buf.length);
  if (err) fail(`${flag}: ${err}`);
  return buf.toString("utf8");
}

function readPdf(path, flag) {
  const buf = readBytes(path, flag);
  const err = validatePdf(buf);
  if (err) fail(`${flag}: ${err}`);
  return buf;
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (e) {
    if (e instanceof UsageError) fail(`${e.message}\n\n${USAGE}`, 2);
    throw e;
  }
  if (args.help) { console.log(USAGE); return; }

  const argErr = validateArgs(args);
  if (argErr) fail(`${argErr}\n\n${USAGE}`, 2);
  const st = resolveStage(args.stage, args.allowProduction);
  if (!st.ok) fail(st.message, 2);
  const a = normalize(args);

  // Read + validate every input BEFORE the token is touched or any network call.
  const inputs = {};
  let bytes = 0;
  if (a.target === "public" || (a.target === "room" && a.kind === "html")) {
    inputs.text = readHtml(a.file, "--file");
    bytes = Buffer.byteLength(inputs.text);
  } else if (a.kind === "pdf") {
    inputs.bytes = readPdf(a.file, "--file");
    bytes = inputs.bytes.length;
  } else if (a.kind === "built") {
    inputs.config = readJsonObject(a.config, "--config");
    if (a.notes) inputs.notes = readJsonObject(a.notes, "--notes");
    if (a.artifact) {
      if (a.artifactContentType === "pdf") inputs.artifactBytes = readPdf(a.artifact, "--artifact");
      else inputs.artifactText = readHtml(a.artifact, "--artifact");
      bytes = (inputs.artifactBytes ?? Buffer.from(inputs.artifactText)).length;
    }
  }

  let fileContents;
  try { fileContents = readFileSync(TOKEN_FILE, "utf8"); } catch { /* optional */ }
  const resolved = resolveToken({ flag: args.token, env: process.env.NSLS_PUBLISH_TOKEN, fileContents });
  if (!resolved) {
    fail(
      "No publishing token found.\n" +
      "Generate one from the dashboard (Marketing Pages / publishing tokens), then either:\n" +
      "  export NSLS_PUBLISH_TOKEN=...   (this shell)\n" +
      `  save it to ${TOKEN_FILE}   (chmod 600)\n` +
      "  or pass --token <t>\nTokens are per environment (staging vs production).",
      2,
    );
  }

  const roleErr = a.kind === "built"
    ? validateEffectiveDeckRole({ config: inputs.config, deckRole: a.deckRole, visibility: a.visibility })
    : null;
  if (roleErr) fail(roleErr, 2);

  const body = buildBody(args, resolved.token, inputs);
  const json = JSON.stringify(body);
  const sizeErr = checkPayload(json);
  if (sizeErr) fail(sizeErr);
  if (st.stage === "production") console.warn("\nPRODUCTION: publishing to the live site.\n");
  console.log(`publish-to-dsr — stage: ${st.stage}, target: ${a.target}, kind: ${a.kind}, ${(bytes / 1024).toFixed(1)} KB (token from ${resolved.source})`);

  let res;
  try {
    res = await fetch(st.baseUrl + ENDPOINT_PATH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: json,
      signal: AbortSignal.timeout(120_000),
      redirect: "error", // the token is in the body: never re-send it to a redirect host
    });
  } catch (e) {
    fail(`Network error: ${e.message}\nThe request may or may not have reached the server — check the destination (docs.nsls.org / library / room) before retrying.`);
  }
  let out;
  try { out = await res.json(); } catch { out = undefined; }
  const result = describeResponse(res.status, out);
  if (!result.ok) fail(result.message);
  console.log(`\n${result.message}\n`);
}

let isMain = false;
try { isMain = realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1]); } catch { /* not a CLI run */ }
if (isMain) {
  main().catch((e) => fail(`Unexpected error: ${e.message}`));
}
