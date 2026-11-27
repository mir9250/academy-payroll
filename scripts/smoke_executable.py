"""Run built binary on Windows, using only synthetic data in a clean runner profile."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop import read_service

folder = Path(os.environ['LOCALAPPDATA']) / 'AcademyPayroll'
assert not folder.exists(), 'Smoke test requires a clean runner profile'
original = Path(sys.argv[1]).resolve()
# Exercise executable extraction from a Korean path, not just Korean DB filenames.
package_dir = Path(tempfile.mkdtemp(prefix='급여관리 실행 경로 '))
executable = str(package_dir / '학원 급여관리.exe')
shutil.copy2(original, executable)
process = None
try:
    process = subprocess.Popen([executable, '--headless'])
    deadline = time.monotonic() + 90
    url = None
    while time.monotonic() < deadline:
        url = read_service(folder)
        if url:
            break
        assert process.poll() is None, 'Executable exited before readiness'
        time.sleep(.2)
    assert url, 'Executable readiness timeout'
    assert urllib.request.urlopen(url).status == 200
    assert urllib.request.urlopen(url + '/static/app.js').status == 200
    payload = json.dumps({'name':'가상 원장', 'role':'director', 'login_id':'smoke-admin', 'password':'0123'}).encode()
    request = urllib.request.Request(url + '/api/auth/setup', data=payload, headers={'Content-Type':'application/json'})
    assert urllib.request.urlopen(request).status == 200
    duplicate = subprocess.run([executable, '--headless'], timeout=30)
    assert duplicate.returncode == 0 and process.poll() is None
    (folder / 'stop.request').touch()
    assert process.wait(timeout=30) == 0
    # Replace the executable as an update, retaining the exact database and PIN hash.
    with sqlite3.connect(folder / 'academy_payroll.sqlite3') as db:
        password_before = db.execute('SELECT password_hash FROM teachers').fetchone()[0]
    shutil.copy2(original, executable)
    process = subprocess.Popen([executable, '--headless'])
    deadline = time.monotonic() + 60
    url = None
    while time.monotonic() < deadline:
        url = read_service(folder)
        if url:
            break
        time.sleep(.2)
    assert url
    with urllib.request.urlopen(url + '/api/bootstrap') as response:
        assert json.load(response)['setup_required'] is False
    with sqlite3.connect(folder / 'academy_payroll.sqlite3') as db:
        assert db.execute('SELECT password_hash FROM teachers').fetchone()[0] == password_before
    login = urllib.request.Request(url + '/api/auth/login', data=json.dumps({'login_id':'smoke-admin', 'password':'0123'}).encode(), headers={'Content-Type':'application/json'})
    assert urllib.request.urlopen(login).status == 200
    (folder / 'stop.request').touch()
    assert process.wait(timeout=30) == 0
    print('Windows EXE readiness/resources/duplicate/restart/data smoke passed')
finally:
    if process and process.poll() is None:
        process.kill()
        process.wait()

    shutil.rmtree(package_dir, ignore_errors=True)
