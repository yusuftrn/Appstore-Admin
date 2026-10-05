#!/usr/bin/env python3
"""Fail closed on unexpected package files and common embedded credentials.

Print filenames and categories only, never the matched secret. This is a release
hygiene check, not proof that arbitrary private business data can be published.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

ROOT_FILES = {'README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
              'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'MANIFEST.in', '.gitignore',
              '.gitattributes', 'bin/appstore-admin', '.githooks/pre-commit',
              'examples/profile.example.json', 'examples/metadata.example.json'}
PATTERNS = [r'src/appstore_admin/[a-z_]+\.py', r'src/appstore_admin/data/openapi-4\.5\.json',
            r'tests/test_[a-z_]+\.py', r'tools/[a-z_]+\.py', r'docs/[a-z_-]+\.md',
            r'skills/appstore-admin/SKILL\.md', r'skills/appstore-admin/agents/openai\.yaml',
            r'skills/appstore-admin/scripts/admin\.py', r'skills/appstore-admin/references/workflow\.md',
            r'\.github/workflows/[a-z_-]+\.yml', r'\.github/ISSUE_TEMPLATE/[a-z_-]+\.yml',
            r'\.github/PULL_REQUEST_TEMPLATE\.md', r'\.github/dependabot\.yml']
SECRET_PATTERNS = [
    rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    rb'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b',
    rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b', rb'\bgithub_pat_[A-Za-z0-9_]{30,}\b',
    rb'\bAKIA[A-Z0-9]{16}\b',
    rb'/(?:Users|Volumes)/[^\s"\x27<>]+',
]


def allowed(name, packaged=False):
    if name in ROOT_FILES or any(re.fullmatch(pattern, name) for pattern in PATTERNS):
        return True
    if packaged and name in {'PKG-INFO', 'setup.cfg'}:
        return True
    if packaged and re.fullmatch(r'(?:src/)?appstore_admin\.egg-info/(?:PKG-INFO|SOURCES\.txt|dependency_links\.txt|entry_points\.txt|top_level\.txt|requires\.txt)', name):
        return True
    return False


def inspect(name, content, packaged=False):
    problems = []
    parts = PurePosixPath(name).parts
    if name.startswith('/') or '..' in parts or '\\' in name:
        problems.append('unsafe archive path')
    if not allowed(name, packaged):
        problems.append('file outside the public source allowlist')
    if name == 'examples/profile.example.json':
        try:
            expected = {'issuer_id': 'ISSUER_ID', 'key_id': 'KEY_ID', 'private_key': 'AuthKey.p8'}
            if json.loads(content) != expected:
                problems.append('profile example must contain only documented placeholders')
        except ValueError:
            problems.append('invalid profile example')
    if any(re.search(pattern, content) for pattern in SECRET_PATTERNS):
        problems.append('possible credential or personal filesystem path')
    return [name + ': ' + problem for problem in problems]


def entries(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Archive symlinks are forbidden')
                yield item.filename, archive.read(item)
    else:
        with tarfile.open(path) as archive:
            for item in archive.getmembers():
                if item.uid != 0 or item.gid != 0 or item.uname or item.gname:
                    raise ValueError('Source archive contains machine owner metadata; use tools/build_release.py')
                if item.isdir():
                    continue
                if not item.isfile():
                    raise ValueError('Archive links/special files are forbidden')
                yield item.name, archive.extractfile(item).read()


def check(path):
    problems, count = [], 0
    for name, content in entries(path):
        count += 1
        if path.suffix == '.whl':
            if re.fullmatch(r'appstore_admin-[0-9.]+\.dist-info/(?:METADATA|WHEEL|RECORD|entry_points\.txt|top_level\.txt|licenses/(?:LICENSE|THIRD_PARTY_NOTICES\.md))', name):
                canonical = 'README.md'  # Still inspect metadata content for secrets.
            elif name.startswith('appstore_admin/'):
                canonical = 'src/' + name
            elif re.match(r'appstore_admin-[0-9.]+\.data/data/share/appstore-admin/', name):
                canonical = re.sub(r'^appstore_admin-[0-9.]+\.data/data/share/appstore-admin/', '', name)
            else:
                canonical = name
        else:
            if not re.match(r'^appstore_admin-[0-9.]+/', name):
                problems.append(name + ': unexpected source archive root')
            canonical = name.split('/', 1)[-1]
        problems.extend(inspect(canonical, content, packaged=True))
    if not count:
        problems.append('Empty archive')
    return count, problems


def check_tree(root, tracked=False, staged=False):
    root = root.resolve()
    if tracked or staged:
        records = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root).decode().split('\0')
        indexed = [record.split('\t', 1) for record in records if record]
        symlinks = {name for info, name in indexed if info.startswith('120000 ')}
        paths = [root / name for _, name in indexed]
    else:
        symlinks = set()
        paths = []
        for path in root.rglob('*'):
            parts = path.relative_to(root).parts
            if any(part in {'.git', '.venv', 'build', 'dist', '__pycache__'} or part.endswith('.egg-info') for part in parts):
                continue
            if path.is_file() or path.is_symlink():
                paths.append(path)
    problems = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        if path.is_symlink() or name in symlinks:
            problems.append(name + ': source symlink forbidden')
        else:
            content = subprocess.check_output(['git', 'show', ':' + name], cwd=root) if staged else path.read_bytes()
            problems.extend(inspect(name, content))
    return len(paths), problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*', type=Path)
    parser.add_argument('--tree', type=Path)
    parser.add_argument('--tracked', action='store_true', help='Check all tracked files, including ignored files')
    parser.add_argument('--staged', action='store_true', help='Inspect exact Git index contents for pre-commit')
    args = parser.parse_args()
    if not args.archives and not args.tree:
        parser.error('Provide --tree or at least one archive')
    failed = False
    targets = [(p.name, lambda p=p: check(p)) for p in args.archives]
    if args.tree:
        targets.insert(0, ('source tree', lambda: check_tree(args.tree, args.tracked, args.staged)))
    for label, run in targets:
        try:
            count, problems = run()
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
            count, problems = 0, ['Cannot inspect distribution (' + type(error).__name__ + ')']
        print(label + ': ' + ('FAILED' if problems else 'PASSED') + f' ({count} files)')
        for problem in problems:
            print('  ' + problem)
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
