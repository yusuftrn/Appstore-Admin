#!/usr/bin/env python3
"""Build wheel/sdist and strip machine identity from source archive metadata."""
import argparse
import gzip
import os
from pathlib import Path
import tarfile
import tempfile


def normalize_archive(path):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as output:
                    with tarfile.open(path) as source:
                        for member in source.getmembers():
                            if not member.isdir() and not member.isfile():
                                raise ValueError('Release archives may contain only regular files and directories')
                            data = source.extractfile(member) if member.isfile() else None
                            member.uid = member.gid = 0
                            member.uname = member.gname = ''
                            member.mtime = 0
                            member.pax_headers = {}
                            member.mode = 0o755 if member.isdir() or member.name.endswith(('bin/appstore-admin', '.githooks/pre-commit')) else 0o644
                            output.addfile(member, data)
                            if data is not None:
                                data.close()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--normalize', nargs='+', type=Path, help='Normalize existing source archives without building')
    args = parser.parse_args()
    if args.normalize:
        for path in args.normalize:
            normalize_archive(path)
            print('Normalized archive metadata:', path.name)
        return
    import build
    root = Path(__file__).resolve().parents[1]
    destination = root / 'dist'
    builder = build.ProjectBuilder(str(root))
    source = Path(builder.build('sdist', str(destination)))
    normalize_archive(source)
    wheel = Path(builder.build('wheel', str(destination)))
    from check_distribution import check
    for path in (source, wheel):
        count, problems = check(path)
        if problems:
            raise RuntimeError('Release archive failed public-file checks; run check_distribution.py for filenames')
        print('Checked release:', path.name, f'({count} files)')


if __name__ == '__main__':
    main()
