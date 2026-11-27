"""Real-browser routing checks. Install requirements-dev.txt and Chromium first."""
import os
import socket
import threading
import time

import pytest
import uvicorn

playwright = pytest.importorskip('playwright.sync_api')
from app import create_app
from tests.helpers import PASSWORD


@pytest.fixture
def browser_page(tmp_path, request):
    # Every write, automatic Excel backup and restore stays in a temporary directory.
    server_socket = socket.socket()
    server_socket.bind(('127.0.0.1', 0))
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(data_dir=tmp_path), log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [server_socket]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.01)
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
            try:
                page = browser.new_page(accept_downloads=True)
                base = f'http://127.0.0.1:{port}'
                tid=None
                if getattr(request,'param',True):
                    setup=page.request.post(base+'/api/auth/setup',data={'name':'원장','role':'director','login_id':'admin','password':PASSWORD})
                    assert setup.ok
                    response = page.request.post(base + '/api/teachers', data={'name': '라우팅 테스트', 'hourly_rate': 16000,'login_id':'teacher','password':PASSWORD,'effective_date':'2026-01-01'})
                    assert response.ok
                    tid = response.json()['teacher']['id']
                    page.request.post(base+'/api/auth/logout')
                page.goto(base)
                screen(page, '#home', '학원')
                yield page, base, tid
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        server_socket.close()


def screen(page, hash_value, text):
    page.wait_for_function('(hash) => location.hash === hash', arg=hash_value)
    playwright.expect(page.locator('#app')).to_contain_text(text)
    playwright.expect(page.locator('#app .empty').filter(has_text='데이터를 불러오는 중')).to_have_count(0)


def click(page, action):
    page.locator(f'#app [data-action="{action}"]').first.click()


def login_ui(page, login_id=None):
    if login_id:page.locator('#login-form [name=login_id]').fill(login_id)
    page.locator('#login-form [name=password]').fill(PASSWORD)
    page.locator('#login-form [type=submit]').click()


def test_teacher_history_and_work_updates(browser_page):
    page, base, tid = browser_page
    click(page, 'choose')
    screen(page, '#teachers', '선생님 선택')
    click(page, 'open-teacher')
    screen(page,f'#teacher/{tid}','선생님 로그인')
    login_ui(page)
    screen(page, f'#teacher/{tid}', '출퇴근 시각 입력')
    length = page.evaluate('history.length')
    click(page, 'month-prev')
    screen(page, f'#teacher/{tid}', '출퇴근 시각 입력')
    page.locator('#start-time').fill('15:00')
    page.locator('#end-time').fill('17:00')
    page.locator('#work-form button[type=submit]').click()
    playwright.expect(page.locator('#toast')).to_contain_text('저장 완료')
    playwright.expect(page.locator('#app')).to_contain_text('2시간')
    click(page, 'edit-day')
    assert page.evaluate('history.length') == length
    page.reload()
    screen(page, f'#teacher/{tid}', '출퇴근 시각 입력')
    page.go_back()
    screen(page, '#teachers', '선생님 선택')
    page.go_back()
    screen(page, '#home', '학원')
    page.go_forward()
    screen(page, '#teachers', '선생님 선택')
    page.go_forward()
    screen(page, f'#teacher/{tid}', '출퇴근 시각 입력')
    page.locator('.topbar .brand').click()
    screen(page, '#home', '학원')
    length = page.evaluate('history.length')
    page.locator('.topbar .brand').click()
    assert page.evaluate('history.length') == length
    page.go_back()
    screen(page, f'#teacher/{tid}', '출퇴근 시각 입력')


def test_director_and_management_history(browser_page):
    page, base, tid = browser_page
    click(page, 'director')
    screen(page,'#director','원장님 로그인')
    login_ui(page,'admin')
    screen(page, '#director', '선생님별 월간 현황')
    click(page, 'open-director-teacher')
    screen(page, f'#director/teacher/{tid}', '읽기 전용')
    assert page.locator('#work-form').count() == 0
    click(page, 'payslip')
    screen(page, f'#director/teacher/{tid}/payslip', '급여명세서')
    page.reload()
    screen(page, f'#director/teacher/{tid}/payslip', '급여명세서')
    with page.expect_download() as downloaded:
        click(page, 'download-png')
    assert downloaded.value.suggested_filename.endswith('.png')
    page.go_back()
    screen(page, f'#director/teacher/{tid}', '읽기 전용')
    page.go_back()
    screen(page, '#director', '선생님별 월간 현황')
    page.go_back()
    screen(page, '#home', '학원')
    click(page, 'manage')
    screen(page, '#management', '신규 선생님 등록')
    click(page, 'tab-edit')
    screen(page, '#management/edit', '수정할 선생님 선택')
    page.locator(f'[data-action=manage-edit-teacher][data-id="{tid}"]').click()
    screen(page, f'#management/edit/{tid}', '변경사항 저장')
    page.reload()
    screen(page, f'#management/edit/{tid}', '변경사항 저장')
    page.go_back()
    screen(page, '#management/edit', '수정할 선생님 선택')
    page.go_back()
    screen(page, '#management', '신규 선생님 등록')
    click(page, 'tab-status')
    screen(page, '#management/status', '선생님 비활성화')
    page.go_back()
    screen(page, '#management', '신규 선생님 등록')
    page.go_back()
    screen(page, '#home', '학원')
    page.go_forward()
    screen(page, '#management', '신규 선생님 등록')


