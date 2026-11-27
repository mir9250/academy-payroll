# Only explicitly listed application assets are bundled. Never collect data/.
from pathlib import Path
base = Path(SPECPATH)
a = Analysis([str(base / 'desktop.py')], pathex=[str(base)],
             binaries=[], datas=[(str(base / 'static'), 'static')],
             hiddenimports=['a2wsgi', 'waitress'],
             excludes=['pytest', 'playwright'], noarchive=False)
for entry in a.datas:
    name = entry[0].lower().replace('\\', '/')
    if name.startswith('data/') or name.endswith(('.sqlite3', '.db', '.xlsx', '.pem', '.key', '.log')):
        raise RuntimeError('Persistent/sensitive data must not be bundled')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='AcademyPayroll',
          debug=False, strip=False, upx=False, console=False)
