import io
import sqlite3
from datetime import date

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import create_app
from tests.helpers import PASSWORD, login, register, setup_admin


def work(client, tid, day, start='15:00', end='16:00'):
    return client.put(f'/api/teachers/{tid}/records/{day}',json={'start_time':start,'end_time':end})


def salary(client, tid, month):
    r=client.get(f'/api/teachers/{tid}/months/{month}')
    assert r.status_code==200,r.text
    return r.json()['salary']


def test_authorization_sessions_and_account_lifecycle(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as admin, TestClient(application) as teacher, TestClient(application) as anonymous:
        assert anonymous.post('/api/teachers',json={}).status_code==401
        assert anonymous.get('/api/overview/2026-11').status_code==401
        setup_admin(admin)
        assert admin.post('/api/auth/setup',json={'name':'원장','role':'director','login_id':'second','password':PASSWORD}).status_code==409
        a=register(admin).json()['teacher']['id']
        b=register(admin,name='김 선생',login_id='teacher2').json()['teacher']['id']
        public=anonymous.get('/api/bootstrap').json()
        assert all('amount' not in t and 'phone' not in t and 'contract_history' not in t for t in public['teachers'])
        login(teacher,'teacher')
        cookie=teacher.cookies.get('payroll_session')
        assert cookie and 'httponly' in teacher.post('/api/auth/login',json={'login_id':'teacher','password':PASSWORD}).headers['set-cookie'].lower()
        assert teacher.get(f'/api/teachers/{a}/months/2026-11').status_code==200
        for method,path in [('get',f'/api/teachers/{b}/months/2026-11'),('get','/api/overview/2026-11'),('get','/api/backup/excel'),('put','/api/settings'),('post','/api/teachers'),('patch',f'/api/teachers/{b}/status?active=false')]:
            assert getattr(teacher,method)(path).status_code==403
        assert work(admin,a,'2026-11-01').status_code==403
        assert admin.delete(f'/api/teachers/{a}/records/2026-11-01').status_code==403
        assert admin.patch(f'/api/teachers/{a}/status?active=false').status_code==200
        assert teacher.get(f'/api/teachers/{a}/months/2026-11').status_code==401
        assert teacher.post('/api/auth/login',json={'teacher_id':a,'password':PASSWORD}).status_code==401
        assert admin.patch(f'/api/teachers/{a}/status?active=true').status_code==200
        login(teacher,'teacher')
        assert teacher.post('/api/auth/logout').status_code==200
        assert teacher.get(f'/api/teachers/{a}/months/2026-11').status_code==401
        admin_id=admin.get('/api/bootstrap').json()['user']['id']
        assert admin.patch(f'/api/teachers/{admin_id}/status?active=false').status_code==409
        assert anonymous.get('/api/bootstrap').headers['cache-control']=='no-store'
        assert anonymous.post('/api/auth/login',headers={'Origin':'https://evil.example'},json={'login_id':'admin','password':PASSWORD}).status_code==403
        for _ in range(5):
            assert anonymous.post('/api/auth/login',json={'login_id':'teacher2','password':'9999'}).status_code==401
        assert anonymous.post('/api/auth/login',json={'login_id':'teacher2','password':PASSWORD}).status_code==429
    with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as conn:
        values=conn.execute('SELECT password_hash FROM teachers').fetchall()
        assert all(v[0].startswith('scrypt$') and PASSWORD not in v[0] for v in values)
        assert all(len(v[0])==64 for v in conn.execute('SELECT token_hash FROM sessions'))


def test_daily_contracts_snapshots_and_mixed_month(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as admin, TestClient(application) as teacher:
        setup_admin(admin)
        tid=register(admin,pay_type='daily',amount=120000,effective_date='2026-01-01').json()['teacher']['id']
        login(teacher,'teacher')
        assert work(teacher,tid,'2026-08-01','15:00','15:01').status_code==200
        assert work(teacher,tid,'2026-08-01','15:00','18:00').status_code==200
        assert work(teacher,tid,'2026-08-02','15:00','14:00').status_code==422
        s=salary(teacher,tid,'2026-08')
        assert (s['work_days'],s['total_minutes'],s['gross'],s['net'])==(1,180,120000,116040)
        changed=admin.put(f'/api/teachers/{tid}',json={'name':'일급','pay_type':'daily','amount':150000,'effective_date':'2026-08-02'})
        assert changed.status_code==200,changed.text
        assert work(teacher,tid,'2026-08-02','15:00','15:01').status_code==200
        assert work(teacher,tid,'2026-08-01','15:00','16:00').json()['month']['records'][0]['applied_amount']==120000
        assert salary(teacher,tid,'2026-08')['gross']==270000
        assert admin.put(f'/api/teachers/{tid}',json={'name':'일급','pay_type':'hourly','amount':20000,'effective_date':'2026-08-03'}).status_code==200
        assert work(teacher,tid,'2026-08-03').status_code==200
        s=salary(teacher,tid,'2026-08')
        assert s['needs_review'] and s['gross'] is None and s['net'] is None
        assert admin.put(f'/api/teachers/{tid}',json={'name':'일급','pay_type':'daily','amount':100,'effective_date':'2026-08-01'}).status_code==422
        assert salary(teacher,tid,'2026-07')['gross']==0


def test_monthly_full_no_records_and_partial_contracts(tmp_path):
    application=create_app(tmp_path)
    month=date.today().strftime('%Y-%m')
    with TestClient(application) as admin, TestClient(application) as teacher:
        setup_admin(admin)
        tid=register(admin,pay_type='monthly',amount=3000000,effective_date='2026-01-01').json()['teacher']['id']
        s=salary(admin,tid,'2026-02')
        assert s['gross']==3000000 and s['net']==2901000 and s['work_days']==0
        login(teacher,'teacher')
        assert work(teacher,tid,'2026-02-01','15:00','15:01').status_code==200
        assert salary(teacher,tid,'2026-02')['gross']==3000000
        # A future midmonth amount change is retained as a distinct contract, without prorating.
        r=admin.put(f'/api/teachers/{tid}',json={'name':'월급','pay_type':'monthly','amount':3200000,'effective_date':'2027-01-15'})
        assert r.status_code==200,r.text
        assert salary(admin,tid,'2027-01')['needs_review']
        assert salary(admin,tid,'2027-01')['gross'] is None
        assert salary(admin,tid,'2027-02')['gross']==3200000
        assert salary(admin,tid,'2026-02')['gross']==3000000
        mid=register(admin,name='중도입사',login_id='midmonth',pay_type='monthly',amount=2000000,effective_date='2026-01-15').json()['teacher']['id']
        assert salary(admin,mid,'2026-01')['needs_review']
        assert salary(admin,mid,'2026-02')['gross']==2000000
        assert admin.patch(f'/api/teachers/{tid}/status?active=false').status_code==200
        assert salary(admin,tid,month)['needs_review']
        overview=admin.get(f'/api/overview/{month}').json()
        assert len(overview['items'])==2 and all(t['teacher']['role']=='teacher' for t in overview['items'])
        assert overview['needs_review']


def test_excel_contract_roundtrip_no_secrets_and_legacy_import(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as client:
        setup_admin(client)
        tid=register(client,pay_type='daily',amount=99000,effective_date='2026-01-01').json()['teacher']['id']
        login(client,'teacher');assert work(client,tid,'2026-05-03').status_code==200
        login(client,'admin')
        content=client.get('/api/backup/excel').content
        wb=load_workbook(io.BytesIO(content))
        assert wb['급여계약']['E2'].value==99000
        assert all('password' not in str(cell.value).lower() and 'scrypt$' not in str(cell.value) and 'token_hash' not in str(cell.value) for ws in wb for row in ws for cell in row)
        r=client.post('/api/backup/restore',files={'file':('backup.xlsx',content)})
        assert r.status_code==200,r.text
        assert client.get(f'/api/teachers/{tid}/months/2026-05').status_code==401
        login(client,'teacher')
        assert salary(client,tid,'2026-05')['gross']==99000
        login(client,'admin')
        # Convert the workbook to exactly the legacy v1.2 sheet/column format.
        for name in ['급여계약','재직변경']:del wb[name]
        wb['선생님정보'].delete_cols(8,1)
        wb['근무기록'].delete_cols(9,3)
        for row in range(wb['설정'].max_row,1,-1):
            if wb['설정'].cell(row,1).value=='format_version':wb['설정'].delete_rows(row)
        legacy=io.BytesIO();wb.save(legacy);wb.close()
        r=client.post('/api/backup/restore',files={'file':('legacy.xlsx',legacy.getvalue())})
        assert r.status_code==200,r.text
        login(client,'admin')
        assert salary(client,tid,'2026-05')['pay_type']=='hourly'
        assert list((tmp_path/'backups').glob('복원전_안전백업_*.sqlite3'))


def test_additive_migration_preserves_legacy_database(tmp_path):
    db=tmp_path/'academy_payroll.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
        CREATE TABLE teachers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',hourly_rate INTEGER NOT NULL,role TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE work_records(id INTEGER PRIMARY KEY AUTOINCREMENT,teacher_id INTEGER NOT NULL REFERENCES teachers(id),work_date TEXT NOT NULL,start_time TEXT NOT NULL,end_time TEXT NOT NULL,minutes INTEGER NOT NULL,applied_hourly_rate INTEGER NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(teacher_id,work_date));
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO teachers(id,name,hourly_rate,role) VALUES(7,'기존 선생님',16000,'teacher'),(8,'기존 원장님',20000,'director');
        INSERT INTO work_records(teacher_id,work_date,start_time,end_time,minutes,applied_hourly_rate) VALUES(7,'2026-08-03','15:00','20:00',300,15000),(8,'2026-08-03','15:00','20:00',300,20000);
        ''')
        before=conn.execute('SELECT * FROM work_records ORDER BY id').fetchall()
    application=create_app(tmp_path)
    create_app(tmp_path)  # migration is repeatable without additional snapshots or mutations
    snapshots=list((tmp_path/'backups').glob('마이그레이션전_v12_*.sqlite3'))
    assert len(snapshots)==1
    with sqlite3.connect(snapshots[0]) as conn:
        assert conn.execute('SELECT * FROM work_records ORDER BY id').fetchall()==before
        assert 'password_hash' not in [r[1] for r in conn.execute('PRAGMA table_info(teachers)')]
    with sqlite3.connect(db) as conn:
        assert [r[:8] for r in conn.execute('SELECT * FROM work_records ORDER BY id')]==before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with TestClient(application) as client:
        setup_admin(client)
        assert salary(client,7,'2026-08')['gross']==75000
        assert len(client.get('/api/overview/2026-08').json()['items'])==1
        r=client.put('/api/teachers/7',json={'name':'기존 선생님','hourly_rate':16000,'login_id':'legacy','password':PASSWORD,'effective_date':'2026-11-01'})
        assert r.status_code==200,r.text
        login(client,'legacy')
        assert salary(client,7,'2026-08')['gross']==75000


def test_manual_settlement_flag_backup_restore_and_clear(tmp_path):
    application=create_app(tmp_path)
    with TestClient(application) as admin, TestClient(application) as teacher:
        setup_admin(admin)
        tid=register(admin,pay_type='monthly',amount=2800000,effective_date='2026-01-01').json()['teacher']['id']
        login(teacher,'teacher')
        assert teacher.put(f'/api/teachers/{tid}/review/2026-02',json={'reason':'조정'}).status_code==403
        assert salary(admin,tid,'2026-02')['gross']==2800000
        assert admin.put(f'/api/teachers/{tid}/review/2026-02',json={'reason':'결근에 따른 별도 정산 확인'}).status_code==200
        s=salary(teacher,tid,'2026-02')
        assert s['needs_review'] and s['gross'] is None and '결근' in s['review_reason']
        data=admin.get('/api/backup/excel').content
        result=admin.post('/api/backup/restore',files={'file':('backup.xlsx',data)})
        assert result.status_code==200,result.text
        login(admin,'admin')
        assert salary(admin,tid,'2026-02')['needs_review']
        assert admin.put(f'/api/teachers/{tid}/review/2026-02',json={'reason':''}).status_code==200
        assert salary(admin,tid,'2026-02')['gross']==2800000
        assert salary(admin,tid,'2026-02')['work_days']==0
        # Corrupt a contract amount: validation must reject before replacing any rows.
        wb=load_workbook(io.BytesIO(data));wb['급여계약']['E2']=-1
        invalid=io.BytesIO();wb.save(invalid);wb.close()
        assert admin.post('/api/backup/restore',files={'file':('invalid.xlsx',invalid.getvalue())}).status_code==422
        assert salary(admin,tid,'2026-02')['gross']==2800000
