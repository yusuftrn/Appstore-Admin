# Contributing

Use fictional apps and generated temporary keys. Do not run tests against a
maintainer's or contributor's App Store Connect account. Keep credentials,
exports, plans, journals and backups outside the checkout.

Install `.[dev]` in a virtual environment and run the commands in the README.
`tools/run_tests.py` blocks socket connections during tests. Test meaningful
safety boundaries and observable behavior; do not require a live key in CI.

Write operations must remain scoped to listing metadata, editable drafts and
screenshots. Maintain default read-only behavior, explicit `--apply`, account
and parent ownership, concurrent-change checks, resumable journals and final
read-back verification. New endpoints or transport domains need specific tests
and a documented reason. Do not add automatic destructive cleanup or publish
steps as retries.

Run the source allowlist check before committing and archive checks after every
build. If introducing a new public source file, update the allowlist deliberately.
Optional hook: `git config core.hooksPath .githooks`. Review the staged file list
and diff yourself; scanners cannot recognize all private business information.

For bugs, share a minimal fictional reproduction, command shape, tool version,
OS/Python versions and a sanitized error code. Do not attach account profiles or
full plans/backups. Use the private reporting instructions in SECURITY.md for
security findings.
