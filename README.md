# AppStoreAdmin

A local Python CLI and optional Codex skill for managing App Store Connect
listing metadata, locales, screenshots and private app backups. Every user
supplies their own Apple API key. No account, app export, screenshot, journal,
backup or owner-specific configuration is included.

Python **3.10+**, **OpenSSL** and **macOS or Linux** are required. The runtime uses
only Python's standard library. This is an independent developer tool; it is not
affiliated with Apple. It is a local CLI/skill, rather than a hosted service or
browser login integration.

## Install

From a clone or downloaded source archive:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
appstore-admin --help
```

For source-only use, `python3 bin/appstore-admin --help` needs no pip installation.
For a supplied wheel, install the wheel in your own virtual environment with
`python -m pip install --no-deps /path/to/appstore_admin-1.0.1-py3-none-any.whl`.
The package has not been published to PyPI; do not assume a registry package
with a similar name is this project.

## Configure your own account

Create an API key in App Store Connect with the minimum permissions needed for
your task. Individual key permissions follow the user; team keys are not
isolated to one app. See Apple's [API key guide](https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api).

Keep the profile and `.p8` **outside your repository**, in a dedicated private
folder. Copy the shape of [examples/profile.example.json](examples/profile.example.json),
then replace the placeholders in your private copy:

```json
{
  "issuer_id": "ISSUER_ID",
  "key_id": "KEY_ID",
  "private_key": "AuthKey.p8"
}
```

For an individual key, omit `issuer_id`. A relative key path is resolved against
the profile directory. Profiles may contain only those three fields. The key
must be an unencrypted ES256/P-256 private key. Do not put its contents in an
environment variable, shell argument, issue or chat message.

```sh
chmod 700 /private/path/account
chmod 600 /private/path/account/profile.json /private/path/account/AuthKey.p8
appstore-admin account import --profile /private/path/account/profile.json
appstore-admin account check
appstore-admin apps
```

These paths are placeholders for your private directory. Import copies the key
into private per-user storage; it refuses to replace an existing account and
checks permissions and key validity. Default storage is
`~/.local/share/appstore-admin/`, outside the source checkout. Set
`APPSTORE_ADMIN_HOME` to a dedicated private directory before running commands
to isolate another account or automation. It must not be your home directory or
the checkout root. An explicit global `--profile` goes **before** the subcommand:
`appstore-admin --profile /private/path/profile.json apps`.

`account check` and `apps` print accessible app names, IDs and bundle IDs, but
never the API profile or bearer token. Treat that inventory as account data too.
Protect existing output directories with `chmod 700`; new output files use 600.

## Review and apply metadata changes

Confirm both the Apple ID and bundle ID, then export the intended editable draft:

```sh
appstore-admin show --app APPLE_ID --bundle com.example.app
appstore-admin export --app APPLE_ID --bundle com.example.app \
  --out /private/work/metadata.json
