import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseArgs, validateArgs, validateSlug, validateMeta, validateHtml, validatePdf, validateUrl,
  resolveToken, describeResponse, checkPayload, validateEffectiveDeckRole, MAX_REQUEST_BYTES, resolveStage, buildBody, buildDeckConfig, UsageError, BASE_URLS,
} from "./lib.mjs";

const U1 = "11111111-1111-4111-8111-111111111111";
const U2 = "22222222-2222-4222-8222-222222222222";
const v = (o) => validateArgs(parseArgs(Object.entries(o).filter(([, x]) => x !== undefined).flatMap(([k, val]) => (val === true ? [`--${k}`] : [`--${k}`, val]))));
const room = { target: "room", institution: "UW", visibility: "room", title: "T" };

test("slug rules", () => {
  for (const ok of ["a", "fall-2026", "a1-b2-c3", "a".repeat(80)]) assert.equal(validateSlug(ok), null, ok);
  for (const bad of ["", undefined, "Fall", "a--b", "-a", "a-", "a_b", "a b", "ph", "a".repeat(81)]) assert.ok(validateSlug(bad), String(bad));
});
test("meta caps", () => {
  assert.ok(validateMeta({ title: "" }));
  assert.ok(validateMeta({ title: "x".repeat(201) }));
  assert.ok(validateMeta({ title: "t", description: "x".repeat(2001) }));
  assert.equal(validateMeta({ title: "x".repeat(200), description: "x".repeat(2000) }), null);
});
test("html + pdf + url checks", () => {
  assert.equal(validateHtml("<!DOCTYPE html><p>"), null);
  assert.ok(validateHtml("")); assert.ok(validateHtml("text"));
  assert.ok(validateHtml("<html>", 26 * 1024 * 1024));
  assert.equal(validatePdf(Buffer.from("%PDF-1.4 x")), null);
  assert.ok(validatePdf(Buffer.from("nope"))); assert.ok(validatePdf(Buffer.alloc(0)));
  assert.equal(validateUrl("https://a.com/x"), null);
  for (const b of ["", "ftp://a.com", "javascript:alert(1)", "not a url", undefined]) assert.ok(validateUrl(b), String(b));
});

test("public: defaults, slug, file", () => {
  assert.equal(v({ target: "public", title: "T", slug: "ok", file: "p.html" }), null);
  assert.match(v({ target: "public", title: "T", file: "p.html" }), /slug/);
  assert.match(v({ target: "public", title: "T", slug: "Bad", file: "p.html" }), /invalid/);
  assert.match(v({ target: "public", title: "T", slug: "ph", file: "p.html" }), /reserved/);
  assert.match(v({ target: "public", title: "T", slug: "ok" }), /--file/);
  assert.match(v({ target: "public", kind: "pdf", title: "T", slug: "ok", file: "p" }), /only supports --kind html/);
  assert.match(v({ target: "public", title: "T", slug: "ok", file: "p.html", visibility: "room" }), /not allowed here/);
});

test("library: pdf/link only; html and built rejected", () => {
  assert.equal(v({ target: "library", kind: "pdf", title: "T", file: "a.pdf" }), null);
  assert.equal(v({ target: "library", kind: "link", title: "T", url: "https://a.com" }), null);
  for (const kind of ["html", "built"]) {
    assert.match(v({ target: "library", kind, title: "T", file: "x" }), /--target room .* --also-library/, kind);
  }
  assert.match(v({ target: "library", title: "T", file: "a.pdf" }), /--kind is required/);
  assert.match(v({ target: "library", kind: "pdf", title: "T" }), /--file/);
  assert.match(v({ target: "library", kind: "link", title: "T" }), /--url/);
  assert.match(v({ target: "library", kind: "link", title: "T", url: "ftp://x" }), /http/);
  assert.match(v({ target: "library", kind: "pdf", title: "T", file: "a.pdf", slug: "x" }), /slug/);
  assert.match(v({ target: "library", kind: "pdf", title: "T", file: "a.pdf", "also-library": true }), /not allowed here/);
});

