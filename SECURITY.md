# Security

## Reporting

Found a vulnerability? Please open a
[GitHub security advisory](https://github.com/MikkoNumminen/claude-continue/security/advisories/new)
or a private issue rather than a public one. We'll respond as fast as we can.

## Trust model

`claude-continue` is a local automation tool. It has no server, collects no
telemetry, and makes exactly two kinds of outbound action:

1. **The reset action** — types `continue` into your own terminals (iTerm2 / tmux
   / Windows terminal) or runs a headless `claude -p`. It never sends your data
   anywhere; it only nudges sessions you already run.
2. **Self-update** — talks to the public GitHub Releases API/CDN over HTTPS.

### Self-update integrity

The ⟳ Update / `claude-continue update` path is the only code that downloads and
runs new bytes, so it's the most security-relevant. It:

- restricts downloads to an **HTTPS GitHub host allowlist**
  (`github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`);
- **verifies the SHA-256** that the GitHub API reports for the asset before
  installing — a mismatch aborts the install;
- never uses the (attacker-influenceable) asset filename as a filesystem path or
  in the Windows helper script (a self-chosen local name is used), avoiding path
  traversal / command injection;
- on a frozen build, verifies certs against the OS system CA bundle (the bundled
  OpenSSL may ship none) — verification is **never disabled**.

**Residual risk (documented, not a bug):** checksum verification defends the
download leg (corruption / on-path tampering) but shares GitHub's trust root —
it does **not** defend a fully compromised release/repo, which would require a
detached signature checked against a pinned key. For a personal tool the trust
anchor is "you trust this GitHub repo." The macOS `.app` is only ad-hoc signed
(not Developer-ID signed or notarized) and the Windows build is **unsigned**; on a
browser download macOS Gatekeeper will warn (right-click → Open) and Windows
SmartScreen / a strict antivirus may warn or need an allow-list entry for the
install folder. Only Authenticode signing (Windows) + Developer-ID notarization
(macOS) can guarantee no heuristic block.

### Self-removal

**Remove app… / `uninstall --app`** deletes your config, logs, and the app
itself (via a detached helper that waits for the process to exit). The deletion
target is derived from `sys.executable` — the macOS `.app` bundle, or the Windows
one-dir install **folder** (the exe's parent); it is never a shallow/`/`-style
path, and a `%`-bearing (cmd-unsafe) path is refused rather than scripted. A
failed self-delete is surfaced, not hidden.

## Supported versions

Only the latest release is supported. Update via the in-app button or the
[releases page](https://github.com/MikkoNumminen/claude-continue/releases/latest).
