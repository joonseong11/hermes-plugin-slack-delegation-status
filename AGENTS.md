# AGENTS.md

Rules for any agent or person changing this repository. Read this before the first write.

Design, safety boundaries and deployment mechanics are in [README.md](README.md). Hermes compatibility is in [COMPATIBILITY.md](COMPATIBILITY.md). This file covers one thing: how versions, branches and tags are kept in step with the code.

## Where the version lives

A version is declared in five places, and they must always agree:

| Place | Form |
|---|---|
| `__init__.py` | `PLUGIN_VERSION = "X.Y.Z"` |
| `plugin.yaml` | `version: X.Y.Z` |
| `README.md` | ``Current release: `vX.Y.Z` `` |
| `CHANGELOG.md` | topmost `## [X.Y.Z] - YYYY-MM-DD` heading |
| git | annotated tag `vX.Y.Z` |

`scripts/check-version.sh` checks the first four on every push, and checks the tag name when CI runs for a tag. A version that has no tag is not a release.

## Choosing the number

- **Patch** (`0.7.1` → `0.7.2`): fixes that do not change the Slack status lifecycle states or texts, the settings, the hooks the plugin provides, or the contracts it needs from Hermes and `delegate-task-routing`.
- **Minor** (`0.7.1` → `0.8.0`): any change to the status lifecycle (states, transitions, status texts), settings or their defaults, `provides_hooks`, fallback behaviour, or the Hermes / routing-plugin contracts it depends on (see `COMPATIBILITY.md`).
- **Major**: reserved for `1.0.0` and later breaking changes.
- Changes to documentation, tests or CI only do not change the version and are not tagged.

One version number belongs to exactly one release commit, the one its tag points at. Later documentation, test or CI commits keep declaring that version; they are not a new release. Never reuse a number for different code, and never skip a number.

## Release procedure

Do these in order. Do not start the next version until the last step is done for the current one.

1. Create a new branch from an up-to-date `main`: `<type>/<short-topic>-<YYYYMMDD>`.
2. Make the change. In the final commit of the branch, set the new version in all four files and add the `CHANGELOG.md` section. Date it with the day the release is merged and tagged; if the merge slips to a later day, correct the date on the branch before merging.
3. Run `scripts/verify.sh` and `scripts/check-version.sh`. Both must pass.
4. Push the branch and open a pull request against `main`. CI must pass.
5. Merge with a **merge commit**. Do not squash or rebase: the tag must point at a commit that keeps its hash.
6. Tag the commit that set the version, and push the tag:

   ```bash
   git fetch origin
   git tag -a vX.Y.Z <release-commit> -m "slack-delegation-status vX.Y.Z"
   git push origin vX.Y.Z
   ```

   The tagged commit must be reachable from `origin/main`. Check with `git merge-base --is-ancestor vX.Y.Z origin/main`.
7. In the working checkout, return to `main` and fast-forward it: `git switch main && git pull --ff-only`.

A branch is finished once its pull request is merged. Do not push further commits to it; new work starts at step 1 on a new branch.

## Released is not the same as active

A tag says the code is in `main` and passed static checks. It does not say the running gateway has loaded it.

- Files on disk are imported only when the gateway restarts, and the gateway must not be restarted while work is running (see "Scope, settings, and installation" in the README).
- Record activation state per version in the "Activation" table of `COMPATIBILITY.md`, with the evidence (gateway log line, file hashes, smoke test result).
- Do not describe a version as operational until `docs/SLACK_SMOKE_TEST.md` passes.
- Never edit plugin files on the server without committing the same bytes here. A server copy that differs from the tag is a defect to be recorded as a new version, not left in place.

## CHANGELOG rules

- The date in a heading is the date the version was tagged.
- `## [Unreleased]` is allowed only as the topmost section, while its branch is still open. `scripts/check-version.sh` skips it on feature branches and pull requests and rejects it on `main` and on a tag. It also requires the topmost release heading to have the full `## [X.Y.Z] - YYYY-MM-DD` form. Replace it with the version and date in the final commit, before merging.
- Never edit the section of a version that is already tagged, except to correct a factual error.

## Known gaps in history

- **No tags existed before 2026-10-07.** `v0.6.3` was tagged retroactively on `926a156`, the initial-release commit. That commit predates `scripts/check-version.sh`, `CHANGELOG.md` and the README release line, so the check cannot be run against it.
- **Versions between `0.6.3` and `0.7.1` never had their own commits on `main`.** Whatever intermediate versions existed were not committed separately; the history goes from `0.6.3` straight to `0.7.1`.
- **`release/v0.7.1` (`ea3fb0e`) is not `v0.7.1`.** That branch was never merged, and production ran later edits to `__init__.py` and `tests/test_status.py` that were made on the server on 2026-09-28 and never committed. `v0.7.1` points at the last commit of the branch that recorded those deployed bytes and added the release metadata (this file, `CHANGELOG.md`, the version check), not at `ea3fb0e`. This is a one-time exception to step 6 of the release procedure: no single commit both set `0.7.1` and held the code that actually ran.