test("unknown target/kind refused", () => {
  assert.match(v({ target: "prototype", kind: "html", title: "T" }), /--target must be/);
  assert.match(v({ target: "room", kind: "deck", title: "T" }), /--kind must be/);
});

test("room: institution/group, visibility, kinds", () => {
  assert.equal(v({ ...room, kind: "pdf", file: "a.pdf" }), null);
  assert.equal(v({ ...room, kind: "html", file: "a.html", "also-library": true }), null);
  assert.equal(v({ ...room, kind: "link", url: "https://a.com" }), null);
  assert.equal(v({ target: "room", "group-id": U1, visibility: "staff", kind: "pdf", file: "a.pdf", title: "T" }), null);
  assert.match(v({ ...room, institution: undefined, kind: "pdf", file: "a" }) ?? "", /institution|group-id/);
  assert.match(v({ target: "room", visibility: "room", kind: "pdf", file: "a", title: "T" }), /institution/);
  assert.match(v({ ...room, "group-id": U1, kind: "pdf", file: "a" }), /not both/);
  assert.match(v({ ...room, institution: undefined, "group-id": "nope", kind: "pdf", file: "a" }) ?? "", /uuid/);
  assert.match(v({ ...room, visibility: "public", kind: "pdf", file: "a" }), /visibility/);
  assert.match(v({ ...room, kind: "pdf" }), /--file/);
  assert.match(v({ ...room, kind: "pdf", file: "a", "manifest-id": "x" }), /not allowed here/);
});

const built = { ...room, kind: "built", "manifest-id": "society-sales-deck", config: "c.json" };
test("built: required + cross-flag rules", () => {
  assert.equal(v(built), null);
  assert.match(v({ ...built, "manifest-id": undefined }) ?? "", /manifest-id/);
  assert.match(v({ ...built, config: undefined }) ?? "", /--config/);
  assert.match(v({ ...built, file: "a.html" }), /--artifact/);
  assert.match(v({ ...built, artifact: "a.html" }), /content-type/);
  assert.match(v({ ...built, "artifact-content-type": "html" }), /needs --artifact/);
  assert.match(v({ ...built, artifact: "a", "artifact-content-type": "docx" }), /html or pdf/);
  assert.match(v({ ...built, "also-library": true }), /needs --artifact/);
  assert.match(v({ ...built, "target-doc-id": U1 }), /only apply with --artifact/);
  const art = { ...built, artifact: "a.html", "artifact-content-type": "html" };
  assert.equal(v({ ...art, "also-library": true, "target-doc-id": U1, "deck-pair-id": U2 }), null);
  assert.match(v({ ...art, "target-doc-id": "x" }), /uuid/);
  // presenter ⇒ staff; prospect ⇏ staff
  assert.match(v({ ...art, "deck-role": "presenter" }), /requires --visibility staff/);
  assert.equal(v({ ...art, "deck-role": "presenter", visibility: "staff" }), null);
  assert.match(v({ ...art, "deck-role": "prospect", visibility: "staff" }), /cannot be/);
  assert.equal(v({ ...art, "deck-role": "prospect" }), null);
  assert.match(v({ ...art, "deck-role": "boss" }), /deck-role/);
});

test("production guard uses own-property lookup", () => {
  assert.equal(resolveStage().stage, "staging");
  assert.equal(resolveStage("staging").baseUrl, BASE_URLS.staging);
  const refused = resolveStage("production", false);
  assert.equal(refused.ok, false); assert.match(refused.message, /--allow-production/);
  assert.equal(resolveStage("production", true).baseUrl, BASE_URLS.production);
  for (const s of ["prod", "", "toString", "__proto__", "constructor", "hasOwnProperty"]) assert.equal(resolveStage(s, true).ok, false, s);
});

test("token precedence", () => {
  assert.equal(resolveToken({ flag: "f", env: "e", fileContents: "c" }).token, "f");
  assert.equal(resolveToken({ env: "e", fileContents: "c" }).token, "e");
  assert.equal(resolveToken({ fileContents: " c\n" }).token, "c");
  assert.equal(resolveToken({ flag: " ", env: "", fileContents: "" }), null);
});

