"""Per-user credentials and runtime data, outside the source checkout by default."""
import os
from pathlib import Path
import shutil
import tempfile

from .core import Client, read_json, save_json
from .security import private_directory, private_file, validate_profile, validate_signing_key


def package_root():
    return Path(__file__).resolve().parents[2]


def local_root():
    configured = os.environ.get('APPSTORE_ADMIN_HOME')
    root = Path(configured).expanduser().absolute() if configured else Path.home() / '.local/share/appstore-admin'
    resolved = root.resolve()
    if resolved == Path.home().resolve() or resolved in package_root().resolve().parents or resolved == package_root().resolve():
        raise RuntimeError('APPSTORE_ADMIN_HOME must be a dedicated private data directory')
    return root


def profile_path(override=None):
    return Path(override).expanduser().absolute() if override else local_root() / 'account/profile.json'


def schema_path(override=None):
    if override:
        return Path(override).expanduser().absolute()
    updated = local_root() / 'schema/openapi.json'
    if updated.is_file():
        return updated
    return Path(__file__).parent / 'data/openapi-4.5.json'


def account_identity(profile):
    return profile.get('issuer_id') or 'individual:' + profile['key_id']


def client_for(override=None, readonly=True):
    path = profile_path(override)
    if not path.is_file():
        raise RuntimeError('No active account. Run account import --profile /private/path/profile.json')
    return Client(path, readonly=readonly)


def import_account(source):
    source = private_file(source)
    values = validate_profile(read_json(source))
    key = Path(values['private_key']).expanduser()
    key = validate_signing_key(key if key.is_absolute() else source.parent / key)
    root = private_directory(local_root())
    destination = root / 'account'
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('An active account/import already exists; do not silently replace it')
    staging = Path(tempfile.mkdtemp(prefix='.account-import-', dir=root))
    try:
        target = staging / 'AuthKey.p8'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output, key.open('rb') as input_file:
            shutil.copyfileobj(input_file, output)
        validate_signing_key(target)
        result = {k: values[k] for k in ('issuer_id', 'key_id') if k in values}
        result['private_key'] = 'AuthKey.p8'
        save_json(staging / 'profile.json', result, overwrite=False)
        from .operations import app_lock
        with app_lock(root / 'runs', 'local-account-import', 'account'):
            if destination.exists() or destination.is_symlink():
                raise RuntimeError('An active account/import already exists; do not silently replace it')
            staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination / 'profile.json'
