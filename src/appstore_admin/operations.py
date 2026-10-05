"""Apply and verify narrowly scoped plans; no publishing or deletion endpoints."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .config import account_identity
from .core import digest, key, linkage, now, read_json, save_json
from .media import ScreenshotUploader
from .security import private_directory
from .metadata import (INFO_FIELDS, VERSION_FIELDS, check_plan, checked_id, children,
                       context, index_locales, media_fingerprint, screenshot_file,
                       sha_json, validate_fields)


@contextmanager
def app_lock(directory, account, app_id):
    directory = private_directory(directory)
    path = directory / ('lock-' + sha_json([account, app_id]) + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another apply is running for this account/app') from None
        yield
    finally:
        os.close(fd)


def live_context(client, plan):
    check_plan(plan)
    if account_identity(client.profile) != plan['account']:
        raise RuntimeError('Plan belongs to another Apple account')
    ctx = context(client, plan['appId'], plan['bundleId'], plan['versionId'], plan['appInfoId'], plan['platform'], editable=True)
    ctx['infoLocales'] = index_locales(children(client, ctx['info'], 'appInfoLocalizations'))
    ctx['versionLocales'] = index_locales(children(client, ctx['version'], 'appStoreVersionLocalizations'))
    return ctx


def owner_context(ctx, op):
    kinds = {'appInfoLocalizations': ('info', 'infoLocales', INFO_FIELDS, 'appInfo'),
             'appStoreVersionLocalizations': ('version', 'versionLocales', VERSION_FIELDS, 'appStoreVersion')}
    if op['kind'] == 'appStoreVersions':
        if op['action'] != 'patch' or op['id'] != ctx['version']['id']:
            raise RuntimeError('Invalid version operation ownership')
        return None, ctx['version'], {'copyright'}, None
    if op['kind'] not in kinds:
        raise RuntimeError('Unsupported metadata resource kind')
    owner_name, locale_rows, fields, relation = kinds[op['kind']]
    owner = ctx[owner_name]
    if op['owner'] != linkage(owner):
        raise RuntimeError('Operation parent is not part of the selected app/version')
    target = ctx[locale_rows].get(op['locale'])
    if op['action'] == 'patch' and (not target or target['id'] != op['id']):
        raise RuntimeError('Operation locale is not owned by selected app/version')
    return owner, target, fields, relation


def screenshot_set(client, ctx, op):
    locale = ctx['versionLocales'].get(op['locale'])
    rows = children(client, locale, 'appScreenshotSets') if locale else []
    matches = [r for r in rows if r['attributes']['screenshotDisplayType'] == op['displayType']]
    if len(matches) > 1:
        raise RuntimeError('Duplicate screenshot sets')
    target = matches[0] if matches else None
    if op.get('setId') and (not target or target['id'] != op['setId']):
        raise RuntimeError('Screenshot set no longer belongs to selected locale')
    return locale, target


def validate_screenshots(client, schema, ctx, op):
    specs = schema.request_schema('appScreenshotSets', 'post')['properties']['attributes']['properties']['screenshotDisplayType']
    if op['displayType'] not in schema.resolve(specs)['enum'] or not 1 <= len(op['files']) <= 10:
        raise RuntimeError('Invalid screenshot operation')
    for file in op['files']:
        if screenshot_file(file['path']) != file:
            raise RuntimeError('Screenshot bytes/path changed since plan; regenerate')
    _, target = screenshot_set(client, ctx, op)
    rows = children(client, target, 'appScreenshots') if target else []
    requested = {f['fileName']: f for f in op['files']}
    if len(requested) != len(op['files']):
        raise RuntimeError('Duplicate screenshot files')
    for row in rows:
        file = requested.get(row['attributes'].get('fileName'))
        if not file or row['attributes'].get('sourceFileChecksum') not in {None, file['md5']}:
            raise RuntimeError('Unplanned screenshot appeared or checksum changed; no deletion is performed')
    return rows


def preflight(client, schema, plan, ctx):
    for op in plan['operations']:
        if op['action'] == 'screenshots':
            validate_screenshots(client, schema, ctx, op)
            continue
        if op['action'] not in {'patch', 'post'}:
            raise RuntimeError('Unsupported operation')
        _, target, fields, _ = owner_context(ctx, op)
        validate_fields(op['after'], fields, schema, op['kind'], op['action'])
        if op['action'] == 'post' and op['kind'] == 'appInfoLocalizations' and not op['after'].get('name'):
            raise RuntimeError('New app-info locale needs a name')
        if op['action'] == 'patch':
            if set(op['before']) != set(op['after']):
                raise RuntimeError('Invalid plan field preconditions')
            for field, wanted in op['after'].items():
                current = target['attributes'].get(field)
                if current != wanted and current != op['before'][field]:
                    raise RuntimeError('Concurrent metadata change; regenerate plan: ' + field)
        elif target and any(target['attributes'].get(k) != v for k, v in op['after'].items()):
            raise RuntimeError('Planned new locale now exists with other content; regenerate plan')


class LocalImages:
    def __init__(self, files):
        self.files = {file['sha256']: file for file in files}

    def asset(self, resource):
        file = self.files[resource['id']]
        path = Path(file['path'])
        if digest(path) != file['sha256']:
            raise RuntimeError('Screenshot changed during upload')
        return path


def upload_screenshots(client, schema, plan, ctx, op, run_dir, asset_wait):
    locale, target = screenshot_set(client, ctx, op)
    if not locale:
        raise RuntimeError('Screenshot locale was not created')
    if not target:
        body = schema.payload('appScreenshotSets', 'post', {'screenshotDisplayType': op['displayType']},
                              {'appStoreVersionLocalization': {'data': linkage(locale)}})
        target = client.request('POST', '/v1/appScreenshotSets', body)['data']
    existing = children(client, target, 'appScreenshots')
    identity = sha_json([op['locale'], op['displayType']])
    state_path = run_dir / (identity + '-assets.json')
    state = read_json(state_path) if state_path.is_file() else {'mapping': {}, 'assetHashes': {}, 'issues': [],
             'account': plan['account'], 'appId': plan['appId'], 'planSha256': plan['planSha256']}
    if (state['account'], state['appId'], state['planSha256']) != (plan['account'], plan['appId'], plan['planSha256']):
        raise RuntimeError('Asset journal belongs to another plan/account/app')
    engine = ScreenshotUploader(client, schema, LocalImages(op['files']), state, state_path, asset_wait)
    engine.save()
    uploaded = []
    for file in op['files']:
        source = {'type': 'appScreenshots', 'id': file['sha256'],
                  'attributes': {'fileName': 'screenshot' + Path(file['path']).suffix.lower()}}
        matches = [row for row in existing if row['attributes'].get('fileName') == file['fileName']]
        if len(matches) > 1:
            raise RuntimeError('Duplicate upload reservation')
        saved = engine.state['mapping'].get(key(source))
        if saved and not matches:
            raise RuntimeError('Saved screenshot reservation is not visible in this set; inspect/retry later instead of creating a duplicate')
        result = engine.upload(source, target, matches[0] if matches else None)
        engine.record(source, result)
        uploaded.append(linkage(result))
    actual = children(client, target, 'appScreenshots')
    if {row['id'] for row in actual} != {row['id'] for row in uploaded}:
        raise RuntimeError('Screenshot set changed during upload; refuse reorder')
    if [row['id'] for row in actual] != [row['id'] for row in uploaded]:
        client.request('PATCH', f'/v1/appScreenshotSets/{checked_id(target["id"])}/relationships/appScreenshots', {'data': uploaded})
    if [row['id'] for row in children(client, target, 'appScreenshots')] != [row['id'] for row in uploaded]:
        raise RuntimeError('Apple screenshot ordering verification failed')


def audit_plan(client, schema, plan):
    ctx = live_context(client, plan)
    problems = []
    for index, op in enumerate(plan['operations']):
        if op['action'] == 'screenshots':
            try:
                rows = validate_screenshots(client, schema, ctx, op)
                if [r['attributes'].get('fileName') for r in rows] != [f['fileName'] for f in op['files']]:
                    raise RuntimeError('Screenshot order/count differs')
                for row, file in zip(rows, op['files']):
                    if row['attributes'].get('sourceFileChecksum') != file['md5'] or row['attributes'].get('assetDeliveryState', {}).get('state') != 'COMPLETE':
                        raise RuntimeError('Screenshot checksum/processing differs')
            except RuntimeError as error:
                problems.append({'operation': index, 'reason': str(error)})
        else:
            _, target, _, _ = owner_context(ctx, op)
            for field, value in op['after'].items():
                if not target or target['attributes'].get(field) != value:
                    problems.append({'operation': index, 'field': field, 'reason': 'Value differs/missing'})
    return {'format': 1, 'status': 'VERIFIED' if not problems else 'NEEDS_ACTION', 'checkedAt': now(),
            'appId': plan['appId'], 'planSha256': plan['planSha256'], 'checkedOperations': len(plan['operations']), 'problems': problems}


def apply_plan(client, schema, plan, runs_dir, asset_wait=45):
    check_plan(plan)
    if client.readonly:
        raise RuntimeError('Write client required; pass --apply')
    run_dir = Path(runs_dir) / plan['planSha256']
    private_directory(run_dir)
    with app_lock(runs_dir, plan['account'], plan['appId']):
        ctx = live_context(client, plan)
        preflight(client, schema, plan, ctx)
        before = run_dir / 'before.json'
        if not before.exists():
            save_json(before, ctx)
        save_json(run_dir / 'plan.json', plan)
        receipt = {'status': 'RUNNING', 'appId': plan['appId'], 'planSha256': plan['planSha256'],
                   'startedAt': now(), 'completed': [], 'error': None}
        save_json(run_dir / 'receipt.json', receipt)
        try:
            for index, op in enumerate(plan['operations']):
                if op['action'] == 'screenshots':
                    ctx = live_context(client, plan)
                    validate_screenshots(client, schema, ctx, op)
                    upload_screenshots(client, schema, plan, ctx, op, run_dir, asset_wait)
                else:
                    # Recheck immediately before each write, not only at start.
                    fresh = live_context(client, plan)
                    preflight(client, schema, {**plan, 'operations': [op]}, fresh)
                    owner, target, _, relation = owner_context(fresh, op)
                    attrs = {k: v for k, v in op['after'].items() if not target or target['attributes'].get(k) != v}
                    if target and attrs:
                        body = schema.payload(op['kind'], 'patch', attrs, resource_id=target['id'])
                        client.request('PATCH', schema.endpoint(op['kind'], 'patch').replace('{id}', checked_id(target['id'])), body)
                    elif not target:
                        body = schema.payload(op['kind'], 'post', {'locale': op['locale'], **attrs}, {relation: {'data': linkage(owner)}})
                        client.request('POST', schema.endpoint(op['kind'], 'post'), body)
                    ctx = live_context(client, plan)
                receipt['completed'].append(index)
                save_json(run_dir / 'receipt.json', receipt)
            report = audit_plan(client, schema, plan)
            save_json(run_dir / 'audit.json', report)
            receipt['status'] = report['status']
            receipt['finishedAt'] = now()
            save_json(run_dir / 'receipt.json', receipt)
            return report
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            receipt.update(status='NEEDS_ACTION', error=str(error), finishedAt=now())
            save_json(run_dir / 'receipt.json', receipt)
            raise