test("parseArgs", () => {
  const a = parseArgs(["--group-id", U1, "--also-library", "--allow-production", "--title=T"]);
  assert.equal(a.groupId, U1); assert.equal(a.alsoLibrary, true); assert.equal(a.allowProduction, true); assert.equal(a.title, "T");
  assert.equal(parseArgs([]).stage, "staging");
  assert.throws(() => parseArgs(["--nope", "x"]), UsageError);
  assert.throws(() => parseArgs(["--title"]), UsageError);
  assert.throws(() => parseArgs(["--title", "--stage"]), UsageError);
  assert.throws(() => parseArgs(["stray"]), UsageError);
  assert.throws(() => parseArgs(["SECRET-POSITIONAL-TOKEN"]), (e) => e instanceof UsageError && !e.message.includes("SECRET-POSITIONAL-TOKEN") && /positional/.test(e.message));
  assert.throws(() => parseArgs(["--__proto__", "x"]), UsageError);
});

test("buildBody: public html as text", () => {
  const b = buildBody({ target: "public", title: " T ", slug: "ok", description: "d" }, "tok", { text: "<html>" });
  assert.deepEqual(b, { token: "tok", target: "public", kind: "html", title: "T", description: "d", slug: "ok", html: "<html>" });
});
test("buildBody: pdf is base64", () => {
  const bytes = Buffer.from([0x25, 0x50, 0x44, 0x46, 0xff, 0x00]);
  const b = buildBody({ target: "library", kind: "pdf", title: "T" }, "tok", { bytes });
  assert.equal(b.file, bytes.toString("base64"));
  assert.deepEqual(Buffer.from(b.file, "base64"), bytes);
  assert.equal(b.html, undefined);
});
test("buildBody: room link / groupId / addToLibrary", () => {
  const b = buildBody({ ...room, institution: undefined, groupId: U1.toUpperCase(), kind: "link", url: "https://a.com", alsoLibrary: true }, "tok");
  assert.equal(b.groupId, U1); assert.equal(b.institution, undefined);
  assert.equal(b.addToLibrary, true); assert.equal(b.url, "https://a.com"); assert.equal(b.visibility, "room");
  assert.equal(buildBody({ ...room, kind: "link", url: "https://a.com" }, "t").addToLibrary, undefined);
});
test("buildBody: built with pdf and html artifacts, notes merge", () => {
  const base = { ...room, kind: "built", manifestId: "m", manifestVersion: "v1", targetDocId: U1, deckPairId: U2 };
  const pdf = Buffer.from("%PDF-1");
  const p = buildBody({ ...base, artifact: "a", artifactContentType: "pdf", alsoLibrary: true }, "t", { config: { x: 1, notes: { a: "old" } }, notes: { a: "new" }, artifactBytes: pdf });
  assert.deepEqual(p.artifact, { contentType: "application/pdf", file: pdf.toString("base64"), targetDocId: U1, deckPairId: U2 });
  assert.deepEqual(p.config, { x: 1, notes: { a: "new" } });
  assert.equal(p.manifestVersion, "v1"); assert.equal(p.addToLibrary, true);
  const h = buildBody({ ...base, artifact: "a", artifactContentType: "html", deckRole: "presenter", targetDocId: undefined, deckPairId: undefined }, "t", { config: {}, artifactText: "<html>" });
  assert.deepEqual(h.artifact, { contentType: "text/html", html: "<html>" });
  assert.equal(h.config.artifact, "presenter");
  assert.deepEqual(buildDeckConfig({ config: { notes: { k: 1 } } }), { notes: { k: 1 } });
  assert.deepEqual(buildDeckConfig({ config: {} }), { notes: {} });
});

