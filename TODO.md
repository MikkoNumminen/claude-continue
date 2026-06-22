# TODO

Tracked follow-ups. **Hard constraint for this project: nothing that costs money.**
Paid signing/notarization is out of scope; only free options below.

## Explore FREE ways to cut AV / Gatekeeper false-positives

Follow-up to the v0.9.0 one-dir AV fix (root cause: IPVanish Threat Protection blocked
the old one-file exe's per-launch `%TEMP%` unpack of `python311.dll`). v0.9.0 removed
that behavior + added version metadata + dropped UPX — all free. Remaining ideas:

- [ ] **SignPath.io free OSS code-signing tier** for the Windows build. Free *for
  open-source projects*, but requires applying + approval — **verify eligibility
  BEFORE wiring up any CI** (don't build it then find out we don't qualify).
  *(needs the maintainer to apply — can't be done autonomously)*
- [ ] **Report false positives to AV vendors** (Microsoft Defender via the WDSI
  submission portal; IPVanish / Ziff Davis Threat Protection support) so they
  whitelist the binary. Free; just turnaround time.
  *(needs the maintainer to submit — can't be done autonomously)*
- [x] Document the **user-side allow-list** steps in the README (done — see the
  "Standalone Windows build" section).

Known **no free option** (record, don't chase): macOS notarization needs the Apple
Developer Program ($99/yr) — paid, no free equivalent. Keep the documented manual
Gatekeeper workaround (right-click → Open / `xattr -dr com.apple.quarantine`).

## Hardening surfaced by the #37 review (free, deferred as out-of-scope there)

- [x] `update.cleanup_stale_update` reaps every `cc-update-*` temp dir unconditionally
  — added a 1h age-guard so a concurrent in-flight update isn't raced (done, #45).
- [x] `release.yml` `Compress-Archive` (under `shell: pwsh`) forward-slash zip entries
  — pinned the shell with a comment (done, #45).
