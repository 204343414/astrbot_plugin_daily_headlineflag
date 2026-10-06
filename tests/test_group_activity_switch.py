"""「群里有人说话才推送」必须是一个可关的开关，而且关掉后真的不再看群活跃度。

背景
----
_group_ready() 依赖 qq_official 适配器的 ``_session_scene``，而它只在群内有人发言
时才写入（见 test_pending_report.py 的说明）。这意味着 Bot 每次重启后，已经订阅好、
主动消息权限也验过的群，仍要被"等群友说一句话"卡住一段时间——对只想要定时推送的
部署来说，这个前置条件是没有必要的。

于是新增配置项 ``push_requires_group_activity``（默认 true，行为不变）：

- true  ：保持原样，_session_scene / _ready_groups 说了算；
- false ：不再检查群活跃度，QQ 官方平台在跑且该群已订阅即视为可推。

关掉开关**不等于**跳过主动消息权限：订阅时那条确认消息本身是探针，推送时 QQ 明确
回「主动消息失败/无权限」仍然会把该群移出订阅名单。这里只钉住"活跃度"这一层。

这些测试沿用 test_pending_report.py 的 AstrBot stub 加载器，不依赖真实框架。
"""
import json

import pytest

from test_pending_report import G1, ROOT, _load_plugin_module


@pytest.fixture
def plugin():
    """只带 _group_ready 所需状态的最小实例。"""
    module = _load_plugin_module()
    sys = __import__("sys")
    sys.modules["astrbot.api"].logger.lines.clear()
    instance = object.__new__(module.DailyHeadlineFlagPlugin)
    instance._ready_groups = set()
    return instance


def _install_platform(plugin, name="qq_official", session_scene=None):
    """把 _loaded_platforms() 换成固定答案，绕开真实平台管理器。"""
    import types

    adapter = types.SimpleNamespace(_session_scene=dict(session_scene or {}))
    plugin._loaded_platforms = lambda: {
        "qq_official": {"name": name, "instance": adapter}
    }


def test_the_switch_defaults_to_the_existing_behaviour():
    """默认必须是 true：升级不该悄悄改变别人的推送时机。"""
    schema = json.loads((ROOT / "_conf_schema.json").read_text("utf-8"))
    option = schema["push_requires_group_activity"]
    assert option["type"] == "bool"
    assert option["default"] is True


def test_the_plugin_reads_the_switch_from_the_config():
    """开关要真的接上线，而不是只写在 schema 里。"""
    source = (ROOT / "main.py").read_text("utf-8")
    assert 'config.get("push_requires_group_activity", True)' in source
    assert "config.get(" in source


def test_gate_on_still_waits_for_a_group_message(plugin):
    """开关为 true 时维持原行为：没人说话就不算就绪。"""
    plugin.require_group_activity = True
    _install_platform(plugin)
    assert plugin._group_ready(G1) is False


def test_gate_on_accepts_the_platform_scene_without_this_process_seen_a_message(plugin):
    """热重载后适配器里已有的 scene 仍然算数（原有行为，别被新开关改坏）。"""
    plugin.require_group_activity = True
    _install_platform(plugin, session_scene={"AAAA1111": "group"})
    assert plugin._group_ready(G1) is True
    assert G1 in plugin._ready_groups


def test_gate_off_is_ready_without_any_group_message(plugin):
    """关掉后：没人说话也直接算可推 —— 这就是这个开关的全部意义。"""
    plugin.require_group_activity = False
    _install_platform(plugin)
    assert plugin._group_ready(G1) is True


def test_gate_off_does_not_cache_readiness(plugin):
    """不写缓存：运行中把开关改回 true 时，不能让上一轮的"就绪"残留下来继续推。"""
    plugin.require_group_activity = False
    _install_platform(plugin)
    for _ in range(3):
        assert plugin._group_ready(G1) is True
    assert plugin._ready_groups == set()


def test_gate_off_still_needs_the_qq_official_platform_loaded(plugin):
    """平台没加载 / 不是 qq_official 时一律不就绪：开关只关活跃度，不关平台检查。"""
    plugin.require_group_activity = False
    _install_platform(plugin, name="aiocqhttp")
    assert plugin._group_ready(G1) is False


def test_status_command_reports_the_switch():
    """运维要能一眼看出当前是哪一种模式。"""
    source = (ROOT / "main.py").read_text("utf-8")
    assert "群活跃检查：" in source
