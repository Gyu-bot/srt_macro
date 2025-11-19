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
from fastapi.staticfiles import StaticFiles

import macro_core

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
            os.environ[key] = value


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
                self.last_error = self._clean_error_message(error_msg)
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
        return True

    def refresh(self) -> None:
        """Drain status queue to capture late errors/finish events."""
        if self.proc is not None and not self.proc.is_alive():
            if self._status_q is not None or self._logs_q is not None:
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
                    except Exception:
                        pass
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
                    self.last_error = self._clean_error_message(error_msg)
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
        except Exception:
            pass
    
    def _clean_error_message(self, error_msg: str) -> str:
        lines = error_msg.split('\n')
        cleaned_lines = []
        for line in lines:
            if line.strip().startswith('Traceback'):
                break
            if line.strip().startswith('File "'):
                break
            cleaned_lines.append(line)
        while cleaned_lines and not cleaned_lines[-1].strip():
            cleaned_lines.pop()
        result = '\n'.join(cleaned_lines).strip()
        return result if result else "실행 중 오류가 발생했습니다."

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
                    if not self.running:
                        break
                    continue
                if line is None:
                    break
                try:
                    s = str(line)
                except Exception:
                    s = repr(line)
                self._append_log(s)
            self._logs_q = None

        self._log_thread = threading.Thread(target=_worker, daemon=True)
        self._log_thread.start()

    def _append_log(self, line: str) -> None:
        self._log_buffer.append(line)
        def _safe_put(q: asyncio.Queue, item: str):
            try:
                q.put_nowait(item)
            except Exception:
                pass
        for loop, q in list(self._listeners):
            try:
                loop.call_soon_threadsafe(_safe_put, q, line)
            except Exception:
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
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass


STATE = MacroState()


