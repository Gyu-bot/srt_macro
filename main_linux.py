# edit date : 2024-04-26
# version : 1.9.0-playwright-linux

import os
import sys
import time
import webbrowser
from typing import Iterable, Optional

import dotenv
import requests
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

dotenv.load_dotenv()


RESERVATION_URL = "https://etk.srail.kr/hpg/hra/02/selectReservationList.do?pageId=TK0102010000"

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
            # Discord 전송 실패는 무시 (로그만 남김)
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
    data = {"content": message}
    response = requests.post(webhook_url, json=data)
    return response.status_code == 204


def wait_for_page_idle(page, timeout: int = 5000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def get_cell_text(page, selector: str, required: bool = False) -> str:
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


def has_element(page, selector: str, required: bool = False) -> bool:
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
    )


def open_reservation_page(url: str) -> None:
    for command in iter_browser_commands():
        try:
            webbrowser.get(command).open(url)
            return
        except webbrowser.Error:
            continue
    webbrowser.open(url)


############# 자동 예매 원하는 설정으로 변경 ##############

# 환경변수는 main() 함수 내에서 읽도록 변경 (모듈 레벨에서 읽지 않음)
DEFAULT_ARRIVAL = "동대구"
DEFAULT_DEPARTURE = "동탄"
DEFAULT_STANDARD_DATE = "20251024"  # 기준날짜 ex) 20221101
DEFAULT_STANDARD_TIME = "18"  # 기준 시간 ex) 00 - 22 // 2의 배수로 입력
DEFAULT_SEAT_TYPES = "both"  # 선택 가능: special, standard, both

"""
현재 페이지에 나타난 기차 몇번째 줄부터 몇번째 줄의 기차까지 조회할지 선택
"""
DEFAULT_FROM_TRAIN_NUMBER = 1
DEFAULT_TO_TRAIN_NUMBER = 3


def prompt_str(prompt: str, default: str) -> str:
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input or default


def prompt_int(prompt: str, default: int, *, min_value: int = 1, max_value: int = 10) -> int:
    while True:
        user_input = input(f"{prompt} [{default}]: ").strip()
        if not user_input:
            return default
        try:
            value = int(user_input)
        except ValueError:
            print("숫자를 입력하세요.")
            continue
        if not (min_value <= value <= max_value):
            print(f"{min_value}부터 {max_value} 사이의 숫자를 입력하세요.")
            continue
        return value

