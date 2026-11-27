import io
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import create_app
from payroll_v13 import migrate, password_hash
from payroll_v14 import migrate_v14
from tests.helpers import setup_admin, register, login


def make_v13(folder):
    folder.mkdir(exist_ok=True)
    backup=folder/'backups';backup.mkdir(exist_ok=True)
    database=folder/'academy_payroll.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.executescript('''
        CREATE TABLE teachers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',hourly_rate INTEGER NOT NULL,role TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE work_records(id INTEGER PRIMARY KEY AUTOINCREMENT,teacher_id INTEGER NOT NULL REFERENCES teachers(id),work_date TEXT NOT NULL,start_time TEXT NOT NULL,end_time TEXT NOT NULL,minutes INTEGER NOT NULL,applied_hourly_rate INTEGER NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(teacher_id,work_date));
        CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        migrate(db,backup)
        pin=password_hash('0123')
        db.executemany('INSERT INTO teachers(id,name,phone,hourly_rate,role,login_id,password_hash,is_active) VALUES (?,?,?,?,?,?,?,?)',[(7,'가상 기존 원장','',1,'director','old-admin',pin,1),(8,'가상 기존 강사','010-0000-0000',15000,'teacher','old-teacher',pin,1),(9,'가상 퇴사자','',20000,'teacher','inactive',pin,0)])
        db.execute("INSERT INTO contracts(teacher_id,effective_date,pay_type,amount) VALUES (8,'2026-01-01','hourly',15000)")
        db.execute("INSERT INTO contracts(teacher_id,effective_date,pay_type,amount,is_active) VALUES (9,'2026-01-01','daily',100000,0)")
        db.execute("INSERT INTO work_records(teacher_id,work_date,start_time,end_time,minutes,applied_hourly_rate,applied_pay_type,applied_amount,withholding_bps) VALUES (8,'2026-08-01','15:00','20:00',300,15000,'hourly',15000,330)")
        db.execute("UPDATE settings SET value='1' WHERE key='auth_initialized'")
        db.executemany('INSERT INTO settings VALUES (?,?)',[('academy_name','가상 학원'),('principal_name','가상 원장')])
        db.execute('INSERT INTO sessions VALUES (?,?,?)',('a'*64,7,4102444800))
        db.commit()
    return database


def test_real_v13_migration_preserves_every_old_value(tmp_path):
    db_path=make_v13(tmp_path)
    with closing(sqlite3.connect(db_path)) as db:
        columns={table:[r[1] for r in db.execute(f'PRAGMA table_info({table})')] for table in ('teachers','contracts','work_records','sessions','settings','auth_attempts','employment_events','payroll_reviews')}
        before={table:db.execute(f'SELECT * FROM {table}').fetchall() for table in columns}
    app=create_app(tmp_path)
    create_app(tmp_path)
    with closing(sqlite3.connect(db_path)) as db:
        for table,names in columns.items():
            assert db.execute(f"SELECT {','.join(names)} FROM {table}").fetchall()==before[table]
        assert db.execute('PRAGMA user_version').fetchone()[0]==14
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    snapshots=list((tmp_path/'backups').glob('마이그레이션전_v13_*'))
    assert len(snapshots)==1
    with closing(sqlite3.connect(snapshots[0])) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==13
        assert db.execute('SELECT * FROM teachers').fetchall()==before['teachers']
    with TestClient(app) as client:
        login(client,'old-admin')
        salary=client.get('/api/teachers/8/months/2026-08').json()['salary']
        assert (salary['gross'],salary['net'])==(75000,72525)
        assert client.post('/api/auth/login',json={'login_id':'inactive','password':'0123'}).status_code==401
        login(client,'old-teacher')


def test_migration_requires_successful_backup(tmp_path):
    path=make_v13(tmp_path)
    invalid=tmp_path/'file-not-directory';invalid.write_text('test')
    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(sqlite3.Error): migrate_v14(db,invalid)
        assert db.execute('PRAGMA user_version').fetchone()[0]==13
        assert 'bank_name' not in [r[1] for r in db.execute('PRAGMA table_info(teachers)')]


def test_extended_backup_roundtrip_and_tamper_rejection(tmp_path):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        tid=register(admin,pay_type='ratio',teacher_share_bps=6000,phone='010-0000-9999',bank_name='가상 은행',account_number='000-123',account_holder='가상 예금주').json()['teacher']['id']
        endpoint=f'/api/teachers/{tid}/settlements/2026-08'
        payload=dict(revenue=1000000,base_deduction=0,teacher_share_bps=6000,tax_amount=0,status='completed',expected_revision=0)
        assert admin.put(endpoint,json=payload).status_code==200
        payload.update(expected_revision=1,confirm_revision=True,tax_amount=1000,memo='가상 이력')
        assert admin.put(endpoint,json=payload).status_code==200
        draft=f'/api/teachers/{tid}/settlements/2026-09'
        assert admin.put(draft,json=dict(expected_revision=0,status='draft',revenue=0,base_deduction=0,teacher_share_bps=0,tax_amount=None)).status_code==200
        data=admin.get('/api/backup/excel').content
        wb=load_workbook(io.BytesIO(data))
        assert '비율정산' in wb.sheetnames and '정산변경이력' in wb.sheetnames
        assert wb['선생님정보'].cell(3,10).value=='000-123'
        assert all('scrypt$' not in str(cell.value) and 'token_hash' not in str(cell.value) for sheet in wb for row in sheet for cell in row)
        result=admin.post('/api/backup/restore',files={'file':('v14.xlsx',data)})
        assert result.status_code==200,result.text
        login(admin,'admin')
        assert admin.get(endpoint).json()['net']==599000
        assert len(admin.get(endpoint).json()['history'])==2
        assert admin.get(draft).json()['tax_amount'] is None
        assert admin.get(draft).json()['revenue']==0
        profile=next(t for t in admin.get('/api/bootstrap').json()['teachers'] if t['id']==tid)
        assert profile['account_number']=='000-123' and profile['phone']=='010-0000-9999'
        wb['비율정산']['L2']=999999999
        invalid=io.BytesIO();wb.save(invalid)
        assert admin.post('/api/backup/restore',files={'file':('tampered.xlsx',invalid.getvalue())}).status_code==422
        assert admin.get(endpoint).json()['net']==599000
        wb.close()


def test_old_v13_excel_without_new_columns(tmp_path):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        tid=register(admin,pay_type='monthly',amount=3000000).json()['teacher']['id']
        wb=load_workbook(io.BytesIO(admin.get('/api/backup/excel').content))
        wb['선생님정보'].delete_cols(9,3)
        wb['급여계약'].delete_cols(8,3)
        del wb['비율정산'];del wb['정산변경이력']
        for row in wb['설정']:
            if row[0].value=='format_version':row[1].value='13'
        old=io.BytesIO();wb.save(old);wb.close()
        r=admin.post('/api/backup/restore',files={'file':('v13.xlsx',old.getvalue())})
        assert r.status_code==200,r.text
        login(admin,'admin')
        profile=next(t for t in admin.get('/api/bootstrap').json()['teachers'] if t['id']==tid)
        assert profile['account_number']=='' and profile['pay_type']=='monthly'
        login(admin,'teacher')
