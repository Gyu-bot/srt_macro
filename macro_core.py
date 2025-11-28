# edit date : 2024-04-26
# version : 2.0.0-playwright

import os
import sys
import time
import webbrowser
from typing import Iterable, Optional, List, Dict, Any

import dotenv
import requests
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

dotenv.load_dotenv()

# Constants
RESERVATION_URL = "https://etk.srail.kr/hpg/hra/02/selectReservationList.do?pageId=TK0102010000"
LOGIN_URL = "https://etk.srail.co.kr/cmc/01/selectLoginForm.do"
SEARCH_URL = "https://etk.srail.kr/hpg/hra/01/selectScheduleList.do"

DEFAULT_TIMEOUT = 15000
SHORT_TIMEOUT = 5000

# 전역 변수: 로깅 큐 (api_server.py에서 전달됨)
_status_q: Optional[object] = None
_logs_q: Optional[object] = None


def log_error(message: str, error: Optional[Exception] = None, exit_on_error: bool = False) -> None:
    """에러 로그를 기록하고 필요시 종료합니다."""
    error_msg = f"[ERROR] {message}"
    
    if error:
        error_msg += f"\n예외 정보: {type(error).__name__}: {str(error)}"
    
    # 콘솔 출력
    print(error_msg, file=sys.stderr)
    
    # logs_q에 전달 (api_server.py에서 사용)
    if _logs_q is not None:
        try:
            _logs_q.put(error_msg)
        except Exception:
            pass
    
    # status_q에 에러 전달
    if _status_q is not None:
        try:
            _status_q.put({"status": "error", "message": message})
        except Exception:
            pass
    
    if exit_on_error:
        # Discord 웹훅으로 오류 알림 전송
        discord_msg = f"❌ SRT 매크로 오류 발생\n\n{message}"
        if error:
            discord_msg += f"\n\n오류 상세: {type(error).__name__}: {str(error)}"
        try:
            send_discord_notification(discord_msg)
        except Exception:
            pass
        
        # 상태를 finished로 변경하여 api_server.py가 종료 상태를 인식하도록 함
        if _status_q is not None:
            try:
                _status_q.put({"status": "finished"})
            except Exception:
                pass
        # 예외를 발생시켜서 api_server.py의 except 블록에서 처리되도록 함
        raise RuntimeError(message) from error if error else RuntimeError(message)


def log_info(message: str) -> None:
    """정보 로그를 기록합니다."""
    print(message)
    
    # logs_q에 전달
    if _logs_q is not None:
        try:
            _logs_q.put(message)
        except Exception:
            pass


def send_discord_notification(message: str) -> bool:
    webhook_url = os.getenv("DISCORD_WEB_HOOK")
    if not webhook_url:
        return False
    try:
        data = {"content": message}
        response = requests.post(webhook_url, json=data, timeout=5)
        return response.status_code == 204
    except Exception:
        return False


def wait_for_page_idle(page: Page, timeout: int = SHORT_TIMEOUT) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def get_cell_text(page: Page, selector: str, required: bool = False) -> str:
    """셀 텍스트를 가져옵니다. required=True일 경우 요소를 찾지 못하면 에러 발생."""
    locator = page.locator(selector)
    try:
        count = locator.count()
        if count == 0:
            if required:
                log_error(f"필수 요소를 찾을 수 없습니다: {selector}", exit_on_error=True)
            return ""
        text = locator.inner_text(timeout=1000)
        return (text or "").strip()
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        if required:
            log_error(f"요소를 읽는 중 오류 발생: {selector}", error=e, exit_on_error=True)
        return ""


def has_element(page: Page, selector: str, required: bool = False) -> bool:
    """요소 존재 여부를 확인합니다. required=True일 경우 요소를 찾지 못하면 에러 발생."""
    try:
        count = page.locator(selector).count()
        if required and count == 0:
            log_error(f"필수 요소를 찾을 수 없습니다: {selector}", exit_on_error=True)
        return count > 0
    except PlaywrightError as e:
        if required:
            log_error(f"요소 확인 중 오류 발생: {selector}", error=e, exit_on_error=True)
        return False


