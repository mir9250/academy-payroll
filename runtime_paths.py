"""Persistent storage is independent of the executable/extraction directory."""
import os
import sys
from pathlib import Path


def data_directory():
    if getattr(sys, 'frozen', False):
        # Never allow a packaged update to redirect production data via environment.
        if sys.platform == 'win32':
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buffer):
                raise OSError('Windows LocalAppData unavailable')
            return Path(buffer.value) / 'AcademyPayroll'
        return Path.home() / '.local' / 'share' / 'AcademyPayroll'
    return Path(os.environ.get('PAYROLL_DATA_DIR', Path(__file__).parent / 'data')).resolve()
