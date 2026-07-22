import asyncio
import builtins
import datetime as dt
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_daily_headlineflag"
API_URL = "https://60s-api.viki.moe/v2/60s"

if not hasattr(builtins, "_ASTRBOT_DAILY_HEADLINE_RUNTIME"):
    builtins._ASTRBOT_DAILY_HEADLINE_RUNTIME = {
        "generation": 0,
        "instance": None,
        "task": None,
    }


@register(
    "astrbot_plugin_daily_headlineflag",
    "ハ·七",
    "QQ官方群每日60秒新闻：检测今日新闻更新后向所有已观察群主动推送一次",
    "1.0.0",
    "",
)
class DailyHeadlineFlagPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.check_interval = max(int(config.get("check_interval_seconds", 300)), 60)
        self.send_interval = max(float(config.get("send_interval_seconds", 5.0)), 3.0)
        self.save_days = max(int(config.get("save_days", 3)), 2)

        self.data_dir = self._resolve_data_dir()
        self.news_dir = self.data_dir / "news"
        self.news_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.state = self._load_state()
        self._save_state()
        self._ready_groups: set[str] = set()

        runtime = builtins._ASTRBOT_DAILY_HEADLINE_RUNTIME
        old_task = runtime.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
            logger.warning("[头条新闻] 已取消跨重载残留任务 id=%s", id(old_task))
        runtime["generation"] = int(runtime.get("generation", 0)) + 1
        self._generation = runtime["generation"]
        runtime["instance"] = self
        self._task = asyncio.create_task(self._monitor_loop())
        runtime["task"] = self._task

        logger.info("[头条新闻] 数据目录: %s", self.data_dir)
        logger.info("[头条新闻] 已登记 QQ 官方群: %d", len(self.state["groups"]))
        logger.info("[头条新闻] 全天每 %d 秒检查新闻更新", self.check_interval)

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            root = Path(get_astrbot_data_path())
        except (ImportError, AttributeError, TypeError):
            root = Path("data").resolve()
        path = root / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _default_state() -> dict:
        return {
            "groups": {},
            "last_detected_date": "",
            "last_detected_hash": "",
        }

    def _load_state(self) -> dict:
        state = self._default_state()
        if self.state_file.exists():
            try:
                loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
                    if not isinstance(state.get("groups"), dict):
                        state["groups"] = {}
            except Exception as exc:
                logger.error("[头条新闻] 状态文件读取失败，保留原文件并使用空内存状态: %s", exc)
        self._import_legacy_groups(state)
        return state

    def _import_legacy_groups(self, state: dict) -> None:
        """只迁移旧插件已观察到的群会话，不迁移活跃/休眠/退订逻辑。"""
        legacy = Path("data/plugin_data/astrbot_plugin_daily_60s_news/news/news_push_data.json").resolve()
        marker = self.data_dir / ".legacy_groups_imported"
        if marker.exists() or not legacy.exists():
            return
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
            count = 0
            for origin in old.get("targets", {}):
                if "GroupMessage" not in str(origin):
                    continue
                state["groups"].setdefault(str(origin), self._new_group_state())
                count += 1
            marker.write_text(str(int(time.time())), encoding="utf-8")
            if count:
                logger.info("[头条新闻] 从旧插件导入 %d 个群会话，平台类型将在发送前校验", count)
        except Exception as exc:
            logger.warning("[头条新闻] 旧群会话导入失败，不影响新群自动发现: %s", exc)

    @staticmethod
    def _new_group_state() -> dict:
        return {
            "discovered_at": int(time.time()),
            "last_seen_at": int(time.time()),
            "last_attempt_date": "",
            "last_attempt_hash": "",
            "last_attempt_at": 0,
            "last_delivery": "NEVER",
            "last_error": "",
        }

    def _save_state(self) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(self.state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, self.state_file)

    def _is_current(self) -> bool:
        runtime = builtins._ASTRBOT_DAILY_HEADLINE_RUNTIME
        return runtime.get("instance") is self and runtime.get("generation") == self._generation

    def _loaded_platforms(self) -> dict:
        manager = getattr(self.context, "platform_manager", None)
        try:
            instances = manager.get_insts() if manager and hasattr(manager, "get_insts") else getattr(manager, "platform_insts", [])
        except Exception:
            instances = []
        result = {}
        for instance in instances or []:
            try:
                meta = instance.meta()
                result[str(meta.id)] = {"name": str(meta.name), "instance": instance}
            except Exception:
                continue
        return result

    def _is_qq_official_group(self, origin: str) -> bool:
        if "GroupMessage" not in origin:
            return False
        platform_id = origin.split(":", 1)[0]
        platform = self._loaded_platforms().get(platform_id)
        return bool(platform and platform.get("name") == "qq_official")

    def _group_ready(self, origin: str) -> bool:
        if origin in self._ready_groups:
            return True
        platform_id = origin.split(":", 1)[0]
        platform = self._loaded_platforms().get(platform_id)
        if not platform or platform.get("name") != "qq_official":
            return False
        session_id = origin.split(":", 2)[-1]
        scenes = getattr(platform.get("instance"), "_session_scene", {})
        if isinstance(scenes, dict) and scenes.get(session_id) == "group":
            self._ready_groups.add(origin)
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe_group(self, event: AstrMessageEvent):
        """群主开启全量消息后，任意群消息都会让该 QQ 官方群自动加入推送列表。"""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._is_qq_official_group(origin):
            return
        self._ready_groups.add(origin)
        now = int(time.time())
        is_new = origin not in self.state["groups"]
        group = self.state["groups"].setdefault(origin, self._new_group_state())
        # 新群立即落盘；已有群最多每小时更新一次 last_seen，避免全量消息造成磁盘写放大。
        if is_new or now - int(group.get("last_seen_at", 0) or 0) >= 3600:
            group["last_seen_at"] = now
            self._save_state()
            if is_new:
                logger.info("[头条新闻] 发现 QQ 官方群: %s", origin)

    @staticmethod
    def _date_from_arg(date_text: str | None) -> dt.date:
        if not date_text:
            return dt.date.today()
        return dt.datetime.strptime(str(date_text), "%Y%m%d").date()

    def _image_path(self, date_value: dt.date) -> Path:
        return self.news_dir / f"{date_value.strftime('%Y%m%d')}.jpg"

    @staticmethod
    def _valid_image_bytes(raw: bytes) -> bool:
        if len(raw) < 1000:
            return False
        return raw.startswith(b"\xff\xd8") or raw.startswith(b"\x89PNG\r\n\x1a\n")

    async def _fetch_news_image(self, date_value: dt.date, force: bool = False) -> tuple[Path, str]:
        path = self._image_path(date_value)
        if path.exists() and not force:
            raw = path.read_bytes()
            if self._valid_image_bytes(raw):
                return path, hashlib.sha256(raw).hexdigest()

        params = {
            "date": date_value.strftime("%Y-%m-%d"),
            "encoding": "image-proxy",
        }
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        headers = {"User-Agent": "Mozilla/5.0 (AstrBot DailyHeadlineFlag/1.0)"}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True, headers=headers) as session:
            async with session.get(API_URL, params=params) as response:
                if response.status != 200:
                    raise RuntimeError(f"NEWS_API_HTTP_{response.status}")
                raw = await response.read()
        if not self._valid_image_bytes(raw):
            raise RuntimeError("NEWS_API_INVALID_IMAGE")
        tmp = path.with_suffix(".jpg.tmp")
        with open(tmp, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        return path, hashlib.sha256(raw).hexdigest()

    async def _detect_current_news(self) -> tuple[Path, str] | None:
        today = dt.date.today()
        yesterday = today - dt.timedelta(days=1)
        try:
            _, yesterday_hash = await self._fetch_news_image(yesterday)
            today_path, today_hash = await self._fetch_news_image(today, force=True)
        except Exception as exc:
            logger.warning("[头条新闻] 新闻检测失败: %s", exc)
            return None
        if today_hash == yesterday_hash:
            logger.info("[头条新闻] 今日图片仍与昨日相同，等待下次检查 hash=%s", today_hash[:12])
            return None
        self.state["last_detected_date"] = today.isoformat()
        self.state["last_detected_hash"] = today_hash
        self._save_state()
        return today_path, today_hash

    async def _push_news(self, image_path: Path, news_hash: str) -> None:
        candidates = []
        today_text = dt.date.today().isoformat()
        for origin, group in self.state["groups"].items():
            # 同一自然日最多主动尝试一次；即使 API 当天修订图片也不二次群发。
            if group.get("last_attempt_date") == today_text:
                continue
            if not self._is_qq_official_group(origin):
                continue
            if not self._group_ready(origin):
                logger.info("[头条新闻] 群尚未就绪，保留待推状态: %s", origin)
                continue
            candidates.append(origin)

        if not candidates:
            return
        logger.info("[头条新闻] 新闻已更新，准备向 %d 个 QQ 官方群推送", len(candidates))
        for index, origin in enumerate(candidates):
            if not self._is_current():
                logger.warning("[头条新闻] 任务实例已失效，中止群发")
                return
            group = self.state["groups"][origin]
            # 先持久化尝试标记；即使 API 超时也不自动重复同一新闻，避免群发事故。
            group["last_attempt_date"] = today_text
            group["last_attempt_hash"] = news_hash
            group["last_attempt_at"] = int(time.time())
            group["last_delivery"] = "ATTEMPTING"
            group["last_error"] = ""
            self._save_state()
            try:
                chain = MessageChain().message("每日60秒新闻：").file_image(str(image_path))
                sent = bool(await self.context.send_message(origin, chain))
                group["last_delivery"] = "SUCCESS" if sent else "FAILED"
                group["last_error"] = "" if sent else "SEND_RETURNED_FALSE"
                if sent:
                    logger.info("[头条新闻] ✅ %s (%d/%d)", origin, index + 1, len(candidates))
                else:
                    logger.error("[头条新闻] ❌ %s 返回失败", origin)
            except Exception as exc:
                group["last_delivery"] = "FAILED"
                group["last_error"] = type(exc).__name__
                logger.error("[头条新闻] ❌ %s 发送异常: %s", origin, exc)
            self._save_state()
            if index < len(candidates) - 1:
                await asyncio.sleep(self.send_interval)

    async def _check_once(self) -> None:
        detected = await self._detect_current_news()
        if detected:
            await self._push_news(*detected)
        await self._cleanup_images()

    async def _cleanup_images(self) -> None:
        cutoff = dt.date.today() - dt.timedelta(days=self.save_days)
        for path in self.news_dir.glob("*.jpg"):
            try:
                file_date = dt.datetime.strptime(path.stem, "%Y%m%d").date()
                if file_date < cutoff:
                    path.unlink()
            except Exception:
                continue

    async def _monitor_loop(self) -> None:
        while self._is_current():
            try:
                await self._check_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[头条新闻] 监控循环异常: %s", exc)
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    @filter.command("新闻", alias={"早报", "news"})
    async def news_command(self, event: AstrMessageEvent, date_text: str | None = None):
        """查看今日或指定日期新闻。用法：/新闻 或 /新闻 20260701"""
        try:
            date_value = self._date_from_arg(date_text)
        except ValueError:
            yield event.plain_result("日期格式错误，请使用 YYYYMMDD，例如：/新闻 20260701")
            return
        try:
            path, _ = await self._fetch_news_image(date_value)
        except Exception as exc:
            yield event.plain_result(f"新闻获取失败：{exc}")
            return
        yield event.image_result(str(path))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("新闻状态")
    async def status_command(self, event: AstrMessageEvent):
        """查看新闻监控和 QQ 官方群投递状态。"""
        success = sum(1 for group in self.state["groups"].values() if group.get("last_delivery") == "SUCCESS")
        failed = sum(1 for group in self.state["groups"].values() if group.get("last_delivery") == "FAILED")
        ready = sum(1 for origin in self.state["groups"] if self._group_ready(origin))
        yield event.plain_result(
            f"📰 头条新闻状态\n"
            f"检查间隔：{self.check_interval} 秒\n"
            f"已登记官方群：{len(self.state['groups'])}\n"
            f"本次启动已就绪：{ready}\n"
            f"最近成功：{success} / 最近失败：{failed}\n"
            f"最近新闻日期：{self.state.get('last_detected_date') or '尚未检测'}\n"
            f"最近内容哈希：{(self.state.get('last_detected_hash') or '')[:12] or '无'}"
        )

    async def terminate(self):
        runtime = builtins._ASTRBOT_DAILY_HEADLINE_RUNTIME
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if runtime.get("instance") is self:
            runtime["instance"] = None
            runtime["task"] = None
        self._save_state()
        logger.info("[头条新闻] 插件已停止")