def test_direct_urls_fallbacks_and_rapid_navigation(browser_page):
    page, base, tid = browser_page
    assert page.request.post(base+'/api/auth/login',data={'login_id':'teacher','password':PASSWORD}).ok
    page.goto(base+f'/#teacher/{tid}')
    screen(page,f'#teacher/{tid}','출퇴근 시각 입력')
    page.reload()
    screen(page,f'#teacher/{tid}','출퇴근 시각 입력')
    assert page.request.post(base+'/api/auth/login',data={'login_id':'admin','password':PASSWORD}).ok
    for route, text in [(f'#director/teacher/{tid}', '읽기 전용'),
                        ('#management/status', '선생님 비활성화')]:
        page.goto(base + '/' + route)
        screen(page, route, text)
        page.reload()
        screen(page, route, text)
    for invalid, fallback, text in [('#teacher/999999', '#teachers', '선생님 선택'),
                                    ('#director/teacher/999999', '#director', '선생님별 월간 현황'),
                                    ('#director/teacher/999999/payslip', '#director', '선생님별 월간 현황'),
                                    ('#management/edit/999999', '#management/edit', '수정할 선생님 선택'),
                                    ('#teacher/nope', '#home', '학원'),
                                    ('#unknown', '#home', '학원')]:
        page.goto(base + '/' + invalid)
        screen(page, fallback, text)
    # An earlier slow monthly response must never overwrite a later home screen.
    def leave_before_response(route):
        page.evaluate("location.hash = '#home'")
        route.continue_()

    page.route('**/api/teachers/*/months/*', leave_before_response)
    with page.expect_response('**/api/teachers/*/months/*'):
        page.evaluate(f"location.hash = '#director/teacher/{tid}'")
    screen(page, '#home', '학원')
    page.wait_for_timeout(200)
    screen(page, '#home', '학원')


def test_ui_contract_registration_logout_and_access_guard(browser_page):
    page,base,tid=browser_page
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    click(page,'manage')
    screen(page,'#management','원장님 로그인')
    login_ui(page,'admin')
    screen(page,'#management','신규 선생님 등록')
    page.locator('#teacher-pay-type').select_option('daily')
    playwright.expect(page.locator('#teacher-amount-label')).to_have_text('일급 (원/근무일) *')
    page.locator('#teacher-pay-type').select_option('monthly')
    playwright.expect(page.locator('#teacher-amount-label')).to_have_text('월급 (원/월) *')
    page.locator('#teacher-name').fill('월급 선생님')
    page.locator('#teacher-login').fill('monthly')
    page.locator('#teacher-password').fill(PASSWORD)
    page.locator('#teacher-amount').fill('3000000')
    page.locator('#contract-date').fill('2026-01-01')
    page.locator('#create-teacher-form [type=submit]').click()
    playwright.expect(page.locator('#toast')).to_contain_text('저장 완료')
    playwright.expect(page.locator('#teacher-name')).to_have_value('')
    page.locator('#teacher-role').select_option('director')
    playwright.expect(page.locator('#teacher-pay-fields')).to_be_hidden()
    page.locator('#teacher-name').fill('추가 원장님')
    page.locator('#teacher-login').fill('director2')
    page.locator('#teacher-password').fill(PASSWORD)
    page.locator('#create-teacher-form [type=submit]').click()
    playwright.expect(page.locator('#teacher-name')).to_have_value('')
    page.locator('.topbar .brand').click()
    screen(page,'#home','학원')
    click(page,'director')
    screen(page,'#director','월급 선생님')
    playwright.expect(page.locator('#app')).not_to_contain_text('추가 원장님')
    playwright.expect(page.locator('#app')).to_contain_text('2,901,000원')
    page.locator('#logout-button').click()
    screen(page,'#director','원장님 로그인')
    playwright.expect(page.locator('#app')).not_to_contain_text('2,901,000원')
    page.go_back()
    screen(page,'#home','학원')
    page.go_forward()
    screen(page,'#director','원장님 로그인')
    page.reload()
    screen(page,'#director','원장님 로그인')
    page.goto(base+f'/#teacher/{tid}')
    screen(page,f'#teacher/{tid}','선생님 로그인')
    login_ui(page)
    screen(page,f'#teacher/{tid}','출퇴근 시각 입력')
    page.goto(base+'/#management')
    screen(page,'#management','원장님 로그인')
    assert page.request.get(base+'/api/backup/excel').status==403
    assert not errors


