import asyncio
import base64
import json
import multiprocessing as mp
import os
import pathlib
import threading
import time
from collections import deque
from typing import List, Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response

import main_linux


app = FastAPI(title="SRT Macro Controller")

# 환경변수 암호화 관련
ENV_FILE = pathlib.Path(".env.encrypted")
KEY_FILE = pathlib.Path(".env.key")


def get_encryption_key() -> bytes:
    """암호화 키를 가져오거나 생성합니다."""
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    # 새 키 생성 (기기 고유 정보 기반)
    import platform
    
    machine_id = f"{platform.node()}{os.getcwd()}"
    # PBKDF2를 사용하여 키 생성
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"srt_macro_salt",
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(machine_id.encode()))
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)  # 소유자만 읽기/쓰기
    return key


def encrypt_env_vars(env_vars: dict[str, str]) -> bool:
    """환경변수를 암호화하여 저장합니다."""
    try:
        key = get_encryption_key()
        fernet = Fernet(key)
        
        env_json = json.dumps(env_vars, ensure_ascii=False)
        encrypted = fernet.encrypt(env_json.encode())
        
        ENV_FILE.write_bytes(encrypted)
        ENV_FILE.chmod(0o600)  # 소유자만 읽기/쓰기
        return True
    except Exception as e:
        print(f"[env] 암호화 저장 실패: {e}")
        return False


def decrypt_env_vars() -> Optional[dict[str, str]]:
    """암호화된 환경변수를 복호화하여 반환합니다."""
    if not ENV_FILE.exists():
        return None
    try:
        key = get_encryption_key()
        fernet = Fernet(key)
        
        encrypted = ENV_FILE.read_bytes()
        decrypted = fernet.decrypt(encrypted)
        env_vars = json.loads(decrypted.decode())
        return env_vars
    except Exception as e:
        print(f"[env] 복호화 실패: {e}")
        return None


def load_env_vars() -> dict[str, str]:
    """환경변수를 로드합니다 (암호화된 파일 또는 시스템 환경변수)."""
    env_vars = {}
    
    # 암호화된 파일에서 로드 시도
    encrypted_vars = decrypt_env_vars()
    if encrypted_vars:
        env_vars.update(encrypted_vars)
    
    # 시스템 환경변수로 덮어쓰기 (우선순위 높음)
    for key in ["MEMBER_NUMBER", "PASSWORD", "DISCORD_WEB_HOOK"]:
        sys_val = os.getenv(key)
        if sys_val:
            env_vars[key] = sys_val
    
    return env_vars


def check_env_vars() -> dict[str, bool]:
    """필수 환경변수가 설정되어 있는지 확인합니다."""
    env_vars = load_env_vars()
    return {
        "MEMBER_NUMBER": bool(env_vars.get("MEMBER_NUMBER")),
        "PASSWORD": bool(env_vars.get("PASSWORD")),
        "DISCORD_WEB_HOOK": bool(env_vars.get("DISCORD_WEB_HOOK")),
    }


def apply_env_vars_to_os() -> None:
    """로드한 환경변수를 os.environ에 적용합니다."""
    env_vars = load_env_vars()
    for key, value in env_vars.items():
        if value:
            os.environ[key] = value  # 기존 값이 있어도 덮어쓰기


