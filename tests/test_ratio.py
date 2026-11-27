import io
import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import create_app
from payroll_v14 import allocation, migrate_v14
from tests.helpers import setup_admin, register, login


@pytest.mark.parametrize('bps,gross,academy', [(0,0,100001),(10000,100001,0),(3333,33330,66671)])
def test_integer_ratio_allocation(bps, gross, academy):
    result=allocation(dict(revenue=100001,base_deduction=0,teacher_share_bps=bps,tax_amount=0))
    assert not result['errors']
    assert (result['gross'],result['academy_amount'],result['net'])==(gross,academy,gross)
    assert result['academy_share_bps']+bps==10000


@pytest.mark.parametrize('key,value',[('revenue',-1),('base_deduction',-1),('tax_amount',-1),('teacher_share_bps',10001),('teacher_share_bps',-1),('revenue',True),('revenue',1.1)])
def test_invalid_allocation_input(key,value):
    values=dict(revenue=100,base_deduction=0,teacher_share_bps=5000,tax_amount=0)
    values[key]=value
    with pytest.raises(HTTPException): allocation(values)


def test_missing_zero_and_excess_deductions():
    assert allocation({})['net'] is None
    assert allocation(dict(revenue=0,base_deduction=0,teacher_share_bps=0,tax_amount=0))['net']==0
    assert allocation(dict(revenue=100,base_deduction=101,teacher_share_bps=5000,tax_amount=0))['errors']
    assert allocation(dict(revenue=100,base_deduction=0,teacher_share_bps=5000,tax_amount=51))['net'] is None


def test_ratio_contract_and_safe_migration(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        setup_admin(client)
        tid=register(client,pay_type='ratio',teacher_share_bps=6500,default_deduction=10000).json()['teacher']['id']
        assert client.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['gross'] is None
    with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as db:
        before=db.execute('SELECT id,login_id,password_hash FROM teachers').fetchall()
        contracts=db.execute('SELECT * FROM contracts').fetchall()
    create_app(tmp_path)
    with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as db:
        assert db.execute('SELECT id,login_id,password_hash FROM teachers').fetchall()==before
        assert db.execute('SELECT * FROM contracts').fetchall()==contracts
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert len(list((tmp_path/'backups').glob('마이그레이션전_v13_*')))==1


def test_settlement_status_confirmation_history_and_access(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as teacher, TestClient(app) as anonymous:
        setup_admin(admin)
        tid=register(admin,pay_type='ratio',teacher_share_bps=6000,default_deduction=20000).json()['teacher']['id']
        login(teacher,'teacher')
        endpoint=f'/api/teachers/{tid}/settlements/2026-08'
        assert teacher.get(endpoint).status_code==403
        assert teacher.put(endpoint,json={}).status_code==403
        assert anonymous.get(endpoint).status_code==401
        missing=admin.get(endpoint).json()
        assert missing['status']=='missing' and missing['revenue'] is None and missing['tax_amount'] is None
        assert missing['teacher_share_bps']==6000 and missing['base_deduction']==20000
        payload=dict(revenue=1000000,base_deduction=20000,teacher_share_bps=6000,tax_amount=None,expected_revision=0,status='draft',memo='가상 정산 메모')
        assert admin.put(endpoint,json=payload).status_code==200
        assert teacher.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['net'] is None
        payload.update(status='completed',expected_revision=1)
        assert admin.put(endpoint,json=payload).status_code==422
        payload['tax_amount']=10000
        response=admin.put(endpoint,json=payload)
        assert response.status_code==200,response.text
        data=response.json()['settlement']
        assert (data['gross'],data['academy_amount'],data['net'])==(588000,392000,578000)
        salary=teacher.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']
        assert salary['net']==578000 and 'academy_amount' not in salary
        assert admin.put(endpoint,json=payload).status_code==409
        payload.update(expected_revision=2,tax_amount=11000)
        assert admin.put(endpoint,json=payload).status_code==409
        payload.update(confirm_revision=True)
        assert admin.put(endpoint,json=payload).status_code==200
        history=admin.get(endpoint).json()['history']
        assert len(history)==3 and json.loads(history[1]['snapshot'])['net']==578000
        assert admin.put(f'/api/teachers/{tid}',json=dict(name='비율 강사',pay_type='ratio',teacher_share_bps=7000,effective_date='2027-01-01')).status_code==200
        assert teacher.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['net']==577000
        # Attendance is preserved and has no effect on ratio payroll.
        assert teacher.put(f'/api/teachers/{tid}/records/2026-08-01',json={'start_time':'15:00','end_time':'20:00'}).status_code==200
        assert teacher.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['net']==577000
    with TestClient(create_app(tmp_path)) as admin:
        login(admin,'admin')
        assert len(admin.get(endpoint).json()['history'])==3
        with sqlite3.connect(tmp_path/'academy_payroll.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM ratio_settlements').fetchone()[0]==1


@pytest.mark.parametrize('values',[{'base_deduction':1000001},{'tax_amount':1000001},{'tax_amount':None}])
def test_excess_and_missing_blocks_confirmation(tmp_path,values):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        tid=register(admin,pay_type='ratio',teacher_share_bps=5000).json()['teacher']['id']
        payload=dict(revenue=1000000,base_deduction=0,teacher_share_bps=5000,tax_amount=0,expected_revision=0,status='completed',**{})
        payload.update(values)
        assert admin.put(f'/api/teachers/{tid}/settlements/2026-08',json=payload).status_code==422


def test_midmonth_contract_needs_review(tmp_path):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        tid=register(admin,pay_type='ratio',teacher_share_bps=5000,effective_date='2026-08-15').json()['teacher']['id']
        endpoint=f'/api/teachers/{tid}/settlements/2026-08'
        assert admin.get(endpoint).json()['review_reasons']
        payload=dict(revenue=100,base_deduction=0,teacher_share_bps=5000,tax_amount=0,status='completed',expected_revision=0)
        assert admin.put(endpoint,json=payload).status_code==422
        payload['status']='draft'
        assert admin.put(endpoint,json=payload).status_code==200


@pytest.mark.parametrize('bps,expected',[(0,0),(10000,100001)])
def test_zero_and_full_ratio_complete_api(tmp_path,bps,expected):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        tid=register(admin,pay_type='ratio',teacher_share_bps=bps,default_deduction=0).json()['teacher']['id']
        response=admin.put(f'/api/teachers/{tid}/settlements/2026-08',json=dict(revenue=100001,base_deduction=0,teacher_share_bps=bps,tax_amount=0,status='completed',expected_revision=0))
        assert response.status_code==200,response.text
        assert response.json()['settlement']['net']==expected
        assert admin.get(f'/api/teachers/{tid}/months/2026-08').json()['salary']['net']==expected