def iter_browser_commands() -> Iterable[str]:
    custom_command = os.getenv("BROWSER_OPEN_COMMAND")
    if custom_command:
        yield custom_command
    yield from (
        "google-chrome %s",
        "chromium-browser %s",
        "chromium %s",
        "xdg-open %s",
        "open %s",  # Mac
    )


def open_reservation_page(url: str) -> None:
    for command in iter_browser_commands():
        try:
            webbrowser.get(command).open(url)
            return
        except webbrowser.Error:
            continue
    webbrowser.open(url)


# Default configuration
DEFAULT_ARRIVAL = "동대구"
DEFAULT_DEPARTURE = "동탄"
DEFAULT_STANDARD_DATE = "20251024"
DEFAULT_STANDARD_TIME = "18"
DEFAULT_SEAT_TYPES = "both"
DEFAULT_FROM_TRAIN_NUMBER = 1
DEFAULT_TO_TRAIN_NUMBER = 3


def get_launch_options() -> dict:
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    launch_options: dict = {"headless": headless}

    browser_path = os.getenv("PLAYWRIGHT_BROWSER_PATH")
    if browser_path:
        launch_options["executable_path"] = browser_path
    else:
        browser_channel = os.getenv("PLAYWRIGHT_BROWSER_CHANNEL", "chrome")
        if browser_channel:
            launch_options["channel"] = browser_channel
    return launch_options