# Simple process manager to run/stop the macro
class MacroState:
    def __init__(self) -> None:
        self.proc: Optional[mp.Process] = None
        self.started_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._status_q: Optional[mp.Queue] = None
        self._logs_q: Optional[mp.Queue] = None
        self._log_thread: Optional[threading.Thread] = None
        self._log_buffer: deque[str] = deque(maxlen=500)
        self._listeners: List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        # 현재 실행 중인 파라미터 저장
        self.current_params: Optional[dict] = None

    @property
    def running(self) -> bool:
        if self.proc is None:
            return False
        # 프로세스가 종료되었는지 확인
        if not self.proc.is_alive():
            # 프로세스가 종료되었으면 상태 정리
            # 상태 큐에서 남은 메시지 확인 (에러 메시지 등)
            if self._status_q is not None:
                try:
                    while True:
                        msg = self._status_q.get_nowait()
                        if isinstance(msg, dict):
                            status = msg.get("status")
                            if status == "error":
                                error_msg = msg.get("message") or "실행 중 오류 발생"
                                self.last_error = self._clean_error_message(error_msg)
                except Exception:
                    pass
            # 상태 정리
            self.proc = None
            self.started_at = None
            self._status_q = None
            self._logs_q = None
            self.current_params = None
            return False
        return True

    def start(self, **kwargs) -> bool:
        if self.running:
            return False
        # Reset previous error
        self.last_error = None
        # 현재 실행 중인 파라미터 저장 (UI 표시용)
        self.current_params = {
            "arrival": kwargs.get("arrival"),
            "departure": kwargs.get("departure"),
            "from_train_number": kwargs.get("from_train_number"),
            "to_train_number": kwargs.get("to_train_number"),
            "standard_date": kwargs.get("standard_date"),
            "standard_time": kwargs.get("standard_time"),
            "seat_types": kwargs.get("seat_types"),
        }
        # Queues for status and logs
        status_q: mp.Queue = mp.Queue()
        logs_q: mp.Queue = mp.Queue()
        kwargs = dict(kwargs)
        kwargs["status_q"] = status_q
        kwargs["logs_q"] = logs_q
        # Do not run as daemon (Playwright spawns children)
        self.proc = mp.Process(target=run_macro, kwargs=kwargs)
        self.proc.start()
        self.started_at = time.time()
        self._status_q = status_q
        self._logs_q = logs_q
        # Start log pump thread
        self._start_log_pump()

        # Wait briefly for immediate startup errors
        try:
            msg = status_q.get(timeout=8)
        except Exception:
            # No immediate message; if process already died, treat as error
            if not self.running:
                exitcode = self.proc.exitcode if self.proc else None
                self.last_error = f"프로세스가 즉시 종료되었습니다. exitcode={exitcode}"
                self.proc = None
                self.started_at = None
                self._status_q = None
                self._logs_q = None
                return False
            return True

        # Handle message
        if isinstance(msg, dict):
            status = msg.get("status")
            if status == "error":
                error_msg = msg.get("message") or "시작 중 알 수 없는 오류"
                # traceback 제거
                self.last_error = self._clean_error_message(error_msg)
                # Ensure termination
                if self.proc and self.proc.is_alive():
                    self.proc.terminate()
                    try:
                        self.proc.join(timeout=3)
                    except Exception:
                        pass
                self.proc = None
                self.started_at = None
                self._status_q = None
                self._logs_q = None
                self.current_params = None
                return False
            if status == "finished":
                # Macro finished immediately; not considered running
                self.last_error = "작업이 즉시 종료되었습니다. 조건을 확인하세요."
                self.proc = None
                self.started_at = None
                self._status_q = None
                self._logs_q = None
                self.current_params = None
                return False
        return True

    def stop(self) -> bool:
        if not self.proc:
            return False
        if self.proc.is_alive():
            self.proc.terminate()
            try:
                self.proc.join(timeout=5)
            except Exception:
                pass
        self.proc = None
        self.started_at = None
        self._status_q = None
        self._logs_q = None
        self.current_params = None
        # Allow log thread to end naturally
        return True

    def refresh(self) -> None:
        """Drain status queue to capture late errors/finish events."""
        # 프로세스가 종료되었는지 먼저 확인
        if self.proc is not None and not self.proc.is_alive():
            # 프로세스가 종료되었지만 상태가 정리되지 않은 경우
            if self._status_q is not None or self._logs_q is not None:
                # 상태 큐에서 남은 메시지 확인
                q = self._status_q
                if q is not None:
                    try:
                        while True:
                            msg = q.get_nowait()
                            if isinstance(msg, dict):
                                status = msg.get("status")
                                if status == "error":
                                    error_msg = msg.get("message") or "실행 중 오류 발생"
                                    self.last_error = self._clean_error_message(error_msg)
                                elif status == "finished":
                                    # 이미 종료된 상태
                                    pass
                    except Exception:
                        pass
                # 상태 정리
                self.proc = None
                self.started_at = None
                self._status_q = None
                self._logs_q = None
                self.current_params = None
                return
        
        q = self._status_q
        if q is None:
            return
        try:
            while True:
                msg = q.get_nowait()
                if not isinstance(msg, dict):
                    continue
                status = msg.get("status")
                if status == "error":
                    error_msg = msg.get("message") or "실행 중 오류 발생"
                    # traceback 제거 (사용자 친화적인 메시지만 표시)
                    self.last_error = self._clean_error_message(error_msg)
                    # Ensure stopped state
                    if self.proc and self.proc.is_alive():
                        self.proc.terminate()
                        try:
                            self.proc.join(timeout=3)
                        except Exception:
                            pass
                    self.proc = None
                    self.started_at = None
                    self._status_q = None
                    self._logs_q = None
                    self.current_params = None
                elif status == "finished":
                    # Treat as completed run - 프로세스 종료 처리
                    if self.proc and self.proc.is_alive():
                        # 프로세스가 아직 살아있으면 종료
                        self.proc.terminate()
                        try:
                            self.proc.join(timeout=3)
                        except Exception:
                            pass
                    self.proc = None
                    self.started_at = None
                    self._status_q = None
                    self._logs_q = None
                    self.current_params = None
        except Exception:
            # Empty queue or other non-critical error
            pass
    
    def _clean_error_message(self, error_msg: str) -> str:
        """에러 메시지에서 traceback을 제거하고 사용자 친화적인 메시지만 반환합니다."""
        # traceback이 포함된 경우 첫 번째 줄만 반환
        lines = error_msg.split('\n')
        # "Traceback"으로 시작하는 줄을 찾아서 그 이전까지만 반환
        cleaned_lines = []
        for line in lines:
            if line.strip().startswith('Traceback'):
                break
            if line.strip().startswith('File "'):
                break
            cleaned_lines.append(line)
        
        # 마지막 줄이 비어있으면 제거
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()
        
        result = '\n'.join(cleaned_lines).strip()
        return result if result else "실행 중 오류가 발생했습니다."

    # ----- Logging (SSE broadcast) -----
    def _start_log_pump(self) -> None:
        if self._log_thread and self._log_thread.is_alive():
            return

        def _worker():
            q = self._logs_q
            while True:
                if q is None:
                    break
                try:
                    line = q.get(timeout=0.5)
                except Exception:
                    # If process ended and queue is likely drained, stop
                    if not self.running:
                        break
                    continue
                if line is None:
                    # sentinel
                    break
                try:
                    s = str(line)
                except Exception:
                    s = repr(line)
                self._append_log(s)
            # finalize
            self._logs_q = None

        self._log_thread = threading.Thread(target=_worker, daemon=True)
        self._log_thread.start()

    def _append_log(self, line: str) -> None:
        self._log_buffer.append(line)
        # Broadcast to listeners (best-effort)
        def _safe_put(q: asyncio.Queue, item: str):
            try:
                q.put_nowait(item)
            except Exception:
                pass
        for loop, q in list(self._listeners):
            try:
                loop.call_soon_threadsafe(_safe_put, q, line)
            except Exception:
                # ignore bad listeners
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self._listeners.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners = [(lp, qq) for (lp, qq) in self._listeners if qq is not q]
        # drain
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass


