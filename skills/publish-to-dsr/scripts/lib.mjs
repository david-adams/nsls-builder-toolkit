/**
 * lib.mjs — pure logic for publish-to-dsr (no I/O beyond what's passed in).
 * Kept out of publish.mjs so it is unit-tested (see lib.test.mjs).
 *
 * Request shape mirrors parsePublishBody in invitation-dashboard
 * src/lib/publish-by-token.ts (POST /api/portal/public-pages/publish-by-token).
 */

export const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const SLUG_MAX = 80;
export const RESERVED_SLUGS = new Set(["ph"]);
export const TITLE_MAX = 200;
export const DESCRIPTION_MAX = 2000;
export const URL_MAX = 2048;
/**
 * The dashboard runs on OpenNext/Lambda behind CloudFront: the real hard bound is
 * ~6 MB per REQUEST, and base64 inflates binary content ~33% (a PDF over ~4 MB
 * will not fit once encoded). The server's own 25 MB document cap is unreachable
 * through this API, so we refuse locally past ~5.5 MB.
 */
export const MAX_REQUEST_BYTES = Math.floor(5.5 * 1024 * 1024);
export const MAX_BYTES = MAX_REQUEST_BYTES;
export const TOO_LARGE_MSG =
  "the content is too large to publish over the token API (~6MB request limit); a PDF over ~4MB won't fit once encoded";

export const TARGETS = ["public", "library", "room"];
export const KINDS = ["html", "pdf", "link", "built"];
export const VISIBILITIES = ["room", "staff"];
export const ARTIFACT_TYPES = { html: "text/html", pdf: "application/pdf" };
export const DECK_ROLES = ["prospect", "presenter"];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const BASE_URLS = {
  staging: "https://staging.invitations.nsls.org",
  production: "https://invitations.nsls.org",
};
export const ENDPOINT_PATH = "/api/portal/public-pages/publish-by-token";

export class UsageError extends Error {}

const VALUE_FLAGS = [
  "target", "kind", "title", "description", "slug", "institution", "group-id", "visibility",
  "file", "url", "manifest-id", "manifest-version", "config", "notes", "artifact",
  "artifact-content-type", "deck-role", "target-doc-id", "deck-pair-id", "token", "stage",
];
const BOOL_FLAGS = { "--allow-production": "allowProduction", "--also-library": "alsoLibrary" };

const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

export function parseArgs(argv) {
  const out = { stage: "staging", allowProduction: false, alsoLibrary: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") { out.help = true; continue; }
    if (Object.hasOwn(BOOL_FLAGS, arg)) { out[BOOL_FLAGS[arg]] = true; continue; }
    let name, value;
    if (arg.startsWith("--") && arg.includes("=")) {
      name = arg.slice(2, arg.indexOf("="));
      value = arg.slice(arg.indexOf("=") + 1);
    } else if (arg.startsWith("--")) {
      name = arg.slice(2);
      value = argv[++i];
      if (value === undefined || value.startsWith("--")) throw new UsageError(`--${name} needs a value`);
    } else {
      throw new UsageError("Unexpected positional argument (did you forget a --flag name?)");
    }
    if (!VALUE_FLAGS.includes(name)) throw new UsageError(`Unknown flag --${name}`);
    out[camel(name)] = value;
  }
  return out;
}

/** Returns an error string, or null when the slug is valid. */
export function validateSlug(slug) {
  if (!slug) return "--slug is required for --target public";
  if (slug.length > SLUG_MAX) return `Slug is ${slug.length} characters; the maximum is ${SLUG_MAX}.`;
  if (!SLUG_RE.test(slug)) {
    return `Slug "${slug}" is invalid. Use lowercase letters, digits and single hyphens only (e.g. fall-2026-launch), no leading/trailing hyphen.`;
  }
  if (RESERVED_SLUGS.has(slug)) return `Slug "${slug}" is reserved. Pick another.`;
  return null;
}

export function validateMeta({ title, description }) {
  if (!title || !title.trim()) return "--title is required";
  if (title.length > TITLE_MAX) return `Title is ${title.length} characters; the maximum is ${TITLE_MAX}.`;
  if (description !== undefined && description.length > DESCRIPTION_MAX) {
    return `Description is ${description.length} characters; the maximum is ${DESCRIPTION_MAX}.`;
  }
  return null;
}

export function validateUrl(url) {
  if (!url) return "--url is required for --kind link";
  if (url.length > URL_MAX) return `URL is ${url.length} characters; the maximum is ${URL_MAX}.`;
  try {
    const p = new URL(url);
    if (p.protocol !== "https:" && p.protocol !== "http:") throw new Error("scheme");
  } catch {
    return `--url must be a valid http(s) URL (got "${url}").`;
  }
  return null;
}

