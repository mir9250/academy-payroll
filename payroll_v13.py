"""Additive v1.3 migration, password hashing and date-effective payroll contracts."""
import calendar
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import date, datetime

from fastapi import HTTPException

LOGIN_FAILURE_LIMIT = 5
LOGIN_COOLDOWN_SECONDS = 300

PAY_TYPES = {'hourly': '시급', 'daily': '일급', 'monthly': '월급'}
TEACHER_COLUMNS = ['id', 'name', 'phone', 'hourly_rate', 'role', 'is_active', 'created_at', 'login_id']
WORK_COLUMNS = ['id', 'teacher_id', 'work_date', 'start_time', 'end_time', 'minutes', 'applied_hourly_rate', 'updated_at', 'applied_pay_type', 'applied_amount', 'withholding_bps']
CONTRACT_COLUMNS = ['id', 'teacher_id', 'effective_date', 'pay_type', 'amount', 'withholding_bps', 'is_active']


def valid_pin(password):
    return isinstance(password,str) and re.fullmatch(r'[0-9]{4}',password) is not None


def password_hash(password):
    if not valid_pin(password):
        raise HTTPException(422, '비밀번호는 숫자 4자리로 입력해 주세요.')
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


def _verify_scrypt_secret(password, stored):
    """Hash verification only; legacy credentials are accepted solely by the local reset tool."""
    try:
        kind, salt, expected = stored.split('$')
        if kind != 'scrypt' or not isinstance(password,str) or not 1<=len(password)<=128:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, AttributeError):
        return False


def verify_password(password, stored):
    return valid_pin(password) and _verify_scrypt_secret(password, stored)


def migrate(conn, backup_dir):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version >= 13:
        return
    # A consistent backup is mandatory, and must succeed before any schema change.
    snapshot = backup_dir / f'마이그레이션전_v12_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3'
    with sqlite3.connect(snapshot) as dst:
        conn.backup(dst)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('ALTER TABLE teachers ADD COLUMN login_id TEXT')
        conn.execute('ALTER TABLE teachers ADD COLUMN password_hash TEXT')
        conn.execute('CREATE UNIQUE INDEX idx_login_id ON teachers(login_id COLLATE NOCASE) WHERE login_id IS NOT NULL')
        conn.execute("ALTER TABLE work_records ADD COLUMN applied_pay_type TEXT NOT NULL DEFAULT 'hourly'")
        conn.execute('ALTER TABLE work_records ADD COLUMN applied_amount INTEGER NOT NULL DEFAULT 1')
        conn.execute('ALTER TABLE work_records ADD COLUMN withholding_bps INTEGER NOT NULL DEFAULT 330')
        conn.execute('UPDATE work_records SET applied_amount=applied_hourly_rate')
        conn.execute('''CREATE TABLE contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL REFERENCES teachers(id),
            effective_date TEXT NOT NULL,
            pay_type TEXT NOT NULL CHECK(pay_type IN ('hourly','daily','monthly')),
            amount INTEGER NOT NULL CHECK(amount>0),
            withholding_bps INTEGER NOT NULL DEFAULT 330 CHECK(withholding_bps BETWEEN 0 AND 10000),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            UNIQUE(teacher_id,effective_date))''')
        conn.execute("INSERT INTO contracts(teacher_id,effective_date,pay_type,amount,is_active) SELECT id,'2000-01-01','hourly',hourly_rate,is_active FROM teachers WHERE role='teacher'")
        conn.execute('''CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY, teacher_id INTEGER NOT NULL REFERENCES teachers(id),
            expires_at INTEGER NOT NULL)''')
        conn.execute('CREATE TABLE payroll_reviews (teacher_id INTEGER NOT NULL REFERENCES teachers(id), month TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(teacher_id,month))')
        conn.execute('CREATE TABLE employment_events (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL REFERENCES teachers(id), effective_date TEXT NOT NULL, is_active INTEGER NOT NULL CHECK(is_active IN (0,1)))')
        conn.execute('CREATE TABLE auth_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until INTEGER NOT NULL)')
        conn.execute("INSERT INTO settings(key,value) VALUES ('auth_initialized','0')")
        conn.execute('PRAGMA user_version=13')
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def public_teacher(conn, teacher):
    result = {key: teacher[key] for key in TEACHER_COLUMNS}
    result['credentials_set'] = bool(teacher['password_hash'])
    contract = contract_on(conn, teacher['id'], date.today().isoformat())
    latest = conn.execute('SELECT * FROM contracts WHERE teacher_id=? ORDER BY effective_date DESC LIMIT 1', (teacher['id'],)).fetchone()
    result.update(pay_type=contract['pay_type'] if contract else 'hourly', amount=contract['amount'] if contract else teacher['hourly_rate'])
    result['contract_history'] = [dict(r) for r in conn.execute('SELECT * FROM contracts WHERE teacher_id=? ORDER BY effective_date', (teacher['id'],))]
    result['review_flags']={r['month']:r['reason'] for r in conn.execute('SELECT month,reason FROM payroll_reviews WHERE teacher_id=?',(teacher['id'],))}
    result['latest_contract'] = dict(latest) if latest else None
    return result


