"""Explicit local PIN reset, authenticated with an existing director's password.

Run `python reset_pin.py` with the server stopped. No secret is passed via command arguments.
Importing this module never creates or migrates a database.
"""
import getpass
from contextlib import closing
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from payroll_v13 import (LOGIN_COOLDOWN_SECONDS, LOGIN_FAILURE_LIMIT,
                         _verify_scrypt_secret, password_hash)


def reset_pin(db_path, admin_login, admin_password, target_login, new_pin):
    db_path=Path(db_path).resolve(strict=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory=sqlite3.Row
        admin=conn.execute('SELECT * FROM teachers WHERE login_id=? COLLATE NOCASE',(admin_login,)).fetchone()
        key=f"account:{admin['id']}" if admin else 'unknown:local-reset'
        now=int(time.time())
        attempt=conn.execute('SELECT * FROM auth_attempts WHERE key=?',(key,)).fetchone()
        if attempt and attempt['blocked_until']>now:
            raise HTTPException(429,'로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.')
        if not admin or admin['role']!='director' or not admin['is_active'] or not _verify_scrypt_secret(admin_password,admin['password_hash']):
            failures=(attempt['failures'] if attempt and not attempt['blocked_until'] else 0)+1
            conn.execute('INSERT OR REPLACE INTO auth_attempts VALUES (?,?,?)',
                         (key,failures,now+LOGIN_COOLDOWN_SECONDS if failures>=LOGIN_FAILURE_LIMIT else 0))
            conn.commit()
            raise HTTPException(401,'관리자 인증 정보를 확인해 주세요.')
        hashed=password_hash(new_pin)
        target=conn.execute('SELECT id FROM teachers WHERE login_id=? COLLATE NOCASE',(target_login,)).fetchone()
        if not target:
            raise HTTPException(404,'변경할 계정을 찾을 수 없습니다.')
        backup_dir=db_path.parent/'backups'
        backup_dir.mkdir(exist_ok=True)
        snapshot=backup_dir/f'PIN재설정전_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3'
        with closing(sqlite3.connect(snapshot)) as dst:
            conn.backup(dst)
        conn.execute('BEGIN IMMEDIATE')
        # Recheck authorization after acquiring the write lock.
        current=conn.execute('SELECT * FROM teachers WHERE id=?',(admin['id'],)).fetchone()
        if not current or not current['is_active'] or current['role']!='director' or current['password_hash']!=admin['password_hash']:
            raise HTTPException(401,'관리자 인증 정보를 확인해 주세요.')
        conn.execute('UPDATE teachers SET password_hash=? WHERE id=?',(hashed,target['id']))
        conn.execute('DELETE FROM sessions WHERE teacher_id=?',(target['id'],))
        conn.execute('DELETE FROM auth_attempts WHERE key IN (?,?)',(key,f"account:{target['id']}"))
        conn.commit()
        return snapshot


def main():
    db=Path(os.environ.get('PAYROLL_DATA_DIR',Path(__file__).resolve().parent/'data'))/'academy_payroll.sqlite3'
    print('서버를 종료한 상태에서 실행하세요. 기존 원장님 비밀번호로 인증한 뒤 지정한 계정의 PIN만 변경합니다.')
    admin_login=input('관리자 로그인 아이디: ').strip()
    old=getpass.getpass('기존 관리자 비밀번호: ')
    target=input('변경할 계정의 로그인 아이디: ').strip()
    pin=getpass.getpass('새 PIN (숫자 4자리): ')
    if pin!=getpass.getpass('새 PIN 확인: '):
        print('입력한 PIN이 일치하지 않습니다. 변경하지 않았습니다.')
        return 1
    try:
        snapshot=reset_pin(db,admin_login,old,target,pin)
    except HTTPException as exc:
        print(exc.detail)
        return 1
    except (OSError,sqlite3.Error):
        print('데이터베이스나 백업을 처리하지 못했습니다. 경로와 접근 권한을 확인해 주세요.')
        return 1
    print(f'PIN 변경 완료. 해당 계정은 다시 로그인해야 합니다. 변경 전 백업: {snapshot}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