export function validateSize(sizeBytes) {
  if (sizeBytes > MAX_BYTES) {
    return `File is ${(sizeBytes / 1048576).toFixed(1)} MB: ${TOO_LARGE_MSG}.`;
  }
  return null;
}

/** Non-empty, within size cap, and looks like HTML. External https: references are allowed. */
export function validateHtml(html, sizeBytes = Buffer.byteLength(html, "utf8")) {
  if (!html || !html.trim()) return "The HTML file is empty.";
  const sizeErr = validateSize(sizeBytes);
  if (sizeErr) return sizeErr;
  const stripped = html.replace(/^﻿/, "").replace(/^(?:\s|<!--[\s\S]*?-->)+/, "");
  if (!/^<!doctype html/i.test(stripped) && !/^<html[\s>]/i.test(stripped)) {
    return "File does not look like HTML (expected it to start, after BOM/whitespace/comments, with <!doctype html> or <html>).";
  }
  return null;
}

export function validatePdf(buf) {
  if (!buf || buf.length === 0) return "The PDF file is empty.";
  const sizeErr = validateSize(buf.length);
  if (sizeErr) return sizeErr;
  if (buf.subarray(0, 1024).indexOf("%PDF-") === -1) return "File does not look like a PDF (no %PDF- header).";
  return null;
}

/** Token precedence: flag, then env, then file contents. Returns {token, source} or null. */
export function resolveToken({ flag, env, fileContents }) {
  for (const [source, v] of [["--token flag", flag], ["NSLS_PUBLISH_TOKEN", env], ["config file", fileContents]]) {
    const t = typeof v === "string" ? v.trim() : "";
    if (t) return { token: t, source };
  }
  return null;
}

/**
 * Resolve --stage and enforce the production guard.
 * Default staging; production needs BOTH stage=production and allowProduction.
 */
export function resolveStage(stage = "staging", allowProduction = false) {
  if (typeof stage !== "string" || !Object.hasOwn(BASE_URLS, stage)) {
    return { ok: false, message: `--stage must be "staging" or "production" (got "${stage}").` };
  }
  if (stage === "production" && !allowProduction) {
    return {
      ok: false,
      message:
        "REFUSING to publish to production.\n" +
        "This puts content live in the production Digital Sales Room / public docs site. If you really mean it, re-run with BOTH:\n" +
        "  --stage production --allow-production",
    };
  }
  return { ok: true, stage, baseUrl: BASE_URLS[stage] };
}

/** Refuse a serialized request body that would not fit the real ~6 MB request limit. */
export function checkPayload(json) {
  const n = Buffer.byteLength(json, "utf8");
  return n > MAX_REQUEST_BYTES ? `Request is ${(n / 1048576).toFixed(1)} MB once encoded: ${TOO_LARGE_MSG}.` : null;
}

/**
 * The deck role that will actually be sent: --deck-role wins, else config.artifact.
 * Enforced on this EFFECTIVE value so a config FILE saying "presenter" cannot
 * bypass the staff-visibility rule. Returns an error string or null.
 */
export function validateEffectiveDeckRole({ config, deckRole, visibility }) {
  const role = deckRole ?? config?.artifact;
  if (role === undefined) return null;
  if (role === "presenter" && visibility !== "staff") return "A presenter deck (config.artifact / --deck-role presenter) carries rep-only notes and requires --visibility staff.";
  if (role === "prospect" && visibility === "staff") return "A prospect deck (config.artifact / --deck-role prospect) cannot be --visibility staff.";
  return null;
}

/**
 * Pure flag validation, BEFORE any file or network I/O. Returns an error string or null.
 * `args` is the parseArgs output (target/kind default applied by the caller via normalize()).
 */
