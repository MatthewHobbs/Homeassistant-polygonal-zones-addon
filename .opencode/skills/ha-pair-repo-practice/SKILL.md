---
name: ha-pair-repo-practice
description: >
  Working practice for a Home Assistant integration plus its companion Supervisor
  app (add-on), kept as two repos with one shared file contract. Covers the traps
  that only a real instance exposes: discovery addresses and tokens, entry data
  rebuilt from a form, local app branch testing, merge-is-not-release, immutable
  tags, image attestation, licence/SPDX deadlock, and the CI gates worth wiring.
  Load when building, reviewing or releasing an HA integration or app, when
  setting up discovery between them, or when a release is being prepared.
---

# Home Assistant integration + app: working practice

Work only with the tools this session actually exposes. Commit, push, publish,
deploy and external messages require the user's explicit request.

Everything below was learned by shipping one pair of repos and testing it on a
supervised instance. Each rule names the failure it prevents. Prefer the
mechanism over the prose: if a rule can be checked by CI, wire the check.

## 1. Enforceable rules — put them in CI, not in a document

| Rule | Mechanism | Prevents |
| --- | --- | --- |
| Cap comment length in every shipped language | a script (`check_comment_length.py`): `#` runs for `.py`/`.yml`/`.yaml`, `//` and `/* */` for `.js`; docstrings excluded; run in `just ci` and a required job | comment bloat returning the moment nobody watches |
| Test the tests | inject a few deliberate bugs one at a time and confirm each fails the suite | a test that patches out the very thing it observes, so the bug it names can never fail it |
| Cross-repo conformance | feed the producer's real output through the consumer's own parser in a test | the two halves drifting apart while both suites stay green |
| Real-runtime suite as its own job | `pytest-homeassistant-custom-component`, one exact HA version per release, own install | unit tests stubbing the registry, so migrations and reloads only fail in production |
| Version and release guards | changelog-has-a-heading, versions-only-go-up, published-SHA-matches-tag | an immutable tag pointing at something nobody checked |
| Pin actions by SHA; run docker actions by digest | workflow pins, including a digest for container actions | a "pinned" action that still resolves `:latest` |
| Attest, then verify | `gh attestation verify` on the image and on the release zip | provenance nobody checks |
| Keep the licence body intact | two copyright lines immediately above `Permission is hereby granted` | SPDX `NOASSERTION` on the default branch, which fails a required check on every PR |

## 2. Discovery between app and integration

- The app announces itself to the Supervisor at every start, and after every
  save it asks Core to re-read the file. Look for one existing Core service
  before inventing an endpoint; an entity-targeted service cannot serve a
  producer that knows no entity ids, so add a domain-wide one.
- Announce the **Supervisor's own `ip_address`** (`GET /addons/self/info`),
  not `socket.gethostname()`. A container hostname can resolve through
  `*.local.hass.io` to the host address and be refused by Core's resolver,
  while the IP answers `403` — that is, it reaches the app.
- Carry the save token in the discovery payload so the user types nothing.
  Persist a generated token, or it rotates on every start and the integration
  depends on winning a race with discovery.
- The integration marks the entry as discovery-owned, and that flag is what
  authorises sending the token to an address that is neither an IP literal,
  `localhost` nor `.local`.
- `unique_id` from the app's slug; on rediscovery **update only the keys the
  producer owns** (address, token) and never rebuild the entry from a form —
  flags that are not form fields are lost.
- Set privacy-relevant keys **explicitly** at creation. An absent
  `expose_coordinates` means "legacy entry", which exposes coordinates by
  default and raises a repair notice on a brand-new install.

## 3. The file contract

- The producer must refuse anything the consumer cannot load: blank name,
  non-integer priority, features or vertices over the shared caps. Otherwise a
  save produces a file that stops every zone loading on the next restart.
- The consumer must validate the **whole document it is about to write** with
  the parser the loader uses, not just the feature it was handed.
- An entry with no source (no URL, no local copy) reports a successful load of
  zero entries. Refuse it at save time; it looks healthy for ever.
- Duplicates are ambiguous: reject duplicate names and ids at both ends.
- Keep user-owned data out of the producer's update path (entities, consent,
  exposure) and producer-owned data out of the consumer's (address, token).

## 4. Testing a branch without a release

- Integration: copy `custom_components/<domain>/` into `/config/`, restart HA.
- App: copy the app folder into `/addons/`, **delete its `image:` line** and
  raise `version:`; then *Install app → the local version*. With `image:` set
  the Supervisor pulls the published image, so a branch is silently untested.
- Rebuilds are keyed on the version: raise it on every copy, or nothing installs.
- The local app has its own `/data` and slug (`local_<slug>`); stop the store
  version first, and expect an empty zone file.
- Clear a **pending discovery card** before re-announcing: HA reuses the old
  flow, so a stale card shows the stale address and the old wording.

## 5. Release

- **Merge is not release.** The app's Supervisor listing keys on `version:` in
  `config.yaml`; HACS keys on a published GitHub release. Everything can land on
  `main` without a user seeing it.
- One bump PR per repo: version and changelog heading only, never mixed with
  behaviour. Tags are immutable, so a bad tag costs a patch release.
- The app's workflow publishes on a `refs/tags/v` push; the integration's runs on
  `release: published`, so a tag alone publishes nothing.
- A dispatch of the release workflow is a dry run: it never reaches publish or
  attestation. Do not plan an "attestation pre-flight"; a throwaway `v*` tag
  cannot be deleted.
- List the version in `breaking_versions` when the change removes an option or
  refuses previously accepted input: it turns an automatic update into a
  deliberate one and shrinks the window where the version is visible before its
  images exist.
- Release notes state behaviour only. No exploit, no reproduction, no payload.

## 6. Traps worth recognising

- A required check can deadlock the default branch: if a check reads the default
  branch (licence detection does), the PR that fixes it can never pass. Do not
  relax admin enforcement to get past it: stop, say so, and hand back; the fix
  is a check that does not read the default branch.
- Two config entries pointing at the same producer share a title and are
  indistinguishable in the UI. Mark discovery-created entries, or raise a
  problem when a second one appears.
- Logs and state times are UTC. `14:40Z` is not "before the reboot" at 15:32
  local; read the offset before concluding anything.
- A user-configured entity that cannot be read must not fail at `DEBUG` only,
  and "no history" must be distinguishable from "the lookup failed".
- No real coordinates, zone names or home-area screenshots in a public repo.
- Documentation is tested too: internal anchors, prettier, and a plain-English
  read. British spelling if that is the project's voice.

## 7. Planning files

Keep `PLAN.md`, `BACKLOG.md`, `COMPLETED.md` local and git-ignored. A backlog
that lists finished work is worse than no backlog: when a release lands, move
the closed items to `COMPLETED.md` with the PR that closed them, and give every
dropped item a reason and every deferred item a trigger.
