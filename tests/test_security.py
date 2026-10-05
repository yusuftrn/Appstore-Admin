"""Offline adversarial checks using fictional data and generated temporary keys."""
import argparse
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zipfile
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
from appstore_admin import cli, config, core, media, metadata, operations, security
import check_distribution as distribution
import install_skill
import build_release
from test_admin import FakeAPI, row


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_temp = tempfile.TemporaryDirectory()
        cls.key = Path(cls.key_temp.name) / 'generated.p8'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(cls.key)], check=True, capture_output=True)
        cls.schema = core.schema_at(ROOT / 'src/appstore_admin/data/openapi-4.5.json')

    @classmethod
    def tearDownClass(cls):
        cls.key_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.key = self.directory / 'generated.p8'
        self.key.write_bytes(type(self).key.read_bytes())
        self.key.chmod(0o600)
        self.profile = self.directory / 'profile.json'
        core.save_json(self.profile, {'issuer_id': 'EXAMPLE-ISSUER', 'key_id': 'EXAMPLEKEY', 'private_key': self.key.name})

    def tearDown(self):
        self.temp.cleanup()

    def test_group_readable_credentials_refused(self):
        for target in (self.profile, self.key):
            with self.subTest(target=target.name):
                target.chmod(0o640)
                with self.assertRaisesRegex(RuntimeError, '600'):
                    core.Client(self.profile)
                target.chmod(0o600)

    def test_symlink_profile_and_key_refused(self):
        link = self.directory / 'link.json'
        link.symlink_to(self.profile)
        with self.assertRaisesRegex(RuntimeError, 'symlinks'):
            core.Client(link)
        key_link = self.directory / 'linked.p8'
        key_link.symlink_to(self.key)
        core.save_json(self.profile, {'key_id': 'EXAMPLEKEY', 'private_key': key_link.name})
        with self.assertRaisesRegex(RuntimeError, 'symlinks'):
            core.Client(self.profile)

    def test_private_directory_symlink_refused(self):
        link = self.directory / 'linked'
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlinks'):
            security.private_directory(link)

    def test_profile_unknown_fields_and_controls_refused_without_echo(self):
        for value in ({'key_id': 'KEY', 'private_key': 'key.p8', 'secret': 'do-not-print'},
                      {'key_id': 'KEY\nInjected', 'private_key': 'key.p8'}):
            with self.assertRaises(RuntimeError) as error:
                security.validate_profile(value)
            self.assertNotIn('do-not-print', str(error.exception))
            self.assertNotIn('Injected', str(error.exception))

    def test_wrong_curve_and_invalid_key_refused(self):
        invalid = self.directory / 'invalid.p8'
        invalid.write_bytes(b'not a signing key')
        invalid.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'ES256'):
            security.validate_signing_key(invalid)
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:secp384r1', '-out', str(invalid)], check=True, capture_output=True)
        invalid.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'P-256'):
            security.validate_signing_key(invalid)

    def test_default_home_is_outside_checkout(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.local_root().is_relative_to(ROOT))
            self.assertEqual(config.local_root(), Path.home() / '.local/share/appstore-admin')

    def test_home_override_cannot_chmod_home_or_checkout(self):
        for target in (Path.home(), ROOT, ROOT.parent, Path('/')):
            with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(target)}):
                with self.assertRaisesRegex(RuntimeError, 'dedicated'):
                    config.local_root()

    def test_import_failure_leaves_no_account_or_staged_key(self):
        home = self.directory / 'private-home'
        with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(home)}), mock.patch.object(config, 'save_json', side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError):
                config.import_account(self.profile)
        self.assertFalse((home / 'account').exists())
        self.assertEqual(list(home.glob('.account-import-*')), [])

    def test_two_imports_cannot_overwrite_account(self):
        home = self.directory / 'private-home'
        with mock.patch.dict(os.environ, {'APPSTORE_ADMIN_HOME': str(home)}):
            imported = config.import_account(self.profile)
            before = imported.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                config.import_account(self.profile)
        self.assertEqual(imported.read_bytes(), before)

    def test_atomic_json_ignores_predictable_tmp_symlink(self):
        victim = self.directory / 'victim.txt'
        victim.write_text('preserve')
        output = self.directory / 'output.json'
        output.with_name(output.name + '.tmp').symlink_to(victim)
        core.save_json(output, {'value': 1})
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_exclusive_json_refuses_existing_symlink(self):
        victim = self.directory / 'victim.txt'
        victim.write_text('preserve')
        output = self.directory / 'output.json'
        output.symlink_to(victim)
        with self.assertRaises(FileExistsError):
            core.save_json(output, {}, overwrite=False)
        self.assertEqual(victim.read_text(), 'preserve')
        self.assertEqual(list(self.directory.glob('.json-*')), [])

    def test_unique_output_race_cannot_overwrite(self):
        output = self.directory / 'new.json'
        original = cli.save_json
        def raced(path, value, overwrite=True):
            path.write_text('created by another process')
            original(path, value, overwrite=overwrite)
        with mock.patch.object(cli, 'save_json', side_effect=raced), self.assertRaises(FileExistsError):
            cli.unique_output(output, {})
        self.assertEqual(output.read_text(), 'created by another process')

    def test_authenticated_redirect_never_forwards_credentials(self):
        request = urllib.request.Request(core.API + '/v1/apps', headers={'Authorization': 'Bearer fictional'})
        with self.assertRaisesRegex(core.ApiError, 'redirect'):
            core.NoRedirect().redirect_request(request, None, 302, '', {}, 'https://example.com/')

    def test_malicious_api_hosts_and_mutations_refused_before_signing(self):
        client = core.Client(self.profile, readonly=False)
        targets = [('GET', 'https://api.appstoreconnect.apple.com.evil.test/v1/apps'),
                   ('GET', 'https://api.appstoreconnect.apple.com@evil.test/v1/apps'),
                   ('GET', '/v1/apps/../users'), ('DELETE', '/v1/appScreenshots/example'),
                   ('POST', '/v1/appStoreVersionSubmissions'), ('POST', '/v1/subscriptionPrices')]
        with mock.patch.object(client, 'bearer', side_effect=AssertionError('Must not sign rejected requests')):
            for method, path in targets:
                with self.subTest(method=method, path=path), self.assertRaises(RuntimeError):
                    client.request(method, path, {})

    def test_library_client_is_readonly_by_default(self):
        client = core.Client(self.profile)
        with self.assertRaisesRegex(RuntimeError, 'read-only'):
            client.request('POST', '/v1/appStoreVersions', {})

    def test_api_error_does_not_echo_metadata_or_token(self):
        client = core.Client(self.profile, readonly=False)
        details = 'private-copy fictional-secret-token https://signed.example/?secret=private'
        error = urllib.error.HTTPError(core.API + '/v1/appStoreVersions', 400, 'Rejected', {}, io.BytesIO(json.dumps({'errors': [{'code': 'ENTITY_ERROR', 'detail': details}]}).encode()))
        with mock.patch.object(client, 'bearer', return_value='fictional-secret-token'), mock.patch.object(client.opener, 'open', side_effect=error):
            with self.assertRaises(core.ApiError) as caught:
                client.request('POST', '/v1/appStoreVersions', {'data': {'attributes': {'description': 'private-copy'}}})
        self.assertIn('ENTITY_ERROR', str(caught.exception))
        for value in ('private-copy', 'fictional-secret-token', 'signed.example'):
            self.assertNotIn(value, str(caught.exception))

    def test_ambiguous_create_failure_is_never_replayed(self):
        client = core.Client(self.profile, readonly=False)
        error = urllib.error.HTTPError(core.API + '/v1/appScreenshots', 503, 'Unknown outcome', {}, io.BytesIO(b'{}'))
        with mock.patch.object(client, 'bearer', return_value='fictional'), mock.patch.object(client.opener, 'open', side_effect=error) as opened:
            with self.assertRaises(core.ApiError):
                client.request('POST', '/v1/appScreenshots', {})
        self.assertEqual(opened.call_count, 1)

    def test_individual_jwt_has_sub_instead_of_issuer(self):
        core.save_json(self.profile, {'key_id': 'EXAMPLEKEY', 'private_key': self.key.name})
        token = core.Client(self.profile).bearer()
        encoded = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        self.assertEqual(payload['sub'], 'user')
        self.assertNotIn('iss', payload)

    def test_asset_local_hosts_credentials_ports_and_lookalikes_refused(self):
        for url in ('http://cdn.mzstatic.com/file', 'https://localhost/file', 'https://127.0.0.1/file',
                    'https://[::1]/file', 'https://169.254.169.254/file', 'https://apple.com.evil.test/file',
                    'https://apple.com@localhost/file', 'https://user:password@cdn.apple.com/file',
                    'https://cdn.apple.com:444/file', 'https://example.com/file'):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                security.external_url(url)
        self.assertEqual(security.external_url('https://cdn.mzstatic.com/file'), 'https://cdn.mzstatic.com/file')
        self.assertEqual(security.external_url('https://bucket.s3.amazonaws.com/file?signature=fictional'), 'https://bucket.s3.amazonaws.com/file?signature=fictional')

    def test_download_redirect_rechecks_domain(self):
        request = urllib.request.Request('https://cdn.mzstatic.com/image')
        with self.assertRaises(RuntimeError):
            core.HttpsRedirect().redirect_request(request, None, 302, '', {}, 'https://localhost/private')

    def test_upload_header_injection_and_credentials_refused(self):
        for name, value in (('Authorization', 'Bearer fictional'), ('Cookie', 'secret=fictional'),
                            ('Host', 'localhost'), ('X-Header', 'a\r\nInjected: yes')):
            with self.assertRaises(RuntimeError):
                security.upload_headers([{'name': name, 'value': value}])

    def png(self):
        import struct
        path = self.directory / 'shot.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 100, 100) + b'fictional image')
        return path

    def uploader(self, operations_list):
        path = self.png()
        file = metadata.screenshot_file(path)
        client = FakeAPI(readonly=False)
        state = {'mapping': {}, 'assetHashes': {}}
        source = {'type': 'appScreenshots', 'id': file['sha256']}
        reservation = row('appScreenshots', 'reserved', uploadOperations=operations_list)
        actual = client.request
        def api(method, target, body=None, params=None):
            if method == 'POST':
                client.rows[core.key(reservation)] = reservation
                return {'data': copy.deepcopy(reservation)}
            if method == 'PATCH':
                value = actual(method, target, body, params)
                client.rows[core.key(reservation)]['attributes']['assetDeliveryState'] = {'state': 'COMPLETE'}
                return {'data': copy.deepcopy(client.rows[core.key(reservation)])}
            return actual(method, target, body, params)
        client.request = api
        engine = media.ScreenshotUploader(client, self.schema, operations.LocalImages([file]), state, self.directory / 'assets.json', 0)
        return engine, source, path

    def test_chunks_upload_bytes_without_api_authorization_and_commit(self):
        path = self.png()
        size = path.stat().st_size
        parts = [{'method': 'PUT', 'url': 'https://upload.apple.com/1', 'offset': 0, 'length': 4, 'requestHeaders': []},
                 {'method': 'PUT', 'url': 'https://upload.apple.com/2', 'offset': 4, 'length': size - 4, 'requestHeaders': []}]
        engine, source, path = self.uploader(parts)
        response = mock.MagicMock()
        opened = mock.Mock()
        opened.open.return_value = response
        with mock.patch.object(media.urllib.request, 'build_opener', return_value=opened):
            target = engine.upload(source, row('appScreenshotSets', 'set'))
        requests = [call.args[0] for call in opened.open.call_args_list]
        self.assertEqual(b''.join(request.data for request in requests), path.read_bytes())
        self.assertTrue(all('Authorization' not in request.headers for request in requests))
        self.assertEqual(target['attributes']['sourceFileChecksum'], core.digest(path, 'md5'))
        self.assertIn(core.key(source), core.read_json(engine.state_path)['mapping'])

    def test_invalid_upload_chunk_retains_reservation_without_network(self):
        for offset, length, method in ((-1, 2, 'PUT'), (0, 9999, 'PUT'), (0, 1, 'POST'), (1, 2, 'PUT')):
            engine, source, _ = self.uploader([{'method': method, 'url': 'https://upload.apple.com/chunk', 'offset': offset, 'length': length}])
            with mock.patch.object(media.urllib.request, 'build_opener', side_effect=AssertionError('Must not send bytes')), self.assertRaises(RuntimeError):
                engine.upload(source, row('appScreenshotSets', 'set'))
            self.assertIn(core.key(source), core.read_json(engine.state_path)['mapping'])

    def test_cli_malformed_profile_does_not_print_private_value(self):
        core.save_json(self.profile, {'key_id': 'KEY', 'private_key': 'x', 'secret': 'fictional-sensitive-value'})
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            status = cli.main(['--profile', str(self.profile), 'account', 'check'])
        self.assertEqual(status, 1)
        self.assertNotIn('fictional-sensitive-value', output.getvalue())

    def test_distribution_scanner_rejects_private_files_and_markers(self):
        bad = self.directory / 'bad.whl'
        with zipfile.ZipFile(bad, 'w') as archive:
            archive.writestr('appstore_admin/account/profile.json', '{}')
            archive.writestr('appstore_admin/core.py', '-----BEGIN ' + 'PRIVATE KEY-----')
            archive.writestr('../outside.py', '')
        _, problems = distribution.check(bad)
        self.assertGreaterEqual(len(problems), 3)
        self.assertFalse(any('-----BEGIN' in problem for problem in problems))

    def test_release_normalizes_machine_owner_metadata(self):
        archive_path = self.directory / 'source.tar.gz'
        with tarfile.open(archive_path, 'w:gz') as archive:
            content = b'public source'
            member = tarfile.TarInfo('appstore_admin-1.0.1/README.md')
            member.uid, member.gid = 501, 20
            member.uname, member.gname = 'fictional-user', 'fictional-group'
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        with self.assertRaises(ValueError):
            distribution.check(archive_path)
        build_release.normalize_archive(archive_path)
        count, problems = distribution.check(archive_path)
        self.assertEqual(count, 1)
        self.assertEqual(problems, [])
        with tarfile.open(archive_path) as archive:
            member = archive.getmembers()[0]
            self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))

    def test_staged_scanner_reads_index_not_safe_working_copy(self):
        repo = self.directory / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        source = repo / 'src/appstore_admin/core.py'
        source.parent.mkdir(parents=True)
        source.write_text('-----BEGIN ' + 'PRIVATE KEY-----')
        subprocess.run(['git', '-C', str(repo), 'add', 'src/appstore_admin/core.py'], check=True, capture_output=True)
        source.write_text('safe working copy')
        _, problems = distribution.check_tree(repo, staged=True)
        self.assertTrue(any('possible credential' in problem for problem in problems))

    def test_public_tree_does_not_ignore_private_runtime_directory(self):
        (self.directory / '.local').mkdir()
        (self.directory / '.local/profile.json').write_text('{}')
        _, problems = distribution.check_tree(self.directory)
        self.assertTrue(any('.local/profile.json' in problem for problem in problems))

    def test_skill_install_is_portable_and_refuses_existing_destination(self):
        target = self.directory / 'skill'
        install_skill.install(target)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            install_skill.install(target)
        result = subprocess.run([sys.executable, str(target / 'scripts/admin.py'), '--help'],
                                env={**os.environ, 'APPSTORE_ADMIN_PACKAGE': str(ROOT)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--apply', result.stdout)

    def test_skill_missing_explicit_checkout_never_falls_back(self):
        result = subprocess.run([sys.executable, str(ROOT / 'skills/appstore-admin/scripts/admin.py'), '--help'],
                                env={**os.environ, 'APPSTORE_ADMIN_PACKAGE': str(self.directory / 'missing')}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Configured package not found', result.stderr)

    def test_lock_symlink_is_refused(self):
        directory = self.directory / 'runs'
        directory.mkdir()
        path = directory / ('lock-' + metadata.sha_json(['account', 'app']) + '.lock')
        victim = self.directory / 'victim'
        victim.write_text('preserve')
        path.symlink_to(victim)
        with self.assertRaises(OSError):
            with operations.app_lock(directory, 'account', 'app'):
                self.fail('Symlink lock must not be acquired')
        self.assertEqual(victim.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
