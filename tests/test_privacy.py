from fastapi.testclient import TestClient
from app import create_app
from tests.helpers import setup_admin, login, register


def test_personal_data_director_only_and_preserve_strings(tmp_path):
    with TestClient(create_app(tmp_path)) as admin, TestClient(create_app(tmp_path)) as teacher, TestClient(create_app(tmp_path)) as anonymous:
        setup_admin(admin)
        tid=register(admin,phone='010-0000-1111',bank_name='가상 은행',account_number='000-0123-456',account_holder='가상 예금주').json()['teacher']['id']
        def profile(): return next(t for t in admin.get('/api/bootstrap').json()['teachers'] if t['id']==tid)
        assert profile()['account_number']=='000-0123-456'
        assert profile()['phone']=='010-0000-1111'
        login(teacher,'teacher')
        for client in (teacher,anonymous):
            content=client.get('/api/bootstrap').text
            for secret in ('010-0000-1111','000-0123-456','가상 은행','가상 예금주'):
                assert secret not in content
        content=teacher.get(f'/api/teachers/{tid}/months/2026-08').text
        content+=admin.get('/api/overview/2026-08').text
        content+=admin.get(f'/api/teachers/{tid}/months/2026-08').text
        for field in ('phone','bank_name','account_number','account_holder'):
            assert field not in content
        assert teacher.put(f'/api/teachers/{tid}',json={'name':'위조','account_number':'123'}).status_code==403
        assert admin.put(f'/api/teachers/{tid}',json={'name':'가상 수정','bank_name':'수정 은행','account_number':'000-999','account_holder':'수정 예금주','phone':'02-000-0000','hourly_rate':16000}).status_code==200
        assert profile()['account_number']=='000-999' and profile()['phone']=='02-000-0000'
        assert admin.put(f'/api/teachers/{tid}',json={'name':'가상 수정','hourly_rate':16000}).status_code==200
        assert profile()['account_number']=='000-999'


def test_backup_failure_logs_omit_personal_data(tmp_path,caplog):
    with TestClient(create_app(tmp_path)) as admin:
        setup_admin(admin)
        target=tmp_path/'backups'/'급여관리_최신.xlsx'
        target.unlink()
        target.mkdir()  # Simulate an inaccessible destination without using real data.
        result=register(admin,phone='010-0000-9999',account_number='000-987-654',bank_name='가상 개인정보 은행')
        assert not result.json()['backup']['ok']
        assert 'Automatic backup failed' in caplog.text
        for secret in ('010-0000-9999','000-987-654','가상 개인정보 은행'):
            assert secret not in caplog.text
            assert secret not in result.json()['backup']['message']