export function validateArgs(raw) {
  const a = normalize(raw);
  if (a.target === undefined) return `--target is required (${TARGETS.join(" | ")}); it is never defaulted, so nothing is published world-readable by accident.`;
  if (!TARGETS.includes(a.target)) return `--target must be one of ${TARGETS.join(" | ")} (got "${a.target}").`;
  if (!a.kind) return `--kind is required for --target ${a.target} (one of ${KINDS.join(" | ")}).`;
  if (!KINDS.includes(a.kind)) return `--kind must be one of ${KINDS.join(" | ")} (got "${a.kind}").`;
  const metaErr = validateMeta(a);
  if (metaErr) return metaErr;

  const set = (...names) => names.filter((n) => raw[n] !== undefined && raw[n] !== false);
  const forbid = (flags, why) => {
    const bad = set(...flags);
    return bad.length ? `${bad.map((n) => "--" + n.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())).join(", ")} not allowed here (${why})` : null;
  };
  const builtOnly = ["manifestId", "manifestVersion", "config", "notes", "artifact", "artifactContentType", "deckRole", "targetDocId", "deckPairId"];

  if (a.target === "public") {
    if (a.kind !== "html") return `--target public only supports --kind html (got "${a.kind}").`;
    const e = validateSlug(a.slug) || (a.file ? null : "--file <page.html> is required");
    if (e) return e;
    return forbid(["institution", "groupId", "visibility", "url", "alsoLibrary", ...builtOnly], "only apply to --target room or library (not public).");
  }
  if (a.slug !== undefined) return "--slug only applies to --target public.";
  if (a.target === "library") {
    if (a.kind === "html" || a.kind === "built") {
      return "--target library supports only --kind pdf or --kind link. For HTML or built decks use --target room ... --also-library.";
    }
    if (a.kind === "pdf" && !a.file) return "--file <doc.pdf> is required for --kind pdf";
    if (a.kind === "pdf" && a.url !== undefined) return "--url only applies to --kind link.";
    if (a.kind === "link") {
      const e = validateUrl(a.url);
      if (e) return e;
      if (a.file !== undefined) return "--file does not apply to --kind link.";
    }
    return forbid(["institution", "groupId", "visibility", "alsoLibrary", ...builtOnly], "only apply to --target room.");
  }
  // room
  if (a.institution !== undefined && !a.institution.trim()) return "--institution must not be empty.";
  if (a.institution && a.groupId) return "Give --institution OR --group-id, not both.";
  if (!a.institution && !a.groupId) return "--target room needs --institution <name> or --group-id <uuid>.";
  if (a.groupId && !UUID_RE.test(a.groupId)) return `--group-id must be a uuid (got "${a.groupId}").`;
  if (!VISIBILITIES.includes(a.visibility)) return "--target room needs --visibility room|staff.";
  if (a.kind === "link") {
    const e = validateUrl(a.url);
    if (e) return e;
    if (a.file !== undefined) return "--file does not apply to --kind link.";
  } else if (!a.file && a.kind !== "built") {
    return `--file is required for --kind ${a.kind}`;
  }
  if (a.kind !== "link" && a.url !== undefined) return "--url only applies to --kind link.";
  if (a.kind !== "built") {
    const e = forbid(builtOnly, "only apply to --kind built.");
    if (e) return e;
    return null;
  }
  // built
  if (a.file !== undefined) return "--file does not apply to --kind built; use --artifact <path> (and --config).";
  if (!a.manifestId || !a.manifestId.trim() || a.manifestId.length > 200) return "--manifest-id is required for --kind built (max 200 chars).";
  if (a.manifestVersion !== undefined && a.manifestVersion.length > 100) return "--manifest-version is too long (max 100 chars).";
  if (!a.config) return "--config <path.json> is required for --kind built.";
  if (a.artifact && !a.artifactContentType) return "--artifact needs --artifact-content-type html|pdf.";
  if (a.artifactContentType !== undefined) {
    if (!Object.hasOwn(ARTIFACT_TYPES, a.artifactContentType)) return "--artifact-content-type must be html or pdf.";
    if (!a.artifact) return "--artifact-content-type needs --artifact <path>.";
  }
  if (!a.artifact) {
    if (a.alsoLibrary) return "--also-library on a built document needs --artifact (the delivered file is what gets filed).";
    if (a.targetDocId || a.deckPairId) return "--target-doc-id / --deck-pair-id only apply with --artifact.";
    if (a.deckRole) return "--deck-role only applies with --artifact.";
  }
  for (const k of ["targetDocId", "deckPairId"]) {
    if (a[k] !== undefined && !UUID_RE.test(a[k])) return `--${k === "targetDocId" ? "target-doc-id" : "deck-pair-id"} must be a uuid.`;
  }
  if (a.deckRole !== undefined) {
    if (!DECK_ROLES.includes(a.deckRole)) return `--deck-role must be one of ${DECK_ROLES.join(" | ")}.`;
    // The presenter carries rep-only content; a prospect deck hidden from staff-only would never reach the school.
    if (a.deckRole === "presenter" && a.visibility !== "staff") return "--deck-role presenter requires --visibility staff (it carries rep-only notes).";
    if (a.deckRole === "prospect" && a.visibility === "staff") return "--deck-role prospect cannot be --visibility staff.";
  }
  return null;
}

