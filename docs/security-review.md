# Shareable package review — 2026-10-05

This is a source review and offline regression/packaging validation performed
while preparing 1.0.1. It is not an independent penetration test, a certification,
or evidence of live write behavior on Apple's production service.

## Personal-data separation

The shareable tree was created by copying only source, tests, the public API
schema and skill helpers from an existing private workspace. Account profiles,
`.p8` keys, app exports, screenshots, plans, before-state snapshots, receipts,
journals and backups were excluded. The old private README and verification
report were not carried over. Owner paths and app names in helper/test files
were replaced with portable resolution and fictional examples. Git history
starts fresh; no old repository history or remote is copied. Initial commit
identity uses a generic contributor identity.

Distribution scanning checks the exact public file allowlist, common PEM/JWT/
GitHub/AWS credential patterns, personal filesystem paths and placeholder-only
profile examples. Source tar archives have owner names, UID/GID and timestamps
normalized. These checks cannot identify every possible private business fact;
maintainers must still review the tracked file list and content before publication.

## Corrections made

| Finding | Change |
| --- | --- |
| Personal checkout fallback in skill | Explicit source override, adjacent checkout or installed package only |
| Credentials defaulted into source checkout | Dedicated per-user storage outside the checkout |
| Weak profile/key validation | Owner/permission/symlink checks, strict profile shape, actual P-256 key validation |
| Partial account import could persist | Private staging, validation, serialized import and cleanup on failure |
| Predictable temporary files and output races | Private random temporary files, atomic replace or exclusive publication |
| API error details could echo private contents | Status and bounded API error codes only; raw details omitted |
| HTTPS alone permitted arbitrary asset targets | Apple/CDN/AWS domain checks, redirect checks and safe upload headers |
| Upload chunk offsets/methods were not fully bounded | PUT-only, contiguous bounded coverage and local hash checks |
| Legacy restore/commercial mutation surfaces | Removed; dedicated screenshot uploader retains required behavior |
| Packaging ignored tar ownership metadata | Release builder normalizes metadata; scanner refuses unnormalized archives |

## Verification performed

- **77 tests passed** on macOS with Python 3.14.3 and OpenSSL. Socket connections
  were forbidden during the test suite. Tests generated temporary fictional
  signing keys; no real Apple account was loaded or contacted.
- Bandit 1.9.4 reported no unsuppressed findings in source, skill helpers or tools.
  MD5 checking is exempted because Apple requires it for upload matching; local
  integrity uses SHA-256. OpenSSL subprocess import/call are narrowly suppressed
  with fixed argument lists, resolved executable paths, no shell and a timeout.
- All 14 source/helper modules parsed with Python 3.10 syntax rules. Actual
  Python 3.10/3.13 and Linux execution is configured in CI, not claimed as locally run.
- The public Codex skill passed the skill-creator frontmatter validator.
  Source-checkout and installed-wheel launchers were exercised.
- Fresh wheel/source archives passed public file/content/ownership checks.
- The wheel installed in a separate empty virtual environment with
  `--no-index --no-deps`. CLI help/version, installed skill and bundled schema
  loading passed outside the checkout. An isolated, unconfigured account failed
  locally without creating or discovering a real profile.
- The GitHub-ready source archive is exported from the clean Git index/history,
  with normalized metadata, and checked separately before delivery.

No review submission, release, app deletion, commercial write, credential import
from the owner's private account, GitHub push or global skill replacement was
performed. GitHub workflow execution, branch rules and security reporting must
be verified/configured after the repository is created.

## Remaining boundaries

Trust the local OS user, Python/OpenSSL, files, DNS/proxy and Apple responses.
This tool is not a sandbox or egress firewall. Plans are checksummed, not signed.
Apple writes can partially commit; locks do not span independent devices and
cannot prevent concurrent portal edits. Existing private output directories
must be protected by their owner. See SECURITY.md for complete operational limits.
