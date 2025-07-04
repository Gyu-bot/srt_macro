FROM selenium/standalone-chrome:latest

USER root

# Python3 설치 (기본 이미지에 없음)
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv curl unzip

# uv 설치 (pip로 부트스트랩)
RUN pip3 install uv

# 작업 디렉토리 복사
COPY . /app
WORKDIR /app

# uv를 사용해 pyproject.toml 기반 의존성 설치
RUN uv pip install --system

# 실행
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