STATE = MacroState()


def run_macro(**kwargs) -> None:
    # Child process entrypoint: run the Playwright macro
    # 환경변수 적용
    apply_env_vars_to_os()
    
    # 파라미터를 먼저 추출 (pop 전에 추출하여 안전하게 처리)
    arrival = kwargs.pop("arrival", None)
    departure = kwargs.pop("departure", None)
    from_train_number = kwargs.pop("from_train_number", None)
    to_train_number = kwargs.pop("to_train_number", None)
    standard_date = kwargs.pop("standard_date", None)
    standard_time = kwargs.pop("standard_time", None)
    seat_types = kwargs.pop("seat_types", None)
    status_q: Optional[mp.Queue] = kwargs.pop("status_q", None)
    logs_q: Optional[mp.Queue] = kwargs.pop("logs_q", None)
    
    # 예상치 못한 키가 있는지 확인 (디버깅용)
    if kwargs:
        import sys
        print(f"[WARNING] 예상치 못한 파라미터: {list(kwargs.keys())}", file=sys.stderr)
    
    # Redirect prints to logs queue
    import sys

    class _StreamToQueue:
        def __init__(self, q):
            self.q = q
            self._buf = ""

        def write(self, s):
            if self.q is None:
                return
            self._buf += str(s)
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line:
                    try:
                        self.q.put(line)
                    except Exception:
                        pass

        def flush(self):
            if self.q is None:
                return
            if self._buf:
                try:
                    self.q.put(self._buf)
                except Exception:
                    pass
                self._buf = ""

    if logs_q is not None:
        sys.stdout = _StreamToQueue(logs_q)  # type: ignore
        sys.stderr = _StreamToQueue(logs_q)  # type: ignore
        try:
            logs_q.put("[macro] starting...")
            # 전달받은 파라미터 로깅 (디버깅용)
            params_str = f"arrival={arrival}, departure={departure}, standard_date={standard_date}, standard_time={standard_time}, seat_types={seat_types}, from_train_number={from_train_number}, to_train_number={to_train_number}"
            logs_q.put(f"[macro] 파라미터: {params_str}")
        except Exception:
            pass
    try:
        # 명시적으로 파라미터 전달
        main_linux.main(
            arrival=arrival,
            departure=departure,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            status_q=status_q,
            logs_q=logs_q,
        )
        if status_q is not None:
            status_q.put({"status": "finished"})
    except Exception as e:
        # traceback 없이 에러 메시지만 전달
        error_message = str(e)
        if status_q is not None:
            status_q.put({"status": "error", "message": error_message})
            # 에러 발생 시 finished 상태도 전달하여 종료 상태를 명확히 함
            status_q.put({"status": "finished"})
        if logs_q is not None:
            try:
                logs_q.put(f"[ERROR] {error_message}")
            except Exception:
                pass
        # Exit child process
        return


