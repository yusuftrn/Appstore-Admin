"""Narrow metadata planner: app ownership, draft states, optimistic concurrency."""
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit

from .config import account_identity
from .core import EDITABLE, LIVE, digest, now, state_of, version_key

INFO_FIELDS = {'name', 'subtitle', 'privacyPolicyUrl', 'privacyChoicesUrl', 'privacyPolicyText'}
VERSION_FIELDS = {'description', 'keywords', 'marketingUrl', 'promotionalText', 'supportUrl', 'whatsNew'}
LENGTHS = {'name': 30, 'subtitle': 30, 'keywords': 100, 'description': 4000,
           'promotionalText': 170, 'whatsNew': 4000}
PLATFORMS = ('IOS', 'MAC_OS', 'TV_OS', 'VISION_OS')


def sha_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]+', value):
        raise RuntimeError('Invalid Apple resource ID')
    return value


def children(client, parent, relation):
    return client.pages(f'/v1/{parent["type"]}/{checked_id(parent["id"])}/{relation}', {'limit': 200})[0]


def select_record(rows, identifier, kind, platform=None, editable=False):
    if platform:
        rows = [row for row in rows if row['attributes'].get('platform') == platform]
    if identifier:
        rows = [row for row in rows if row['id'] == identifier or row['attributes'].get('versionString') == identifier]
    elif editable:
        rows = [row for row in rows if state_of(row) in EDITABLE]
    else:
        drafts = [row for row in rows if state_of(row) in EDITABLE]
        if drafts:
            rows = drafts
        else:
            live = [row for row in rows if state_of(row) in LIVE]
            if kind == 'appStoreVersions' and live:
                rows = [max(live, key=lambda r: version_key(r['attributes'].get('versionString', '0')))]
            elif live:
                rows = live
    if len(rows) != 1:
        raise RuntimeError(f'Choose one {kind} ID/version explicitly; found {len(rows)} matching records')
    if editable and state_of(rows[0]) not in EDITABLE:
        raise RuntimeError(f'{kind} is not an editable draft ({state_of(rows[0])})')
    return rows[0]


def context(client, app_id, bundle_id, version=None, info=None, platform='IOS', editable=False):
    if platform not in PLATFORMS:
        raise RuntimeError('Unsupported platform')
    app = client.request('GET', '/v1/apps/' + checked_id(app_id))['data']
    if app['attributes'].get('bundleId') != bundle_id:
        raise RuntimeError('App/bundle ID mismatch; no writes made')
    versions = children(client, app, 'appStoreVersions')
    infos = children(client, app, 'appInfos')
    return {'app': app,
            'version': select_record(versions, version, 'appStoreVersions', platform, editable),
            'info': select_record(infos, info, 'appInfos', editable=editable)}


def export_metadata(client, app_id, bundle_id, version=None, info=None, platform='IOS'):
    ctx = context(client, app_id, bundle_id, version, info, platform)
    document = {'format': 1, 'appId': app_id, 'bundleId': bundle_id, 'platform': platform,
                'versionId': ctx['version']['id'], 'appInfoId': ctx['info']['id'],
                'versionAttributes': {k: v for k, v in ctx['version']['attributes'].items()
                                      if k == 'copyright' and isinstance(v, str)},
                'locales': {}, '_inventory': {'versionString': ctx['version']['attributes'].get('versionString'),
                    'versionState': state_of(ctx['version']), 'infoState': state_of(ctx['info']),
                    'exportedAt': now(), 'media': {}}}
    for owner, relation, section, fields in ((ctx['info'], 'appInfoLocalizations', 'appInfo', INFO_FIELDS),
                                            (ctx['version'], 'appStoreVersionLocalizations', 'version', VERSION_FIELDS)):
        for row in children(client, owner, relation):
            locale = row['attributes']['locale']
            document['locales'].setdefault(locale, {})[section] = {
                k: v for k, v in row['attributes'].items() if k in fields and isinstance(v, str)}
            if section == 'version':
                media = []
                for set_row in children(client, row, 'appScreenshotSets'):
                    media.append({'id': set_row['id'], 'displayType': set_row['attributes']['screenshotDisplayType'],
                                  'screenshots': [{'id': shot['id'], 'fileName': shot['attributes'].get('fileName'),
                                       'state': shot['attributes'].get('assetDeliveryState', {}).get('state')}
                                       for shot in children(client, set_row, 'appScreenshots')]})
                document['_inventory']['media'][locale] = media
    return document


