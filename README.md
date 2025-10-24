## 사용법

`main.py` 내에서 아래 변수 변경<br/>

```
member_number = "0000000000" # 회원번호
password= "1234" # 비밀번호
arrival = "수서" # 출발지
departure = "동대구" # 도착지
standard_date = "20230217" # 기준날짜 ex) 20221101
standard_time = "16" # 기준 시간 ex) 00 - 22 // 2의 배수로 입력
from_train_number = 1 # 몇번째 기차부터 조회할지  min = 1, max = 10
to_train_number = 10 # 몇번째 기차까지 조회할지 min = from_train_number, max = 10

`send_discord_notification` 부분은 Discord 알림 미사용시 주석처리
```

## 환경변수

`.env`파일 생성 후 아래 변수 추가<br/>
```
MEMBER_NUMBER="123456789" 
PASSWORD="password" 
DISCORD_WEB_HOOK="https://discordapp.com/api/webhooks/~~"
```

## 웹 UI로 실행/정지 및 값 입력

브라우저에서 값을 입력하고 매크로 실행/정지 상태를 확인할 수 있는 간단한 웹 페이지를 제공합니다.

1) 사전 준비
- `.env` 파일에 `MEMBER_NUMBER`, `PASSWORD`가 설정되어 있어야 합니다.
- 의존성 설치 및 Playwright 브라우저 설치:
  - `pip install -e .` (또는 `pip install fastapi uvicorn`)
  - `playwright install`

2) 서버 실행
- `python api_server.py`

3) 사용 방법
- 브라우저에서 `http://localhost:8000` 접속
- 폼에 출발지/도착지/날짜/시간/좌석 종류/조회 범위를 입력 후 `시작`
- 상태가 "실행 중"으로 표시되며 `정지` 버튼으로 중단할 수 있습니다.
