"""One-computer academy payroll MVP.

Run: python app.py  (browser: http://127.0.0.1:8000)
Local accounts with server-side authorization and date-effective payroll contracts.
"""
from __future__ import annotations

import calendar
from contextlib import closing, contextmanager
import io
import logging
import os
import re
import shutil
import sqlite3
import threading
import hashlib
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from runtime_paths import data_directory

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from payroll_v13 import (migrate, password_hash, verify_password, public_teacher, contract_on,
                         calculate_salary, LOGIN_FAILURE_LIMIT, LOGIN_COOLDOWN_SECONDS, PAY_TYPES, TEACHER_COLUMNS, WORK_COLUMNS, CONTRACT_COLUMNS)

ROOT = Path(__file__).resolve().parent
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAX_BPS = 330  # 3.3% (business income withholding illustration)
MAX_EXCEL_BYTES = 12 * 1024 * 1024


class TeacherInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(default="", max_length=40)
    hourly_rate: int = Field(default=1, ge=1, le=1_000_000)
    login_id: str | None = Field(default=None, max_length=80)
    password: str | None = Field(default=None, max_length=128)
    pay_type: str = 'hourly'
    amount: int | None = Field(default=None, ge=1, le=1_000_000_000)
    effective_date: str | None = None
    role: str = "teacher"


class WorkInput(BaseModel):
    start_time: str
    end_time: str


class SettingsInput(BaseModel):
    academy_name: str = Field(min_length=1, max_length=100)
    principal_name: str = Field(default="", max_length=80)


def clean_teacher(data: TeacherInput) -> dict[str, Any]:
    name = data.name.strip()
    if not name:
        raise HTTPException(422, "선생님 이름을 입력해 주세요.")
    if data.role not in ("teacher", "director"):
        raise HTTPException(422, "역할이 올바르지 않습니다.")
    if data.pay_type not in PAY_TYPES:
        raise HTTPException(422, '급여 유형이 올바르지 않습니다.')
    login_id = data.login_id.strip() if data.login_id is not None else None
    if login_id is not None and not re.fullmatch(r'[A-Za-z0-9_.-]{3,80}', login_id):
        raise HTTPException(422, '아이디는 영문·숫자·._- 3~80자로 입력해 주세요.')
    if data.role=='teacher' and data.pay_type=='hourly' and (data.amount or data.hourly_rate)>1_000_000:
        raise HTTPException(422, '시급은 1,000,000원 이하여야 합니다.')
    return {"name": name, "phone": data.phone.strip() if data.role=='teacher' else '',
            "hourly_rate": data.hourly_rate, "role": data.role, "login_id": login_id,
            "pay_type": data.pay_type, "amount": data.amount or data.hourly_rate,
            "effective_date": check_date(data.effective_date or date.today().isoformat())}



def check_month(month: str) -> tuple[int, int]:
    if not MONTH_RE.fullmatch(month):
        raise HTTPException(422, "월 형식은 YYYY-MM이어야 합니다.")
    y, m = map(int, month.split("-"))
    if not (2000 <= y <= 2100):
        raise HTTPException(422, "지원하는 연도는 2000~2100년입니다.")
    return y, m


def check_date(day: str) -> str:
    if not DATE_RE.fullmatch(day):
        raise HTTPException(422, "날짜 형식은 YYYY-MM-DD이어야 합니다.")
    try:
        d = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(422, "존재하지 않는 날짜입니다.") from None
    if not (2000 <= d.year <= 2100):
        raise HTTPException(422, "지원하는 연도는 2000~2100년입니다.")
    return day