def contract_on(conn, tid, day):
    row = conn.execute('SELECT * FROM contracts WHERE teacher_id=? AND effective_date<=? ORDER BY effective_date DESC LIMIT 1', (tid, day)).fetchone()
    return dict(row) if row else None


def calculate_salary(conn, teacher, month, records):
    first = month + '-01'
    y, m = map(int, month.split('-'))
    last = f'{month}-{calendar.monthrange(y,m)[1]:02d}'
    contracts = [dict(r) for r in conn.execute('SELECT * FROM contracts WHERE teacher_id=? AND effective_date<=? ORDER BY effective_date', (teacher['id'], last))]
    opening = contract_on(conn, teacher['id'], first)
    changes = [c for c in contracts if first < c['effective_date'] <= last]
    applicable = ([opening] if opening else []) + changes
    types = {c['pay_type'] for c in applicable if c['is_active']}
    types.update(r['applied_pay_type'] for r in records)
    needs_review = len(types) > 1
    reasons = ['월중 급여 유형 변경'] if needs_review else []
    if 'monthly' in types:
        events = conn.execute('SELECT * FROM employment_events WHERE teacher_id=? AND effective_date BETWEEN ? AND ?', (teacher['id'],first,last)).fetchall()
        prior = conn.execute('SELECT is_active FROM employment_events WHERE teacher_id=? AND effective_date<? ORDER BY effective_date DESC,id DESC LIMIT 1', (teacher['id'],first)).fetchone()
        if not opening or not opening['is_active'] or changes or events or (prior and not prior['is_active']):
            needs_review = True
            reasons.append('월급제 중도 계약·금액·재직 상태 변경: 일할 계산 미적용')
    inactive_month = bool('monthly' in types and opening and prior and not prior['is_active'] and not events and not changes and not records)
    if inactive_month:
        needs_review=False
        reasons=[]
    manual=conn.execute('SELECT reason FROM payroll_reviews WHERE teacher_id=? AND month=?',(teacher['id'],month)).fetchone()
    if manual:
        needs_review=True
        reasons.append(manual['reason'])
    gross = 0
    if not needs_review:
        if types == {'monthly'}:
            gross = 0 if inactive_month else opening['amount']
            bps = opening['withholding_bps']
        else:
            gross = sum(r['minutes'] * r['applied_amount'] for r in records if r['applied_pay_type']=='hourly') // 60
            gross += sum(r['applied_amount'] for r in records if r['applied_pay_type']=='daily')
            bps = 330
        withholding = gross * bps // 10000
    else:
        gross = withholding = None
    rates = {r['applied_hourly_rate'] for r in records}
    selected = (applicable[-1] if applicable else contract_on(conn, teacher['id'], last))
    label = str(next(iter(rates))) if len(rates)==1 else '시급 변동' if rates else str(teacher['hourly_rate'])
    amounts={r['applied_amount'] for r in records}
    amount_label='근무일별 상이' if len(amounts)>1 else f'{next(iter(amounts)):,}원' if amounts and types!={'monthly'} else f"{selected['amount'] if selected else 0:,}원"
    return dict(contract_amount_label=amount_label,total_minutes=sum(r['minutes'] for r in records), work_days=len(records),
                gross=gross, withholding=withholding, net=gross-withholding if gross is not None else None,
                hourly_rate_label=label, pay_type=next(iter(types)) if len(types)==1 else 'mixed' if types else 'hourly',
                amount=selected['amount'] if selected else 0, needs_review=needs_review, review_reason=' · '.join(reasons))
