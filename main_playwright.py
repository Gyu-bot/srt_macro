# edit date : 2024-04-26
# version : 1.9.0-playwright

import os
import time
import webbrowser

import dotenv
import requests
# from playwright import __version__ as playwright_version
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

dotenv.load_dotenv()


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


chrome_path = 'open -a "Google Chrome" %s'

############# 자동 예매 원하는 설정으로 변경 ##############

member_number = os.getenv("MEMBER_NUMBER")  # 회원번호
password = os.getenv("PASSWORD")  # 비밀번호
arrival = "동대구"  # 출발지
departure = "동탄"  # 도착지
standard_date = "20251024"  # 기준날짜 ex) 20221101
standard_time = "18"  # 기준 시간 ex) 00 - 22 // 2의 배수로 입력
seat_types = "standard"
seat_type_list = []

"""
현재 페이지에 나타난 기차 몇번째 줄부터 몇번째 줄의 기차까지 조회할지 선택
"""
from_train_number = 1 # 몇번째 기차부터 조회할지  min = 1, max = 10
to_train_number = 3  # 몇번째 기차까지 조회할지 min = from_train_number, max = 10

#################################################################


def main() -> None:
    reserved = False

    print("--------------- Start SRT Macro ---------------")
    # print("playwright version : ", playwright_version)

    if seat_types == "standard":
        seat_type_list[:] = [7]
    elif seat_types == "special":
        seat_type_list[:] = [6]
    elif seat_types == "both":
        seat_type_list[:] = [6, 7]

    refresh_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
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
        wait_for_page_idle(page, timeout=1000)

        train_rows = page.locator(
            "#result-form > fieldset > div.tbl_wrap.th_thead > table > tbody > tr"
        ).element_handles()
        print(train_rows)

        while True:
            try:
                for seat_type in [6, 7]:
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
                                webbrowser.get(chrome_path).open(
                                    "https://etk.srail.kr/hpg/hra/02/selectReservationList.do?pageId=TK0102010000"
                                )
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
                                        webbrowser.get(chrome_path).open(
                                            "https://etk.srail.kr/hpg/hra/02/selectReservationList.do?pageId=TK0102010000"
                                        )
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

                time.sleep(2)

            else:
                time.sleep(1000)
                break

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