def render_page(message: str = "", **form_params) -> HTMLResponse:
    STATE.refresh()
    running = STATE.running
    pid = STATE.proc.pid if STATE.proc else None
    last_error = STATE.last_error
    
    # 환경변수 확인
    env_check = check_env_vars()
    env_warning = ""
    if not all(env_check.values()):
        missing = [k for k, v in env_check.items() if not v]
        env_warning = f"⚠️ 환경변수가 설정되지 않았습니다: {', '.join(missing)}. '환경변수 입력' 버튼을 클릭하여 설정하세요."

    # 기본값 설정: 전달받은 파라미터 > 실행 중인 파라미터 > 기본값
    defaults = dict(
        arrival=main_linux.DEFAULT_ARRIVAL,
        departure=main_linux.DEFAULT_DEPARTURE,
        standard_date=main_linux.DEFAULT_STANDARD_DATE,
        standard_time=main_linux.DEFAULT_STANDARD_TIME,
        seat_types=main_linux.DEFAULT_SEAT_TYPES,
        from_train_number=main_linux.DEFAULT_FROM_TRAIN_NUMBER,
        to_train_number=main_linux.DEFAULT_TO_TRAIN_NUMBER,
    )
    
    # 실행 중인 파라미터가 있으면 사용
    if STATE.current_params:
        defaults.update(STATE.current_params)
    
    # 전달받은 파라미터가 있으면 우선 사용
    if form_params:
        defaults.update({k: v for k, v in form_params.items() if v is not None})
    
    # 저장된 환경변수 로드 (표시용, 실제 값은 마스킹)
    saved_env = load_env_vars()
    masked_env = {}
    for key in ["MEMBER_NUMBER", "PASSWORD", "DISCORD_WEB_HOOK"]:
        val = saved_env.get(key, "")
        if val:
            if key == "PASSWORD":
                masked_env[key] = "*" * min(len(val), 8)
            elif key == "MEMBER_NUMBER":
                masked_env[key] = val[:3] + "*" * (len(val) - 3) if len(val) > 3 else "*" * len(val)
            else:
                masked_env[key] = val[:10] + "..." if len(val) > 10 else val

    html = f"""
    <!doctype html>
    <html lang=ko>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>SRT Macro Controller</title>
        <style>
          * {{ box-sizing: border-box; }}
          body {{ 
            font-family: system-ui, -apple-system, sans-serif; 
            margin: 0;
            padding: 1rem;
            min-height: 100vh;
            background: #f9fafb;
            display: flex;
            flex-direction: column;
            align-items: center;
          }}
          .container {{
            width: 100%;
            max-width: 800px;
            margin: 0 auto;
          }}
          h1 {{
            margin: 1.5rem 0;
            font-size: clamp(1.5rem, 4vw, 2rem);
            text-align: center;
            color: #111827;
          }}
          .card {{ 
            background: white;
            max-width: 100%;
            padding: clamp(1rem, 3vw, 1.5rem);
            border: 1px solid #e5e7eb; 
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            margin-bottom: 1rem;
          }}
          .row {{ 
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            margin-bottom: 1rem;
          }}
          .row label {{
            font-weight: 500;
            color: #374151;
            font-size: clamp(0.875rem, 2vw, 0.9375rem);
            margin-bottom: 0.25rem;
          }}
          input, select {{ 
            width: 100%;
            padding: clamp(0.625rem, 2vw, 0.75rem) clamp(0.75rem, 2vw, 0.875rem);
            border: 1px solid #d1d5db; 
            border-radius: 8px;
            font-size: clamp(0.875rem, 2vw, 1rem);
            transition: border-color 0.2s, box-shadow 0.2s;
          }}
          input:focus, select:focus {{
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
          }}
          .actions {{ 
            display: flex; 
            gap: 0.75rem;
            margin-top: 1.5rem;
            flex-wrap: wrap;
          }}
          button {{ 
            flex: 1;
            min-width: 120px;
            padding: clamp(0.75rem, 2vw, 0.875rem) clamp(1rem, 3vw, 1.25rem);
            border: 0; 
            border-radius: 8px;
            cursor: pointer;
            font-size: clamp(0.875rem, 2vw, 1rem);
            font-weight: 500;
            transition: opacity 0.2s, transform 0.1s;
          }}
          button:hover:not(:disabled) {{
            opacity: 0.9;
            transform: translateY(-1px);
          }}
          button:active:not(:disabled) {{
            transform: translateY(0);
          }}
          button:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
          }}
          .primary {{ background:rgb(62, 66, 75); color: white; }}
          .danger {{ background: #dc2626; color: white; }}
          .muted {{ 
            color: #6b7280;
            font-size: clamp(0.75rem, 2vw, 0.875rem);
            text-align: center;
            margin-top: 1rem;
            padding: 0 1rem;
          }}
          .status {{ 
            margin-bottom: 1.25rem;
            padding: 0.75rem;
            background: #f9fafb;
            border-radius: 8px;
            font-size: clamp(0.875rem, 2vw, 1rem);
          }}
          .msg {{ 
            margin: 0.75rem 0;
            padding: 0.75rem;
            border-radius: 8px;
            color: #374151;
            font-size: clamp(0.875rem, 2vw, 0.9375rem);
            line-height: 1.5;
          }}
          .msg[style*="color:#dc2626"] {{
            background: #fef2f2;
            border: 1px solid #fecaca;
          }}
          .running {{ color: #16a34a; font-weight: 600; }}
          .stopped {{ color: #9ca3af; font-weight: 600; }}
          h3 {{
            margin: 1.5rem 0 0.75rem 0;
            font-size: clamp(1rem, 3vw, 1.125rem);
            color: #111827;
          }}
          pre {{
            background: #0b1020; 
            color: #d1d5db; 
            padding: clamp(0.75rem, 2vw, 1rem);
            border-radius: 8px;
            height: clamp(200px, 30vh, 300px);
            width: 100%;
            overflow-y: auto; 
            overflow-x: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
            font-size: clamp(0.75rem, 1.5vw, 0.875rem);
            line-height: 1.5;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
          }}
          .env-btn {{
            background: #3b82f6;
            color: white;
          }}
          .env-warning {{
            background: #fef3c7;
            border: 1px solid #fcd34d;
            color: #92400e;
            padding: 0.75rem;
            border-radius: 8px;
            margin-bottom: 1rem;
            font-size: clamp(0.875rem, 2vw, 0.9375rem);
          }}
          .secondary {{
            background: #6b7280;
            color: white;
          }}
          @media (min-width: 640px) {{
            body {{ padding: 2rem; }}
            .row {{
              flex-direction: row;
              align-items: center;
            }}
            .row label {{
              flex: 0 0 180px;
              margin-bottom: 0;
            }}
            input, select {{
              flex: 1;
              max-width: none;
            }}
            .actions {{
              flex-wrap: nowrap;
            }}
            button {{
              flex: 0 1 auto;
            }}
            pre {{
              height: 300px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="container">
          <h1>SRT Macro Controller</h1>
          <div class="card">
            {f'<div class="env-warning">{env_warning}</div>' if env_warning else ''}
            <div class="status">
              상태: <span id="status-text" class="{('running' if running else 'stopped')}">{'동작 중' if running else '대기'}</span> <span id="status-pid">{('PID ' + str(pid) if running and pid else '')}</span>
            </div>
            {f'<div class=msg>{message}</div>' if message else ''}
            {f'<div class=msg style="color:#dc2626;white-space:pre-wrap">{last_error}</div>' if last_error else ''}
          <form id="startForm" method="post" action="/start">
            <div class="row"><label>출발지</label><input name="arrival" value="{defaults['arrival']}" required></div>
            <div class="row"><label>도착지</label><input name="departure" value="{defaults['departure']}" required></div>
            <div class="row"><label>기준 날짜</label><input name="standard_date" value="{defaults['standard_date']}" pattern="\\d{{8}}" required></div>
            <div class="row"><label>기준 시간</label><input name="standard_time" value="{defaults['standard_time']}" pattern="(00|02|04|06|08|10|12|14|16|18|20|22)" required></div>
            <div class="row"><label>좌석 종류</label>
              <select name="seat_types">
                <option value="both" {'selected' if defaults['seat_types']=='both' else ''}>일반+특실</option>
                <option value="standard" {'selected' if defaults['seat_types']=='standard' else ''}>일반</option>
                <option value="special" {'selected' if defaults['seat_types']=='special' else ''}>특실</option>
              </select>
            </div>
            <div class="row"><label>조회 시작 열차 순번</label><input type="number" name="from_train_number" value="{defaults['from_train_number']}" min="1" max="10" required></div>
            <div class="row"><label>조회 종료 열차 순번</label><input type="number" name="to_train_number" value="{defaults['to_train_number']}" min="1" max="10" required></div>
            <div class="actions">
              <button class="primary" type="submit" form="startForm" {'disabled' if running else ''}>시작</button>
              <button class="danger" type="submit" form="stopForm" {'disabled' if not running else ''}>정지</button>
              <button class="primary env-btn" onclick="openEnvModal()">환경변수 입력</button>
            </div>
          </form>
          <form id="stopForm" method="post" action="/stop" style="display:none;"></form>
            <h3>실시간 로그</h3>
            <pre id="logbox">[logs] 초기화 중...</pre>
          </div>
          <p class="muted">환경변수는 암호화되어 저장됩니다. MEMBER_NUMBER, PASSWORD, DISCORD_WEB_HOOK 변수가 필요합니다.</p>
        </div>
        
        <script>
          function openEnvModal() {{
            var width = 600;
            var height = 650;
            var left = (screen.width - width) / 2;
            var top = (screen.height - height) / 2;
            var features = 'width=' + width + ',height=' + height + ',left=' + left + ',top=' + top + ',resizable=yes,scrollbars=yes';
            var popup = window.open('/env/form', 'envModal', features);
            if(!popup || popup.closed || typeof popup.closed == 'undefined') {{
              alert('팝업이 차단되었습니다. 브라우저 설정에서 팝업을 허용해주세요.');
            }}
          }}
        </script>
        
        <script>
          // 인라인으로 즉시 실행 (캐시 문제 방지)
          // 중복 실행 방지: 이미 초기화되었는지 확인
          if(window.logSystemInitialized) {{
            console.log('[logs] 이미 초기화됨, 스킵');
          }} else {{
            window.logSystemInitialized = true;
            (function(){{
              var logEl = document.getElementById('logbox');
              var statusTextEl = document.getElementById('status-text');
              var statusPidEl = document.getElementById('status-pid');
              if(!logEl) {{ console.error('[logs] logbox not found'); return; }}
              
              var seenLines = new Set(); // 중복 방지를 위한 Set
              
              function append(line){{
                // 중복 체크: 같은 라인이 연속으로 오면 무시
                if(seenLines.has(line)) {{
                  return;
                }}
                seenLines.add(line);
                // Set 크기 제한 (메모리 관리)
                if(seenLines.size > 1000) {{
                  var firstKey = seenLines.values().next().value;
                  seenLines.delete(firstKey);
                }}
                try{{ logEl.textContent += (line + "\\n"); logEl.scrollTop = logEl.scrollHeight; }}
                catch(e){{ console.error('append failed', e); }}
              }}
              
              function updateStatus(running, pid){{
                if(!statusTextEl) return;
                if(running){{
                  statusTextEl.textContent = '동작 중';
                  statusTextEl.className = 'running';
                  if(statusPidEl && pid) statusPidEl.textContent = 'PID ' + pid;
                }}else{{
                  statusTextEl.textContent = '대기';
                  statusTextEl.className = 'stopped';
                  if(statusPidEl) statusPidEl.textContent = '';
                }}
              }}
              
              var es = null; var pollTimer = null; var established = false; var usingSSE = false;
              
              function stopPoll(){{
                if(pollTimer) {{
                  clearInterval(pollTimer);
                  pollTimer = null;
                }}
              }}
              
              function startPoll(){{
                if(pollTimer || usingSSE) return;
                append('[logs] 폴링 시작');
                var lastLen = 0;
                pollTimer = setInterval(function(){{
                  fetch('/logs.json').then(function(res){{ return res.json(); }}).then(function(j){{
                    var lines = (j && j.lines) || [];
                    if(lastLen > lines.length) lastLen = 0;
                    for(var i=lastLen;i<lines.length;i++) {{
                      var line = lines[i];
                      if(line && !seenLines.has(line)) {{
                        append(line);
                      }}
                    }}
                    lastLen = lines.length;
                  }}).catch(function(err){{ console.warn('poll failed', err); }});
                  // 상태도 폴링으로 업데이트
                  fetch('/status').then(function(res){{ return res.json(); }}).then(function(s){{
                    updateStatus(s.running, s.pid);
                  }}).catch(function(err){{ console.warn('status poll failed', err); }});
                }}, 1500);
              }}
              
              // 즉시 폴링 시작
              startPoll();
              
              var fallbackTimer = setTimeout(function(){{
                if(!established){{
                  append('[logs] SSE 지연 - 폴백 유지');
                  try{{ if(es) es.close(); }}catch(e){{}}
                }}
              }}, 1500);
              
              try{{
                es = new EventSource('/logs');
                es.onopen = function(){{
                  established = true; 
                  usingSSE = true;
                  clearTimeout(fallbackTimer); 
                  stopPoll(); // 폴링 먼저 중지
                  append('[logs] SSE 연결됨');
                }};
                es.onmessage = function(e){{
                  established = true; 
                  clearTimeout(fallbackTimer);
                  var line = e.data;
                  if(line && line.trim() && !line.startsWith(':')) {{
                    append(line);
                  }}
                }};
                es.onerror = function(){{
                  usingSSE = false;
                  append('[logs] SSE 연결 끊김 - 폴링으로 계속');
                  try{{ if(es) es.close(); }}catch(e){{}}
                  es = null;
                  if(!pollTimer) startPoll();
                }};
              }}catch(e){{ 
                console.error('SSE init failed', e); 
                if(!pollTimer) startPoll(); 
              }}
            }})();
          }}
        </script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # 환경변수 로드 및 적용
    apply_env_vars_to_os()
    try:
        STATE._append_log("[ui] 페이지 로드")
    except Exception:
        pass
    return render_page()


@app.post("/start")
def start(
    arrival: str = Form(...),
    departure: str = Form(...),
    standard_date: str = Form(...),
    standard_time: str = Form(...),
    seat_types: str = Form("both"),
    from_train_number: int = Form(1),
    to_train_number: int = Form(1),
):
    # 환경변수 적용
    apply_env_vars_to_os()
    
    # 환경변수 확인
    env_check = check_env_vars()
    if not env_check.get("MEMBER_NUMBER") or not env_check.get("PASSWORD"):
        return render_page(
            "⚠️ 환경변수가 설정되지 않았습니다. '환경변수 입력' 버튼을 클릭하여 설정하세요.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )
    
    # Basic input validation
    if from_train_number > to_train_number:
        return render_page(
            "조회 시작 순번은 종료 순번보다 클 수 없습니다.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )

    if not (1 <= from_train_number <= 10 and 1 <= to_train_number <= 10):
        return render_page(
            "열차 순번은 1~10 사이여야 합니다.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )

    if not (len(standard_date) == 8 and standard_date.isdigit()):
        return render_page(
            "기준 날짜는 YYYYMMDD 8자리 숫자여야 합니다.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )

    if standard_time not in {"00","02","04","06","08","10","12","14","16","18","20","22"}:
        return render_page(
            "기준 시간은 00,02,04,...,22 중 하나여야 합니다.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )

    if STATE.running:
        return render_page("이미 실행 중입니다.")

    ok = STATE.start(
        arrival=arrival,
        departure=departure,
        from_train_number=from_train_number,
        to_train_number=to_train_number,
        standard_date=standard_date,
        standard_time=standard_time,
        seat_types=seat_types,
    )
    if not ok:
        # best-effort: show error also in logs
        try:
            STATE._append_log("[ui] 시작 실패: " + (STATE.last_error or "사유 미상"))
        except Exception:
            pass
        return render_page(
            "시작할 수 없습니다.",
            arrival=arrival,
            departure=departure,
            standard_date=standard_date,
            standard_time=standard_time,
            seat_types=seat_types,
            from_train_number=from_train_number,
            to_train_number=to_train_number,
        )
    try:
        STATE._append_log("[ui] 매크로 시작")
    except Exception:
        pass
    return render_page(
        "시작되었습니다.",
        arrival=arrival,
        departure=departure,
        standard_date=standard_date,
        standard_time=standard_time,
        seat_types=seat_types,
        from_train_number=from_train_number,
        to_train_number=to_train_number,
    )


@app.post("/stop")
def stop():
    if not STATE.running:
        return render_page("실행 중이 아닙니다.")
    STATE.stop()
    return render_page("정지했습니다.")


@app.get("/env/form", response_class=HTMLResponse)
def env_form() -> HTMLResponse:
    """환경변수 입력 폼 페이지"""
    saved_env = load_env_vars()
    masked_env = {}
    for key in ["MEMBER_NUMBER", "PASSWORD", "DISCORD_WEB_HOOK"]:
        val = saved_env.get(key, "")
        if val:
            if key == "PASSWORD":
                masked_env[key] = "*" * min(len(val), 8)
            elif key == "MEMBER_NUMBER":
                masked_env[key] = val[:3] + "*" * (len(val) - 3) if len(val) > 3 else "*" * len(val)
            else:
                masked_env[key] = val[:10] + "..." if len(val) > 10 else val
    
    html = f"""
    <!doctype html>
    <html lang=ko>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>환경변수 입력</title>
        <style>
          * {{ box-sizing: border-box; }}
          body {{ 
            font-family: system-ui, -apple-system, sans-serif; 
            margin: 0;
            padding: 1.5rem;
            background: #f9fafb;
            min-height: 100vh;
          }}
          .container {{
            max-width: 500px;
            margin: 0 auto;
            background: white;
            padding: 2rem;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
          }}
          h2 {{
            margin: 0 0 1.5rem 0;
            font-size: 1.5rem;
            color: #111827;
          }}
          .row {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            margin-bottom: 1rem;
          }}
          label {{
            font-weight: 500;
            color: #374151;
            font-size: 0.9375rem;
          }}
          input {{
            width: 100%;
            padding: 0.75rem;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.2s, box-shadow 0.2s;
          }}
          input:focus {{
            outline: none;
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
          }}
          .actions {{
            display: flex;
            gap: 0.75rem;
            margin-top: 1.5rem;
          }}
          button {{
            flex: 1;
            padding: 0.875rem 1.25rem;
            border: 0;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: 500;
            transition: opacity 0.2s;
          }}
          button:hover {{
            opacity: 0.9;
          }}
          .primary {{
            background: rgb(62, 66, 75);
            color: white;
          }}
          .secondary {{
            background: #6b7280;
            color: white;
          }}
          .msg {{
            margin: 0.75rem 0;
            padding: 0.75rem;
            border-radius: 8px;
            font-size: 0.9375rem;
            line-height: 1.5;
            display: none;
          }}
        </style>
      </head>
      <body>
        <div class="container">
          <h2>환경변수 입력</h2>
          <form id="envForm" onsubmit="saveEnvVars(event)">
            <div class="row">
              <label>회원번호 (MEMBER_NUMBER)</label>
              <input type="text" id="member_number" name="member_number" value="{masked_env.get('MEMBER_NUMBER', '')}" placeholder="회원번호를 입력하세요" required>
            </div>
            <div class="row">
              <label>비밀번호 (PASSWORD)</label>
              <input type="password" id="password" name="password" placeholder="비밀번호를 입력하세요" required>
            </div>
            <div class="row">
              <label>Discord Webhook (DISCORD_WEB_HOOK)</label>
              <input type="url" id="discord_webhook" name="discord_webhook" value="{masked_env.get('DISCORD_WEB_HOOK', '')}" placeholder="Discord Webhook URL을 입력하세요">
            </div>
            <div class="actions">
              <button type="submit" class="primary">저장</button>
              <button type="button" class="secondary" onclick="window.close()">취소</button>
            </div>
            <div id="envMsg" class="msg"></div>
          </form>
        </div>
        <script>
          function saveEnvVars(event) {{
            event.preventDefault();
            var formData = new FormData(event.target);
            var data = {{
              member_number: formData.get('member_number'),
              password: formData.get('password'),
              discord_webhook: formData.get('discord_webhook') || ''
            }};
            fetch('/env/save', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify(data)
            }})
            .then(function(res) {{ return res.json(); }})
            .then(function(result) {{
              var msgEl = document.getElementById('envMsg');
              msgEl.style.display = 'block';
              if(result.success) {{
                msgEl.textContent = '환경변수가 저장되었습니다.';
                msgEl.style.color = '#16a34a';
                msgEl.style.background = '#f0fdf4';
                msgEl.style.border = '1px solid #86efac';
                setTimeout(function() {{
                  if(window.opener) {{
                    window.opener.location.reload();
                  }}
                  window.close();
                }}, 1500);
              }} else {{
                msgEl.textContent = '저장 실패: ' + (result.message || '알 수 없는 오류');
                msgEl.style.color = '#dc2626';
                msgEl.style.background = '#fef2f2';
                msgEl.style.border = '1px solid #fecaca';
              }}
            }})
            .catch(function(err) {{
              var msgEl = document.getElementById('envMsg');
              msgEl.style.display = 'block';
              msgEl.textContent = '저장 실패: ' + err.message;
              msgEl.style.color = '#dc2626';
              msgEl.style.background = '#fef2f2';
              msgEl.style.border = '1px solid #fecaca';
            }});
          }}
        </script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/env/check")
