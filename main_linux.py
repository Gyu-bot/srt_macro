# edit date : 2024-04-26
# version : 1.9.0-playwright-linux

import os
import time
import webbrowser
from typing import Iterable

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


def get_cell_text(page, selector: str) -> str:
    locator = page.locator(selector)
    try:
        if locator.count() == 0:
            return ""
        text = locator.inner_text(timeout=1000)
        return (text or "").strip()
    except (PlaywrightTimeoutError, PlaywrightError):
        return ""


def has_element(page, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except PlaywrightError:
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

member_number = os.getenv("MEMBER_NUMBER")  # 회원번호
password = os.getenv("PASSWORD")  # 비밀번호
DEFAULT_ARRIVAL = "수서"
DEFAULT_DEPARTURE = "부산역"
DEFAULT_STANDARD_DATE = "20250926"  # 기준날짜 ex) 20221101
DEFAULT_STANDARD_TIME = "18"  # 기준 시간 ex) 00 - 22 // 2의 배수로 입력
DEFAULT_SEAT_TYPES = "both"  # 선택 가능: special, standard, both

"""
현재 페이지에 나타난 기차 몇번째 줄부터 몇번째 줄의 기차까지 조회할지 선택
"""
DEFAULT_FROM_TRAIN_NUMBER = 3
DEFAULT_TO_TRAIN_NUMBER = 4


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
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "false").lower() == "true"
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
    arrival: str = DEFAULT_ARRIVAL,
    departure: str = DEFAULT_DEPARTURE,
    from_train_number: int = DEFAULT_FROM_TRAIN_NUMBER,
    to_train_number: int = DEFAULT_TO_TRAIN_NUMBER,
    standard_date: str = DEFAULT_STANDARD_DATE,
    standard_time: str = DEFAULT_STANDARD_TIME,
    seat_types: str = DEFAULT_SEAT_TYPES,
) -> None:
    reserved = False

    print("--------------- Start SRT Macro ---------------")

    seat_preference = (seat_types or DEFAULT_SEAT_TYPES).strip().lower()
    if seat_preference == "standard":
        seat_type_list: list[int] = [7]
    elif seat_preference == "special":
        seat_type_list = [6]
    else:
        if seat_preference not in {"both", "special", "standard"}:
            print("알 수 없는 좌석 종류입니다. 일반+특실로 진행합니다.")
        seat_type_list = [6, 7]

    refresh_count = 0

    with sync_playwright() as playwright:
        browser, context = launch_browser(playwright)

        page = context.new_page()
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(15000)

        def close_extra_pages(new_page):
            if new_page != page:
                new_page.close()

        context.on("page", close_extra_pages)

        page.goto("https://etk.srail.co.kr/cmc/01/selectLoginForm.do")
        wait_for_page_idle(page, timeout=15000)

        page.fill("#srchDvNm01", member_number or "")
        page.fill("#hmpgPwdCphd01", password or "")

        page.locator(
            "xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[1]/div[2]/div[2]/div/div[2]/input"
        ).click()
        wait_for_page_idle(page, timeout=5000)

        page.goto("https://etk.srail.kr/hpg/hra/01/selectScheduleList.do")
        wait_for_page_idle(page, timeout=5000)

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

        page.locator("css=input[value='조회하기']").click()
        wait_for_page_idle(page, timeout=5000)

        train_rows = page.locator(
            "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > tr"
        ).element_handles()
        print(train_rows)

        while True:
            try:
                for seat_type in seat_type_list:
                    for row_index in range(from_train_number, to_train_number + 1):
                        standard_selector = (
                            "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > "
                            f"tr:nth-child({row_index}) > td:nth-child(7)"
                        )
                        standard_seat = get_cell_text(page, standard_selector)
                        page_time = time.time()

                        if "예약하기" in standard_seat:
                            print(f"page_time: {page_time}")
                            print("예약 가능 클릭")
                            page.locator(
                                "xpath=/html/body/div[1]/div[4]/div/div[3]/div[1]/form/fieldset/"
                                f"div[6]/table/tbody/tr[{row_index}]/td[{seat_type}]/a/span"
                            ).click(force=True)
                            click_time = time.time()
                            wait_for_page_idle(page, timeout=10000)
                            page_time = time.time()
                            print(f"click_time: {click_time}")
                            print(f"page_time: {page_time}")
                            print(f"page_time - click_time: {page_time - click_time}")

                            if has_element(page, "#isFalseGotoMain"):
                                reserved = True
                                print("예약 성공")
                                send_discord_notification("예약을 성공했습니다. 10분내에 결제해주세요")
                                open_reservation_page(RESERVATION_URL)
                                break
                            else:
                                print("잔여석 없음. 다시 검색")
                                page.go_back(wait_until="load")
                                wait_for_page_idle(page, timeout=3000)

                        else:
                            try:
                                standby_selector = (
                                    "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > "
                                    f"tr:nth-child({row_index}) > td:nth-child(8)"
                                )
                                standby_seat = get_cell_text(page, standby_selector)

                                if "신청하기" in standby_seat:
                                    print("예약 대기 신청")
                                    page.locator(
                                        "xpath=/html/body/div[1]/div[4]/div/div[3]/div[1]/form/fieldset/div[6]/"
                                        f"table/tbody/tr[{row_index}]/td[8]/a/span"
                                    ).click(force=True)
                                    wait_for_page_idle(page, timeout=10000)

                                    if has_element(page, "#isFalseGotoMain"):
                                        reserved = True
                                        print("예약대기 성공")
                                        send_discord_notification("예약대기 성공했습니다.")
                                        open_reservation_page(RESERVATION_URL)
                                        break
                                    else:
                                        print("예약 대기 신청 실패. 다시 검색")
                                        page.go_back(wait_until="load")
                                        wait_for_page_idle(page, timeout=5000)

                            except Exception:
                                print("예약 대기 신청 불가")
                                pass

                    if reserved:
                        break
                if reserved:
                    break

            except Exception:
                print("잔여석 조회 불가")
                pass

            if not reserved:
                try:
                    submit = page.locator(
                        "xpath=/html/body/div/div[4]/div/div[2]/form/fieldset/div[2]/input"
                    )
                    submit.evaluate("el => el.click()")
                    refresh_count += 1
                    print(f"{refresh_count}번째 새로고침")
                except Exception:
                    print("잔여석 없음 #2. 초기화")
                    page.go_back(wait_until="load")
                    wait_for_page_idle(page, timeout=5000)
                    page.reload(wait_until="load")
                    wait_for_page_idle(page, timeout=5000)

                page.wait_for_timeout(2000)
                time.sleep(2)

            else:
                time.sleep(1000)
                break

        context.close()
        browser.close()


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
