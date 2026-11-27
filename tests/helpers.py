PASSWORD='0123'


def setup_admin(client):
    r=client.post('/api/auth/setup',json={'name':'원장','role':'director','login_id':'admin','password':PASSWORD})
    assert r.status_code==200,r.text


def login(client,login_id):
    r=client.post('/api/auth/login',json={'login_id':login_id,'password':PASSWORD})
    assert r.status_code==200,r.text


def register(client,**extra):
    payload={'name':'김 선생','login_id':'teacher','password':PASSWORD,'hourly_rate':16000,'effective_date':'2026-01-01',**extra}
    r=client.post('/api/teachers',json=payload)
    assert r.status_code==200,r.text
    return r