test("status messages", () => {
  const ok = describeResponse(200, { publicUrl: "https://docs.nsls.org/x", institutionName: "UW", document: { id: U1 } });
  assert.equal(ok.ok, true); assert.match(ok.message, /docs\.nsls\.org\/x/); assert.match(ok.message, /UW/); assert.match(ok.message, new RegExp(U1));
  assert.match(describeResponse(401, {}).message, /per environment/);
  assert.match(describeResponse(400, { error: "bad thing" }).message, /bad thing/);
  assert.match(describeResponse(413, {}).message, /too large/);
  const t = describeResponse(409, { error: "title_exists" });
  assert.match(t.message, /--target-doc-id/);
  assert.match(describeResponse(409, { code: "title_exists" }).message, /--target-doc-id/);
  assert.match(describeResponse(409, { error: "slug taken" }).message, /slug or title/);
  assert.match(describeResponse(502, {}).message, /BEFORE retrying/);
  assert.equal(describeResponse(500, undefined).ok, false);
  assert.equal(describeResponse(200, undefined).ok, false);
});

test("token never appears in any message", () => {
  const TOKEN = "SECRET-TOKEN-abc123";
  const msgs = [401, 400, 409, 413, 502, 500, 200].map((s) => describeResponse(s, { error: "x" }).message);
  msgs.push(resolveStage("production", false).message, validateArgs({ target: "library", kind: "html", title: "T", token: TOKEN }));
  for (const m of msgs) assert.ok(!String(m).includes(TOKEN));
  assert.throws(() => parseArgs(["--token"]), (e) => !e.message.includes(TOKEN));
});

test("--target is required, never defaulted", () => {
  assert.match(v({ kind: "html", title: "T", slug: "ok", file: "p.html" }), /--target is required/);
  assert.match(v({ title: "T", slug: "ok", file: "p.html" }), /--target is required/);
});

test("--institution trimmed / non-empty", () => {
  assert.match(v({ ...room, institution: "   ", kind: "pdf", file: "a" }), /must not be empty/);
  assert.match(v({ ...room, institution: "", kind: "pdf", file: "a" }), /must not be empty/);
  assert.equal(buildBody({ ...room, institution: "  UW  ", kind: "link", url: "https://a.com" }, "t").institution, "UW");
});

test("oversized payload refused locally", () => {
  assert.equal(checkPayload("x".repeat(1000)), null);
  const big = JSON.stringify({ file: Buffer.alloc(MAX_REQUEST_BYTES).toString("base64") });
  assert.match(checkPayload(big), /~6MB request limit/);
  assert.match(checkPayload(big), /PDF over ~4MB/);
  // a ~4.5MB PDF is ~6MB once base64'd
  const pdf = buildBody({ target: "library", kind: "pdf", title: "T" }, "t", { bytes: Buffer.alloc(4.5 * 1024 * 1024) });
  assert.ok(checkPayload(JSON.stringify(pdf)));
  assert.match(validateHtml("<html>", MAX_REQUEST_BYTES + 1), /~6MB/);
});

test("200 with a non-object body is unverified", () => {
  for (const b of [undefined, null, "ok", [1], 7]) {
    const r = describeResponse(200, b);
    assert.equal(r.ok, false); assert.match(r.message, /UNVERIFIED/);
  }
  assert.equal(describeResponse(200, {}).ok, true);
});

test("effective config.artifact enforces presenter => staff", () => {
  assert.match(validateEffectiveDeckRole({ config: { artifact: "presenter" }, visibility: "room" }), /staff/);
  assert.equal(validateEffectiveDeckRole({ config: { artifact: "presenter" }, visibility: "staff" }), null);
  assert.match(validateEffectiveDeckRole({ config: {}, deckRole: "presenter", visibility: "room" }), /staff/);
  assert.match(validateEffectiveDeckRole({ config: { artifact: "prospect" }, visibility: "staff" }), /cannot be/);
  // flag overrides the file
  assert.equal(validateEffectiveDeckRole({ config: { artifact: "presenter" }, deckRole: "prospect", visibility: "room" }), null);
  assert.equal(validateEffectiveDeckRole({ config: {}, visibility: "room" }), null);
});
