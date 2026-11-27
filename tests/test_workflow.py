import io
import sqlite3

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import create_app
from tests.helpers import setup_admin, login, register, PASSWORD


def test_full_payroll_backup_restore(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        setup_admin(client)
        r=register(client,phone='010-1111-2222')
        assert r.status_code==200,r.text
        assert r.json()['backup']['ok']
        tid=r.json()['teacher']['id']
        login(client,'teacher')
        r=client.put(f'/api/teachers/{tid}/records/2026-08-03',json={'start_time':'15:00','end_time':'20:00'})
        assert r.status_code==200,r.text
        assert r.json()['month']['salary']['total_minutes']==300
        assert r.json()['month']['salary']['gross']==80000
        login(client,'admin')
        r=client.put(f'/api/teachers/{tid}',json={'name':'김 선생','phone':'','hourly_rate':20000,'role':'teacher','effective_date':'2026-08-04'})
        assert r.status_code==200,r.text
        login(client,'teacher')
        r=client.put(f'/api/teachers/{tid}/records/2026-08-05',json={'start_time':'17:00','end_time':'18:30'})
        assert r.status_code==200,r.text
        data=client.get(f'/api/teachers/{tid}/months/2026-08').json()
        assert {k:data['salary'][k] for k in ('total_minutes','gross','withholding','net','hourly_rate_label')}=={'total_minutes':390,'gross':110000,'withholding':3630,'net':106370,'hourly_rate_label':'시급 변동'}
        assert [r['applied_hourly_rate'] for r in data['records']]==[16000,20000]
        # Editing a prior workday retains its original applied hourly rate.
        r=client.put(f'/api/teachers/{tid}/records/2026-08-03',json={'start_time':'14:00','end_time':'20:00'})
        assert r.status_code==200
        assert r.json()['month']['records'][0]['applied_hourly_rate']==16000
        assert client.put(f'/api/teachers/{tid}/records/2026-08-03',json={'start_time':'20:00','end_time':'15:00'}).status_code==422
        assert client.put(f'/api/teachers/{tid}/records/2026-08-32',json={'start_time':'15:00','end_time':'16:00'}).status_code==422
        assert client.put(f'/api/teachers/{tid}/records/2026-08-03',json={'start_time':'20:00','end_time':'20:00'}).status_code==422
        assert len(client.get(f'/api/teachers/{tid}/months/2026-08').json()['records'])==2
        login(client,'admin')
        response=client.get('/api/backup/excel')
        assert response.status_code==200
        excel=response.content
        wb=load_workbook(io.BytesIO(excel),data_only=True)
        assert wb.sheetnames==['선생님정보','근무기록','급여계약','재직변경','정산확인','월별급여','설정']
        assert wb['근무기록'].max_row==3
        assert wb['월별급여']['E2'].value==126000
        wb.close()
        assert (tmp_path/'backups'/'급여관리_최신.xlsx').exists()
        assert (tmp_path/'backups'/'급여관리_최신.sqlite3').exists()
        assert client.patch(f'/api/teachers/{tid}/status?active=false').status_code==200
        assert client.post('/api/auth/login',json={'login_id':'teacher','password':PASSWORD}).status_code==401
        assert client.get(f'/api/teachers/{tid}/months/2026-08').status_code==200
        assert register(client,name='임시',login_id='temporary',hourly_rate=10000).status_code==200
        restored=client.post('/api/backup/restore',files={'file':('backup.xlsx',excel,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        assert restored.status_code==200,restored.text
        assert restored.json()['restored_teachers']==2
        assert restored.json()['restored_records']==2
        login(client,'admin')
        result=[t for t in client.get('/api/bootstrap').json()['teachers'] if t['role']=='teacher']
        assert len(result)==1 and result[0]['is_active']==1
        # Imported prior work is preserved and current teacher state restored.
        assert client.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['net']==121842
        assert list((tmp_path/'backups').glob('복원전_안전백업_*.sqlite3'))
        assert client.get('/api/overview/2026-08').json()['totals']['gross']==126000


def test_monthly_aggregate_floor_once(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        setup_admin(client)
        teacher=register(client,name='A',hourly_rate=10001).json()['teacher']
        login(client,'teacher')
        tid=teacher['id']
        for d in ['2026-11-01','2026-11-02','2026-11-03']:
            r=client.put(f'/api/teachers/{tid}/records/{d}',json={'start_time':'15:00','end_time':'15:01'})
            assert r.status_code==200
        summary=client.get(f'/api/teachers/{tid}/months/2026-11').json()['salary']
        assert summary['total_minutes']==3
        assert summary['gross']==500
        assert summary['withholding']==16
        assert summary['net']==484


def test_reject_invalid_restore_and_external_writes(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        setup_admin(client)
        x=client.post('/api/backup/restore',files={'file':('bad.xlsx',b'not excel')})
        assert x.status_code==422
        x=client.post('/api/teachers',headers={'Origin':'https://evil.example'},json={'name':'X','hourly_rate':10000})
        assert x.status_code==403
        assert [t for t in client.get('/api/bootstrap').json()['teachers'] if t['role']=='teacher']==[]