def launch_browser(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    launch_options = get_launch_options()
    try:
        browser = playwright.chromium.launch(**launch_options)
    except PlaywrightError:
        fallback_options = {"headless": launch_options.get("headless", False)}
        browser_path = os.getenv("PLAYWRIGHT_BROWSER_FALLBACK", "/usr/bin/google-chrome")
        if os.path.exists(browser_path):
            fallback_options["executable_path"] = browser_path
        else:
            fallback_options["channel"] = "chromium"
        browser = playwright.chromium.launch(**fallback_options)
    
    context = browser.new_context()
    context.set_default_timeout(DEFAULT_TIMEOUT)
    context.set_default_navigation_timeout(DEFAULT_TIMEOUT)
    
    # Prevent window.open
    context.add_init_script(
        """
        (() => {
            const noop = () => null;
            try {
                Object.defineProperty(window, 'open', { value: noop, configurable: false });
            } catch (e) {
                window.open = noop;
            }
        })();
        """
    )
    return browser, context


def main(
    arrival: Optional[str] = None,
    departure: Optional[str] = None,
    from_train_number: Optional[int] = None,
    to_train_number: Optional[int] = None,
    standard_date: Optional[str] = None,
    standard_time: Optional[str] = None,
    seat_types: Optional[str] = None,
    status_q: Optional[object] = None,
    logs_q: Optional[object] = None,
) -> None:
    """메인 함수. status_q와 logs_q는 api_server.py에서 전달됩니다."""
    global _status_q, _logs_q
    _status_q = status_q
    _logs_q = logs_q
    
    # Defaults
    arrival = arrival or DEFAULT_ARRIVAL
    departure = departure or DEFAULT_DEPARTURE
    from_train_number = from_train_number or DEFAULT_FROM_TRAIN_NUMBER
    to_train_number = to_train_number or DEFAULT_TO_TRAIN_NUMBER
    standard_date = standard_date or DEFAULT_STANDARD_DATE
    standard_time = standard_time or DEFAULT_STANDARD_TIME
    seat_types = seat_types or DEFAULT_SEAT_TYPES
    
    reserved = False

    log_info("--------------- Start SRT Macro ---------------")
    log_info(f"설정: {arrival} -> {departure}, {standard_date} {standard_time}시, 좌석: {seat_types}")
    log_info(f"열차 범위: {from_train_number} ~ {to_train_number}")
    
    # Load env vars
    member_number = os.getenv("MEMBER_NUMBER")
    password = os.getenv("PASSWORD")
    
    if not member_number or not password:
        log_error("환경변수 MEMBER_NUMBER 또는 PASSWORD가 설정되지 않았습니다.", exit_on_error=True)

    # Seat type mapping (Special: 6, Standard: 7)
    seat_preference = (seat_types or DEFAULT_SEAT_TYPES).strip().lower()
    if seat_preference == "standard":
        seat_type_list: list[int] = [7]
    elif seat_preference == "special":
        seat_type_list = [6]
    else:
        seat_type_list = [6, 7]

    refresh_count = 0

    try:
        with sync_playwright() as playwright:
            try:
                browser, context = launch_browser(playwright)
            except Exception as e:
                log_error("브라우저 실행 실패", error=e, exit_on_error=True)

            page = context.new_page()
            
            # Close extra pages
            context.on("page", lambda p: p.close() if p != page else None)

            # 1. Login
            try:
                log_info("로그인 페이지로 이동 중...")
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                wait_for_page_idle(page)
            except Exception as e:
                log_error("로그인 페이지 로드 실패", error=e, exit_on_error=True)

            has_element(page, "#srchDvNm01", required=True)
            has_element(page, "#hmpgPwdCphd01", required=True)
            
            try:
                log_info("로그인 정보 입력 중...")
                page.fill("#srchDvNm01", member_number)
                page.fill("#hmpgPwdCphd01", password)
                
                # Click login button (using class or more robust selector if possible, fallback to xpath)
                # The original xpath was brittle. Let's try to find by text or class if possible.
                # Usually login button is input[type=submit] or similar.
                # Based on original code: xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[1]/div[2]/div[2]/div/div[2]/input
                # Let's try a CSS selector for the submit button in the login form
                login_btn = page.locator("form fieldset .login_wrap input[type='submit'], form fieldset input[alt='확인'], form fieldset .btn_login")
                if login_btn.count() > 0:
                    login_btn.first.click()
                else:
                    # Fallback to the specific xpath if generic fails
                    page.locator("xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[1]/div[2]/div[2]/div/div[2]/input").click()
                
                wait_for_page_idle(page)
            except Exception as e:
                log_error("로그인 실패", error=e, exit_on_error=True)

            # 2. Search Schedule
            try:
                log_info("일정 조회 페이지로 이동 중...")
                page.goto(SEARCH_URL, wait_until="domcontentloaded")
                wait_for_page_idle(page)
            except Exception as e:
                log_error("일정 조회 페이지 로드 실패", error=e, exit_on_error=True)

            has_element(page, "#dptRsStnCdNm", required=True)
            has_element(page, "#arvRsStnCdNm", required=True)
            
            try:
                page.fill("#dptRsStnCdNm", arrival)
                page.fill("#arvRsStnCdNm", departure)
                page.select_option("#dptDt", value=standard_date)
                
                # Time selection
                try:
                    page.select_option("#dptTm", label=standard_time)
                except PlaywrightError:
                    page.select_option("#dptTm", value=standard_time)
            except Exception as e:
                log_error("일정 조회 조건 입력 실패", error=e, exit_on_error=True)

            # Click search button
            try:
                log_info("조회 버튼 클릭...")
                page.click("input[value='조회하기']")
                wait_for_page_idle(page, timeout=10000)
            except Exception as e:
                log_error("조회 버튼 클릭 실패", error=e, exit_on_error=True)

            # 3. Loop for reservation
            result_table_selector = "#result-form table tbody"
            log_info("결과 테이블 대기 중...")
            try:
                page.wait_for_selector(result_table_selector, timeout=15000)
            except PlaywrightTimeoutError:
                log_error(f"결과 테이블을 찾을 수 없습니다. URL: {page.url}", exit_on_error=True)

            while True:
                try:
                    for seat_type in seat_type_list:
                        # seat_type: 6 (Special), 7 (Standard)
                        # Column indices in the table:
                        # The table structure might change, but usually:
                        # ... | 특실 | 일반실 | 예약대기 | ...
                        # nth-child is 1-based.
                        
                        for row_index in range(from_train_number, to_train_number + 1):
                            # Using nth-child for row
                            row_selector = f"{result_table_selector} > tr:nth-child({row_index})"
                            
                            # Check if row exists
                            if page.locator(row_selector).count() == 0:
                                continue

                            # Cell selector
                            cell_selector = f"{row_selector} > td:nth-child({seat_type})"
                            cell_text = get_cell_text(page, cell_selector)
                            
                            if "예약하기" in cell_text:
                                log_info(f"[{row_index}번 열차] 예약 가능 확인! 시도 중...")
                                
                                # Click the link/button inside the cell
                                btn_selector = f"{cell_selector} a"
                                try:
                                    page.click(btn_selector, force=True)
                                    wait_for_page_idle(page, timeout=10000)
                                    
                                    # Check success
                                    if has_element(page, "#isFalseGotoMain") or "결제" in page.title():
                                        reserved = True
                                        log_info(">>> 예약 성공! <<<")
                                        send_discord_notification("SRT 예약 성공! 10분 내에 결제하세요.")
                                        open_reservation_page(RESERVATION_URL)
                                        break
                                    else:
                                        log_info("예약 실패 (잔여석 선점됨). 다시 검색...")
                                        page.go_back(wait_until="domcontentloaded")
                                        wait_for_page_idle(page)
                                except Exception as e:
                                    log_error(f"예약 클릭 중 오류 (row={row_index})", error=e)
                                    page.go_back()
                            
                            # Standby (Queue)
                            elif "신청하기" in get_cell_text(page, f"{row_selector} > td:nth-child(8)"):
                                # Standby column is usually 8
                                log_info(f"[{row_index}번 열차] 예약 대기 가능. 신청 시도...")
                                try:
                                    page.click(f"{row_selector} > td:nth-child(8) a", force=True)
                                    wait_for_page_idle(page, timeout=10000)
                                    
                                    if has_element(page, "#isFalseGotoMain"):
                                        reserved = True
                                        log_info(">>> 예약 대기 신청 성공! <<<")
                                        send_discord_notification("SRT 예약 대기 신청 성공!")
                                        open_reservation_page(RESERVATION_URL)
                                        break
                                    else:
                                        log_info("예약 대기 신청 실패. 다시 검색...")
                                        page.go_back()
                                        wait_for_page_idle(page)
                                except Exception as e:
                                    log_error(f"예약 대기 클릭 중 오류 (row={row_index})", error=e)
                                    page.go_back()
                        
                        if reserved: break
                    if reserved: break

                except Exception as e:
                    log_error("잔여석 조회 루프 중 오류", error=e)
                    if "Execution context was destroyed" in str(e):
                        send_discord_notification(f"⚠️ SRT 매크로 재시도 중 오류 발생\n\n{str(e)}")

                # Refresh logic
                if not reserved:
                    refresh_count += 1
                    log_info(f"새로고침 {refresh_count}회")
                    
                    try:
                        # Try to click the 'Refresh' button if available, or just reload
                        # Usually there is a submit button in the list page to refresh
                        submit_btn = page.locator("#submit, input[value='조회하기']")
                        if submit_btn.count() > 0:
                            submit_btn.first.click()
                        else:
                            page.reload()
                        
                        wait_for_page_idle(page)
                        # Wait a bit to avoid being blocked
                        time.sleep(0.5)
                    except Exception as e:
                        log_error("새로고침 실패, 페이지 재로딩", error=e)
                        page.reload()
                        wait_for_page_idle(page)
                else:
                    break

            context.close()
            browser.close()

    except KeyboardInterrupt:
        log_info("사용자에 의해 중단되었습니다.")
        if _status_q: _status_q.put({"status": "finished"})
        raise
    except Exception as e:
        log_error("치명적 오류 발생", error=e, exit_on_error=True)
    finally:
        log_info("--------------- SRT Macro 종료 ---------------")
