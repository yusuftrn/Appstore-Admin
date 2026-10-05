# CLI workflow

Run `python3 scripts/admin.py --help` from the installed skill directory.
Global `--profile` and `--schema` flags precede the command. Default credentials
and journals are under `~/.local/share/appstore-admin`; `APPSTORE_ADMIN_HOME`
selects a separate, dedicated private directory. Missing credentials require
the user to import their own profile; do not search unrelated directories.

```sh
python3 scripts/admin.py account check
python3 scripts/admin.py apps
python3 scripts/admin.py show --app APPLE_ID --bundle com.example.app
python3 scripts/admin.py export --app APPLE_ID --bundle com.example.app --out /private/work/metadata.json
python3 scripts/admin.py plan --metadata /private/work/metadata.json --out /private/work/plan.json
python3 scripts/admin.py apply --plan /private/work/plan.json
python3 scripts/admin.py apply --plan /private/work/plan.json --apply
python3 scripts/admin.py audit --plan /private/work/plan.json
```

The metadata JSON identifies `appId`, `bundleId`, `platform`, `versionId`, and
`appInfoId`. Edit only the requested `locales` sections. Missing fields/locales
are preserved, `""` clears a field, and `null` is rejected. `_inventory` is context.

- `appInfo`: name, subtitle, privacyPolicyUrl, privacyChoicesUrl, privacyPolicyText.
- `version`: description, keywords, marketingUrl, promotionalText, supportUrl, whatsNew.
- Top-level `versionAttributes`: copyright.
- `screenshots`: display type to 1–10 unique PNG/JPEG paths, relative to metadata JSON.

Only the selected editable draft can be changed. On ambiguous selection use
`export --version ID_OR_VERSION --info APP_INFO_ID`. For a requested new version:
`draft --app APPLE_ID --bundle com.example.app --version 1.3` previews it;
adding `--apply` creates it with MANUAL release. It does not upload a build.

`backup --app APPLE_ID --bundle com.example.app --out /private/work/backup`
uses GET only. `verify --backup ...` and `inventory --backup ...` are offline.
Backups may contain review credentials and commercial records; never attach them
to GitHub issues or include them in a package.

Exit codes: 0 success, 1 failed, 2 verification gaps/remaining action, 130 interrupted.
Keep journals after partial writes. Coordinate automation across machines;
local locking covers processes sharing the same private data directory only.