def minutes_between(start: str, end: str) -> int:
    if not (TIME_RE.fullmatch(start) and TIME_RE.fullmatch(end)):
        raise HTTPException(422, "시각은 HH:MM 형식이어야 합니다.")
    def to_min(t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    minutes = to_min(end) - to_min(start)
    if minutes <= 0:
        raise HTTPException(422, "퇴근 시각은 출근 시각보다 늦어야 합니다. 자정을 넘기는 근무는 MVP에서 지원하지 않습니다.")
    return minutes


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    target_dir = Path(data_dir or data_directory()).resolve()
    backup_dir = target_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "academy_payroll.sqlite3"
    lock = threading.RLock()

    @contextmanager
    def connect():
        conn = sqlite3.connect(db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize() -> None:
        with connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS teachers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    hourly_rate INTEGER NOT NULL CHECK(hourly_rate > 0),
                    role TEXT NOT NULL CHECK(role IN ('teacher','director')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS work_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
                    work_date TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    minutes INTEGER NOT NULL CHECK(minutes > 0 AND minutes < 1440),
                    applied_hourly_rate INTEGER NOT NULL CHECK(applied_hourly_rate > 0),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(teacher_id, work_date)
                );
                CREATE INDEX IF NOT EXISTS idx_work_month ON work_records(work_date,teacher_id);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('academy_name','학원 급여 관리')")
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('principal_name','')")

    def as_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
        values = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
        return {"academy_name": values.get("academy_name", "학원 급여 관리"),
                "principal_name": values.get("principal_name", ""), "withholding_bps": TAX_BPS}

    def get_teacher(conn: sqlite3.Connection, teacher_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
        if not row:
            raise HTTPException(404, "선생님을 찾을 수 없습니다.")
        return public_teacher(conn, row)

    def get_records(conn: sqlite3.Connection, teacher_id: int, month: str) -> list[dict[str, Any]]:
        return [as_dict(r) for r in conn.execute(
            "SELECT * FROM work_records WHERE teacher_id=? AND substr(work_date,1,7)=? ORDER BY work_date",
            (teacher_id, month))]

    def monthly_payload(conn: sqlite3.Connection, teacher_id: int, month: str) -> dict[str, Any]:
        teacher = get_teacher(conn, teacher_id)
        if teacher['role']!='teacher':
            raise HTTPException(404,'원장님은 급여 계산 대상이 아닙니다.')
        records = get_records(conn, teacher_id, month)
        salary = calculate_salary(conn, teacher, month, records)
        if not salary["hourly_rate_label"]:
            salary["hourly_rate_label"] = str(teacher["hourly_rate"])
        return {"teacher": teacher, "month": month, "records": records, "salary": salary}

    def save_spreadsheet(conn: sqlite3.Connection, dst: Path) -> None:
        wb=Workbook()
        wb.remove(wb.active)
        teachers=[dict(r) for r in conn.execute('SELECT * FROM teachers ORDER BY id')]
        records=[dict(r) for r in conn.execute('SELECT * FROM work_records ORDER BY work_date,teacher_id')]
        contracts=[dict(r) for r in conn.execute('SELECT * FROM contracts ORDER BY teacher_id,effective_date')]
        events=[dict(r) for r in conn.execute('SELECT * FROM employment_events ORDER BY id')]
        reviews=[dict(r) for r in conn.execute('SELECT * FROM payroll_reviews ORDER BY teacher_id,month')]
        def sheet(name, columns, rows):
            ws=wb.create_sheet(name)
            ws.append(columns)
            for r in rows:
                ws.append([r.get(k) for k in columns])
            return ws
        sheet('선생님정보',TEACHER_COLUMNS,teachers)
        sheet('근무기록',WORK_COLUMNS,records)
        sheet('급여계약',CONTRACT_COLUMNS,contracts)
        sheet('재직변경',['id','teacher_id','effective_date','is_active'],events)
        sheet('정산확인',['teacher_id','month','reason'],reviews)
        # Include record-free monthly salary months through the current month, plus contract months.
        groups={(r['work_date'][:7],r['teacher_id']) for r in records}
        groups.update((r['month'],r['teacher_id']) for r in reviews)
        for c in contracts:
            if c['pay_type']=='monthly':
                y,m=map(int,c['effective_date'][:7].split('-'))
                end=max(date.today().strftime('%Y-%m'),c['effective_date'][:7])
                while f'{y:04d}-{m:02d}'<=end:
                    groups.add((f'{y:04d}-{m:02d}',c['teacher_id']))
                    y,m=(y+1,1) if m==12 else (y,m+1)
        by_id={t['id']:t for t in teachers}
        summary=[]
        for month,tid in sorted(groups):
            if by_id[tid]['role']!='teacher':
                continue
            payroll=calculate_salary(conn,by_id[tid],month,[r for r in records if r['teacher_id']==tid and r['work_date'].startswith(month)])
            summary.append(dict(month=month,teacher_id=tid,teacher_name=by_id[tid]['name'],**payroll))
        sheet('월별급여',['month','teacher_id','teacher_name','total_minutes','gross','withholding','net','work_days','pay_type','amount','needs_review','review_reason'],summary)
        settings=get_settings(conn)
        sheet('설정',['key','value'],[{'key':k,'value':v} for k,v in settings.items()]+[{'key':'format_version','value':'13'}])
        for ws in wb:
            ws.freeze_panes='A2'
            ws.auto_filter.ref=ws.dimensions
            for cell in ws[1]:
                cell.font=Font(bold=True,color='FFFFFF')
                cell.fill=PatternFill('solid',fgColor='204B49')
            from openpyxl.utils import get_column_letter
            for col in ws.columns:
                ws.column_dimensions[get_column_letter(col[0].column)].width=min(max(14,max(len(str(c.value or '')) for c in col)+3),42)
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value,str):
                        cell.data_type='s'
        wb.save(dst)
        wb.close()

    def backup_after_change(conn: sqlite3.Connection) -> dict[str, Any]:
        """SQLite is source of truth. Report Excel failures, never claim they saved."""
        try:
            latest = backup_dir / "급여관리_최신.xlsx"
            tmp = backup_dir / "_latest_working.xlsx"
            save_spreadsheet(conn,tmp)
            os.replace(tmp,latest)
            # SQLite's online backup API produces a consistent snapshot.
            db_latest = backup_dir / "급여관리_최신.sqlite3"
            db_tmp = backup_dir / "_latest_working.sqlite3"
            with closing(sqlite3.connect(db_tmp)) as dst:
                conn.backup(dst)
            os.replace(db_tmp,db_latest)
            today_folder = backup_dir / datetime.now().strftime("%Y-%m-%d")
            today_folder.mkdir(exist_ok=True)
            # Keep previous daily copies, not just a single always-overwritten file.
            day_xlsx = today_folder / "급여관리_일별보관.xlsx"
            day_sqlite = today_folder / "급여관리_일별보관.sqlite3"
            if not day_xlsx.exists():
                shutil.copy2(latest,day_xlsx)
            if not day_sqlite.exists():
                shutil.copy2(db_latest,day_sqlite)
            return {"ok":True,"message":"엑셀 및 SQLite 백업 갱신 완료"}
        except Exception:
            logging.getLogger(__name__).error("Automatic backup failed")
            return {"ok":False,"message":"데이터베이스 저장은 완료되었으나 자동 백업이 실패했습니다. 열려 있는 Excel 파일을 닫고 저장 공간과 백업 폴더의 쓰기 권한을 확인해 주세요."}

    def parse_restore(content):
        try:
            wb=load_workbook(io.BytesIO(content),read_only=True,data_only=False)
        except Exception:
            raise HTTPException(422,'엑셀 파일을 열 수 없습니다.') from None
        try:
            def rows(name, required):
                if name not in wb.sheetnames:
                    raise ValueError(f'{name} 시트가 없습니다.')
                values=list(wb[name].values)
                if not values or not set(required).issubset(values[0]) or len(set(values[0]))!=len(values[0]):
                    raise ValueError(f'{name} 열 이름이 올바르지 않습니다.')
                return [dict(zip(values[0],r)) for r in values[1:] if any(v is not None for v in r)]
            teachers=rows('선생님정보',TEACHER_COLUMNS[:7])
            records=rows('근무기록',WORK_COLUMNS[:8])
            settings={str(r['key']):str(r['value'] or '') for r in rows('설정',['key','value'])}
            modern=settings.get('format_version')=='13'
            if settings.get('format_version') not in (None,'','13'):
                raise ValueError('지원하지 않는 백업 버전입니다.')
            contracts=rows('급여계약',CONTRACT_COLUMNS) if modern else []
            events=rows('재직변경',['id','teacher_id','effective_date','is_active']) if modern else []
            reviews=rows('정산확인',['teacher_id','month','reason']) if modern else []
            ids=set(); logins=set()
            for t in teachers:
                tid=int(t['id']); active=int(t['is_active']); rate=int(t['hourly_rate'])
                if tid<=0 or tid in ids or active not in (0,1) or not 1<=rate<=1_000_000:
                    raise ValueError('선생님 정보가 올바르지 않습니다.')
                data=clean_teacher(TeacherInput(name=t['name'],phone=str(t['phone'] or ''),hourly_rate=rate,role=t['role'],login_id=t.get('login_id') or None))
                if data['login_id'] and data['login_id'].casefold() in logins:
                    raise ValueError('로그인 아이디 중복')
                if data['login_id']: logins.add(data['login_id'].casefold())
                ids.add(tid)
                t.update(id=tid,name=data['name'],phone=str(t['phone'] or ''),hourly_rate=rate,is_active=active,login_id=data['login_id'])
                if not modern and t['role']=='teacher':
                    contracts.append(dict(id=len(contracts)+1,teacher_id=tid,effective_date='2000-01-01',pay_type='hourly',amount=rate,withholding_bps=330,is_active=active))
            def validate_rows(items, dates=False):
                seen=set(); unique=set()
                for r in items:
                    r['id']=int(r['id']);r['teacher_id']=int(r['teacher_id'])
                    if r['id']<=0 or r['id'] in seen or r['teacher_id'] not in ids:
                        raise ValueError('기록 ID 또는 사용자 참조 오류')
                    seen.add(r['id'])
                    if dates:
                        check_date(str(r['effective_date']))
                        key=(r['teacher_id'],r['effective_date'])
                        if key in unique: raise ValueError('계약 적용일 중복')
                        unique.add(key)
            validate_rows(contracts,True);validate_rows(events);validate_rows(records)
            for c in contracts:
                c['amount']=int(c['amount']); c['withholding_bps']=int(c['withholding_bps']);c['is_active']=int(c['is_active'])
                if c['pay_type'] not in PAY_TYPES or not 1<=c['amount']<=1_000_000_000 or c['withholding_bps']!=330 or c['is_active'] not in (0,1):
                    raise ValueError('계약 정보가 올바르지 않습니다.')
            for e in events:
                check_date(str(e['effective_date']))
                e['is_active']=int(e['is_active'])
                if e['is_active'] not in (0,1):raise ValueError('재직 상태 오류')
            by_id={t['id']:t for t in teachers}
            if any(by_id[c['teacher_id']]['role']!='teacher' for c in contracts) or any(t['role']=='teacher' and not any(c['teacher_id']==t['id'] for c in contracts) for t in teachers):
                raise ValueError('선생님별 급여 계약이 올바르지 않습니다.')
            review_keys=set()
            for r in reviews:
                r['teacher_id']=int(r['teacher_id']);check_month(str(r['month']))
                key=(r['teacher_id'],r['month'])
                if r['teacher_id'] not in ids or key in review_keys or not isinstance(r['reason'],str) or not 1<=len(r['reason'])<=200:
                    raise ValueError('정산 확인 정보 오류')
                review_keys.add(key)
            keys=set()
            for r in records:
                check_date(str(r['work_date']))
                key=(r['teacher_id'],r['work_date'])
                mins=minutes_between(str(r['start_time']),str(r['end_time']))
                if key in keys or int(r['minutes'])!=mins or not 1<=int(r['applied_hourly_rate'])<=1_000_000:
                    raise ValueError('근무기록 오류')
                keys.add(key)
                r.update(minutes=mins,applied_hourly_rate=int(r['applied_hourly_rate']))
                if not modern:r.update(applied_pay_type='hourly',applied_amount=r['applied_hourly_rate'],withholding_bps=330)
                r['applied_amount']=int(r['applied_amount']);r['withholding_bps']=int(r['withholding_bps'])
                if r['applied_pay_type']=='hourly' and r['applied_amount']!=r['applied_hourly_rate']:
                    raise ValueError('기존 적용 시급과 계약 금액이 일치하지 않습니다.')
                if r['applied_pay_type'] not in PAY_TYPES or not 1<=r['applied_amount']<=1_000_000_000 or r['withholding_bps']!=330:
                    raise ValueError('근무일 계약 정보 오류')
            if not settings.get('academy_name','').strip() or len(settings['academy_name'])>100 or len(settings.get('principal_name',''))>80 or settings.get('withholding_bps')!='330':
                raise ValueError('학원 설정 또는 공제율 오류')
            return teachers,records,contracts,events,reviews,settings,modern
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            raise HTTPException(422,f'엑셀 복원 데이터 검증 실패: {exc}') from None
        finally:
            wb.close()

    initialize()
    with connect() as conn:
        migrate(conn, backup_dir)
    app = FastAPI(title="학원 급여 관리 MVP",docs_url=None,redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])

    @app.middleware("http")
    async def block_external_writes(request: Request,call_next):
        if request.method in ("POST","PUT","PATCH","DELETE"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.headers.get('host')}":
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=403,content={"detail":"다른 사이트에서 이 프로그램을 수정할 수 없습니다."})
        return await call_next(request)

    def initialized(conn):
        return conn.execute("SELECT value FROM settings WHERE key='auth_initialized'").fetchone()[0] == '1'

    def make_session(conn, tid):
        token = secrets.token_urlsafe(32)
        conn.execute('DELETE FROM sessions WHERE expires_at<=?', (int(time.time()),))
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), tid, int(time.time())+8*3600))
        conn.commit()
        response = JSONResponse({'ok': True})
        response.set_cookie('payroll_session', token, httponly=True, samesite='strict', secure=False, max_age=8*3600, path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.middleware('http')
    async def authorize(request: Request, call_next):
        path = request.url.path
        if path.startswith('/api/'):
            # Reject cross-site requests even if an Origin header was omitted.
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail':'다른 사이트에서 접근할 수 없습니다.'}, status_code=403)
            with lock, connect() as conn:
                token = request.cookies.get('payroll_session', '')
                row = conn.execute('''SELECT t.* FROM sessions s JOIN teachers t ON t.id=s.teacher_id
                    WHERE s.token_hash=? AND s.expires_at>? AND t.is_active=1''',
                    (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
                user = dict(row) if row else None
            request.state.user = user
            public = path in ('/api/bootstrap','/api/auth/login','/api/auth/setup','/api/auth/logout')
            if not public:
                if not user:
                    return JSONResponse({'detail':'로그인이 필요합니다.'}, status_code=401, headers={'Cache-Control':'no-store'})
                own = re.fullmatch(r'/api/teachers/(\d+)/(months/[^/]+|records/[^/]+)', path)
                if user['role'] != 'director' and not (own and int(own[1]) == user['id']):
                    return JSONResponse({'detail':'접근 권한이 없습니다.'}, status_code=403, headers={'Cache-Control':'no-store'})
                # Directors can view records but never write them, including forged requests.
                if own and own[2].startswith('records/') and user['role']=='director':
                    return JSONResponse({'detail':'원장님 근무기록 조회는 읽기 전용입니다.'}, status_code=403)
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return await call_next(request)

    @app.post('/api/auth/setup')
    def setup(request: Request, payload: TeacherInput):
        if request.client.host not in ('127.0.0.1','::1','testclient'):
            raise HTTPException(403, '최초 등록은 학원 컴퓨터에서만 가능합니다.')
        t = clean_teacher(payload)
        if t['role'] != 'director' or not t['login_id']:
            raise HTTPException(422, '최초 원장님 계정의 이름·아이디·비밀번호를 입력해 주세요.')
        hashed = password_hash(payload.password)
        with lock, connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if initialized(conn):
                raise HTTPException(409, '최초 관리자 등록이 이미 완료되었습니다.')
            # Bind the first legacy director if present, preserving its ID and old records.
            legacy = conn.execute("SELECT id FROM teachers WHERE role='director' ORDER BY id LIMIT 1").fetchone()
            if legacy:
                tid = legacy['id']
                conn.execute('UPDATE teachers SET name=?,login_id=?,password_hash=?,is_active=1 WHERE id=?', (t['name'],t['login_id'],hashed,tid))
            else:
                tid = conn.execute("INSERT INTO teachers(name,phone,hourly_rate,role,login_id,password_hash) VALUES (?,'',1,'director',?,?)", (t['name'],t['login_id'],hashed)).lastrowid
            conn.execute("UPDATE settings SET value='1' WHERE key='auth_initialized'")
            conn.commit()
            response = make_session(conn,tid)
            backup_after_change(conn)
            return response

    @app.post('/api/auth/login')
    def login(request: Request, payload: dict):
        login_id = str(payload.get('login_id','')).strip()
        tid = payload.get('teacher_id')
        password = payload.get('password','')
        with lock, connect() as conn:
            now = int(time.time())
            # Both name-selection and login-ID requests share the same account budget.
            row = conn.execute('SELECT * FROM teachers WHERE id=?' if isinstance(tid,int) and not isinstance(tid,bool) else 'SELECT * FROM teachers WHERE login_id=? COLLATE NOCASE', (tid if isinstance(tid,int) and not isinstance(tid,bool) else login_id,)).fetchone()
            key = f"account:{row['id']}" if row else f"unknown:{request.client.host}"
            attempts = conn.execute('SELECT * FROM auth_attempts WHERE key=?', (key,)).fetchone()
            if attempts and attempts['blocked_until'] > now:
                raise HTTPException(429, '로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.',
                                    headers={'Retry-After':str(attempts['blocked_until']-now)})
            # After a completed cooldown the user gets a fresh five-attempt budget.
            previous_failures=attempts['failures'] if attempts and not attempts['blocked_until'] else 0
            # Use the same expensive hash path for unprovisioned and unknown accounts.
            dummy = 'scrypt$' + '00'*16 + '$' + '00'*64
            valid = verify_password(password, row['password_hash'] if row and row['password_hash'] else dummy)
            if not row or not row['is_active'] or not valid or payload.get('role',row['role'])!=row['role']:
                failures = previous_failures + 1
                conn.execute('INSERT OR REPLACE INTO auth_attempts VALUES (?,?,?)', (key, failures, now+LOGIN_COOLDOWN_SECONDS if failures>=LOGIN_FAILURE_LIMIT else 0))
                conn.commit()
                raise HTTPException(401, '아이디 또는 비밀번호를 확인해 주세요.')
            conn.execute('DELETE FROM auth_attempts WHERE key=?', (key,))
            return make_session(conn,row['id'])

    @app.post('/api/auth/logout')
    def logout(request: Request):
        with lock, connect() as conn:
            conn.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(request.cookies.get('payroll_session','').encode()).hexdigest(),))
            conn.commit()
        response = JSONResponse({'ok':True})
        response.delete_cookie('payroll_session', path='/')
        return response

    @app.get("/")
    def home():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        with lock,connect() as conn:
            user = request.state.user
            teachers = [public_teacher(conn,r) for r in conn.execute('SELECT * FROM teachers ORDER BY is_active DESC,name,id')]
            if not user or user['role']!='director':
                # Only the chosen person's detailed payroll may be exposed to a teacher.
                teachers = [t if user and t['id']==user['id'] else
                            {k:t[k] for k in ('id','name','role','is_active','login_id','credentials_set')}
                            for t in teachers if t['is_active'] or user and t['id']==user['id']]
            return {'teachers':teachers,'settings':get_settings(conn),
                    'user':{k:user[k] for k in ('id','name','role','login_id')} if user else None,
                    'setup_required':not initialized(conn)}

    def validate_contract(conn, tid, t):
        latest = conn.execute('SELECT * FROM contracts WHERE teacher_id=? ORDER BY effective_date DESC LIMIT 1',(tid,)).fetchone()
        if latest and (latest['pay_type'],latest['amount']) == (t['pay_type'],t['amount']):
            return False
        if latest and t['effective_date'] <= latest['effective_date']:
            raise HTTPException(422, '새 계약 적용일은 마지막 계약 적용일보다 늦어야 합니다.')
        # Backdated contracts cannot change any already saved work or elapsed payroll month.
        if latest and (latest['pay_type']=='monthly' or t['pay_type']=='monthly') and t['effective_date'] < date.today().replace(day=1).isoformat():
            raise HTTPException(422, '지난달 이전으로 계약을 소급할 수 없습니다.')
        if conn.execute('SELECT 1 FROM work_records WHERE teacher_id=? AND work_date>=?',(tid,t['effective_date'])).fetchone():
            raise HTTPException(422, '기존 근무기록 이후 날짜를 계약 적용일로 선택해 주세요.')
        return True

    @app.post('/api/teachers')
    def register_teacher(payload: TeacherInput):
        t=clean_teacher(payload)
        if not t['login_id']:
            raise HTTPException(422,'로그인 아이디를 입력해 주세요.')
        hashed=password_hash(payload.password)
        with lock,connect() as conn:
            try:
                tid=conn.execute('''INSERT INTO teachers(name,phone,hourly_rate,role,login_id,password_hash)
                    VALUES (?,?,?,?,?,?)''',(t['name'],t['phone'],min(t['amount'],1_000_000) if t['pay_type']=='hourly' else 1,t['role'],t['login_id'],hashed)).lastrowid
                if t['role']=='teacher':
                    conn.execute('INSERT INTO contracts(teacher_id,effective_date,pay_type,amount) VALUES (?,?,?,?)',(tid,t['effective_date'],t['pay_type'],t['amount']))
                conn.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(409,'이미 사용 중인 로그인 아이디입니다.') from None
            return {'teacher':get_teacher(conn,tid),'backup':backup_after_change(conn)}

    @app.put('/api/teachers/{teacher_id}')
    def edit_teacher(teacher_id: int,payload: TeacherInput,request: Request):
        t=clean_teacher(payload)
        with lock,connect() as conn:
            old=get_teacher(conn,teacher_id)
            if old['role'] != t['role']:
                raise HTTPException(422,'기존 기록 보호를 위해 등록된 계정의 역할은 바꿀 수 없습니다. 새 계정을 등록해 주세요.')
            login_id=t['login_id'] if t['login_id'] is not None else old['login_id']
            if not login_id:
                raise HTTPException(422,'로그인 아이디를 설정해 주세요.')
            hashed=password_hash(payload.password) if payload.password else None
            if not old['credentials_set'] and not hashed:
                raise HTTPException(422,'기존 계정의 비밀번호를 설정해 주세요.')
            changed=t['role']=='teacher' and validate_contract(conn,teacher_id,t)
            try:
                conn.execute('UPDATE teachers SET name=?,phone=?,login_id=? WHERE id=?',(t['name'],t['phone'],login_id,teacher_id))
                if hashed:
                    conn.execute('UPDATE teachers SET password_hash=? WHERE id=?',(hashed,teacher_id))
                if hashed or login_id!=old['login_id']:
                    conn.execute('DELETE FROM sessions WHERE teacher_id=?',(teacher_id,))
                    conn.execute('DELETE FROM auth_attempts WHERE key=?',(f'account:{teacher_id}',))
                if changed:
                    conn.execute('INSERT INTO contracts(teacher_id,effective_date,pay_type,amount) VALUES (?,?,?,?)',(teacher_id,t['effective_date'],t['pay_type'],t['amount']))
                    if t['pay_type']=='hourly':
                        conn.execute('UPDATE teachers SET hourly_rate=? WHERE id=?',(t['amount'],teacher_id))
                conn.commit()
            except sqlite3.IntegrityError:
                raise HTTPException(409,'이미 사용 중인 로그인 아이디입니다.') from None
            return {'teacher':get_teacher(conn,teacher_id),'backup':backup_after_change(conn)}

    @app.patch('/api/teachers/{teacher_id}/status')
    def status_teacher(teacher_id: int,active: bool,request: Request):
        with lock,connect() as conn:
            teacher=get_teacher(conn,teacher_id)
            if teacher_id==request.state.user['id'] and not active:
                raise HTTPException(409,'현재 로그인한 원장님 계정은 비활성화할 수 없습니다.')
            if not active and teacher['role']=='director' and conn.execute("SELECT COUNT(*) FROM teachers WHERE role='director' AND is_active=1 AND password_hash IS NOT NULL").fetchone()[0]<=1:
                raise HTTPException(409,'마지막 활성 원장님 계정은 비활성화할 수 없습니다.')
            if bool(teacher['is_active'])!=active and teacher['role']=='teacher':
                day=date.today().isoformat()
                conn.execute('INSERT INTO employment_events(teacher_id,effective_date,is_active) VALUES (?,?,?)',(teacher_id,day,int(active)))
            conn.execute('UPDATE teachers SET is_active=? WHERE id=?',(int(active),teacher_id))
            if not active:
                conn.execute('DELETE FROM sessions WHERE teacher_id=?',(teacher_id,))
            conn.commit()
            return {'teacher':get_teacher(conn,teacher_id),'backup':backup_after_change(conn)}

    @app.get("/api/teachers/{teacher_id}/months/{month}")
    def teacher_month(teacher_id: int,month: str):
        check_month(month)
        with lock,connect() as conn:
            return monthly_payload(conn,teacher_id,month)

    @app.put("/api/teachers/{teacher_id}/records/{work_date}")
    def upsert_record(teacher_id: int,work_date: str,payload: WorkInput):
        check_date(work_date)
        minutes = minutes_between(payload.start_time,payload.end_time)
        with lock,connect() as conn:
            teacher = get_teacher(conn,teacher_id)
            if not teacher["is_active"]:
                raise HTTPException(409,"비활성화된 선생님의 근무기록은 수정할 수 없습니다.")
            if teacher['role']!='teacher':
                raise HTTPException(422,'원장님은 급여 계산 대상이 아닙니다.')
            existing = conn.execute("SELECT * FROM work_records WHERE teacher_id=? AND work_date=?",(teacher_id,work_date)).fetchone()
            contract = contract_on(conn,teacher_id,work_date)
            if not existing and not contract:
                raise HTTPException(422,'해당 날짜에 유효한 계약이 없습니다.')
            applied_rate = existing['applied_hourly_rate'] if existing else contract['amount'] if contract['pay_type']=='hourly' else 1
            pay_type = existing['applied_pay_type'] if existing else contract['pay_type']
            amount = existing['applied_amount'] if existing else contract['amount']
            bps = existing['withholding_bps'] if existing else contract['withholding_bps']
            conn.execute("""INSERT INTO work_records(teacher_id,work_date,start_time,end_time,minutes,applied_hourly_rate,applied_pay_type,applied_amount,withholding_bps)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(teacher_id,work_date) DO UPDATE SET
                start_time=excluded.start_time,end_time=excluded.end_time,
                minutes=excluded.minutes,updated_at=CURRENT_TIMESTAMP""",
                (teacher_id,work_date,payload.start_time,payload.end_time,minutes,applied_rate,pay_type,amount,bps))
            conn.commit()
            return {"month":monthly_payload(conn,teacher_id,work_date[:7]),"backup":backup_after_change(conn)}

    @app.delete("/api/teachers/{teacher_id}/records/{work_date}")
    def delete_record(teacher_id: int,work_date: str):
        check_date(work_date)
        with lock,connect() as conn:
            teacher=get_teacher(conn,teacher_id)
            if not teacher["is_active"]:
                raise HTTPException(409,"비활성화된 선생님의 기록은 수정할 수 없습니다.")
            cur=conn.execute("DELETE FROM work_records WHERE teacher_id=? AND work_date=?",(teacher_id,work_date))
            if not cur.rowcount:
                raise HTTPException(404,"삭제할 근무기록이 없습니다.")
            conn.commit()
            return {"month":monthly_payload(conn,teacher_id,work_date[:7]),"backup":backup_after_change(conn)}

    @app.get("/api/overview/{month}")
    def overview(month: str):
        check_month(month)
        with lock,connect() as conn:
            teachers = [public_teacher(conn,r) for r in conn.execute("SELECT * FROM teachers WHERE role='teacher' ORDER BY is_active DESC,name,id")]
            items = [{"teacher":t,"salary":calculate_salary(conn,t,month,get_records(conn,t["id"],month))} for t in teachers]
            return {"month":month,"items":items,
                    "needs_review":any(item["salary"]["needs_review"] for item in items),
                    "totals":{k:sum(item["salary"][k] or 0 for item in items) for k in ("total_minutes","work_days","gross","withholding","net")}}

    @app.put('/api/teachers/{teacher_id}/review/{month}')
    def mark_review(teacher_id: int,month: str,payload: dict):
        check_month(month)
        reason=payload.get('reason','')
        if not isinstance(reason,str) or len(reason)>200:
            raise HTTPException(422,'정산 확인 사유는 200자 이하로 입력해 주세요.')
        with lock,connect() as conn:
            teacher=get_teacher(conn,teacher_id)
            if teacher['role']!='teacher':
                raise HTTPException(422,'일반 선생님만 정산 확인 대상으로 지정할 수 있습니다.')
            if reason.strip():
                conn.execute('INSERT INTO payroll_reviews VALUES (?,?,?) ON CONFLICT(teacher_id,month) DO UPDATE SET reason=excluded.reason',(teacher_id,month,reason.strip()))
            else:
                conn.execute('DELETE FROM payroll_reviews WHERE teacher_id=? AND month=?',(teacher_id,month))
            conn.commit()
            return {'backup':backup_after_change(conn)}

    @app.put("/api/settings")
    def change_settings(payload: SettingsInput):
        name = payload.academy_name.strip()
        if not name:
            raise HTTPException(422,"학원명을 입력해 주세요.")
        with lock,connect() as conn:
            conn.execute("UPDATE settings SET value=? WHERE key='academy_name'",(name,))
            conn.execute("UPDATE settings SET value=? WHERE key='principal_name'",(payload.principal_name.strip(),))
            conn.commit()
            return {"settings":get_settings(conn),"backup":backup_after_change(conn)}

    @app.get("/api/backup/excel")
    def export_excel():
        with lock,connect() as conn:
            # Always regenerate on demand (including a brand-new empty database).
            tmp = backup_dir / "_download.xlsx"
            save_spreadsheet(conn,tmp)
            return FileResponse(tmp,filename="급여관리_백업.xlsx",media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/api/backup/restore")
    async def restore_excel(request: Request, file: UploadFile = File(...)):
        name=file.filename or ""
        if not name.lower().endswith(".xlsx"):
            raise HTTPException(422,".xlsx 백업 파일만 불러올 수 있습니다.")
        content=await file.read(MAX_EXCEL_BYTES+1)
        if len(content)>MAX_EXCEL_BYTES:
            raise HTTPException(413,"엑셀 파일이 너무 큽니다(최대 12MB).")
        teachers,records,contracts,events,reviews,settings,modern = parse_restore(content)
        with lock,connect() as conn:
            current={r['id']:dict(r) for r in conn.execute('SELECT * FROM teachers')}
            # Excel never contains password hashes. Retain credentials only for matching identities.
            for t in teachers:
                old=current.get(t['id'])
                same=old and old['role']==t['role'] and (old['login_id']==t['login_id'] if modern else old['name']==t['name'])
                t['password_hash']=old['password_hash'] if same else None
                if same and not modern:t['login_id']=old['login_id']
            # Retain the signed-in director even when importing a pre-account workbook.
            principal=current[request.state.user['id']]
            imported=next((t for t in teachers if t['id']==principal['id']),None)
            if imported and (imported['role']!='director' or imported['password_hash']!=principal['password_hash'] or not imported['is_active']):
                raise HTTPException(422,'현재 원장님 계정과 백업의 ID가 충돌합니다. 현재 관리자 계정을 보존할 수 없어 복원을 중단했습니다.')
            if not imported:teachers.append(principal)
            safe_path=backup_dir / f'복원전_안전백업_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3'
            with closing(sqlite3.connect(safe_path)) as dst:
                conn.backup(dst)
            try:
                conn.execute('BEGIN IMMEDIATE')
                for table in ('sessions','payroll_reviews','employment_events','contracts','work_records','teachers'):
                    conn.execute(f'DELETE FROM {table}')
                def insert(table,columns,items):
                    for item in items:
                        conn.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",[item.get(k) for k in columns])
                insert('teachers',TEACHER_COLUMNS+['password_hash'],teachers)
                insert('work_records',WORK_COLUMNS,records)
                insert('contracts',CONTRACT_COLUMNS,contracts)
                insert('employment_events',['id','teacher_id','effective_date','is_active'],events)
                insert('payroll_reviews',['teacher_id','month','reason'],reviews)
                for k in ('academy_name','principal_name'):
                    conn.execute('UPDATE settings SET value=? WHERE key=?',(settings.get(k,''),k))
                conn.commit()
            except Exception:
                conn.rollback()
                raise HTTPException(422,'복원 도중 오류가 발생하여 기존 데이터는 유지했습니다.') from None
            return {'restored_teachers':len(teachers),'restored_records':len(records),'reauth_required':True,
                    'backup':backup_after_change(conn)}

    app.mount("/static",StaticFiles(directory=ROOT / "static"),name="static")
    return app


if __name__=="__main__":
    app = create_app()
    import uvicorn
    print("학원 급여 관리: http://127.0.0.1:8000")
    uvicorn.run(app,host="127.0.0.1",port=8000)
