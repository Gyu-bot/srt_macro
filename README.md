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


## 환경변수

```
`.env`파일 생성 후 아래 변수 추가<br/>
```
MEMBER_NUMBER="123456789" 
PASSWORD="password" 
DISCORD_WEB_HOOK="https://discordapp.com/api/webhooks/~~"
```
