"""Small boundaries for credentials, private storage and Apple asset transport."""
import os
from pathlib import Path
import re
import stat
# OpenSSL is the explicit signing dependency; no shell invocation is used.
import subprocess  # nosec B404
import shutil
from urllib.parse import urlsplit


def private_file(path):
    path = Path(path).expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('Credentials must be regular files owned by the current user; symlinks are refused')
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError('Credential file permissions must be 600; run chmod 600 on the profile and key')
    return path


def private_directory(path):
    path = Path(path).expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('Private storage must be a directory owned by the current user; symlinks are refused')
    path.chmod(0o700)
    return path


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) - {'issuer_id', 'key_id', 'private_key'}:
        raise RuntimeError('Profile must contain only key_id, optional issuer_id, and private_key')
    for field in ('key_id', 'issuer_id'):
        value = profile.get(field)
        if field == 'issuer_id' and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', value):
            raise RuntimeError('Invalid credential identifier; inspect the private profile locally')
    key = profile.get('private_key')
    if not isinstance(key, str) or not key.strip() or '\x00' in key:
        raise RuntimeError('Profile needs a private_key file path')
    return profile


def run_openssl(arguments, input_data=None):
    executable = shutil.which('openssl')
    if not executable:
        raise RuntimeError('OpenSSL is required to validate and sign with the API key')
    try:
        # Fixed argument lists and absolute file paths; shell expansion is never used.
        return subprocess.run([str(Path(executable).resolve()), *arguments],  # nosec B603
                              input=input_data, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('OpenSSL validation/signing failed') from None


def validate_signing_key(path):
    path = private_file(path)
    if path.stat().st_size > 16384:
        raise RuntimeError('Invalid API signing key')
    try:
        public = run_openssl(['pkey', '-in', str(path), '-passin', 'pass:', '-pubout'])
        if public.returncode:
            raise RuntimeError('API key must be an unencrypted ES256 (P-256) private key')
        description = run_openssl(['pkey', '-pubin', '-text', '-noout'], public.stdout)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('OpenSSL is required to validate the API signing key') from None
    if description.returncode or b'ASN1 OID: prime256v1' not in description.stdout:
        raise RuntimeError('API key must use the ES256 P-256 curve')
    return path


def external_url(url):
    """Permit HTTPS Apple/CDN and S3 endpoints; never arbitrary or local hosts."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        allowed = ('apple.com', 'mzstatic.com', 'icloud.com', 'amazonaws.com')
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or not any(host == domain or host.endswith('.' + domain) for domain in allowed)):
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeError('Refused asset endpoint: expected HTTPS on an Apple/CDN or AWS host') from None
    return url


def upload_headers(values):
    headers = {}
    for item in values:
        name, value = item['name'], item['value']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]+', name)
                or not isinstance(value, str) or '\r' in value or '\n' in value
                or name.lower() in {'authorization', 'proxy-authorization', 'cookie', 'host'}):
            raise RuntimeError('Refused unsafe upload request headers')
        headers[name] = value
    return headers