```

If selection is ambiguous, provide `--version VERSION_ID_OR_STRING --info APP_INFO_ID`.
When no draft exists, export may select a published version for inspection;
planning a write to it is refused. A new version requires a separate explicit
`draft` action.

Edit only the desired fields in the exported JSON. The fictional
[metadata example](examples/metadata.example.json) shows the shape; replace its
IDs with records from your export. Missing fields/locales stay unchanged,
`""` explicitly clears a field and `null` is rejected. `_inventory` is read-only
context. Supported fields:

| Section | Fields |
| --- | --- |
| `locales.<locale>.appInfo` | name, subtitle, privacyPolicyUrl, privacyChoicesUrl, privacyPolicyText |
| `locales.<locale>.version` | description, keywords, marketingUrl, promotionalText, supportUrl, whatsNew |
| `versionAttributes` | copyright |
| `locales.<locale>.screenshots` | Display type → ordered PNG/JPEG path list |

```sh
appstore-admin plan --metadata /private/work/metadata.json --out /private/work/plan.json
appstore-admin apply --plan /private/work/plan.json
# After reviewing the private plan's before/after values:
appstore-admin apply --plan /private/work/plan.json --apply
appstore-admin audit --plan /private/work/plan.json
```

`plan` and `apply` without `--apply` use a read-only client. Actual writes require
`--apply`, matching account/app/bundle/version ownership, editable states and
unchanged preconditions. Terminal plan summaries list field names without their
contents. Do not edit a saved plan; edit metadata and generate another plan.
The SHA-256 detects accidental editing; it is **not a digital signature** or an
authorization mechanism. Use only plans you created and reviewed yourself.

Applied plans retain private before-state, receipts and media reservations under
`APPSTORE_ADMIN_HOME/runs/`. Reapplying the same plan reconciles matching changes.
Only `VERIFIED` confirms the read-back audit. Partial success has no automatic
rollback: retain journals, inspect Apple state and reconcile before retrying.

Apple's API offers no transaction or ETag guarantee here. Avoid concurrent portal
edits; local locking covers only processes sharing one data directory. Coordinate
separate machines yourself. See [security boundaries](SECURITY.md).

## Drafts and screenshots

```sh
appstore-admin draft --app APPLE_ID --bundle com.example.app --version 1.3
appstore-admin draft --app APPLE_ID --bundle com.example.app --version 1.3 --apply
```

The first command previews; the second creates a **MANUAL** release draft.
Existing drafts/review records prevent another version. No build is uploaded
and no review is submitted. `--platform` supports IOS, MAC_OS, TV_OS and VISION_OS.

Screenshot paths are relative to the metadata JSON:

```json
"screenshots": {
  "APP_IPHONE_67": ["screens/en-US/01.png", "screens/en-US/02.png"]
}
```

A set accepts 1–10 unique PNG/JPEG files, at most 50 MiB each. Apple decides
pixel-dimension and listing eligibility. SHA-256 checks local file integrity;
MD5 is used for Apple's required upload checksum. Reservations are saved before
upload; bounded PUT chunks carry only Apple's upload headers, never the API
bearer token. Upload redirects and unexpected domains/headers are refused.

An occupied set must already contain the same managed, hash-matching images.
The CLI never deletes existing images. For replacement, consciously clear the
selected set in App Store Connect and then regenerate your plan. New video
preview uploads are outside this version's scope.

## Private backups

```sh
appstore-admin backup --app APPLE_ID --bundle com.example.app --out /private/work/backup
appstore-admin verify --backup /private/work/backup
appstore-admin inventory --backup /private/work/backup
appstore-admin schema
```

Backup uses GET only and archives reachable records and downloadable media.
Backups may contain review contacts/credentials, commercial records and signed
asset URLs. Keep them private. The manifest/report identifies API gaps and
missing media; a partial backup exits with code 2. This package does not restore
or migrate records. Schema 4.5 is bundled; `schema` downloads Apple's specification
to private storage and refuses overwriting. Third-party attribution is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Optional Codex skill

The skill is in `skills/appstore-admin/` in the source checkout and source archive.
Install it into your chosen Codex skill directory:

```sh
python3 tools/install_skill.py --dest ~/.codex/skills/appstore-admin
```

The installer refuses an existing skill, so a previous personal skill is not
replaced. Choose another destination if needed. Make the CLI package available
to the Python interpreter executing the skill, or set `APPSTORE_ADMIN_PACKAGE`
to this source checkout. The launcher has no hard-coded owner path and never
falls back to another private checkout.

The wheel also includes the skill under
`<environment-prefix>/share/appstore-admin/skills/appstore-admin/`. Copy that
folder to your skill directory if installing only a wheel. Restart or reload
skills as appropriate for your client, then invoke `$appstore-admin`. The skill
honors the requested app and change scope; it creates no scheduled writes.

## Development and sharing

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python tools/run_tests.py
python -m bandit -c pyproject.toml -r src skills tools
python tools/check_distribution.py --tree .
python tools/build_release.py
python tools/check_distribution.py dist/*.whl dist/*.tar.gz
```

GitHub CI runs offline tests on Python 3.10 and 3.13, static analysis, source
hygiene and wheel/source archive checks. Action revisions are pinned and jobs
have read-only repository permissions. No Apple credentials or live-account
tests are required. Enable the optional local pre-commit check with
`git config core.hooksPath .githooks`. It checks the staged contents.

Read [CONTRIBUTING.md](CONTRIBUTING.md), [security review notes](docs/security-review.md)
and the [release checklist](docs/releasing.md) before sharing. The allowlist
scanner catches unexpected files and common secrets; review fictional examples
and the exact tracked files as well. It is not a guarantee against all private
business data. This repository and its distributions contain no runtime data.

Exit codes: **0** success, **1** failed, **2** incomplete/remaining action,
**130** interrupted. There are no commands for deletion, review submission,
release, signing, privacy questionnaires or pricing changes.

MIT licensed. See [LICENSE](LICENSE). Apple API specification attribution is
separate from the project license.