def validate_fields(fields, allowed, schema, kind, method):
    if not isinstance(fields, dict) or set(fields) - allowed:
        raise RuntimeError(f'Unknown/unsupported fields for {kind}: {sorted(set(fields) - allowed) if isinstance(fields, dict) else "not an object"}')
    spec = schema.request_schema(kind, method)
    if not spec:
        raise RuntimeError('Public API schema has no operation: ' + kind)
    props = schema.resolve(spec['properties'].get('attributes', {})).get('properties', {})
    for field, value in fields.items():
        if field not in props or props[field].get('deprecated'):
            raise RuntimeError('Unsupported schema attribute: ' + kind + '.' + field)
        if not isinstance(value, str):
            raise RuntimeError('Use strings, not null/non-text values: ' + field)
        if field == 'name' and not value.strip():
            raise RuntimeError('App name cannot be empty')
        if field in LENGTHS and len(value) > LENGTHS[field]:
            raise RuntimeError(f'{field} exceeds {LENGTHS[field]} characters')
        if field.endswith('Url') and value:
            parsed = urlsplit(value)
            if parsed.scheme not in {'https', 'http'} or not parsed.netloc:
                raise RuntimeError('Expected an absolute HTTP(S) URL: ' + field)


def screenshot_file(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError('Screenshot file missing: ' + str(path))
    if path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
        raise RuntimeError('Screenshots must be PNG/JPEG')
    with path.open('rb') as handle:
        header = handle.read(24)
    if path.suffix.lower() == '.png':
        if len(header) < 24 or not header.startswith(b'\x89PNG\r\n\x1a\n') or header[12:16] != b'IHDR':
            raise RuntimeError('Invalid PNG screenshot')
        width, height = struct.unpack('>II', header[16:24])
        if width == 0 or height == 0:
            raise RuntimeError('Invalid PNG dimensions')
    elif not header.startswith(b'\xff\xd8\xff'):
        raise RuntimeError('Invalid JPEG screenshot')
    if not 0 < path.stat().st_size <= 50 * 1024 * 1024:
        raise RuntimeError('Screenshot must be at most 50 MiB')
    sha = digest(path)
    return {'path': str(path), 'sha256': sha, 'md5': digest(path, 'md5'), 'size': path.stat().st_size,
            'fileName': 'screenshot--' + sha + path.suffix}


def index_locales(rows):
    result = {}
    for row in rows:
        locale = row['attributes']['locale']
        if locale in result:
            raise RuntimeError('Duplicate locale in Apple response')
        result[locale] = row
    return result


def media_fingerprint(rows):
    return [{'id': row['id'], 'fileName': row['attributes'].get('fileName'),
             'checksum': row['attributes'].get('sourceFileChecksum'),
             'state': row['attributes'].get('assetDeliveryState', {}).get('state')}
            for row in rows]


def make_plan(client, schema, document, document_dir):
    if not isinstance(document, dict) or document.get('format') != 1:
        raise RuntimeError('Unsupported metadata document format')
    allowed_top = {'format', 'appId', 'bundleId', 'platform', 'versionId', 'appInfoId', 'versionAttributes', 'locales', '_inventory'}
    if set(document) - allowed_top:
        raise RuntimeError('Unknown document keys: ' + ', '.join(sorted(set(document) - allowed_top)))
    ctx = context(client, document['appId'], document['bundleId'], document.get('versionId'),
                  document.get('appInfoId'), document.get('platform', 'IOS'), editable=True)
    plan = {'format': 1, 'account': account_identity(client.profile), 'appId': ctx['app']['id'],
            'bundleId': document['bundleId'], 'platform': document.get('platform', 'IOS'),
            'versionId': ctx['version']['id'], 'appInfoId': ctx['info']['id'], 'createdAt': now(),
            'documentSha256': sha_json(document), 'operations': []}
    attrs = document.get('versionAttributes', {})
    validate_fields(attrs, {'copyright'}, schema, 'appStoreVersions', 'patch')
    differences = {k: v for k, v in attrs.items() if ctx['version']['attributes'].get(k) != v}
    if differences:
        plan['operations'].append({'action': 'patch', 'kind': 'appStoreVersions', 'id': ctx['version']['id'],
                                   'before': {k: ctx['version']['attributes'].get(k) for k in differences}, 'after': differences})
    info_rows = index_locales(children(client, ctx['info'], 'appInfoLocalizations'))
    version_rows = index_locales(children(client, ctx['version'], 'appStoreVersionLocalizations'))
    locales = document.get('locales', {})
    if not isinstance(locales, dict):
        raise RuntimeError('locales must be an object')
    for locale, sections in sorted(locales.items()):
        if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', locale):
            raise RuntimeError('Invalid locale')
        if not isinstance(sections, dict) or set(sections) - {'appInfo', 'version', 'screenshots'}:
            raise RuntimeError('Unknown locale section: ' + locale)
        for section, rows, kind, fields, owner in (
            ('appInfo', info_rows, 'appInfoLocalizations', INFO_FIELDS, ctx['info']),
            ('version', version_rows, 'appStoreVersionLocalizations', VERSION_FIELDS, ctx['version'])):
            desired = sections.get(section)
            if desired is None:
                continue
            target = rows.get(locale)
            method = 'patch' if target else 'post'
            validate_fields(desired, fields, schema, kind, method)
            if not target and section == 'appInfo' and not desired.get('name'):
                raise RuntimeError('New appInfo locale requires name: ' + locale)
            if not target and section == 'version' and locale not in info_rows and 'appInfo' not in sections:
                raise RuntimeError('New version locale also needs appInfo metadata: ' + locale)
            changed = {k: v for k, v in desired.items() if not target or target['attributes'].get(k) != v}
            if not target or changed:
                plan['operations'].append({'action': method, 'kind': kind, 'locale': locale,
                    'id': target['id'] if target else None, 'owner': {'type': owner['type'], 'id': owner['id']},
                    'before': {k: target['attributes'].get(k) for k in changed} if target else None,
                    'after': changed})
        screenshot_groups = sections.get('screenshots', {})
        if not isinstance(screenshot_groups, dict):
            raise RuntimeError('screenshots must map display types to file lists')
        display_spec = schema.request_schema('appScreenshotSets', 'post')['properties']['attributes']['properties']['screenshotDisplayType']
        displays = schema.resolve(display_spec).get('enum', [])
        for display, paths in sorted(screenshot_groups.items()):
            if display not in displays or not isinstance(paths, list) or not 1 <= len(paths) <= 10:
                raise RuntimeError('Invalid screenshot display type/list (1–10): ' + display)
            if locale not in version_rows and 'version' not in sections:
                raise RuntimeError('Screenshot locale requires version metadata')
            files = [screenshot_file(Path(document_dir) / file) for file in paths if isinstance(file, str)]
            if len(files) != len(paths) or len({f['sha256'] for f in files}) != len(files):
                raise RuntimeError('Screenshot paths must be unique image files')
            owner = version_rows.get(locale)
            sets = children(client, owner, 'appScreenshotSets') if owner else []
            matches = [row for row in sets if row['attributes']['screenshotDisplayType'] == display]
            if len(matches) > 1:
                raise RuntimeError('Duplicate screenshot sets')
            target = matches[0] if matches else None
            existing = children(client, target, 'appScreenshots') if target else []
            filenames = {f['fileName']: f for f in files}
            for row in existing:
                file = filenames.get(row['attributes'].get('fileName'))
                if not file or row['attributes'].get('sourceFileChecksum') not in {None, file['md5']}:
                    raise RuntimeError('Set contains other images; replacement/deletion is intentionally unsupported. Use a clean set or manage removal in App Store Connect.')
            expected_names = [file['fileName'] for file in files]
            if [row['attributes'].get('fileName') for row in existing] == expected_names and all(
                    row['attributes'].get('sourceFileChecksum') == file['md5'] and
                    row['attributes'].get('assetDeliveryState', {}).get('state') == 'COMPLETE'
                    for row, file in zip(existing, files)):
                continue
            plan['operations'].append({'action': 'screenshots', 'locale': locale, 'displayType': display,
                                       'setId': target['id'] if target else None,
                                       'before': media_fingerprint(existing), 'files': files})
    plan['planSha256'] = sha_json(plan)
    return plan


def check_plan(plan):
    if not isinstance(plan, dict):
        raise RuntimeError('Plan must be a JSON object')
    unsigned = copy.deepcopy(plan)
    expected = unsigned.pop('planSha256', None)
    if expected != sha_json(unsigned):
        raise RuntimeError('Plan was edited or damaged; regenerate the plan')
    if plan.get('format') != 1:
        raise RuntimeError('Unsupported plan format')
    if not isinstance(plan.get('operations'), list):
        raise RuntimeError('Plan operations must be a list')
    for field in ('appId', 'versionId', 'appInfoId'):
        checked_id(plan.get(field))


def sanitized_summary(plan):
    return {'appId': plan['appId'], 'bundleId': plan['bundleId'], 'versionId': plan['versionId'],
            'planSha256': plan['planSha256'], 'changes': [
                {'action': op['action'], 'locale': op.get('locale'), 'kind': op.get('kind', 'appScreenshots'),
                 'fields': sorted(op.get('after', {})), 'files': len(op.get('files', []))}
                for op in plan['operations']]}
