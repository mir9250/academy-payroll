"""Offline desktop launcher. No payroll data is bundled or logged."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request
import webbrowser

from runtime_paths import data_directory


class InstanceLock:
    def __init__(self, directory):
        self.path = Path(directory) / 'instance.lock'
        self.file = None

    def acquire(self):
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def read_service(directory):
    try:
        info = json.loads((directory / 'service.json').read_text(encoding='utf-8'))
        port = info['port']
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        url = f'http://127.0.0.1:{port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/__desktop_health', timeout=1) as response:
            if response.geturl() == url + '/__desktop_health' and json.load(response) == {'instance': info['instance']}:
                return url
    except (OSError, ValueError, KeyError):
        pass
    return None


def configure_logging(directory):
    log_dir = directory / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'errors.log', maxBytes=512_000, backupCount=3, encoding='utf-8')
    # Deliberately omit exception messages, request URLs, bodies and traceback locals.
    class SafeFormatter(logging.Formatter):
        def format(self, record):
            return time.strftime('%Y-%m-%d %H:%M:%S') + ' ERROR runtime failure; consult support'
    handler.setFormatter(SafeFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.ERROR)


class Service:
    def __init__(self, directory):
        import secrets
        from a2wsgi import ASGIMiddleware
        from waitress.server import create_server
        from app import create_app
        from fastapi.responses import JSONResponse
        self.directory = directory
        self.instance = secrets.token_hex(32)
        self.app = create_app(directory)

        @self.app.get('/__desktop_health', include_in_schema=False)
        def health():
            return JSONResponse({'instance': self.instance})

        self.server = None
        self.adapter = ASGIMiddleware(self.app)
        for port in (8000, 8001, 8002, 8003, 8004, 8005):
            try:
                self.server = create_server(self.adapter, host='127.0.0.1', port=port,
                                            map={}, threads=4, asyncore_loop_timeout=.1,
                                            max_request_body_size=13 * 1024 * 1024)
                break
            except OSError:
                continue
        if self.server is None:
            raise OSError('No available local port')
        self.port = int(self.server.effective_port)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.port}'
        metadata = directory / 'service.json'
        temporary = directory / 'service.tmp'
        temporary.write_text(json.dumps({'port': self.port, 'instance': self.instance}), encoding='utf-8')
        temporary.replace(metadata)
        deadline = time.monotonic() + 15
        while not read_service(directory):
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise OSError('Server readiness timeout')
            time.sleep(.1)

    def close(self):
        (self.directory / 'service.json').unlink(missing_ok=True)
        self.server.task_dispatcher.shutdown(timeout=15)
        from waitress import wasyncore
        wasyncore.close_all(map=self.server._map)
        self.thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--headless', action='store_true', help='CI only: no browser/control window')
    args = parser.parse_args()
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    configure_logging(directory)
    lock = InstanceLock(directory)
    service = None
    root = None
    try:
        if not lock.acquire():
            for _ in range(150):
                url = read_service(directory)
                if url:
                    if not args.headless:
                        webbrowser.open(url)
                    return 0
                time.sleep(.1)
            raise OSError('Existing service is not ready')
        service = Service(directory)
        if args.headless:
            # CI exits gracefully by creating this sentinel in its isolated data folder.
            while not (directory / 'stop.request').exists():
                if not service.thread.is_alive():
                    raise OSError('Server stopped')
                time.sleep(.1)
            (directory / 'stop.request').unlink()
        else:
            import tkinter as tk
            root = tk.Tk()
            root.title('학원 급여관리')
            tk.Label(root, text='급여관리 서버 실행 중\n브라우저를 닫아도 서버는 실행됩니다.', padx=25, pady=20).pack()
            tk.Button(root, text='급여관리 열기', command=lambda: webbrowser.open(service.url)).pack(pady=5)
            tk.Button(root, text='프로그램 종료', command=root.destroy).pack(pady=10)
            webbrowser.open(service.url)
            root.mainloop()
        return 0
    except Exception:
        logging.error('runtime failure')
        if not args.headless:
            import tkinter.messagebox
            tkinter.messagebox.showerror('급여관리 실행 오류',
                f'프로그램을 실행하지 못했습니다. 실행 중인 창을 확인하고 다시 시도해 주세요.\n데이터를 삭제하지 마세요.\n오류 기록: {directory / "logs" / "errors.log"}')
        return 1
    finally:
        if service:
            service.close()
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
