"""Single-account CLI for all apps; write operations require explicit --apply."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .config import (account_identity, client_for, import_account, local_root,
                     profile_path, schema_path)
from .core import Archive, Snapshot, describe, init_schema, save_json, schema_at, state_of
from .metadata import PLATFORMS, checked_id, children, export_metadata, make_plan, sanitized_summary
from .operations import app_lock, apply_plan, audit_plan, live_context, preflight


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def unique_output(path, value):
    path = Path(path).expanduser().resolve()
    if path.exists():
        raise RuntimeError('Output exists; choose a new file: ' + str(path))
    save_json(path, value, overwrite=False)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--profile', help='Optional explicit private profile; default is the single active account')
    parser.add_argument('--schema', help='Optional official Apple OpenAPI schema override')
    sub = parser.add_subparsers(dest='command', required=True)
    account = sub.add_parser('account', help='Check/import the single active account; never prints secrets')
    account_sub = account.add_subparsers(dest='account_action', required=True)
    imp = account_sub.add_parser('import')
    imp.add_argument('--profile', dest='source_profile', required=True)
    account_sub.add_parser('check')
    schema = sub.add_parser('schema', help='Download current official Apple OpenAPI schema')
    schema.add_argument('--out', help='Default private data directory/schema/openapi.json; existing files are not overwritten')
    sub.add_parser('apps', help='List all accessible apps (read-only JSON)')
    show = sub.add_parser('show', help='App/version/info inventory (read-only)')
    show.add_argument('--app', required=True)
    show.add_argument('--bundle', required=True)
    for cmd in ('export', 'draft'):
        p = sub.add_parser(cmd, help='Export editable metadata JSON' if cmd == 'export' else 'Prepare a new MANUAL-release draft version')
        p.add_argument('--app', required=True)
        p.add_argument('--bundle', required=True)
        p.add_argument('--platform', choices=PLATFORMS, default='IOS')
        p.add_argument('--version', required=(cmd == 'draft'), help='Version ID/string for export; version string for draft')
        if cmd == 'export':
            p.add_argument('--info', help='App information record ID')
            p.add_argument('--out', required=True)
        else:
            p.add_argument('--apply', action='store_true')
    plan = sub.add_parser('plan', help='Read live state and save a reviewable metadata plan; no writes')
    plan.add_argument('--metadata', required=True)
    plan.add_argument('--out', required=True)
    for cmd in ('apply', 'audit'):
        p = sub.add_parser(cmd, help='Preflight/apply a saved plan' if cmd == 'apply' else 'Read-back verification of a saved plan')
        p.add_argument('--plan', required=True)
        if cmd == 'apply':
            p.add_argument('--apply', action='store_true', help='Without this flag only read-only preflight runs')
            p.add_argument('--asset-wait', type=int, default=45)
        else:
            p.add_argument('--out')
    backup = sub.add_parser('backup', help='Full app graph/media backup, using only GET')
    backup.add_argument('--app', required=True)
    backup.add_argument('--bundle', required=True)
    backup.add_argument('--out', required=True)
    for cmd in ('verify', 'inventory'):
        p = sub.add_parser(cmd, help='Offline archive checksum/inventory')
        p.add_argument('--backup', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'account' and args.account_action == 'import':
            path = import_account(args.source_profile)
            emit({'status': 'IMPORTED', 'profile': str(path), 'privateKey': 'not displayed'})
        elif args.command in {'verify', 'inventory'}:
            snapshot = Snapshot(args.backup)
            if args.command == 'inventory':
                emit(describe(snapshot))
            else:
                errors = snapshot.verify()
                emit({'valid': not errors, 'gaps': errors})
                return 2 if errors else 0
        elif args.command == 'schema':
            init_schema(argparse.Namespace(out=args.out or str(local_root() / 'schema/openapi.json')))
        else:
            writes = args.command in {'apply', 'draft'} and args.apply
            client = client_for(args.profile, readonly=not writes)
            if args.command in {'apps', 'account'}:
                apps, _ = client.pages('/v1/apps', {'limit': 200})
                emit({'status': 'CONNECTED', 'apps': [{'id': r['id'], 'bundleId': r['attributes'].get('bundleId'),
                       'name': r['attributes'].get('name')} for r in apps]})
            elif args.command == 'show':
                app = client.request('GET', '/v1/apps/' + checked_id(args.app))['data']
                if app['attributes'].get('bundleId') != args.bundle:
                    raise RuntimeError('App/bundle ID mismatch')
                emit({'app': {'id': app['id'], **app['attributes']},
                      'versions': [{'id': r['id'], 'version': r['attributes'].get('versionString'),
                                    'platform': r['attributes'].get('platform'), 'state': state_of(r)}
                                   for r in children(client, app, 'appStoreVersions')],
                      'appInfos': [{'id': r['id'], 'state': state_of(r)} for r in children(client, app, 'appInfos')]})
            elif args.command == 'export':
                doc = export_metadata(client, args.app, args.bundle, args.version, args.info, args.platform)
                path = unique_output(args.out, doc)
                emit({'status': 'EXPORTED', 'path': str(path), 'locales': len(doc['locales']),
                      'inventory': {k: v for k, v in doc['_inventory'].items() if k != 'media'}})
            elif args.command == 'plan':
                document_path = Path(args.metadata).resolve()
                doc = json.loads(document_path.read_text())
                plan = make_plan(client, schema_at(schema_path(args.schema)), doc, document_path.parent)
                path = unique_output(args.out, plan)
                emit({'status': 'PLANNED', 'path': str(path), **sanitized_summary(plan)})
            elif args.command in {'apply', 'audit'}:
                plan = json.loads(Path(args.plan).read_text())
                schema = schema_at(schema_path(args.schema))
                if args.command == 'audit':
                    report = audit_plan(client, schema, plan)
                    if args.out:
                        unique_output(args.out, report)
                    emit(report)
                    return 0 if report['status'] == 'VERIFIED' else 2
                if args.asset_wait < 0 or args.asset_wait > 300:
                    raise RuntimeError('--asset-wait must be 0–300 seconds')
                if not args.apply:
                    ctx = live_context(client, plan)
                    preflight(client, schema, plan, ctx)
                    emit({'status': 'PREFLIGHT_OK', 'writes': False, **sanitized_summary(plan)})
                else:
                    report = apply_plan(client, schema, plan, local_root() / 'runs', args.asset_wait)
                    emit(report)
                    return 0 if report['status'] == 'VERIFIED' else 2
            elif args.command == 'backup':
                app = client.request('GET', '/v1/apps/' + checked_id(args.app))['data']
                if app['attributes'].get('bundleId') != args.bundle:
                    raise RuntimeError('App/bundle ID mismatch')
                return Archive(client, schema_at(schema_path(args.schema)), args.out).backup(args.app)
            elif args.command == 'draft':
                return create_draft(client, schema_at(schema_path(args.schema)), args)
        return 0
    except KeyboardInterrupt:
        print('Interrupted. Do not delete journals; rerun the same plan to reconcile.', file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        # JSON/KeyError/OSError can quote private values or paths. Print only
        # controlled RuntimeError messages; never a traceback by default.
        message = str(error) if isinstance(error, RuntimeError) else 'Invalid input or local file access failure (' + type(error).__name__ + ')'
        print('ERROR:', message, file=sys.stderr)
        return 1


def create_draft(client, schema, args):
    import re
    if not re.fullmatch(r'\d+(?:\.\d+){0,2}', args.version):
        raise RuntimeError('Version must have 1–3 numeric components')
    with app_lock(local_root() / 'runs', account_identity(client.profile), args.app):
        app = client.request('GET', '/v1/apps/' + checked_id(args.app))['data']
        if app['attributes'].get('bundleId') != args.bundle:
            raise RuntimeError('App/bundle ID mismatch')
        versions = [r for r in children(client, app, 'appStoreVersions') if r['attributes'].get('platform') == args.platform]
        matching = [r for r in versions if r['attributes'].get('versionString') == args.version]
        if matching:
            if len(matching) != 1 or state_of(matching[0]) not in {'PREPARE_FOR_SUBMISSION', 'DEVELOPER_REJECTED', 'REJECTED', 'METADATA_REJECTED'}:
                raise RuntimeError('Version already exists and is not a unique editable draft')
            emit({'status': 'DRAFT_EXISTS', 'id': matching[0]['id'], 'writes': False})
            return 0
        if any(state_of(row) not in {'READY_FOR_SALE', 'READY_FOR_DISTRIBUTION', 'REMOVED_FROM_SALE', 'DEVELOPER_REMOVED_FROM_SALE'} for row in versions):
            raise RuntimeError('An existing draft/review/non-live version prevents creating another draft')
        body = schema.payload('appStoreVersions', 'post', {'versionString': args.version, 'platform': args.platform, 'releaseType': 'MANUAL'},
                              {'app': {'data': {'type': 'apps', 'id': args.app}}})
        if not args.apply:
            emit({'status': 'DRAFT_PLAN', 'writes': False, 'appId': args.app, 'version': args.version, 'platform': args.platform, 'releaseType': 'MANUAL'})
            return 0
        row = client.request('POST', '/v1/appStoreVersions', body)['data']
        rows = children(client, app, 'appStoreVersions')
        if not any(r['id'] == row['id'] and r['attributes'].get('versionString') == args.version and
                   r['attributes'].get('releaseType') == 'MANUAL' for r in rows):
            raise RuntimeError('Draft reservation returned but read-back did not match; inspect App Store Connect before retry')
        emit({'status': 'DRAFT_CREATED', 'appId': args.app, 'id': row['id'], 'version': args.version, 'releaseType': 'MANUAL'})
        return 0
