"""凌晨不能把「昨天的报纸」当成今天的新报纸推出去。

真实事故
--------
2026-11-17 00:11，插件在群里推了一张标注「2026年11月16日 星期一」的报纸。

原因不是新闻源拖更，而是它的一种**静默回退**：

    请求 https://60s.crystelf.top/v2/60s?date=2026-11-18   （一个还不存在的日期）
    返回 HTTP 200，图片却是 2026-11-16 的那份

也就是说「图片哈希变了」只能证明内容变了，**不能证明这是今天的那份**。
旧实现只做 `today_hash == yesterday_hash` 比较：凌晨当天报纸还没发布、
上游把 11-16 那份顶上来，哈希从「11-15 那份」变成「11-16 那份」，
看着像"新闻更新了"，于是把昨天的报纸推了出去。

关键事实：**JSON 里的 data.date / data.created 是诚实的**，只有图片接口会回退。
所以第一道闸必须是"问上游这份报纸是哪天的"，而不是"看图片变没变"。

另外两个必须有测试兜住的点：
  * 图片优先用 JSON 里按日期写死的直链（.../static/images/YYYY-MM-DD.png），
    它不会像 image-proxy 那样被旧报纸的缓存顶掉；
  * 去重按「日期 + 内容哈希」。旧的「今天尝试过就不再推」会让凌晨那次误推
    把当天真正的报纸永远挡在门外。
"""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 复用 test_pending_report.py 里已经写好的 AstrBot 桩，避免出现第二套会腐烂的假 API。
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pending_report import _load_plugin_module  # noqa: E402


TODAY = dt.date.today()
TODAY_ISO = TODAY.isoformat()
TODAY_SLASH = TODAY.strftime("%Y/%m/%d")
YESTERDAY_ISO = (TODAY - dt.timedelta(days=1)).isoformat()


def _meta(date_text, created):
    return {
        "date": date_text,
        "created": created,
        "image": f"https://example.invalid/static/images/{date_text}.png",
    }


class _FakePlugin:
    """只带上 _detect_current_news 需要的状态，网络与磁盘全部替身。"""

    def __init__(self, meta, image_hash, last_hash=""):
        # 插件把这几项挂在 self.state 上，名字要和 main.py 保持一致
        self.state = {"last_detected_hash": last_hash, "last_detected_date": ""}
        self._meta = meta
        self._image_hash = image_hash
        self.saved = 0

    # --- 被测代码用到的接口 ---
    def _save_state(self):
        self.saved += 1

    def _image_path(self, date_value):
        return "unused"

    async def _edition_meta(self, date_value):
        return self._meta

    async def _fetch_edition_image(self, date_value, edition_meta, force=False):
        return "unused", self._image_hash


@pytest.fixture
def detect():
    module = _load_plugin_module()
    return module.DailyHeadlineFlagPlugin._detect_current_news


def _run(detect, meta, image_hash, last_hash=""):
    plugin = _FakePlugin(meta, image_hash, last_hash)
    result = asyncio.run(detect(plugin))
    return result, plugin


def test_yesterday_edition_at_midnight_is_not_pushed(detect):
    """核心回归：凌晨上游手上只有昨天的报纸时，必须按住不推。"""
    result, plugin = _run(
        detect, _meta(YESTERDAY_ISO, "2026/11/16 05:30"), "hash-of-yesterday"
    )
    assert result is None, "上游手上还是昨天的报纸，绝不能推"
    assert plugin.state["last_detected_hash"] == "", "不能把昨天的哈希记成'今天已推'"


def test_stale_created_timestamp_is_also_refused(detect):
    """date 字段被镜像凑巧写成今天、但 created 还是旧的时候，同样要按住。"""
    result, _ = _run(detect, _meta(TODAY_ISO, "2026/11/16 05:30"), "fresh-hash")
    assert result is None


def test_todays_edition_is_pushed_once(detect):
    """今天真正的报纸出现 -> 推一次，并记住哈希。"""
    result, plugin = _run(detect, _meta(TODAY_ISO, f"{TODAY_SLASH} 05:31"), "today-hash")
    assert result is not None
    assert plugin.state["last_detected_hash"] == "today-hash"
    assert plugin.state["last_detected_date"] == TODAY_ISO


def test_same_content_is_not_repushed(detect):
    """同一天里内容没变 -> 每 5 分钟检查一次也不能重复推。"""
    result, _ = _run(
        detect, _meta(TODAY_ISO, f"{TODAY_SLASH} 05:31"), "today-hash", "today-hash"
    )
    assert result is None


def test_real_edition_lands_after_a_midnight_mistake(detect):
    """凌晨误推了旧报纸，当天真报纸发布后必须还能补推（哈希不同即可）。"""
    result, plugin = _run(
        detect, _meta(TODAY_ISO, f"{TODAY_SLASH} 05:31"), "real-today-hash", "yesterday-hash"
    )
    assert result is not None
    assert plugin.state["last_detected_hash"] == "real-today-hash"


def test_unreachable_meta_skips_instead_of_guessing(detect):
    """读不到报纸信息就跳过，绝不能拿"看起来像新图"的东西去推。"""
    result, plugin = _run(detect, None, "whatever-hash")
    assert result is None
    assert plugin.state["last_detected_hash"] == ""


def test_push_dedup_is_keyed_by_date_and_hash():
    """去重条件必须是「同一日期 + 同一份内容」，而不是「同一日期」。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'group.get("last_attempt_hash") == news_hash' in source, (
        "去重必须比对内容哈希，否则凌晨误推之后当天真报纸永远推不出去"
    )


def test_image_prefers_the_date_pinned_direct_link():
    """JSON 的 image 字段是按日期写死的直链，必须优先用它。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_fetch_edition_image" in source
    assert 'meta.get("image")' in source, "应优先使用上游给的按日期固定直链"


def test_detection_never_compares_against_yesterdays_file():
    """明确守住"别再退回旧实现"：检测入口不该再拉昨天的图做对比。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    detect_body = source.split("async def _detect_current_news", 1)[1].split(
        "    @staticmethod", 1
    )[0]
    assert "_fetch_news_image(yesterday)" not in detect_body, (
        "旧实现用昨天的图片做基线，凌晨会被上游的旧报纸顶掉而误判"
    )
