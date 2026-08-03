"""The "群尚未就绪" notice must not flood the console.

Why this file exists
--------------------
_group_ready() depends on the qq_official adapter's ``_session_scene`` dict,
which is only written when somebody *speaks* in the group
(qqofficial_platform_adapter.py: ``remember_session_scene(abm.session_id,
"group")``). After a restart it is empty, so a subscribed group stays "not
ready" until the next message there.

Meanwhile _monitor_loop() re-runs every ``check_interval`` (default 300s), and
_detect_current_news() has no once-per-day short circuit -- once the news
updates it returns a result on every cycle for the rest of the day. The notice
used to be logged inside the per-group loop, so N groups produced N lines every
five minutes: 10 groups = 2880 identical lines a day, none of them events.

These tests pin the throttle without importing AstrBot.
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_module():
    """Import main.py with AstrBot and aiohttp stubbed out."""
    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        event = types.ModuleType("astrbot.api.event")
        star = types.ModuleType("astrbot.api.star")

        class _Logger:
            def __init__(self):
                self.lines = []

            def _record(self, level, template, *args):
                self.lines.append((level, template % args if args else template))

            def info(self, template, *args):
                self._record("info", template, *args)

            def warning(self, template, *args):
                self._record("warning", template, *args)

            def error(self, template, *args):
                self._record("error", template, *args)

            def exception(self, template, *args):
                self._record("exception", template, *args)

        api.logger = _Logger()
        api.AstrBotConfig = dict

        class _Anything:
            """Any attribute is a no-op decorator or an opaque enum member.

            Enumerating the decorators main.py happens to use today would make
            this stub a second source of truth that silently rots; the tests
            care about the throttle, not about the filter API surface.
            """

            def __getattr__(self, name):
                return _Anything()

            def __call__(self, *args, **kwargs):
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                return lambda fn: fn

            def __or__(self, other):
                return self

        event.filter = _Anything()
        event.AstrMessageEvent = object
        event.MessageChain = object
        star.Context = object
        star.Star = object
        star.register = lambda *a, **k: (lambda cls: cls)

        astrbot.api = api
        sys.modules.update({
            "astrbot": astrbot, "astrbot.api": api,
            "astrbot.api.event": event, "astrbot.api.star": star,
        })
    if "aiohttp" not in sys.modules:
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")

    import importlib.util

    package = types.ModuleType("dhf")
    package.__path__ = [str(ROOT)]
    sys.modules["dhf"] = package
    bridge_spec = importlib.util.spec_from_file_location(
        "dhf.qq_group_event_bridge", ROOT / "qq_group_event_bridge.py")
    bridge = importlib.util.module_from_spec(bridge_spec)
    sys.modules["dhf.qq_group_event_bridge"] = bridge
    bridge_spec.loader.exec_module(bridge)

    spec = importlib.util.spec_from_file_location("dhf.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dhf.main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reporter():
    """A bare object carrying just the reporter's state, plus the log sink."""
    module = _load_plugin_module()
    sink = sys.modules["astrbot.api"].logger
    sink.lines.clear()

    instance = object.__new__(module.DailyHeadlineFlagPlugin)
    instance.pending_report_interval = 3600
    instance._pending_reported_at = 0.0
    instance._pending_reported_key = frozenset()
    return instance, sink


G1 = "qq_official:GroupMessage:AAAA1111"
G2 = "qq_official:GroupMessage:BBBB2222"


def test_one_line_covers_every_waiting_group(reporter):
    """N groups must not mean N log lines -- that was the whole complaint."""
    plugin, sink = reporter
    plugin._report_pending([G1, G2, "qq_official:GroupMessage:CCCC3333"])
    assert len(sink.lines) == 1
    assert "3 个群尚未就绪" in sink.lines[0][1]


def test_repeat_cycles_stay_silent_within_the_interval(reporter):
    """The steady state is re-evaluated every 5 minutes; say it once an hour."""
    plugin, sink = reporter
    for _ in range(12):          # one hour of 5-minute cycles
        plugin._report_pending([G1])
    assert len(sink.lines) == 1, "同一批未就绪群在间隔内只应提示一次"


def test_the_notice_returns_after_the_interval_elapses(reporter):
    """Silence must not be permanent: a stuck subscription still gets said."""
    import time

    plugin, sink = reporter
    plugin._report_pending([G1])
    plugin._pending_reported_at = time.time() - 3601
    plugin._report_pending([G1])
    assert len(sink.lines) == 2


def test_a_changed_group_set_is_reported_immediately(reporter):
    """A change is an event, not a steady state -- do not throttle it."""
    plugin, sink = reporter
    plugin._report_pending([G1, G2])
    plugin._report_pending([G1])          # G2 became ready
    assert len(sink.lines) == 2
    assert "1 个群尚未就绪" in sink.lines[1][1]


def test_becoming_fully_ready_is_announced_once(reporter):
    plugin, sink = reporter
    plugin._report_pending([G1])
    plugin._report_pending([])
    assert sink.lines[1][1] == "[头条新闻] 所有订阅群均已就绪"
    for _ in range(20):
        plugin._report_pending([])
    assert len(sink.lines) == 2, "全部就绪后不应反复刷屏"


def test_nothing_is_logged_when_nothing_was_ever_pending(reporter):
    """The common case -- healthy install, no groups waiting -- is silent."""
    plugin, sink = reporter
    for _ in range(20):
        plugin._report_pending([])
    assert sink.lines == []


def test_the_notice_never_prints_a_raw_origin(reporter):
    """Only the group's short id, not the full platform:type:openid triple."""
    plugin, sink = reporter
    plugin._report_pending([G1])
    assert G1 not in sink.lines[0][1]
    assert "AAAA1111" in sink.lines[0][1]


def test_push_news_reports_pending_groups_exactly_once_per_cycle():
    """Structural: the notice must live outside the per-group loop."""
    source = (ROOT / "main.py").read_text("utf-8")
    push = source[source.index("async def _push_news"):source.index("def _report_pending")]
    loop_body = push[push.index("for origin, group in"):push.index("self._report_pending")]
    assert "logger." not in loop_body, "逐群日志会随订阅数放大，必须聚合后再输出"
    assert "pending.append(origin)" in loop_body


def test_a_long_subscriber_list_does_not_produce_a_giant_line(reporter):
    """The count is the actionable part; ids only identify a stuck group."""
    plugin, sink = reporter
    plugin._report_pending([f"qq_official:GroupMessage:G{i:04d}" for i in range(40)])
    line = sink.lines[0][1]
    assert "40 个群尚未就绪" in line
    assert line.count("G00") == 5, "最多列出 5 个群 id"
    assert len(line) < 200


def test_a_full_day_of_cycles_logs_hourly_not_per_group(reporter):
    """End-to-end cadence check: 288 cycles x 10 groups must not be 2880 lines."""
    import time

    plugin, sink = reporter
    groups = [f"qq_official:GroupMessage:G{i:04d}" for i in range(10)]
    real = time.time
    try:
        for cycle in range(288):          # 24h at the default 300s interval
            time.time = lambda c=cycle: c * 300.0
            plugin._report_pending(groups)
    finally:
        time.time = real
    assert len(sink.lines) == 24, f"一天应约 24 条，实际 {len(sink.lines)}"
