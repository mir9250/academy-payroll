import json
import logging
import sqlite3
import sys
import urllib.request

from fastapi.testclient import TestClient
from app import create_app
from desktop import InstanceLock, Service, configure_logging, read_service
from tests.helpers import setup_admin, register, login


def test_exclusive_lock_and_recovery(tmp_path):
    a, b = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert a.acquire()
    assert not b.acquire()
    a.close()
    assert b.acquire()
    b.close()


def test_storage_override_and_packaged_path(tmp_path, monkeypatch):
    from runtime_paths import data_directory
    monkeypatch.setenv('PAYROLL_DATA_DIR', str(tmp_path / '한글 경로'))
    assert data_directory() == tmp_path / '한글 경로'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert data_directory() != tmp_path / '한글 경로'


def test_update_preserves_credentials_and_database(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        setup_admin(client)
        tid = register(client).json()['teacher']['id']
        login(client, 'teacher')
        response = client.put(f'/api/teachers/{tid}/records/2026-08-03', json={'start_time':'15:00','end_time':'20:00'})
        assert response.status_code == 200
        payroll_before = response.json()['month']
    with sqlite3.connect(tmp_path / 'academy_payroll.sqlite3') as db:
        before = db.execute('SELECT password_hash FROM teachers').fetchone()[0]
    with TestClient(create_app(tmp_path)) as client:
        assert not client.get('/api/bootstrap').json()['setup_required']
        assert client.post('/api/auth/login', json={'login_id':'admin', 'password':'0123'}).status_code == 200
        assert client.get(f'/api/teachers/{tid}/months/2026-08').json() == payroll_before
    with sqlite3.connect(tmp_path / 'academy_payroll.sqlite3') as db:
        assert db.execute('SELECT password_hash FROM teachers').fetchone()[0] == before


def test_waitress_adapter_readiness_auth_backup_restart(tmp_path):
    directory = tmp_path / '한글 사용자 데이터'
    directory.mkdir()
    service = Service(directory)
    try:
        assert read_service(directory) == service.url
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        payload = json.dumps({'name':'가상 원장', 'role':'director', 'login_id':'admin', 'password':'0123'}).encode()
        req = urllib.request.Request(service.url+'/api/auth/setup', data=payload, headers={'Content-Type':'application/json'})
        assert opener.open(req).status == 200
        assert opener.open(service.url + '/api/backup/excel').read()[:2] == b'PK'
        assert opener.open(service.url + '/static/app.js').status == 200
    finally:
        service.close()
    assert not read_service(directory)
    service = Service(directory)
    try:
        with urllib.request.urlopen(service.url + '/api/bootstrap') as response:
            assert not json.load(response)['setup_required']
    finally:
        service.close()


def test_port_collision_uses_next_port(tmp_path):
    import socket
    sock = socket.socket()
    try:
        try:
            sock.bind(('127.0.0.1', 8000))
            sock.listen()
        except OSError:
            pass  # An existing local service already occupies the preferred port.
        service = Service(tmp_path)
        try:
            assert service.port != 8000
            assert read_service(tmp_path) == service.url
        finally:
            service.close()
    finally:
        sock.close()


def test_logs_omit_secrets(tmp_path):
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        configure_logging(tmp_path)
        try:
            raise ValueError('PIN=0123 salary=987654321')
        except ValueError:
            logging.exception('password=secret cookie=token')
        content = (tmp_path/'logs/errors.log').read_text()
        assert 'runtime failure' in content
        for secret in ('0123','987654321','secret','token'):
            assert secret not in content
        for handler in root.handlers:
            handler.close()
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
