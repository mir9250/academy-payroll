import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import create_app
from payroll_v13 import password_hash, verify_password
from tests.helpers import PASSWORD, login, register, setup_admin


@pytest.mark.parametrize('invalid',['123','12345','abcd','12a3','１２３４','١٢٣٤',' 123','123\n',1234,None])
def test_exact_ascii_pin_required(invalid):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        password_hash(invalid)
    assert error.value.status_code==422
    assert not verify_password(invalid,password_hash('0123'))


def test_setup_register_reset_and_leading_zero_login(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as admin,TestClient(app) as teacher:
        for pin in ('123','12345','abcd','１２３４'):
            r=admin.post('/api/auth/setup',json={'name':'원장','role':'director','login_id':'admin','password':pin})
            assert r.status_code==422
        setup_admin(admin)
        tid=register(admin).json()['teacher']['id']
        with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as db:
            before=db.execute('SELECT password_hash FROM teachers WHERE id=?',(tid,)).fetchone()[0]
        for pin in ('123','12345','abcd','１２３４'):
            assert admin.post('/api/teachers',json={'name':'신규','login_id':'newteacher','password':pin}).status_code==422
            assert admin.put(f'/api/teachers/{tid}',json={'name':'김 선생','hourly_rate':16000,'password':pin}).status_code==422
        with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as db:
            assert db.execute('SELECT password_hash FROM teachers WHERE id=?',(tid,)).fetchone()[0]==before
        login(teacher,'teacher')
        assert teacher.get(f'/api/teachers/{tid}/months/2026-11').status_code==200
        # Reset is authenticated and invalidates the existing teacher session.
        assert admin.put(f'/api/teachers/{tid}',json={'name':'김 선생','hourly_rate':16000,'password':'0007'}).status_code==200
        assert teacher.get(f'/api/teachers/{tid}/months/2026-11').status_code==401
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':PASSWORD}).status_code==401
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':'0007'}).status_code==200
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':7}).status_code==401


@pytest.mark.parametrize('pin',['0123','1234'])
def test_both_roles_accept_pin_as_string(tmp_path,pin):
    application=create_app(tmp_path)
    with TestClient(application) as admin,TestClient(application) as teacher:
        assert admin.post('/api/auth/setup',json={'name':'원장','role':'director','login_id':'admin','password':pin}).status_code==200
        tid=register(admin,password=pin).json()['teacher']['id']
        assert admin.post('/api/auth/logout').status_code==200
        assert admin.post('/api/auth/login',json={'login_id':'admin','password':pin}).status_code==200
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':pin}).status_code==200
        assert teacher.post('/api/auth/login',json={'login_id':'teacher','password':pin}).status_code==200


def test_shared_account_cooldown_expires_and_reset_clears_it(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as admin,TestClient(application) as teacher:
        setup_admin(admin)
        tid=register(admin).json()['teacher']['id']
        for index in range(5):
            identity={'teacher_id':tid} if index%2 else {'login_id':'TEACHER'}
            response=teacher.post('/api/auth/login',json={**identity,'password':'9999'})
            assert response.status_code==401
        for identity in ({'teacher_id':tid},{'login_id':'teacher'}):
            blocked=teacher.post('/api/auth/login',json={**identity,'password':PASSWORD})
            assert blocked.status_code==429 and 1<=int(blocked.headers['retry-after'])<=300
        db_path=tmp_path/'academy_payroll.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.execute('UPDATE auth_attempts SET blocked_until=1 WHERE key=?',(f'account:{tid}',))
        # A new failure after the wait is attempt one, rather than immediately blocking again.
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':'9999'}).status_code==401
        login(teacher,'teacher')
        for _ in range(5):
            assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':'9999'}).status_code==401
        assert admin.put(f'/api/teachers/{tid}',json={'name':'김 선생','hourly_rate':16000,'password':'1234'}).status_code==200
        assert teacher.post('/api/auth/login',json={'teacher_id':tid,'password':'1234'}).status_code==200


def test_failed_login_does_not_disclose_pin_validity(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as client:
        setup_admin(client)
        register(client)
        replies=[client.post('/api/auth/login',json={'login_id':username,'password':pin})
                 for username,pin in [('teacher','12ab'),('teacher','123'),('teacher','12345'),('teacher','9999'),('missing','9999')]]
        assert all(r.status_code==401 for r in replies)
        assert len({r.text for r in replies})==1


def test_legacy_director_can_explicitly_reset_without_changing_payroll(tmp_path):
    import hashlib
    from fastapi import HTTPException
    from reset_pin import reset_pin
    application=create_app(tmp_path)
    db_path=tmp_path/'academy_payroll.sqlite3'
    with TestClient(application) as admin:
        setup_admin(admin)
        tid=register(admin).json()['teacher']['id']
        # Simulate a pre-PIN administrator's existing scrypt hash. Never store plaintext.
        salt='ab'*16
        old_secret='legacy-director-password'
        legacy=f'scrypt${salt}$'+hashlib.scrypt(old_secret.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
        with sqlite3.connect(db_path) as db:
            aid=db.execute("SELECT id FROM teachers WHERE login_id='admin'").fetchone()[0]
            db.execute('UPDATE teachers SET password_hash=? WHERE id=?',(legacy,aid))
            original_contracts=db.execute('SELECT * FROM contracts').fetchall()
            teacher_hash=db.execute('SELECT password_hash FROM teachers WHERE id=?',(tid,)).fetchone()[0]
        create_app(tmp_path)
        with sqlite3.connect(db_path) as db:
            assert db.execute('SELECT password_hash FROM teachers WHERE id=?',(aid,)).fetchone()[0]==legacy
        # HTTP endpoints continue to reject legacy passwords; the local tool authenticates them.
        assert admin.post('/api/auth/login',json={'login_id':'admin','password':old_secret}).status_code==401
        with pytest.raises(HTTPException) as denied:
            reset_pin(db_path,'teacher',PASSWORD,'admin','1234')
        assert denied.value.status_code==401
        with pytest.raises(HTTPException):
            reset_pin(db_path,'admin','wrong','admin','1234')
        with sqlite3.connect(db_path) as db:
            assert db.execute('SELECT password_hash FROM teachers WHERE id=?',(aid,)).fetchone()[0]==legacy
        backup=reset_pin(db_path,'admin',old_secret,'admin','1234')
        assert backup.exists()
        with sqlite3.connect(backup) as db:
            assert db.execute('SELECT password_hash FROM teachers WHERE id=?',(aid,)).fetchone()[0]==legacy
        with sqlite3.connect(db_path) as db:
            assert db.execute('SELECT * FROM contracts').fetchall()==original_contracts
            assert db.execute('SELECT password_hash FROM teachers WHERE id=?',(tid,)).fetchone()[0]==teacher_hash
        assert admin.get('/api/overview/2026-11').status_code==401
        assert admin.post('/api/auth/login',json={'login_id':'admin','password':'1234'}).status_code==200
