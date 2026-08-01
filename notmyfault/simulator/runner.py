import sys, copy
from unittest.mock import MagicMock
from notmyfault.simulator.environment import SimulatedEnvironment


class SimulatedRunner:
    def __init__(self, env):
        self.env = env
        self.events = []
        self.engine = None
        self._originals = {}

    def __enter__(self):
        self._setup_mocks()
        return self

    def __exit__(self, *a):
        self.stop()
        self._teardown_mocks()

    def _setup_mocks(self):
        mp = MagicMock()
        mp.NoSuchProcess = type("NSP", (Exception,), {})
        mp.AccessDenied = type("AD", (Exception,), {})
        mp.process_iter = lambda a=None: self._mock_procs(a)
        mp.disk_partitions = lambda a=False: self.env.usb.disk_partitions(a)
        mw = MagicMock()
        mw.user32.EnumWindows = self.env.windows.enum_windows
        mw.user32.IsWindowVisible = self.env.windows.is_window_visible
        mw.user32.GetWindowTextLengthW = self.env.windows.get_window_text_length
        mw.user32.GetWindowTextW = self.env.windows.get_window_text
        mw.user32.GetLastInputInfo = self.env.idle.get_last_input_info
        mw.kernel32.GetTickCount = self.env.idle.get_tick_count
        import datetime as dt
        md = MagicMock()
        md.datetime.now = staticmethod(self.env.time.now)
        md.datetime.strptime = staticmethod(dt.datetime.strptime)
        for name, m in [("psutil", mp), ("datetime", md)]:
            self._originals[name] = sys.modules.get(name)
            sys.modules[name] = m

    def _mock_procs(self, attrs):
        refs = self.env.processes.process_iter(attrs)
        return [type("MockProc", (), {"info": r.info})() for r in refs]

    def _teardown_mocks(self):
        for name, orig in self._originals.items():
            if orig: sys.modules[name] = orig
            else: sys.modules.pop(name, None)
        self._originals.clear()

    def start(self, config=None):
        from notmyfault.core.engine import AutomationEngine
        if config is None:
            from notmyfault.config import DEFAULT_CONFIG
            config = copy.deepcopy(DEFAULT_CONFIG)
        self.engine = AutomationEngine(config, on_event=self._on)
        self.engine._alert_user = lambda *a, **kw: None
        for t in ["process_state","usb_insert","time_schedule","window_title","idle_detect","bluetooth_device"]:
            self.engine.triggers_funcs[t] = lambda m,c,e,se=None: None
            self.engine.triggers_meta[t] = {"semantic": "state"}
        for a in ["set_volume","notify","launch_program","kill_process","lock_screen","run_powershell","bluetooth_toggle"]:
            self.engine.actions_funcs[a] = lambda m,p: None
            self.engine.actions_meta[a] = {}

    def _on(self, et, data):
        self.events.append({"type": et, "data": data})

    def emit(self, tid, payload=None):
        if self.engine:
            self.engine.emit_event(tid, payload or {})

    def step(self, seconds=1.0):
        self.env.time.advance(seconds)

    def stop(self):
        if self.engine:
            self.engine.shutdown()
            self.engine = None

    def last_action(self):
        for ev in reversed(self.events):
            if ev["type"] == "action_executed":
                return ev["data"]
        return None

    def assert_action(self, action_type, params=None):
        last = self.last_action()
        if last is None: return False
        if last.get("action_type") != action_type: return False
        if params is not None and last.get("params") != params: return False
        return True

    def clear_events(self):
        self.events.clear()