#################################################################


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
    context.set_default_timeout(15000)
    context.set_default_navigation_timeout(15000)
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
    
    # 파라미터가 None이면 기본값 사용 (하지만 api_server.py에서 항상 전달하므로 None이면 오류)
    if arrival is None:
        arrival = DEFAULT_ARRIVAL
    if departure is None:
        departure = DEFAULT_DEPARTURE
    if from_train_number is None:
        from_train_number = DEFAULT_FROM_TRAIN_NUMBER
    if to_train_number is None:
        to_train_number = DEFAULT_TO_TRAIN_NUMBER
    if standard_date is None:
        standard_date = DEFAULT_STANDARD_DATE
    if standard_time is None:
        standard_time = DEFAULT_STANDARD_TIME
    if seat_types is None:
        seat_types = DEFAULT_SEAT_TYPES
    
    reserved = False

    log_info("--------------- Start SRT Macro ---------------")
    
    # 전달받은 파라미터 로깅 (디버깅용)
    log_info(f"파라미터 확인 - arrival: {arrival}, departure: {departure}, standard_date: {standard_date}, standard_time: {standard_time}, seat_types: {seat_types}, from_train_number: {from_train_number}, to_train_number: {to_train_number}")
    
    # 환경변수 로드 (main() 함수 내에서 읽도록 변경)
    member_number = os.getenv("MEMBER_NUMBER")
    password = os.getenv("PASSWORD")
    
    # 환경변수 확인 및 디버깅
    log_info(f"환경변수 확인 - MEMBER_NUMBER: {'설정됨' if member_number else '없음'}, PASSWORD: {'설정됨' if password else '없음'}")
    if not member_number or not password:
        log_error("환경변수 MEMBER_NUMBER 또는 PASSWORD가 설정되지 않았습니다.", exit_on_error=True)

    seat_preference = (seat_types or DEFAULT_SEAT_TYPES).strip().lower()
    if seat_preference == "standard":
        seat_type_list: list[int] = [7]
    elif seat_preference == "special":
        seat_type_list = [6]
    else:
        if seat_preference not in {"both", "special", "standard"}:
            log_info("알 수 없는 좌석 종류입니다. 일반+특실로 진행합니다.")
        seat_type_list = [6, 7]

    refresh_count = 0

    try:
        with sync_playwright() as playwright:
            try:
                browser, context = launch_browser(playwright)
            except Exception as e:
                log_error("브라우저 실행 실패", error=e, exit_on_error=True)

            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(15000)

            def close_extra_pages(new_page):
                if new_page != page:
                    new_page.close()

            context.on("page", close_extra_pages)

            # 로그인 페이지 이동
            try:
                log_info("로그인 페이지로 이동 중...")
                page.goto("https://etk.srail.co.kr/cmc/01/selectLoginForm.do", wait_until="domcontentloaded")
                wait_for_page_idle(page, timeout=15000)
            except Exception as e:
                log_error("로그인 페이지 로드 실패", error=e, exit_on_error=True)

            # 로그인 필수 요소 확인
            has_element(page, "#srchDvNm01", required=True)
            has_element(page, "#hmpgPwdCphd01", required=True)
            
            try:
                log_info("로그인 정보 입력 중...")
                page.fill("#srchDvNm01", member_number or "")
                page.fill("#hmpgPwdCphd01", password or "")
            except Exception as e:
                log_error("로그인 정보 입력 실패", error=e, exit_on_error=True)

            # 로그인 버튼 클릭
            login_button_selector = "xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[1]/div[2]/div[2]/div/div[2]/input"
            try:
                has_element(page, login_button_selector, required=True)
                page.locator(login_button_selector).click()
                wait_for_page_idle(page, timeout=5000)
            except Exception as e:
                log_error("로그인 버튼 클릭 실패", error=e, exit_on_error=True)

            # 일정 조회 페이지 이동
            try:
                log_info("일정 조회 페이지로 이동 중...")
                page.goto("https://etk.srail.kr/hpg/hra/01/selectScheduleList.do", wait_until="domcontentloaded")
                wait_for_page_idle(page, timeout=5000)
            except Exception as e:
                log_error("일정 조회 페이지 로드 실패", error=e, exit_on_error=True)

            # 필수 요소 확인
            has_element(page, "#dptRsStnCdNm", required=True)
            has_element(page, "#arvRsStnCdNm", required=True)
            has_element(page, "#dptDt", required=True)
            has_element(page, "#dptTm", required=True)
            
            try:
                dep_stn = page.locator("#dptRsStnCdNm")
                dep_stn.fill("")
                dep_stn.fill(arrival)

                arr_stn = page.locator("#arvRsStnCdNm")
                arr_stn.fill("")
                arr_stn.fill(departure)

                page.select_option("#dptDt", value=standard_date)
                try:
                    page.select_option("#dptTm", label=standard_time)
                except PlaywrightError:
                    page.select_option("#dptTm", value=standard_time)
            except Exception as e:
                log_error("일정 조회 조건 입력 실패", error=e, exit_on_error=True)

            # 조회 버튼 클릭
            query_button_selector = "css=input[value='조회하기']"
            try:
                has_element(page, query_button_selector, required=True)
                log_info("조회 버튼 클릭 중...")
                page.locator(query_button_selector).click()
                # 조회 결과가 나타날 때까지 대기 (더 긴 타임아웃)
                wait_for_page_idle(page, timeout=10000)
            except Exception as e:
                log_error("조회 버튼 클릭 실패", error=e, exit_on_error=True)

            # 결과 테이블 존재 확인 (명시적으로 대기)
            result_table_selector = "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody"
            log_info("결과 테이블 대기 중...")
            try:
                # 요소가 나타날 때까지 명시적으로 대기
                page.wait_for_selector(result_table_selector, timeout=15000)
                log_info("결과 테이블 발견됨")
            except PlaywrightTimeoutError:
                # 페이지 상태 확인을 위한 디버깅 정보
                current_url = page.url
                page_title = page.title()
                log_error(
                    f"결과 테이블을 찾을 수 없습니다. URL: {current_url}, Title: {page_title}",
                    exit_on_error=True
                )
            has_element(page, result_table_selector, required=True)

            while True:
                try:
                    for seat_type in seat_type_list:
                        for row_index in range(from_train_number, to_train_number + 1):
                            standard_selector = (
                                "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > "
                                f"tr:nth-child({row_index}) > td:nth-child(7)"
                            )
                            try:
                                standard_seat = get_cell_text(page, standard_selector)
                                page_time = time.time()

                                if "예약하기" in standard_seat:
                                    log_info(f"page_time: {page_time}")
                                    log_info("예약 가능 클릭")
                                    try:
                                        reserve_button_xpath = (
                                            f"/html/body/div[1]/div[4]/div/div[3]/div[1]/form/fieldset/"
                                            f"div[6]/table/tbody/tr[{row_index}]/td[{seat_type}]/a/span"
                                        )
                                        has_element(page, reserve_button_xpath, required=True)
                                        page.locator(reserve_button_xpath).click(force=True)
                                        click_time = time.time()
                                        wait_for_page_idle(page, timeout=10000)
                                        page_time = time.time()
                                        log_info(f"click_time: {click_time}")
                                        log_info(f"page_time: {page_time}")
                                        log_info(f"page_time - click_time: {page_time - click_time}")

                                        if has_element(page, "#isFalseGotoMain"):
                                            reserved = True
                                            log_info("예약 성공")
                                            send_discord_notification("예약을 성공했습니다. 10분내에 결제해주세요")
                                            open_reservation_page(RESERVATION_URL)
                                            break
                                        else:
                                            log_info("잔여석 없음. 다시 검색")
                                            page.go_back(wait_until="load")
                                            wait_for_page_idle(page, timeout=3000)
                                    except Exception as e:
                                        log_error(f"예약 버튼 클릭 실패 (row={row_index}, seat_type={seat_type})", error=e)
                                        try:
                                            page.go_back(wait_until="load")
                                            wait_for_page_idle(page, timeout=3000)
                                        except Exception:
                                            pass

                                else:
                                    try:
                                        standby_selector = (
                                            "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > "
                                            f"tr:nth-child({row_index}) > td:nth-child(8)"
                                        )
                                        standby_seat = get_cell_text(page, standby_selector)

                                        if "신청하기" in standby_seat:
                                            log_info("예약 대기 신청")
                                            try:
                                                standby_button_xpath = (
                                                    f"/html/body/div[1]/div[4]/div/div[3]/div[1]/form/fieldset/div[6]/"
                                                    f"table/tbody/tr[{row_index}]/td[8]/a/span"
                                                )
                                                has_element(page, standby_button_xpath, required=True)
                                                page.locator(standby_button_xpath).click(force=True)
                                                wait_for_page_idle(page, timeout=10000)

                                                if has_element(page, "#isFalseGotoMain"):
                                                    reserved = True
                                                    log_info("예약대기 성공")
                                                    send_discord_notification("예약대기 성공했습니다.")
                                                    open_reservation_page(RESERVATION_URL)
                                                    break
                                                else:
                                                    log_info("예약 대기 신청 실패. 다시 검색")
                                                    page.go_back(wait_until="load")
                                                    wait_for_page_idle(page, timeout=5000)
                                            except Exception as e:
                                                log_error(f"예약 대기 버튼 클릭 실패 (row={row_index})", error=e)
                                                try:
                                                    page.go_back(wait_until="load")
                                                    wait_for_page_idle(page, timeout=5000)
                                                except Exception:
                                                    pass

                                    except Exception as e:
                                        log_error(f"예약 대기 조회 실패 (row={row_index})", error=e)
                            except Exception as e:
                                log_error(f"좌석 정보 조회 실패 (row={row_index})", error=e)

                        if reserved:
                            break
                    if reserved:
                        break

                except Exception as e:
                    log_error("잔여석 조회 중 예외 발생", error=e)

                if not reserved:
                    try:
                        submit_selector = "xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[2]/input"
                        if has_element(page, submit_selector):
                            submit = page.locator(submit_selector)
                            submit.evaluate("el => el.click()")
                            refresh_count += 1
                            log_info(f"{refresh_count}번째 새로고침")
                            wait_for_page_idle(page, timeout=3000)
                        else:
                            log_info("새로고침 버튼을 찾을 수 없음. 페이지 초기화")
                            page.go_back(wait_until="load")
                            wait_for_page_idle(page, timeout=5000)
                            page.reload(wait_until="load")
                            wait_for_page_idle(page, timeout=5000)
                    except Exception as e:
                        log_error("새로고침 실패. 페이지 초기화 시도", error=e)
                        try:
                            page.go_back(wait_until="load")
                            wait_for_page_idle(page, timeout=5000)
                            page.reload(wait_until="load")
                            wait_for_page_idle(page, timeout=5000)
                        except Exception as reload_error:
                            log_error("페이지 초기화 실패", error=reload_error, exit_on_error=True)
                else:
                    time.sleep(1000)
                    break

            try:
                context.close()
                browser.close()
            except Exception as e:
                log_error("브라우저 종료 중 오류 발생", error=e)
                
    except KeyboardInterrupt:
        log_info("사용자에 의해 중단되었습니다.")
        if _status_q is not None:
            try:
                _status_q.put({"status": "finished"})
            except Exception:
                pass
        raise
    except RuntimeError:
        # log_error에서 exit_on_error=True로 발생시킨 예외는 그대로 전파
        raise
    except Exception as e:
        # 예기치 않은 오류 처리
        error_msg = f"[ERROR] 예기치 않은 오류로 프로그램 종료\n예외 정보: {type(e).__name__}: {str(e)}"
        print(error_msg, file=sys.stderr)
        
        # Discord 웹훅으로 오류 알림 전송
        discord_msg = f"❌ SRT 매크로 예기치 않은 오류 발생\n\n오류: {type(e).__name__}: {str(e)}"
        try:
            send_discord_notification(discord_msg)
        except Exception:
            # Discord 전송 실패는 무시 (로그만 남김)
            pass
        
        if _logs_q is not None:
            try:
                _logs_q.put(error_msg)
            except Exception:
                pass
        
        if _status_q is not None:
            try:
                _status_q.put({"status": "error", "message": str(e)})
                _status_q.put({"status": "finished"})
            except Exception:
                pass
        
        raise
    finally:
        log_info("--------------- SRT Macro 종료 ---------------")


if __name__ == "__main__":
    arrival = prompt_str("출발지를 입력하세요", DEFAULT_ARRIVAL)
    departure = prompt_str("도착지를 입력하세요", DEFAULT_DEPARTURE)
    standard_date = prompt_str("기준 날짜(YYYYMMDD)를 입력하세요", DEFAULT_STANDARD_DATE)
    standard_time = prompt_str("기준 시간(00, 02, ..., 22)을 입력하세요", DEFAULT_STANDARD_TIME)
    seat_types = prompt_str(
        "좌석 종류를 입력하세요 (special / standard / both)", DEFAULT_SEAT_TYPES
    )
    from_train_number = prompt_int(
        "조회 시작 열차 순번", DEFAULT_FROM_TRAIN_NUMBER, min_value=1, max_value=10
    )
    to_default = max(DEFAULT_TO_TRAIN_NUMBER, from_train_number)
    to_train_number = prompt_int(
        "조회 종료 열차 순번",
        to_default,
        min_value=from_train_number,
        max_value=10,
    )
    main(
        arrival,
        departure,
        from_train_number,
        to_train_number,
        standard_date,
        standard_time,
        seat_types,
    )
