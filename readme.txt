< 사용법 > 


1. https://kminito.tistory.com/78 
위 블로그의 설명에 따라 chromedriver.exe 파일을 다운받고 main.py 파일과 동일한 디렉토리에 넣어준다.
(chromedriver는 반드시 본인 브라우저에 맞는 버전을 사용해야합니다.)

2. main.py를 파이썬 IDE를 이용하여 실행시켜준다.

3. main.py 파일 내의 변수를 본인이 자동예매하고자 하는 설정으로 변경해준다.


< 변수 설명 >

member_number : 회원번호
password : 비밀번호
arrival : 출발지
departure : 도착지
standard_date : 기준날짜 ex) 20221101, 20230102 
standard_time : 기준시간 ex) 00 - 22 // 2의 배수로 입력해야됨
from_train_number : 화면에 보여지는 몇번째 기차부터 조회할지  min = 1, max = 10
to_train_number : 화면에 보여지는 몇번째 기차까지 조회할지 min = from_train_number, max = 10