@pytest.mark.parametrize('browser_page',[False],indirect=True)
def test_first_run_admin_setup_ui(browser_page,tmp_path):
    page,base,_=browser_page
    playwright.expect(page.locator('#app')).to_contain_text('최초 원장님 계정 설정')
    page.locator('#setup-form [name=name]').fill('테스트 원장님')
    page.locator('#setup-form [name=login_id]').fill('firstadmin')
    page.locator('#setup-form [name=password]').fill(PASSWORD)
    page.locator('#setup-form [type=submit]').click()
    screen(page,'#home','선생님 관리')
    playwright.expect(page.locator('#session-controls')).to_contain_text('테스트 원장님')
    click(page,'manage')
    screen(page,'#management','신규 선생님 등록')
    page.screenshot(path=str(tmp_path/'v13-management.png'),full_page=True)
    page.reload()
    screen(page,'#management','신규 선생님 등록')
    assert page.request.post(base+'/api/auth/setup',data={'name':'또 등록','role':'director','login_id':'second','password':PASSWORD}).status==409


@pytest.mark.parametrize('browser_page',[False],indirect=True)
def test_four_digit_pin_validation_in_browser(browser_page):
    page,base,_=browser_page
    pin=page.locator('#setup-form [name=password]')
    playwright.expect(pin).to_have_attribute('type','password')
    playwright.expect(pin).to_have_attribute('inputmode','numeric')
    playwright.expect(pin).to_have_attribute('minlength','4')
    playwright.expect(pin).to_have_attribute('maxlength','4')
    page.locator('#setup-form [name=name]').fill('원장')
    page.locator('#setup-form [name=login_id]').fill('admin')
    for invalid in ('123','12a3','１２３４'):
        pin.fill(invalid)
        page.locator('#setup-form [type=submit]').click()
        assert not pin.evaluate('(el)=>el.validity.valid')
        if invalid!='123':playwright.expect(pin).to_have_value('123')
        assert pin.evaluate('(el)=>el.validationMessage')=='비밀번호는 숫자 4자리로 입력해 주세요.'
        assert page.request.get(base+'/api/bootstrap').json()['setup_required']
    pin.fill('0123')
    page.locator('#setup-form [type=submit]').click()
    screen(page,'#home','선생님 관리')
    click(page,'manage')
    screen(page,'#management','신규 선생님 등록')
    playwright.expect(page.locator('#teacher-password')).to_have_attribute('pattern','[0-9]{4}')
    page.locator('#logout-button').click()
    screen(page,'#management','원장님 로그인')
    playwright.expect(page.locator('#login-form [name=password]')).to_have_attribute('maxlength','4')
    login_ui(page,'admin')
    screen(page,'#management','신규 선생님 등록')


@pytest.mark.parametrize('browser_page',[False],indirect=True)
def test_pin_rejects_typing_and_paste_without_changing_the_value(browser_page):
    page,_,_=browser_page
    pin=page.locator('#setup-form [name=password]')
    pin.evaluate("el=>{el.value='12ab';el.dispatchEvent(new Event('input',{bubbles:true}));}")
    playwright.expect(pin).to_have_value('')
    pin.press_sequentially('0a1b23')
    playwright.expect(pin).to_have_value('0123')
    playwright.expect(pin).to_have_attribute('type','password')
    pin.fill('01')
    for invalid in ('ab','12ab','１２３４','12345'):
        prevented=pin.evaluate("""(el,text)=>{
            const data=new DataTransfer();data.setData('text/plain',text);
            const event=new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true});
            el.dispatchEvent(event);return event.defaultPrevented;
        }""",invalid)
        assert prevented
        playwright.expect(pin).to_have_value('01')
    # Exercise an actual browser paste as well as the synthetic rejection cases.
    page.context.grant_permissions(['clipboard-read','clipboard-write'])
    page.evaluate("navigator.clipboard.writeText('ab12')")
    pin.press('ControlOrMeta+V')
    playwright.expect(pin).to_have_value('01')
    page.evaluate("navigator.clipboard.writeText('23')")
    pin.press('ControlOrMeta+V')
    playwright.expect(pin).to_have_value('0123')