def run_macro(**kwargs) -> None:
    apply_env_vars_to_os()
    
    arrival = kwargs.pop("arrival", None)
    departure = kwargs.pop("departure", None)
    from_train_number = kwargs.pop("from_train_number", None)
    to_train_number = kwargs.pop("to_train_number", None)
    standard_date = kwargs.pop("standard_date", None)
    standard_time = kwargs.pop("standard_time", None)
    seat_types = kwargs.pop("seat_types", None)
    status_q: Optional[mp.Queue] = kwargs.pop("status_q", None)
    logs_q: Optional[mp.Queue] = kwargs.pop("logs_q", None)
    
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
        except Exception:
            pass
    try:
        macro_core.main(
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
        error_message = str(e)
        if status_q is not None:
            status_q.put({"status": "error", "message": error_message})
            status_q.put({"status": "finished"})
        if logs_q is not None:
            try:
                logs_q.put(f"[ERROR] {error_message}")
            except Exception:
                pass
        return


def render_page(message: str = "", **form_params) -> HTMLResponse:
    STATE.refresh()
    running = STATE.running
    pid = STATE.proc.pid if STATE.proc else None
    last_error = STATE.last_error
    
    env_check = check_env_vars()
    env_warning = ""
    if not all(env_check.values()):
        missing = [k for k, v in env_check.items() if not v]
        env_warning = f"⚠️ 환경변수가 설정되지 않았습니다: {', '.join(missing)}. '환경변수 입력' 버튼을 클릭하여 설정하세요."

    defaults = dict(
        arrival=macro_core.DEFAULT_ARRIVAL,
        departure=macro_core.DEFAULT_DEPARTURE,
        standard_date=macro_core.DEFAULT_STANDARD_DATE,
        standard_time=macro_core.DEFAULT_STANDARD_TIME,
        seat_types=macro_core.DEFAULT_SEAT_TYPES,
        from_train_number=macro_core.DEFAULT_FROM_TRAIN_NUMBER,
        to_train_number=macro_core.DEFAULT_TO_TRAIN_NUMBER,
    )
    
    if STATE.current_params:
        defaults.update(STATE.current_params)
    
    if form_params:
        defaults.update({k: v for k, v in form_params.items() if v is not None})
    
    html = f"""
    <!doctype html>
    <html lang=ko>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>SRT Macro Controller</title>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
          :root {{
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --danger: #ef4444;
            --danger-hover: #dc2626;
            --bg: #f3f4f6;
            --card-bg: #ffffff;
            --text: #1f2937;
            --text-muted: #6b7280;
            --border: #e5e7eb;
            --radius: 12px;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
          }}
          * {{ box-sizing: border-box; }}
          body {{ 
            font-family: 'Inter', system-ui, sans-serif;
            margin: 0;
            padding: 2rem 1rem;
            min-height: 100vh;
            background: var(--bg);
            color: var(--text);
            display: flex;
            justify-content: center;
          }}
          .container {{
            width: 100%;
            max-width: 900px;
          }}
          h1 {{
            text-align: center;
            color: #111827;
            font-weight: 800;
            margin-bottom: 2rem;
            font-size: 2.25rem;
            letter-spacing: -0.025em;
          }}
          .card {{ 
            background: var(--card-bg);
            padding: 2rem;
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            margin-bottom: 1.5rem;
          }}
          .status-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem;
            background: #f9fafb;
            border-radius: 8px;
            margin-bottom: 1.5rem;
            border: 1px solid var(--border);
          }}
          .status-indicator {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-weight: 600;
          }}
          .dot {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #d1d5db;
          }}
          .dot.running {{ background: #10b981; box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2); }}
          .dot.stopped {{ background: #9ca3af; }}
          
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
          }}
          .form-group {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
          }}
          label {{
            font-weight: 500;
            font-size: 0.875rem;
            color: #374151;
          }}
          input, select {{
            padding: 0.75rem;
            border: 1px solid var(--border);
            border-radius: 8px;
            font-size: 0.95rem;
            transition: all 0.2s;
            background: #fff;
          }}
          input:focus, select:focus {{
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
          }}
          
          .actions {{
            display: flex;
            gap: 1rem;
            margin-top: 1rem;
          }}
          button {{
            flex: 1;
            padding: 0.875rem;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.2s;
          }}
          .btn-primary {{
            background: var(--primary);
            color: white;
          }}
          .btn-primary:hover {{ background: var(--primary-hover); }}
          .btn-danger {{
            background: var(--danger);
            color: white;
          }}
          .btn-danger:hover {{ background: var(--danger-hover); }}
          .btn-secondary {{
            background: #fff;
            border: 1px solid var(--border);
            color: var(--text);
          }}
          .btn-secondary:hover {{ background: #f9fafb; }}
          
          button:disabled {{
            opacity: 0.6;
            cursor: not-allowed;
            transform: none !important;
          }}
          
          .log-box {{
            background: #111827;
            color: #e5e7eb;
            padding: 1rem;
            border-radius: 8px;
            height: 300px;
            overflow-y: auto;
            font-family: 'Menlo', 'Monaco', monospace;
            font-size: 0.85rem;
            line-height: 1.6;
          }}
          
          .alert {{
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 1rem;
            font-size: 0.9rem;
          }}
          .alert-warning {{ background: #fffbeb; color: #92400e; border: 1px solid #fcd34d; }}
          .alert-error {{ background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }}
          .alert-info {{ background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }}
          
          /* Custom Scrollbar */
          ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
          ::-webkit-scrollbar-track {{ background: transparent; }}
          ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 4px; }}
          ::-webkit-scrollbar-thumb:hover {{ background: #94a3b8; }}
        </style>
      </head>
      <body>
        <div class="container">
          <h1>🚄 SRT Macro Controller</h1>
          
          <div class="card">
            {f'<div class="alert alert-warning">{env_warning}</div>' if env_warning else ''}
            {f'<div class="alert alert-info">{message}</div>' if message else ''}
            {f'<div class="alert alert-error" style="white-space:pre-wrap">{last_error}</div>' if last_error else ''}
            
            <div class="status-bar">
              <div class="status-indicator">
                <div class="dot {('running' if running else 'stopped')}"></div>
                <span>{('실행 중' if running else '대기 중')}</span>
                {f'<span style="color:var(--text-muted); font-weight:400; font-size:0.9em; margin-left:0.5rem">PID {pid}</span>' if running and pid else ''}
              </div>
              <button class="btn-secondary" onclick="openEnvModal()" style="flex:0 0 auto; padding:0.5rem 1rem; font-size:0.875rem;">🔑 환경변수 설정</button>
            </div>

            <form id="startForm" method="post" action="/start">
              <div class="grid">
                <div class="form-group">
                  <label>출발지</label>
                  <input name="arrival" value="{defaults['arrival']}" required placeholder="예: 동대구">
                </div>
                <div class="form-group">
                  <label>도착지</label>
                  <input name="departure" value="{defaults['departure']}" required placeholder="예: 동탄">
                </div>
                <div class="form-group">
                  <label>기준 날짜 (YYYYMMDD)</label>
                  <input name="standard_date" value="{defaults['standard_date']}" pattern="\\d{{8}}" required>
                </div>
                <div class="form-group">
                  <label>기준 시간 (2의 배수)</label>
                  <input name="standard_time" value="{defaults['standard_time']}" pattern="(00|02|04|06|08|10|12|14|16|18|20|22)" required>
                </div>
                <div class="form-group">
                  <label>좌석 종류</label>
                  <select name="seat_types">
                    <option value="both" {'selected' if defaults['seat_types']=='both' else ''}>일반 + 특실</option>
                    <option value="standard" {'selected' if defaults['seat_types']=='standard' else ''}>일반석만</option>
                    <option value="special" {'selected' if defaults['seat_types']=='special' else ''}>특실만</option>
                  </select>
                </div>
                <div class="form-group">
                  <label>조회 범위 (시작~종료)</label>
                  <div style="display:flex; gap:0.5rem; align-items:center;">
                    <input type="number" name="from_train_number" value="{defaults['from_train_number']}" min="1" max="10" required style="flex:1">
                    <span>~</span>
                    <input type="number" name="to_train_number" value="{defaults['to_train_number']}" min="1" max="10" required style="flex:1">
                  </div>
                </div>
              </div>
              
              <div class="actions">
                <button class="btn-primary" type="submit" form="startForm" {'disabled' if running else ''}>
                  {('실행 중...' if running else '🚀 매크로 시작')}
                </button>
                <button class="btn-danger" type="submit" form="stopForm" {'disabled' if not running else ''}>
                  ⏹ 정지
                </button>
              </div>
            </form>
            <form id="stopForm" method="post" action="/stop" style="display:none;"></form>
          </div>

          <div class="card" style="padding:1.5rem;">
            <h3 style="margin-top:0; margin-bottom:1rem; font-size:1.1rem;">실시간 로그</h3>
            <div id="logbox" class="log-box">[logs] 시스템 준비 완료...</div>
          </div>
        </div>
        
        <script>
          function openEnvModal() {{
            var width = 500;
            var height = 600;
            var left = (screen.width - width) / 2;
            var top = (screen.height - height) / 2;
            window.open('/env/form', 'envModal', 'width='+width+',height='+height+',left='+left+',top='+top);
          }}
          
          window.addEventListener('message', function(event) {{
            if(event.data && event.data.type === 'envSaved' && event.data.reload) {{
              window.location.replace(window.location.pathname);
            }}
          }});
        </script>
        <script src="/client.js"></script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    apply_env_vars_to_os()
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
    apply_env_vars_to_os()
    
    env_check = check_env_vars()
    if not env_check.get("MEMBER_NUMBER") or not env_check.get("PASSWORD"):
        return render_page(
            "⚠️ 환경변수가 설정되지 않았습니다. '환경변수 설정'을 통해 입력해주세요.",
            arrival=arrival, departure=departure, standard_date=standard_date,
            standard_time=standard_time, seat_types=seat_types,
            from_train_number=from_train_number, to_train_number=to_train_number
        )
    
    if from_train_number > to_train_number:
        return render_page("조회 시작 순번은 종료 순번보다 클 수 없습니다.")

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
        return render_page("시작할 수 없습니다. (로그 확인 필요)")
        
    return render_page("매크로가 시작되었습니다.")


@app.post("/stop")
def stop():
    if not STATE.running:
        return render_page("실행 중이 아닙니다.")
    STATE.stop()
    return render_page("정지했습니다.")


@app.get("/env/form", response_class=HTMLResponse)
def env_form() -> HTMLResponse:
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
        <title>환경변수 설정</title>
        <style>
          body {{ font-family: system-ui, sans-serif; padding: 2rem; background: #f9fafb; }}
          .container {{ max-width: 400px; margin: 0 auto; background: white; padding: 2rem; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
          h2 {{ margin-top: 0; color: #111827; }}
          label {{ display: block; margin-bottom: 0.5rem; font-weight: 500; color: #374151; }}
          input {{ width: 100%; padding: 0.75rem; margin-bottom: 1rem; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box; }}
          button {{ width: 100%; padding: 0.75rem; background: #4f46e5; color: white; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; }}
          button:hover {{ background: #4338ca; }}
          .msg {{ margin-top: 1rem; padding: 0.75rem; border-radius: 6px; display: none; font-size: 0.9rem; }}
        </style>
      </head>
      <body>
        <div class="container">
          <h2>환경변수 설정</h2>
          <form onsubmit="saveEnvVars(event)">
            <label>회원번호 (MEMBER_NUMBER)</label>
            <input type="text" name="member_number" value="{masked_env.get('MEMBER_NUMBER', '')}" placeholder="회원번호" required>
            
            <label>비밀번호 (PASSWORD)</label>
            <input type="password" name="password" placeholder="비밀번호 (변경 시에만 입력)" required>
            
            <label>Discord Webhook (선택)</label>
            <input type="url" name="discord_webhook" value="{masked_env.get('DISCORD_WEB_HOOK', '')}" placeholder="https://discord.com/api/webhooks/...">
            
            <button type="submit">저장하기</button>
            <div id="msg" class="msg"></div>
          </form>
        </div>
        <script>
          function saveEnvVars(e) {{
            e.preventDefault();
            var form = new FormData(e.target);
            var data = {{
              member_number: form.get('member_number'),
              password: form.get('password'),
              discord_webhook: form.get('discord_webhook')
            }};
            
            fetch('/env/save', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify(data)
            }}).then(r => r.json()).then(res => {{
              var msg = document.getElementById('msg');
              msg.style.display = 'block';
              if(res.success) {{
                msg.style.background = '#ecfdf5';
                msg.style.color = '#047857';
                msg.textContent = '저장되었습니다. 창을 닫습니다...';
                setTimeout(() => {{
                  if(window.opener) window.opener.postMessage({{type: 'envSaved', reload: true}}, '*');
                  window.close();
                }}, 1500);
              }} else {{
                msg.style.background = '#fef2f2';
                msg.style.color = '#b91c1c';
                msg.textContent = res.message;
              }}
            }});
          }}
        </script>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/env/save")
async def save_env(request: Request):
    try:
        data = await request.json()
        env_vars = {
            "MEMBER_NUMBER": data.get("member_number", "").strip(),
            "PASSWORD": data.get("password", "").strip(),
            "DISCORD_WEB_HOOK": data.get("discord_webhook", "").strip(),
        }
        
        if not env_vars["MEMBER_NUMBER"] or not env_vars["PASSWORD"]:
            return JSONResponse({"success": False, "message": "필수 항목이 누락되었습니다."}, status_code=400)
        
        if encrypt_env_vars(env_vars):
            for key, value in env_vars.items():
                if value: os.environ[key] = value
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"success": False, "message": "저장 실패"}, status_code=500)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/status")
def status():
    STATE.refresh()
    return JSONResponse({
        "running": STATE.running,
        "pid": STATE.proc.pid if STATE.proc else None,
        "started_at": STATE.started_at,
        "last_error": STATE.last_error,
    })


@app.get("/logs")
async def logs_stream():
    q = STATE.subscribe()
    async def event_gen():
        yield "data: [logs] connected\n\n"
        try:
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield f"data: {line}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            STATE.unsubscribe(q)
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })


@app.get("/logs.json")
def logs_json():
    return JSONResponse({
        "running": STATE.running,
        "lines": list(STATE._log_buffer),
        "last_error": STATE.last_error,
    })


@app.get("/client.js")
def client_js():
    js = """
    (function(){
        const logEl = document.getElementById('logbox');
        let es = null;
        let lastLog = "";
        
        function append(line) {
            if(!logEl) return;
            if(line === lastLog) return; // Deduplicate
            lastLog = line;
            
            const div = document.createElement('div');
            div.textContent = line;
            logEl.appendChild(div);
            logEl.scrollTop = logEl.scrollHeight;
        }
        
        function connect() {
            if(es) es.close();
            es = new EventSource('/logs');
            es.onmessage = function(e) {
                append(e.data);
            };
            es.onerror = function() {
                es.close();
                setTimeout(connect, 3000);
            };
        }
        
        // Initial logs
        fetch('/logs.json').then(r=>r.json()).then(d => {
            if(d.lines) d.lines.forEach(append);
            connect();
        });
        
        // Status poller
        setInterval(() => {
            fetch('/status').then(r=>r.json()).then(d => {
                const dot = document.querySelector('.dot');
                const text = document.querySelector('.status-indicator span');
                const startBtn = document.querySelector('button[form="startForm"]');
                const stopBtn = document.querySelector('button[form="stopForm"]');
                
                if(d.running) {
                    dot.className = 'dot running';
                    text.textContent = '실행 중';
                    if(startBtn) {
                        startBtn.disabled = true;
                        startBtn.textContent = '실행 중...';
                    }
                    if(stopBtn) stopBtn.disabled = false;
                } else {
                    dot.className = 'dot stopped';
                    text.textContent = '대기 중';
                    if(startBtn) {
                        startBtn.disabled = false;
                        startBtn.textContent = '🚀 매크로 시작';
                    }
                    if(stopBtn) stopBtn.disabled = true;
                }
                
                // Update PID if available
                const pidSpan = document.querySelector('#status-pid');
                if(d.running && d.pid) {
                    if(!pidSpan) {
                        const span = document.createElement('span');
                        span.id = 'status-pid';
                        span.style.color = 'var(--text-muted)';
                        span.style.fontWeight = '400';
                        span.style.fontSize = '0.9em';
                        span.style.marginLeft = '0.5rem';
                        span.textContent = 'PID ' + d.pid;
                        document.querySelector('.status-indicator').appendChild(span);
                    } else {
                        pidSpan.textContent = 'PID ' + d.pid;
                    }
                } else if(pidSpan) {
                    pidSpan.remove();
                }
                
                // If error occurred, show it (optional, but page reload handles it mostly)
                if(d.last_error) {
                    const errDiv = document.querySelector('.alert-error');
                    if(!errDiv) {
                        // Reload to show error
                        // window.location.reload();
                    }
                }
            });
        }, 1000);
    })();
    """
    return Response(js, media_type="text/javascript")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
