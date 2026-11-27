"""Never let importing the app during tests initialize the operator's real database."""
import atexit
import os
import shutil
import tempfile

_test_data=tempfile.mkdtemp(prefix='payroll-tests-')
os.environ['PAYROLL_DATA_DIR']=_test_data
atexit.register(shutil.rmtree,_test_data,ignore_errors=True)