def check_env():
    """환경변수 확인 API"""
    check_result = check_env_vars()
    return JSONResponse({
        "success": all(check_result.values()),
        "check": check_result,
    })


@app.post("/env/save")
async def save_env(request: Request):
    """환경변수 저장 API"""
    try:
        data = await request.json()
        env_vars = {
            "MEMBER_NUMBER": data.get("member_number", "").strip(),
            "PASSWORD": data.get("password", "").strip(),
            "DISCORD_WEB_HOOK": data.get("discord_webhook", "").strip(),
        }
        
        # 필수 필드 검증
        if not env_vars["MEMBER_NUMBER"]:
            return JSONResponse({
                "success": False,
                "message": "회원번호는 필수입니다."
            }, status_code=400)
        
        if not env_vars["PASSWORD"]:
            return JSONResponse({
                "success": False,
                "message": "비밀번호는 필수입니다."
            }, status_code=400)
        
        # 암호화하여 저장
        if encrypt_env_vars(env_vars):
            # 저장 후 os.environ에도 적용
            for key, value in env_vars.items():
                if value:
                    os.environ[key] = value
            return JSONResponse({
                "success": True,
                "message": "환경변수가 저장되었습니다."
            })
        else:
            return JSONResponse({
                "success": False,
                "message": "저장에 실패했습니다."
            }, status_code=500)
    except Exception as e:
        return JSONResponse({
            "success": False,
            "message": f"오류 발생: {str(e)}"
        }, status_code=500)