/** Apply defaults: kind defaults to html for public only. --target is never defaulted. */
export function normalize(a) {
  const target = a.target;
  const kind = a.kind ?? (target === "public" ? "html" : undefined);
  return { ...a, target, kind };
}

/** Build config sent to the server: --notes OVERRIDES config.notes; --deck-role sets config.artifact. */
export function buildDeckConfig({ config, notes, deckRole }) {
  const out = { ...config, notes: notes ?? config.notes ?? {} };
  if (deckRole) out.artifact = deckRole;
  return out;
}

export const toBase64 = (buf) => Buffer.from(buf).toString("base64");

/**
 * Build the JSON body. `inputs` carries already-read file content:
 *   { text?: string (html), bytes?: Buffer (pdf), config?: object, notes?: object,
 *     artifactText?: string, artifactBytes?: Buffer }
 */
export function buildBody(rawArgs, token, inputs = {}) {
  const a = normalize(rawArgs);
  const body = { token, target: a.target, kind: a.kind, title: a.title.trim() };
  if (a.description !== undefined && a.description.trim()) body.description = a.description.trim();
  if (a.target === "public") {
    body.slug = a.slug;
    body.html = inputs.text;
    return body;
  }
  if (a.target === "room") {
    if (a.groupId) body.groupId = a.groupId.toLowerCase(); else body.institution = a.institution.trim();
    body.visibility = a.visibility;
    if (a.alsoLibrary) body.addToLibrary = true;
  }
  if (a.kind === "link") body.url = a.url;
  else if (a.kind === "pdf") body.file = toBase64(inputs.bytes);
  else if (a.kind === "html") body.html = inputs.text;
  else {
    body.manifestId = a.manifestId.trim();
    if (a.manifestVersion) body.manifestVersion = a.manifestVersion;
    body.config = buildDeckConfig({ config: inputs.config, notes: inputs.notes, deckRole: a.deckRole });
    if (a.artifact) {
      const art = { contentType: ARTIFACT_TYPES[a.artifactContentType] };
      if (a.artifactContentType === "pdf") art.file = toBase64(inputs.artifactBytes);
      else art.html = inputs.artifactText;
      if (a.targetDocId) art.targetDocId = a.targetDocId.toLowerCase();
      if (a.deckPairId) art.deckPairId = a.deckPairId.toLowerCase();
      body.artifact = art;
    }
  }
  return body;
}

export function describeResponse(status, body) {
  const raw = body && typeof body === "object" ? (typeof body.error === "string" ? body.error : typeof body.detail === "string" ? body.detail : "") : "";
  const detail = raw ? ` (${raw})` : "";
  switch (status) {
    case 200: {
      if (body === null || typeof body !== "object" || Array.isArray(body)) {
        return { ok: false, message: "UNVERIFIED: the server returned success but no details — check docs.nsls.org / the library / the institution's room before assuming it published (and before retrying)." };
      }
      const parts = ["Published."];
      if (body?.publicUrl) parts.push(body.publicUrl);
      if (body?.institutionName) parts.push(`Room: ${body.institutionName}.`);
      const id = body?.document?.id;
      if (typeof id === "string") parts.push(`Document id: ${id} (pass as --target-doc-id to update a built deck).`);
      return { ok: true, message: parts.join(" ") };
    }
    case 401:
      return { ok: false, message: "Token rejected: invalid, expired, or revoked, or you no longer have publish permission. Tokens are per environment (staging vs production). Generate a new one from the dashboard." };
    case 400:
      return { ok: false, message: `The server rejected the request as malformed${detail}. Check the flags and files.` };
    case 413:
      return { ok: false, message: "The content is too large for the server. Reduce its size (host big images/video elsewhere)." };
    case 409:
      if (raw.includes("title_exists") || body?.code === "title_exists") {
        return { ok: false, message: "A document with that title already exists in this room. To update a built deck, re-run with --target-doc-id <existing document id> (the id printed when it was first published, or shown in the room). Otherwise pick a different --title." };
      }
      return { ok: false, message: `That slug or title is already taken${detail}. Check it first — if it is your earlier attempt, it already published. Otherwise choose a different slug/title.` };
    case 502:
      return { ok: false, message: "Upstream error or ambiguous result. The content MAY ALREADY BE PUBLISHED — check docs.nsls.org (public) or the library / institution room BEFORE retrying (a retry would 409 for something that did publish). If the server says it could not verify the token, that is transient and a retry is safe." + (raw ? ` Server said: ${raw}` : "") };
    default:
      return { ok: false, message: `Unexpected response ${status}${detail}. Do not retry blindly; check the destination first.` };
  }
}
