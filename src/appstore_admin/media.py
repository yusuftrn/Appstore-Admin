"""Screenshot reservations, bounded chunks and resumable read-back verification."""
from pathlib import Path
import time
import urllib.error
import urllib.request

from .core import NoRedirect, digest, key, linkage, save_json
from .security import external_url, upload_headers


class ScreenshotUploader:
    def __init__(self, client, schema, source, state, state_path, asset_wait=45):
        self.client, self.schema, self.source = client, schema, source
        self.state, self.state_path, self.asset_wait = state, state_path, asset_wait

    def save(self):
        save_json(self.state_path, self.state)

    def record(self, source, target):
        self.state['mapping'][key(source)] = linkage(target)
        self.save()

    @staticmethod
    def media_state(resource):
        return resource.get('attributes', {}).get('assetDeliveryState', {}).get('state')

    def upload(self, source, parent, target=None):
        if source['type'] != 'appScreenshots' or parent['type'] != 'appScreenshotSets':
            raise RuntimeError('Screenshot uploader accepts only a screenshot and its set')
        path = self.source.asset(source)
        size, md5 = path.stat().st_size, digest(path, 'md5')
        filename = 'screenshot--' + source['id'] + path.suffix
        if target:
            checksum = target.get('attributes', {}).get('sourceFileChecksum')
            state = self.media_state(target)
            if checksum and checksum != md5:
                raise RuntimeError('Target screenshot has different bytes; use a clean set')
            if state == 'COMPLETE':
                if checksum != md5:
                    raise RuntimeError('Cannot verify already-complete screenshot bytes')
                return target
            if state == 'FAILED':
                raise RuntimeError('Existing screenshot failed processing; inspect App Store Connect')
            target = self.client.request('GET', '/v1/appScreenshots/' + target['id'])['data']
        else:
            body = self.schema.payload('appScreenshots', 'post', {'fileSize': size, 'fileName': filename},
                                       {'appScreenshotSet': {'data': linkage(parent)}})
            target = self.client.request('POST', '/v1/appScreenshots', body)['data']
        self.state['assetHashes'][key(source)] = digest(path)
        self.record(source, target)  # Persist the reservation before sending bytes.
        operations = target.get('attributes', {}).get('uploadOperations', [])
        if operations:
            validated = []
            cursor = 0
            for op in sorted(operations, key=lambda item: item['offset']):
                offset, length = op['offset'], op['length']
                if (type(offset) is not int or type(length) is not int or offset != cursor
                        or length <= 0 or length > 64 * 1024 * 1024 or offset + length > size
                        or op['method'] != 'PUT'):
                    raise RuntimeError('Invalid screenshot upload chunk/method')
                validated.append((offset, length, external_url(op['url']), upload_headers(op.get('requestHeaders', []))))
                cursor += length
            if cursor != size:
                raise RuntimeError('Screenshot upload chunks do not cover the file')
            with path.open('rb') as handle:
                for offset, length, url, headers in validated:
                    handle.seek(offset)
                    data = handle.read(length)
                    if len(data) != length:
                        raise RuntimeError('Screenshot changed during upload')
                    for attempt in range(4):
                        try:
                            req = urllib.request.Request(url, data=data, headers=headers, method='PUT')
                            with urllib.request.build_opener(NoRedirect()).open(req, timeout=120) as response:
                                response.read(4096)
                            break
                        except (urllib.error.URLError, TimeoutError):
                            if attempt == 3:
                                raise RuntimeError('Screenshot chunk upload failed; retain the journal and reconcile') from None
                            time.sleep(2 ** attempt)
            # Recheck bytes before committing the checksum.
            self.source.asset(source)
            body = self.schema.payload('appScreenshots', 'patch', {'uploaded': True, 'sourceFileChecksum': md5}, resource_id=target['id'])
            target = self.client.request('PATCH', '/v1/appScreenshots/' + target['id'], body)['data']
        elif self.media_state(target) not in {'COMPLETE', 'UPLOAD_COMPLETE', 'PROCESSING'}:
            raise RuntimeError('Reservation has no upload operations; inspect the screenshot')
        return self.poll_asset(target)

    def poll_asset(self, target):
        deadline = time.monotonic() + self.asset_wait
        while True:
            target = self.client.request('GET', '/v1/appScreenshots/' + target['id'])['data']
            state = self.media_state(target)
            if state == 'COMPLETE':
                return target
            if state == 'FAILED':
                raise RuntimeError('Apple screenshot processing failed; inspect App Store Connect')
            if time.monotonic() >= deadline:
                raise RuntimeError('Apple screenshot still processing; reservation retained, reconcile later')
            time.sleep(min(5, max(0, deadline - time.monotonic())))
