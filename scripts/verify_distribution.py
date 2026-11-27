"""Inspect tracked source and PyInstaller archive; never print file contents."""
import subprocess
import sys
from pathlib import PurePosixPath

FORBIDDEN = ('.sqlite3', '.db', '.xlsx', '.log', '.pem', '.key')


def check_names(names):
    for name in names:
        path = PurePosixPath(name.replace('\\', '/').lower())
        if 'data' in path.parts or 'backups' in path.parts or path.name.startswith('.env') or path.name.endswith(FORBIDDEN):
            raise SystemExit(f'Forbidden data file: {name}')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        check_names(subprocess.check_output(['git', 'ls-files'], text=True).splitlines())
    else:
        from PyInstaller.archive.readers import CArchiveReader
        check_names(CArchiveReader(sys.argv[1]).toc.keys())
    print('Distribution privacy check passed')