@app.get("/status")
def status():
    STATE.refresh()
    return JSONResponse(
        {
            "running": STATE.running,
            "pid": STATE.proc.pid if STATE.proc else None,
            "started_at": STATE.started_at,
            "last_error": STATE.last_error,
        }
    )


@app.get("/logs")
async def logs_stream():
    # Subscribe for future messages
    q = STATE.subscribe()

    async def event_gen():
        # Small greeting to prove connection
        yield "data: [logs] connected\n\n"
        # 버퍼 flush 제거: 폴링과 중복 방지
        # SSE는 새로운 메시지만 전송하고, 초기 버퍼는 폴링이 처리하도록 함
        try:
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield f"data: {line}\n\n"
                except asyncio.TimeoutError:
                    # heartbeat comment to keep connection alive
                    yield ": ping\n\n"
        finally:
            STATE.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)


@app.get("/logs.json")
def logs_json():
    # Lightweight polling fallback
    return JSONResponse({
        "running": STATE.running,
        "lines": list(STATE._log_buffer),
        "last_error": STATE.last_error,
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)


# Serve a separate JS client to avoid inline f-string escaping issues
JS_CLIENT = """
"use strict";
(function(){
  function init(){
    try{
      var logEl = document.getElementById('logbox');
      if(!logEl) { console.warn('[logs] logbox not found'); return; }
      function append(line){
        try{ logEl.textContent += (line + "\n"); logEl.scrollTop = logEl.scrollHeight; }
        catch(e){ console.error('append failed', e); }
      }
      var es; var pollTimer = null; var established = false;
      function startPoll(){
        if(pollTimer) return;
        append('[logs] 폴링 시작');
        var lastLen = 0;
        pollTimer = setInterval(function(){
          fetch('/logs.json').then(function(res){ return res.json(); }).then(function(j){
            var lines = (j && j.lines) || [];
            if(lastLen > lines.length) lastLen = 0;
            for(var i=lastLen;i<lines.length;i++) append(lines[i]);
            lastLen = lines.length;
          }).catch(function(_){ });
        }, 1500);
      }
      // 최소 보장: 즉시 폴링 시작 (SSE 연결 시 중지)
      startPoll();
      var fallbackTimer = setTimeout(function(){
        if(!established){
          append('[logs] SSE 지연 - 폴백 유지');
          try{ es && es.close(); }catch(e){}
        }
      }, 1500);
      try{
        es = new EventSource('/logs');
        es.onopen = function(){
          established = true; clearTimeout(fallbackTimer); append('[logs] SSE 연결됨');
          if(pollTimer){ try{ clearInterval(pollTimer); }catch(e){} pollTimer = null; append('[logs] 폴링 중지'); }
        };
        es.onmessage = function(e){ established = true; clearTimeout(fallbackTimer); append(e.data); };
        es.onerror = function(){
          append('[logs] SSE 연결 끊김 - 폴링으로 계속');
          try{ es.close(); }catch(e){}
          if(!pollTimer) startPoll();
        };
      }catch(e){ startPoll(); }
    }catch(e){ console.error('init error', e); }
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""


@app.get("/client.js")
def client_js():
    return Response(JS_CLIENT, media_type="text/javascript; charset=utf-8")
