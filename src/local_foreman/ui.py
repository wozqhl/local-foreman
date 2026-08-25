"""Local live board: stdlib HTTP + SSE on 127.0.0.1:8765.

Chinese status: 干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 /
空转中 / 自己在想 / 展开原文 / 空转动手.
Mock demo needs no keys: read → fake escalate → apply continue.
UI defaults persist+idle ON so the board grows a mind log. Idle never
asks the coach. retrieved / idle_act share the same SSE traj.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from local_foreman.coach import MockCoach
from local_foreman.loop import ForemanLoop
from local_foreman.traj import Trajectory, default_traj_path
from local_foreman.worker import MockWorker, WorkerAction

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEMO_GOAL = "demo: 读 README，再假升级，按教练指示继续"

STATE_LABELS = {
    "act": "干活中",
    "ask": "求助中（正在咨询大模型）",
    "apply": "已收到指示",
    "idle": "空转中",
}
EVENT_LABELS = {
    "work": "干活中",
    "stuck": "求助中（正在咨询大模型）",
    "asked_coach": "求助中（正在咨询大模型）",
    "coach_instruction": "已收到指示",
    "resumed": "继续",
    "thought": "自己在想",
    "retrieved": "展开原文",
    "idle_act": "空转动手",
}


def state_label(state: str, last_kind: str = "") -> str:
    # Idle is local. Never reuse the coach-consult copy.
    if last_kind == "thought":
        return "自己在想"
    if last_kind == "idle_act":
        return "空转动手"
    if last_kind == "retrieved":
        return "展开原文"
    if state == "idle":
        return "空转中"
    if last_kind in EVENT_LABELS:
        return EVENT_LABELS[last_kind]
    return STATE_LABELS.get(state, "干活中")


def load_index_html() -> str:
    p = Path(__file__).with_name("ui.html")
    return p.read_text(encoding="utf-8")


class LiveBoard:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.goal = ""
        self.state = "act"
        self.problem = ""
        self.instruction = ""
        self.events: list[dict] = []
        self.subscribers: list[queue.Queue] = []
        self.demo_lock = threading.Lock()
        self.demo_running = False

    def snapshot(self) -> dict:
        with self.lock:
            last = self.events[-1]["kind"] if self.events else ""
            return {
                "goal": self.goal,
                "state": self.state,
                "state_label": state_label(self.state, last),
                "problem": self.problem,
                "instruction": self.instruction,
                "events": list(self.events),
            }

    def reset(self, goal: str) -> None:
        with self.lock:
            self.goal = goal
            self.state = "act"
            self.problem = ""
            self.instruction = ""
            self.events = []
        self._broadcast(self.snapshot())

    def set_state(self, name: str) -> None:
        with self.lock:
            self.state = name
        self._broadcast(self.snapshot())

    def push(self, ev: dict) -> None:
        with self.lock:
            self.events.append(ev)
            if ev.get("problem"):
                self.problem = ev["problem"]
            if ev.get("instruction"):
                self.instruction = ev["instruction"]
            kind = ev.get("kind") or ""
            if kind in {"stuck", "asked_coach"}:
                self.state = "ask"
            elif kind == "coach_instruction":
                self.state = "apply"
            elif kind == "thought":
                self.state = "idle"
            elif kind == "idle_act":
                self.state = "act"
            elif kind == "retrieved":
                pass
            elif kind in {"resumed", "work"}:
                self.state = "act"
        self._broadcast(self.snapshot())

    def load_traj(self, path) -> None:
        traj = Trajectory(path)
        with self.lock:
            self.events = list(traj.entries)
            if traj.entries:
                last = traj.entries[-1]
                self.goal = str(last.get("goal") or self.goal)
                if last.get("problem"):
                    self.problem = str(last["problem"])
                if last.get("instruction"):
                    self.instruction = str(last["instruction"])
                st = str(last.get("state") or "")
                if st:
                    self.state = st

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.subscribers.append(q)
        try:
            q.put_nowait({"type": "hello", **self.snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _broadcast(self, payload: dict) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def run_demo(
    board: LiveBoard,
    root: Path,
    *,
    pace: float = 0.0,
    persist: bool = True,
    idle: bool = False,
    idle_max: Optional[int] = None,
    max_steps: int = 10,
    traj_path: Optional[Path] = None,
) -> dict:
    """Mock-only: safe read, fake remote escalate, coach continue, resume.

    persist defaults ON so the mind log is the same jsonl the loop writes.
    idle is off for the sync smoke demo (must finish); the live UI turns it on.
    """
    if not board.demo_lock.acquire(blocking=False):
        return board.snapshot()
    board.demo_running = True
    try:
        board.reset(DEMO_GOAL)
        push_cmd = "git " + "push" + " origin HEAD"
        worker = MockWorker(
            script=[
                WorkerAction(
                    kind="tool",
                    tool="read",
                    args={"path": "README.md"},
                    thought="先读 README",
                ),
                WorkerAction(
                    kind="tool",
                    tool="shell",
                    args={"cmd": push_cmd},
                    thought="假的远端写入，用来演示求助",
                ),
                WorkerAction(
                    kind="done",
                    thought="已按教练指示继续并完成本地工作",
                ),
            ],
            think_script=[
                WorkerAction(
                    kind="thought",
                    thought="空转：本地还在，不问教练",
                ),
                WorkerAction(
                    kind="tool",
                    tool="read",
                    args={"path": "README.md"},
                    thought="空转：本地读一眼 README",
                ),
                WorkerAction(
                    kind="thought",
                    thought="空转：读完继续想，不问教练",
                ),
            ],
        )
        coach = MockCoach(["continue"])

        def on_state(name: str) -> None:
            board.set_state(name)

        def on_event(ev: dict) -> None:
            board.push(ev)

        loop = ForemanLoop(
            worker=worker,
            coach=coach,
            root=root,
            max_steps=max_steps,
            on_state=on_state,
            on_event=on_event,
            pace=pace,
            persist=persist,
            idle=idle,
            idle_max=idle_max,
            traj_path=traj_path,
        )
        loop.run(DEMO_GOAL)
        return board.snapshot()
    finally:
        board.demo_running = False
        board.demo_lock.release()


def make_handler(board: LiveBoard, root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, code: int = 200) -> None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(code, raw, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path in {"/", "/index.html"}:
                html = load_index_html().encode("utf-8")
                self._send(200, html, "text/html; charset=utf-8")
                return
            if path == "/state":
                self._json(board.snapshot())
                return
            if path == "/demo":
                self._run_demo(qs)
                return
            if path == "/events":
                self._sse()
                return
            self.send_error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/demo":
                self._run_demo(parse_qs(parsed.query))
                return
            self.send_error(404, "not found")

        def _run_demo(self, qs: dict) -> None:
            sync = (qs.get("sync") or ["0"])[0] in {"1", "true", "yes"}
            if "pace" in qs:
                try:
                    pace = float(qs["pace"][0])
                except ValueError:
                    pace = 0.4
            else:
                pace = 0.0 if sync else 0.4
            if sync:
                snap = run_demo(board, root, pace=0.0, persist=True, idle=False)
                self._json(snap)
                return
            threading.Thread(
                target=run_demo,
                kwargs={
                    "board": board,
                    "root": root,
                    "pace": pace,
                    "persist": True,
                    "idle": True,
                    "max_steps": 0,
                },
                daemon=True,
            ).start()
            self._json({"ok": True, "started": True, **board.snapshot()})

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = board.subscribe()
            try:
                while True:
                    try:
                        payload = q.get(timeout=20)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    data = json.dumps(payload, ensure_ascii=False)
                    chunk = f"event: state\ndata: {data}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                board.unsubscribe(q)

    return Handler


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    root: Optional[Path] = None,
):
    board = LiveBoard()
    httpd = ThreadingHTTPServer((host, port), make_handler(board, Path(root or Path.cwd())))
    httpd.daemon_threads = True
    return httpd, board


def start_ui(
    host: str = DEFAULT_HOST,
    port: int = 0,
    root: Optional[Path] = None,
):
    """Start in a background thread. port=0 picks a free port (smoke)."""
    httpd, board = make_server(host, port, root)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return httpd, board, httpd.server_address[1]


def stop_ui(httpd: ThreadingHTTPServer) -> None:
    httpd.shutdown()
    httpd.server_close()


def serve_forever(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    root: Optional[Path] = None,
    auto_demo: bool = True,
) -> int:
    root = Path(root or Path.cwd())
    httpd, board = make_server(host, port, root)
    actual = httpd.server_address[1]
    print(f"本机看板: http://{host}:{actual}/", flush=True)
    print("状态：干活中 / 求助中（正在咨询大模型） / 已收到指示 / 继续 / 空转中 / 自己在想 / 展开原文 / 空转动手", flush=True)
    print("mock 演示无需 API key。空转只在本地想，不问教练。Ctrl+C 退出。", flush=True)
    board.load_traj(default_traj_path(root))
    if auto_demo:
        threading.Thread(
            target=run_demo,
            kwargs={
                "board": board,
                "root": root,
                "pace": 0.4,
                "persist": True,
                "idle": True,
                "max_steps": 0,
            },
            daemon=True,
        ).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()
    return 0
