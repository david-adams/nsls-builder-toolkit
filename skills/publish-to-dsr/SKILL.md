---
name: publish-to-dsr
description: >-
  Use when an authorized NSLS person wants to publish content to the Digital
  Sales Room from a file or link, using a personal publishing token (no AWS, no
  repo clone): a public page at docs.nsls.org/<slug>, a Library master PDF or
  link, or a document in a named institution's room (HTML, PDF, link, or a
  built deck). Triggers: "publish to the DSR", "publish a sales doc", "put this
  in the library", "publish a public page", "publish a deck", "push this
  microsite live", "put this in <school>'s room".
---

# publish-to-dsr

## Purpose

One CLI, three destinations, all via a publishing token tied to you personally
and Node's built-in `fetch` (Node 18+). No AWS credentials, no shared internal
key, no system-of-record checkout.

| `--target` | `--kind` allowed | Lands in |
|---|---|---|
| `public` | `html` | `docs.nsls.org/<slug>` (public) |
| `library` | `pdf`, `link` | the Library master list |
| `room` | `pdf`, `link`, `html`, `built` | one institution's room (`--visibility room` = school sees it, `staff` = NSLS only) |

**Library does not accept HTML or built decks.** To put those in the Library,
publish to a room with `--also-library`.

## Prerequisites

1. This plugin installed (the script ships with it).
2. A publishing token, generated from the dashboard (Marketing Pages / publishing tokens).
3. The file (or URL) to publish.

**Tokens are per environment.** A staging token does not work against
production, and `~/.config/nsls/publish-token` holds only one. Use the matching
token per `--stage` (`--token`, or `NSLS_PUBLISH_TOKEN`, per run). Lookup order:
`--token`, then `NSLS_PUBLISH_TOKEN`, then the file (chmod 600). Prefer env var
or file (a flag lands in shell history). **Never echo or paste the token in chat.**

## Usage

Run from the user's own directory with absolute file paths. `S` below is
`"${CLAUDE_PLUGIN_ROOT}/skills/publish-to-dsr/scripts/publish.mjs"`.

```bash
# Public page
node "$S" --target public --kind html --slug fall-2026-launch \
  --file /abs/page.html --title "Fall 2026 Launch" --description "Optional"

# Library master: PDF, or link
node "$S" --target library --kind pdf  --file /abs/one-pager.pdf --title "One Pager"
node "$S" --target library --kind link --url https://example.com/x --title "Case Study"

# Institution room: pdf | link | html (visibility room|staff; or --group-id <uuid>)
node "$S" --target room --institution "University of Wisconsin" --visibility room \
  --kind pdf --file /abs/proposal.pdf --title "Proposal"
node "$S" --target room --institution "University of Wisconsin" --visibility room \
  --kind html --file /abs/recap.html --title "Fall Recap" --also-library

# Built deck (re-publishing an existing deck: add --target-doc-id <uuid>)
node "$S" --target room --institution "University of Wisconsin" --visibility room \
  --kind built --manifest-id society-sales-deck --config /abs/dsr-config.json \
  --notes /abs/notes.default.json \
  --artifact /abs/dist/index.html --artifact-content-type html --deck-role prospect \
  --title "Society Sales Deck" --also-library
# Presenter build of the same deck: staff-only, and pair it with the prospect build
#   --visibility staff --deck-role presenter --deck-pair-id <uuid>

# Production — BOTH flags, and the user's explicit go-ahead in chat
node "$S" ... --stage production --allow-production
```

`--help` prints the full flag list.

**Always publish to staging first (the default) and show the user the result,**
then production only after they say so. State the stage before running. Never
add `--allow-production` on your own initiative.

## Rules (checked locally before any network call)

- **`--target` is required** (`public|library|room`); it is never defaulted, so nothing goes world-readable by accident.
- **Slug** (public only): `^[a-z0-9]+(?:-[a-z0-9]+)*$`, max 80, `ph` and `d` reserved (`d` is docs.nsls.org's share-link and library prefix).
- **Title** required, max 200. **Description** optional, max 2000.
- **Size:** the practical limit is ~6 MB per REQUEST (the dashboard sits behind CloudFront/Lambda), not the server's 25 MB document cap. Binary content goes base64 (+33%), so a **PDF over ~4 MB is too big**; the CLI refuses locally above ~5.5 MB encoded. Shrink it or host it elsewhere and publish a `--kind link`. PDF and built-PDF artifacts are sent base64; HTML as text. HTML must start with `<!doctype html>` or `<html>`; a PDF must have a `%PDF-` header.
- **Links** must be `http(s)`.
- **Public pages: outside sources must be on the allowed list.** Scripts, frames and connections (`fetch`, beacons, sockets) may only use sites an admin has allowed; the publish is refused otherwise (see Errors). Page scripts can never call docs.nsls.org itself except PostHog at `/ph/`.
- **Room** needs `--institution` or `--group-id` (uuid), plus `--visibility`.
- **Built** needs `--manifest-id` and `--config <json object>`; `--artifact` needs `--artifact-content-type`; `--also-library`, `--target-doc-id`, `--deck-pair-id`, `--deck-role` need `--artifact`. `--deck-role` sets `config.artifact`. The rule is enforced on the EFFECTIVE value (flag, else `config.artifact` in the config file): `presenter` requires `--visibility staff` (rep-only notes); `prospect` cannot be staff. `--notes` overrides `config.notes`. `--institution` must be non-empty.

## Errors

| Result | Meaning | Do |
|---|---|---|
| 200 | Published | Give the user the printed URL / document id. Keep the id: it is the `--target-doc-id` for updates. |
| 401 | Token invalid, expired, revoked, or wrong environment | Generate a new token for that stage. |
| 400 | Malformed request | Fix the input named in the message. |
| 413 | Too large | Shrink it (~6 MB request limit; PDF over ~4 MB won't fit); host heavy media elsewhere. |
| 200 "UNVERIFIED" | Success with no details | Check the destination before assuming it published or retrying. |
| 409 `title_exists` | A built deck with that title is already in the room | Re-run with `--target-doc-id <existing doc id>` to publish a new version, or pick another title. |
| 400 "isn't on the allowed list" | A public page loads a script, frame or connection from a site not on docs.nsls.org's allowed list | Tell the user which site and use. An admin adds it at invitations.nsls.org/admin/public-page-sources; then publish again. **Don't work around it** (inlining or renaming the source): the page's browser security rules block unlisted sites anyway. Fonts, images and styles need no entry. |
| 400 "can't be allowed … use https" | The source is `http:` or a non-standard port, which can never be allowed | Switch it to `https://` on the default port. |
| 409 (slug) | Slug taken | Open `docs.nsls.org/<slug>`: it may be your own earlier publish. Otherwise pick a new slug. |
| 502 / network error | Upstream or ambiguous | **Do not retry yet.** It may already be live: check docs.nsls.org / the library / the institution's room first. |

If the script exits with "No publishing token found", stop and send the user to
the dashboard; do not ask them to paste the token into chat.

## Tests

`node --test "${CLAUDE_PLUGIN_ROOT}/skills/publish-to-dsr/scripts/lib.test.mjs"` (logic lives in `scripts/lib.mjs`; Node 22 does not accept a bare directory here).
