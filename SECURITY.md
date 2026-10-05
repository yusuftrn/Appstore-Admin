# Security policy

This release supports macOS/Linux, Python 3.10+ and local, user-controlled
execution. Only the current release receives fixes. Use a dedicated API key
with the least Apple permissions your workflow needs. Team keys are not app
scoped; individual key access follows the user's permissions.

## Protections

- Profiles and keys must be regular files owned by the current user, with no
  group/world permissions. Keys are checked as unencrypted P-256 private keys.
- Imported keys, plans and journals are private local files. Default storage
  is outside the checkout. Imports cannot replace an existing account.
- Temporary JSON/media files have unpredictable names and mode 600. JSON
  publication is atomic; new output publication cannot overwrite a raced file.
- API tokens are sent only to the exact HTTPS App Store Connect API host.
  Authenticated redirects are refused. Only GET/allowlisted POST/PATCH are used.
- Asset URLs must use HTTPS on Apple/CDN or AWS domains, without userinfo or
  nonstandard ports. Uploads accept PUT only; authorization/cookie/host overrides
  and malformed headers are rejected. Downloads recheck redirected domains.
- Plans bind account, app, bundle, version and parent ownership. Writable states,
  local image hashes and concurrent field edits are checked before writes.
- Errors expose status and bounded Apple error codes, without raw API details,
  request bodies, profiles or bearer tokens. Some read commands intentionally
  display app metadata; keep their output private when appropriate.
- Release source archives strip owner names, UID/GID and filesystem timestamps.
- Common secrets and private/generated file paths are blocked by public source
  and archive allowlists, with offline regression checks in CI.

## Trust boundaries and limits

The CLI runs with your OS user's permissions; it is not a sandbox for untrusted
metadata, plans, schemas or backups. Use only files you created or independently
reviewed. A plan checksum detects damage, not malicious replacement or approval.
An actor controlling your user account, Python/OpenSSL, local filesystem, proxy,
DNS or trusted upstream responses can defeat local protections.

Asset host checks permit Apple domains and AWS hosts because Apple's storage
may use those providers. They are not a network egress firewall or DNS pinning.
Unexpected provider changes fail closed and require a reviewed code update.
Downloads are capped at 1 GiB per asset; screenshot files at 50 MiB and upload
chunks at 64 MiB. Backups can still consume substantial disk space.

Apple API writes are not atomic across resources. There is no guaranteed ETag
comparison. A failed write may already have committed. Preserve journals and
reconcile before retrying. Local locks cover one shared data directory, not
independent devices or portal users. Files are not encrypted at rest: rely on OS
disk encryption and private directory access. Existing output parent directories
are the user's responsibility.

Backups can contain review credentials, personal contacts, unpublished metadata,
commercial records and signed URLs. Git ignore rules alone cannot make arbitrary
folders safe to publish. Review exact tracked files and fresh distribution
archives; never publish the original private working folder or runtime data.

## Reporting

When the repository is published, enable GitHub private vulnerability reporting
and use its Security → Report a vulnerability flow. Until a private channel is
configured, request a private reporting channel without posting exploit details
or account data in public. Do not attach API keys, profiles, JWTs, plans or backups.

If a credential is exposed, revoke it in App Store Connect and replace it in
private storage. Removing a file or rewriting Git history does not revoke a key.
No external penetration test or live Apple write certification is claimed.
