"""v1.4 additive data, integer allocation policy and settlement snapshots."""
import calendar
import json
import sqlite3
from contextlib import closing
from datetime import datetime

from fastapi import HTTPException
from payroll_v13 import calculate_salary as fixed_salary, contract_on

PRIVATE_COLUMNS = ['phone', 'bank_name', 'account_number', 'account_holder']
RATIO_CONTRACT_COLUMNS = ['teacher_share_bps', 'default_deduction', 'tax_mode']
SETTLEMENT_COLUMNS = ['teacher_id', 'month', 'contract_id', 'revenue', 'base_deduction',
                      'teacher_share_bps', 'tax_amount', 'memo', 'status', 'revision',
                      'distributable', 'gross', 'academy_amount', 'net', 'updated_at']
HISTORY_COLUMNS = ['teacher_id', 'month', 'revision', 'actor_id', 'snapshot', 'changed_at']
MAX_MONEY = 1_000_000_000_000


def migrate_v14(conn, backup_dir):
    if conn.execute('PRAGMA user_version').fetchone()[0] >= 14:
        return
    snapshot = backup_dir / f'마이그레이션전_v13_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3'
    with closing(sqlite3.connect(snapshot)) as dst:
        conn.backup(dst)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for column in PRIVATE_COLUMNS[1:]:
            conn.execute(f"ALTER TABLE teachers ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        # Preserve every existing ID and value while extending the original CHECK constraint.
        conn.execute('''CREATE TABLE contracts_v14 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL REFERENCES teachers(id), effective_date TEXT NOT NULL,
            pay_type TEXT NOT NULL CHECK(pay_type IN ('hourly','daily','monthly','ratio')),
            amount INTEGER NOT NULL CHECK(amount>=0),
            withholding_bps INTEGER NOT NULL DEFAULT 330 CHECK(withholding_bps BETWEEN 0 AND 10000),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            teacher_share_bps INTEGER CHECK(teacher_share_bps BETWEEN 0 AND 10000),
            default_deduction INTEGER CHECK(default_deduction>=0), tax_mode TEXT NOT NULL DEFAULT 'fixed_330',
            UNIQUE(teacher_id,effective_date),
            CHECK((pay_type='ratio' AND teacher_share_bps IS NOT NULL AND amount=0 AND withholding_bps=0 AND tax_mode='manual')
               OR (pay_type!='ratio' AND amount>0 AND teacher_share_bps IS NULL AND tax_mode='fixed_330')))
        ''')
        conn.execute('INSERT INTO contracts_v14(id,teacher_id,effective_date,pay_type,amount,withholding_bps,is_active) SELECT id,teacher_id,effective_date,pay_type,amount,withholding_bps,is_active FROM contracts')
        conn.execute('DROP TABLE contracts')
        conn.execute('ALTER TABLE contracts_v14 RENAME TO contracts')
        conn.execute('''CREATE TABLE ratio_settlements (
            teacher_id INTEGER NOT NULL REFERENCES teachers(id), month TEXT NOT NULL,
            contract_id INTEGER NOT NULL REFERENCES contracts(id),
            revenue INTEGER CHECK(revenue>=0), base_deduction INTEGER CHECK(base_deduction>=0),
            teacher_share_bps INTEGER CHECK(teacher_share_bps BETWEEN 0 AND 10000),
            tax_amount INTEGER CHECK(tax_amount>=0), memo TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('draft','completed')), revision INTEGER NOT NULL CHECK(revision>0),
            distributable INTEGER, gross INTEGER, academy_amount INTEGER, net INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(teacher_id,month))''')
        conn.execute('''CREATE TABLE ratio_settlement_history (
            teacher_id INTEGER NOT NULL REFERENCES teachers(id), month TEXT NOT NULL,
            revision INTEGER NOT NULL, actor_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL, changed_at TEXT NOT NULL,
            PRIMARY KEY(teacher_id,month,revision))''')
        conn.execute('PRAGMA user_version=14')
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def integer(value, label, maximum=MAX_MONEY, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise HTTPException(422, f'{label}은(는) 0~{maximum:,} 범위의 정수로 입력해 주세요.')
    return value


def allocation(values):
    """Policy: floor teacher gross once; academy gets exact remainder. Tax is manual."""
    revenue = integer(values.get('revenue'), '총원비')
    deduction = integer(values.get('base_deduction'), '기본공제액')
    bps = integer(values.get('teacher_share_bps'), '강사 배분 비율', 10000)
    tax = integer(values.get('tax_amount'), '실제 세액')
    errors = []
    if any(v is None for v in (revenue, deduction, bps, tax)):
        errors.append('총원비·기본공제액·강사 비율·실제 세액을 모두 입력해 주세요. 0원도 직접 입력합니다.')
    if revenue is not None and deduction is not None and deduction > revenue:
        errors.append('기본공제액이 총원비를 초과합니다.')
    distributable = revenue - deduction if revenue is not None and deduction is not None and deduction <= revenue else None
    gross = distributable * bps // 10000 if distributable is not None and bps is not None else None
    if gross is not None and tax is not None and tax > gross:
        errors.append('세금이 강사 세전 급여를 초과합니다.')
    return dict(distributable=distributable, gross=gross,
                academy_amount=distributable-gross if gross is not None else None,
                net=gross-tax if gross is not None and tax is not None and tax <= gross else None,
                academy_share_bps=10000-bps if bps is not None else None, errors=errors)


def month_contract(conn, teacher_id, month):
    year, number = map(int, month.split('-'))
    first, last = month+'-01', f'{month}-{calendar.monthrange(year,number)[1]:02d}'
    opening = contract_on(conn, teacher_id, first)
    changes = [dict(r) for r in conn.execute('SELECT * FROM contracts WHERE teacher_id=? AND effective_date>? AND effective_date<=? ORDER BY effective_date', (teacher_id,first,last))]
    applicable = ([opening] if opening else []) + changes
    selected = applicable[-1] if applicable else None
    ratio = any(c['pay_type']=='ratio' for c in applicable)
    reasons = []
    if ratio:
        if not opening or changes or len({c['pay_type'] for c in applicable})>1:
            reasons.append('월중 비율제 계약 변경: 정산 확인 필요')
        event = conn.execute('SELECT 1 FROM employment_events WHERE teacher_id=? AND effective_date BETWEEN ? AND ?', (teacher_id,first,last)).fetchone()
        prior = conn.execute('SELECT is_active FROM employment_events WHERE teacher_id=? AND effective_date<? ORDER BY effective_date DESC,id DESC LIMIT 1', (teacher_id,first)).fetchone()
        if event or prior and not prior['is_active']:
            reasons.append('재직 상태 변경: 정산 확인 필요')
        manual = conn.execute('SELECT reason FROM payroll_reviews WHERE teacher_id=? AND month=?', (teacher_id,month)).fetchone()
        if manual:
            reasons.append(manual['reason'])
    return selected, ratio, reasons


def calculate_salary(conn, teacher, month, records):
    selected, ratio, reasons = month_contract(conn, teacher['id'], month)
    if not ratio and not any(r['applied_pay_type']=='ratio' for r in records):
        return fixed_salary(conn, teacher, month, records)
    row = conn.execute('SELECT * FROM ratio_settlements WHERE teacher_id=? AND month=?', (teacher['id'],month)).fetchone()
    complete = row and row['status']=='completed'
    # Immutable monetary snapshot wins over later contract defaults.
    return dict(total_minutes=sum(r['minutes'] for r in records), work_days=len(records),
                pay_type='ratio', is_ratio=True, amount=0, contract_amount_label='비율제', hourly_rate_label='',
                teacher_share_bps=row['teacher_share_bps'] if row else selected['teacher_share_bps'] if selected else None,
                gross=row['gross'] if complete else None, withholding=row['tax_amount'] if complete else None,
                net=row['net'] if complete else None,
                needs_review=bool(reasons) and not complete, review_reason=' · '.join(reasons),
                settlement_status=row['status'] if row else 'missing')


def settlement_view(conn, teacher_id, month):
    contract, ratio, reasons = month_contract(conn, teacher_id, month)
    if not ratio or not contract or contract['pay_type']!='ratio':
        raise HTTPException(422, '이 달에 비율제 정산을 설정할 수 없습니다.')
    row = conn.execute('SELECT * FROM ratio_settlements WHERE teacher_id=? AND month=?', (teacher_id,month)).fetchone()
    data = dict(row) if row else dict(teacher_id=teacher_id, month=month, contract_id=contract['id'],
            revenue=None, base_deduction=contract['default_deduction'], teacher_share_bps=contract['teacher_share_bps'],
            tax_amount=None, memo='', status='missing', revision=0)
    data['calculation'] = allocation(data)
    data['review_reasons'] = reasons
    data['history'] = [dict(r) for r in conn.execute('SELECT revision,actor_id,snapshot,changed_at FROM ratio_settlement_history WHERE teacher_id=? AND month=? ORDER BY revision DESC', (teacher_id,month))]
    return data


def save_settlement(conn, teacher_id, month, payload, actor_id):
    current = settlement_view(conn, teacher_id, month)
    revision = payload.get('expected_revision')
    if type(revision) is not int or revision != current['revision']:
        raise HTTPException(409, '다른 화면에서 변경되었습니다. 새로고침 후 다시 검토해 주세요.')
    if current['status']=='completed' and payload.get('confirm_revision') is not True:
        raise HTTPException(409, '확정된 정산 변경에는 원장님의 명시적인 확인이 필요합니다.')
    status = payload.get('status', 'draft')
    memo = payload.get('memo', '')
    if status not in ('draft','completed') or not isinstance(memo,str) or len(memo)>1000:
        raise HTTPException(422, '정산 상태 또는 메모가 올바르지 않습니다.')
    data = {key:payload.get(key) for key in ('revenue','base_deduction','teacher_share_bps','tax_amount')}
    calculated = allocation(data)
    if status=='completed' and (calculated['errors'] or current['review_reasons']):
        raise HTTPException(422, ' · '.join(calculated['errors'] + current['review_reasons']))
    now = datetime.now().isoformat(timespec='microseconds')
    data.update(teacher_id=teacher_id,month=month,contract_id=current['contract_id'],memo=memo,
                status=status,revision=current['revision']+1,updated_at=now)
    data.update({key:calculated[key] for key in ('distributable','gross','academy_amount','net')})
    columns = SETTLEMENT_COLUMNS
    conn.execute(f"INSERT INTO ratio_settlements({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(teacher_id,month) DO UPDATE SET "+','.join(f'{key}=excluded.{key}' for key in columns[2:]), [data[k] for k in columns])
    conn.execute('INSERT INTO ratio_settlement_history VALUES (?,?,?,?,?,?)', (teacher_id,month,data['revision'],actor_id,json.dumps(data, ensure_ascii=False),now))
    return data


def validate_extended_backup(teachers, contracts, settlements, histories):
    """Validate all v14 data before restore starts a write transaction."""
    teacher_ids = {t['id'] for t in teachers if t['role']=='teacher'}
    by_contract = {c['id']:c for c in contracts}
    for c in contracts:
        if c['pay_type']=='ratio':
            integer(c.get('teacher_share_bps'),'강사 배분 비율',10000,False)
            integer(c.get('default_deduction'),'기본공제액')
            if c['amount']!=0 or c['withholding_bps']!=0 or c.get('tax_mode')!='manual':
                raise ValueError('비율제 계약 정보 오류')
        elif c.get('teacher_share_bps') is not None or c.get('tax_mode')!='fixed_330':
            raise ValueError('정액제 계약 정보 오류')
    latest = {}
    def validate_snapshot(row):
        tid, month = row.get('teacher_id'), row.get('month')
        contract = by_contract.get(row.get('contract_id'))
        if tid not in teacher_ids or not contract or contract['teacher_id']!=tid or contract['pay_type']!='ratio':
            raise ValueError('비율 정산의 계약 또는 강사 참조 오류')
        if not isinstance(month,str) or not re_month(month):
            raise ValueError('비율 정산 월 오류')
        if contract['effective_date'][:7]>month:
            raise ValueError('정산 적용일 오류')
        integer(row.get('revision'),'정산 버전',1_000_000,False)
        if row['revision']==0 or row.get('status') not in ('draft','completed') or not isinstance(row.get('memo'),str) or len(row['memo'])>1000:
            raise ValueError('정산 상태·버전·메모 오류')
        calc=allocation(row)
        if row['status']=='completed' and calc['errors']:
            raise ValueError('확정된 정산의 필수 항목 오류')
        if any(row.get(k)!=calc[k] for k in ('distributable','gross','academy_amount','net')):
            raise ValueError('정산 계산 결과 불일치')
        datetime.fromisoformat(row['updated_at'])
    for row in settlements:
        validate_snapshot(row)
        key=(row['teacher_id'],row['month'])
        if key in latest:
            raise ValueError('동일 월 정산 중복')
        latest[key]=row
    revisions={key:{} for key in latest}
    for row in histories:
        key=(row.get('teacher_id'),row.get('month'))
        if key not in latest or type(row.get('revision')) is not int or row['revision'] in revisions[key]:
            raise ValueError('정산 이력 중복 또는 사용자 참조 오류')
        integer(row.get('actor_id'),'이력 작성자',1_000_000_000,False)
        snapshot=json.loads(row['snapshot'])
        validate_snapshot(snapshot)
        if (snapshot['teacher_id'],snapshot['month'],snapshot['revision'],snapshot['updated_at'])!=(row['teacher_id'],row['month'],row['revision'],row['changed_at']):
            raise ValueError('정산 이력 메타데이터 불일치')
        revisions[key][row['revision']]=snapshot
    for key,row in latest.items():
        if sorted(revisions[key])!=list(range(1,row['revision']+1)) or revisions[key][row['revision']]!={k:row[k] for k in SETTLEMENT_COLUMNS}:
            raise ValueError('정산 변경 이력 누락 또는 최종 버전 불일치')


def re_month(month):
    import re
    return bool(re.fullmatch(r'(?:20\d\d|2100)-(?:0[1-9]|1[0-2])',month))
