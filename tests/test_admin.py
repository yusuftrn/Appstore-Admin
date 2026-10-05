"""Offline safety/functional tests for the standalone management interface."""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import struct
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from appstore_admin import cli, config, core, metadata, operations

SCHEMA = Path(__file__).resolve().parents[1] / 'src/appstore_admin/data/openapi-4.5.json'


def row(kind, identity, **attrs):
    return {'type': kind, 'id': identity, 'attributes': attrs}


class FakeAPI:
    def __init__(self, readonly=True):
        self.profile = {'issuer_id': 'my-account', 'key_id': 'not-secret'}
        self.readonly = readonly
        self.calls = []
        self.app = row('apps', 'app', bundleId='com.example.app', name='Example')
        self.info = row('appInfos', 'info', appStoreState='PREPARE_FOR_SUBMISSION')
        self.version = row('appStoreVersions', 'version', platform='IOS', versionString='1.2', appVersionState='PREPARE_FOR_SUBMISSION', copyright='Old')
        self.info_loc = row('appInfoLocalizations', 'info-loc', locale='en-US', name='Example', subtitle='Old')
        self.version_loc = row('appStoreVersionLocalizations', 'version-loc', locale='en-US', description='Old', keywords='old')
        self.rows = {core.key(r): r for r in (self.app, self.info, self.version, self.info_loc, self.version_loc)}
        self.edges = {'/v1/apps/app/appInfos': [self.info], '/v1/apps/app/appStoreVersions': [self.version],
                      '/v1/appInfos/info/appInfoLocalizations': [self.info_loc],
                      '/v1/appStoreVersions/version/appStoreVersionLocalizations': [self.version_loc]}
        self.index = 0

    def pages(self, path, params=None):
        self.calls.append(('GET', path, None))
        if path == '/v1/apps':
            return [copy.deepcopy(self.app)], []
        return copy.deepcopy(self.edges.get(path, [])), []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == 'GET':
            pieces = path.split('/')
            return {'data': copy.deepcopy(self.rows[pieces[2] + ':' + pieces[3]])}
        if self.readonly:
            raise AssertionError('Unexpected write on readonly client')
        if method == 'PATCH':
            if '/relationships/' in path:
                records = [self.rows[core.key(ref)] for ref in body['data']]
                self.edges[path.replace('/relationships/', '/')] = records
                return {}
            data = body['data']
            record = self.rows[core.key(data)]
            record['attributes'].update(data.get('attributes', {}))
            return {'data': copy.deepcopy(record)}
        if method == 'POST':
            self.index += 1
            data = body['data']
            record = row(data['type'], 'created-' + str(self.index), **data.get('attributes', {}))
            if data['type'] == 'appScreenshots' and getattr(self, 'uploaded_checksum', None):
                record['attributes'].update(sourceFileChecksum=self.uploaded_checksum,
                                            assetDeliveryState={'state': 'COMPLETE'})
            self.rows[core.key(record)] = record
            for value in data.get('relationships', {}).values():
                parent = value['data']
                relation = {'appStoreVersions': 'appStoreVersions', 'appInfoLocalizations': 'appInfoLocalizations',
                            'appStoreVersionLocalizations': 'appStoreVersionLocalizations',
                            'appScreenshotSets': 'appScreenshotSets', 'appScreenshots': 'appScreenshots'}[data['type']]
                self.edges.setdefault(f'/v1/{parent["type"]}/{parent["id"]}/{relation}', []).append(record)
            return {'data': copy.deepcopy(record)}
        raise AssertionError(method)


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = core.schema_at(SCHEMA)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.client = FakeAPI()
        self.doc = {'format': 1, 'appId': 'app', 'bundleId': 'com.example.app', 'platform': 'IOS',
                    'versionId': 'version', 'appInfoId': 'info', 'locales': {
                        'en-US': {'appInfo': {'subtitle': 'New'}, 'version': {'description': 'New', 'keywords': 'privacy'}}}}

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return metadata.make_plan(self.client, self.schema, self.doc, self.directory)

    def test_plan_has_only_changes_and_never_writes(self):
        self.doc['locales']['en-US']['appInfo']['name'] = 'Example'
        plan = self.plan()
        self.assertEqual(len(plan['operations']), 2)
        self.assertEqual(plan['operations'][0]['after'], {'subtitle': 'New'})
        self.assertTrue(all(call[0] == 'GET' for call in self.client.calls))

    def test_apply_updates_metadata_and_live_audit_then_noop_resume(self):
        plan = self.plan()
        self.client.readonly = False
        report = operations.apply_plan(self.client, self.schema, plan, self.directory / 'runs')
        self.assertEqual(report['status'], 'VERIFIED')
        self.assertTrue((self.directory / 'runs' / plan['planSha256'] / 'before.json').exists())
        writes = [call for call in self.client.calls if call[0] != 'GET']
        operations.apply_plan(self.client, self.schema, plan, self.directory / 'runs')
        self.assertEqual(writes, [call for call in self.client.calls if call[0] != 'GET'])
        self.assertTrue(all(call[0] == 'PATCH' for call in writes))

    def test_readonly_apply_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Write client'):
            operations.apply_plan(self.client, self.schema, self.plan(), self.directory)

    def test_create_locales_both_sections_and_idempotent_resume(self):
        self.doc['locales'] = {'tr': {'appInfo': {'name': 'Örnek', 'subtitle': 'Gizlilik'}, 'version': {'description': 'Yeni açıklama'}}}
        plan = self.plan()
        self.client.readonly = False
        self.assertEqual(operations.apply_plan(self.client, self.schema, plan, self.directory)['status'], 'VERIFIED')
        writes = sum(c[0] == 'POST' for c in self.client.calls)
        self.assertEqual(writes, 2)
        operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(sum(c[0] == 'POST' for c in self.client.calls), 2)

    def test_bundle_mismatch_stops_before_writes(self):
        self.doc['bundleId'] = 'different.bundle'
        with self.assertRaisesRegex(RuntimeError, 'bundle ID mismatch'):
            self.plan()

    def test_published_version_not_editable(self):
        self.client.version['attributes']['appVersionState'] = 'READY_FOR_DISTRIBUTION'
        with self.assertRaisesRegex(RuntimeError, 'not an editable draft'):
            self.plan()

    def test_in_review_info_not_editable(self):
        self.client.info['attributes']['appStoreState'] = 'IN_REVIEW'
        with self.assertRaisesRegex(RuntimeError, 'not an editable draft'):
            self.plan()

    def test_context_filters_platform_and_checks_version_ownership(self):
        self.doc['versionId'] = 'other-app-version'
        with self.assertRaisesRegex(RuntimeError, 'found 0'):
            self.plan()

    def test_changed_account_refused(self):
        plan = self.plan()
        self.client.profile['issuer_id'] = 'another-account'
        with self.assertRaisesRegex(RuntimeError, 'another Apple account'):
            operations.live_context(self.client, plan)

    def test_plan_tampering_detected(self):
        plan = self.plan()
        plan['operations'][0]['after']['subtitle'] = 'Tampered'
        with self.assertRaisesRegex(RuntimeError, 'edited or damaged'):
            metadata.check_plan(plan)

    def test_concurrent_edit_detected_before_any_write(self):
        plan = self.plan()
        self.client.version_loc['attributes']['keywords'] = 'someone,else'
        self.client.readonly = False
        with self.assertRaisesRegex(RuntimeError, 'Concurrent metadata change'):
            operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertFalse(any(c[0] != 'GET' for c in self.client.calls))

    def test_unrelated_resource_id_cannot_be_used(self):
        plan = self.plan()
        plan['operations'][0]['id'] = 'other-app-locale'
        plan.pop('planSha256')
        plan['planSha256'] = metadata.sha_json(plan)
        ctx = operations.live_context(self.client, plan)
        with self.assertRaisesRegex(RuntimeError, 'not owned'):
            operations.preflight(self.client, self.schema, plan, ctx)

    def test_unsupported_mutation_rejected_even_with_recalculated_hash(self):
        plan = self.plan()
        plan['operations'][0]['kind'] = 'subscriptions'
        plan.pop('planSha256')
        plan['planSha256'] = metadata.sha_json(plan)
        ctx = operations.live_context(self.client, plan)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported metadata'):
            operations.preflight(self.client, self.schema, plan, ctx)

    def test_character_limits_and_unknown_fields(self):
        for fields in ({'name': 'x' * 31}, {'subtitle': None}, {'bundleId': 'other'}, {'privacyPolicyUrl': 'file:///private/key'}):
            with self.subTest(fields=fields):
                self.doc['locales']['en-US']['appInfo'] = fields
                with self.assertRaises(RuntimeError):
                    self.plan()

    def test_empty_string_clear_is_explicit_and_not_null(self):
        self.doc['locales']['en-US']['appInfo'] = {'subtitle': ''}
        plan = self.plan()
        self.client.readonly = False
        operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(self.client.info_loc['attributes']['subtitle'], '')

    def test_export_readonly_and_only_allowlisted_metadata(self):
        self.client.info_loc['attributes']['secretUnknownField'] = 'not-exported'
        doc = metadata.export_metadata(self.client, 'app', 'com.example.app')
        self.assertEqual(doc['locales']['en-US']['version']['keywords'], 'old')
        self.assertNotIn('secretUnknownField', doc['locales']['en-US']['appInfo'])
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def png(self, filename='image.PNG'):
        path = self.directory / filename
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 1290, 2796) + b'fixture')
        return path

    def test_screenshot_plan_is_local_hashed_and_no_deletes(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        op = self.plan()['operations'][-1]
        self.assertEqual(op['action'], 'screenshots')
        self.assertEqual(op['files'][0]['sha256'], core.digest(path))
        self.assertTrue(op['files'][0]['fileName'].endswith('.PNG'))

    def test_truncated_png_is_rejected_cleanly(self):
        path = self.directory / 'truncated.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR')
        with self.assertRaisesRegex(RuntimeError, 'Invalid PNG'):
            metadata.screenshot_file(path)

    def test_screenshot_existing_other_images_refused(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        set_row = row('appScreenshotSets', 'set', screenshotDisplayType='APP_IPHONE_67')
        self.client.edges['/v1/appStoreVersionLocalizations/version-loc/appScreenshotSets'] = [set_row]
        self.client.edges['/v1/appScreenshotSets/set/appScreenshots'] = [row('appScreenshots', 'old', fileName='unrelated.png')]
        with self.assertRaisesRegex(RuntimeError, 'other images'):
            self.plan()

    def test_screenshot_changed_bytes_fail_preflight(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        plan = self.plan()
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(RuntimeError, 'changed since plan'):
            operations.preflight(self.client, self.schema, plan, operations.live_context(self.client, plan))

    def test_screenshot_resume_reuses_complete_sha_matching_media(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        plan = self.plan()
        file = plan['operations'][-1]['files'][0]
        set_row = row('appScreenshotSets', 'set', screenshotDisplayType='APP_IPHONE_67')
        shot = row('appScreenshots', 'shot', fileName=file['fileName'], sourceFileChecksum=file['md5'], assetDeliveryState={'state': 'COMPLETE'})
        self.client.rows[core.key(set_row)], self.client.rows[core.key(shot)] = set_row, shot
        self.client.edges['/v1/appStoreVersionLocalizations/version-loc/appScreenshotSets'] = [set_row]
        self.client.edges['/v1/appScreenshotSets/set/appScreenshots'] = [shot]
        self.client.readonly = False
        report = operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(report['status'], 'VERIFIED')
        self.assertFalse(any(c[0] == 'POST' for c in self.client.calls))

    def test_screenshot_duplicates_invalid_type_and_path_refused(self):
        path = self.png()
        for groups in ({'INVALID': [path.name]}, {'APP_IPHONE_67': [path.name, path.name]}, {'APP_IPHONE_67': ['missing.png']}):
            self.doc['locales']['en-US']['screenshots'] = groups
            with self.subTest(groups=groups), self.assertRaises(RuntimeError):
                self.plan()

    def test_screenshot_upload_creates_set_and_records_reservation(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        plan = self.plan()
        self.client.uploaded_checksum = core.digest(path, 'md5')
        self.client.readonly = False
        report = operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(report['status'], 'VERIFIED')
        media_posts = [call for call in self.client.calls if call[0] == 'POST']
        self.assertEqual([call[1] for call in media_posts], ['/v1/appScreenshotSets', '/v1/appScreenshots'])
        screenshot = media_posts[-1][2]['data']
        self.assertEqual(screenshot['relationships']['appScreenshotSet']['data']['type'], 'appScreenshotSets')
        self.assertEqual(screenshot['attributes']['fileName'], plan['operations'][-1]['files'][0]['fileName'])
        journals = list((self.directory / plan['planSha256']).glob('*-assets.json'))
        self.assertEqual(len(journals), 1)
        self.assertIn('appScreenshots:' + core.digest(path), core.read_json(journals[0])['mapping'])
        operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(media_posts, [c for c in self.client.calls if c[0] == 'POST'])

    def test_missing_saved_screenshot_refuses_duplicate_reservation(self):
        path = self.png()
        self.doc['locales']['en-US']['screenshots'] = {'APP_IPHONE_67': [path.name]}
        plan = self.plan()
        self.client.uploaded_checksum = core.digest(path, 'md5')
        self.client.readonly = False
        operations.apply_plan(self.client, self.schema, plan, self.directory)
        target = self.client.edges['/v1/appStoreVersionLocalizations/version-loc/appScreenshotSets'][0]
        self.client.edges[f'/v1/appScreenshotSets/{target["id"]}/appScreenshots'] = []
        writes = [c for c in self.client.calls if c[0] == 'POST']
        with self.assertRaisesRegex(RuntimeError, 'reservation is not visible'):
            operations.apply_plan(self.client, self.schema, plan, self.directory)
        self.assertEqual(writes, [c for c in self.client.calls if c[0] == 'POST'])

    def test_app_lock_prevents_parallel_apply(self):
        with operations.app_lock(self.directory, 'account', 'app'):
            with self.assertRaisesRegex(RuntimeError, 'Another apply'):
                with operations.app_lock(self.directory, 'account', 'app'):
                    pass

    def test_apply_failure_saves_receipt_and_keeps_partial_changes(self):
        plan = self.plan()
        self.client.readonly = False
        original = self.client.request
        def request(method, path, body=None, params=None):
            if method == 'PATCH' and 'appStoreVersionLocalizations' in path:
                raise RuntimeError('API transient error; not automatically retried')
            return original(method, path, body, params)
        with mock.patch.object(self.client, 'request', side_effect=request):
            with self.assertRaisesRegex(RuntimeError, 'transient'):
                operations.apply_plan(self.client, self.schema, plan, self.directory)
        receipt = core.read_json(self.directory / plan['planSha256'] / 'receipt.json')
        self.assertEqual(receipt['status'], 'NEEDS_ACTION')
        self.assertEqual(receipt['completed'], [0])
        self.assertEqual(self.client.info_loc['attributes']['subtitle'], 'New')

    def test_default_cli_apply_only_preflights_readonly(self):
        plan = self.plan()
        path = self.directory / 'plan.json'
        core.save_json(path, plan)
        with mock.patch.object(cli, 'client_for', return_value=self.client) as client, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--schema', str(SCHEMA), 'apply', '--plan', str(path)]), 0)
        self.assertTrue(client.call_args.kwargs['readonly'])
        self.assertTrue(all(c[0] == 'GET' for c in self.client.calls))

    def test_draft_creation_default_is_readonly(self):
        self.client.version['attributes']['appVersionState'] = 'READY_FOR_DISTRIBUTION'
        args = argparse.Namespace(app='app', bundle='com.example.app', platform='IOS', version='1.3', apply=False)
        with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(self.directory)}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.create_draft(self.client, self.schema, args), 0)
        self.assertFalse(any(c[0] != 'GET' for c in self.client.calls))

    def test_draft_refuses_when_other_draft_exists(self):
        args = argparse.Namespace(app='app', bundle='com.example.app', platform='IOS', version='1.3', apply=False)
        with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(self.directory)}):
            with self.assertRaisesRegex(RuntimeError, 'existing draft'):
                cli.create_draft(self.client, self.schema, args)

    def test_profile_import_has_private_permissions_and_no_source_dependency(self):
        source = self.directory / 'input.json'
        private = self.directory / 'source-key.p8'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(private)], check=True, capture_output=True)
        private.chmod(0o600)
        source.write_text(json.dumps({'key_id': 'key', 'issuer_id': 'issuer', 'private_key': private.name}))
        source.chmod(0o600)
        home = self.directory / 'admin'
        with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(home)}):
            imported = config.import_account(source)
            self.assertEqual(core.read_json(imported)['private_key'], 'AuthKey.p8')
            self.assertEqual((imported.parent / 'AuthKey.p8').read_bytes(), private.read_bytes())
            self.assertEqual(imported.stat().st_mode & 0o777, 0o600)
            self.assertEqual(imported.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual((imported.parent / 'AuthKey.p8').stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                config.import_account(source)

    def test_output_does_not_overwrite_files(self):
        path = self.directory / 'output.json'
        path.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'Output exists'):
            cli.unique_output(path, {})
        self.assertEqual(path.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
