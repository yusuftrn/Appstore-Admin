#!/usr/bin/env python3
"""Install the public skill from a checkout without replacing an existing skill."""
import argparse
from pathlib import Path
import shutil


def install(destination):
    source = Path(__file__).resolve().parents[1] / 'skills/appstore-admin'
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Skill destination already exists; choose a separate destination')
    if any(path.is_symlink() for path in source.rglob('*')):
        raise RuntimeError('Skill source contains symlinks')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, default=Path.home() / '.codex/skills/appstore-admin')
    args = parser.parse_args()
    try:
        print('Installed skill:', install(args.dest))
    except (RuntimeError, OSError) as error:
        parser.exit(1, str(error) + '\n')
