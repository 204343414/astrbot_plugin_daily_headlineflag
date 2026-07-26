import asyncio
import builtins
import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from . import qq_group_event_bridge

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
    "QQ官方群每日60秒新闻：仅向主动订阅并通过主动消息测试的群推送",
    "1.2.0",
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
        qq_group_event_bridge.install(PLUGIN_NAME, self._on_group_del_robot)

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
                    # Legacy groups were discovered automatically. Explicitly
                    # keep them unsubscribed until /新闻 订阅此群 succeeds.
                    for group in state["groups"].values():
                        if isinstance(group, dict):
                            group.setdefault("subscribed", False)
            except Exception as exc:
                logger.error("[头条新闻] 状态文件读取失败，保留原文件并使用空内存状态: %s", exc)
        return state

    @staticmethod
    def _new_group_state() -> dict:
        return {
            "subscribed": False,
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

    def _prune_non_official_groups(self) -> int:
        """仅在已确认加载 QQ Official 平台时清除旧平台历史目标。"""
        platforms = self._loaded_platforms()
        official_ids = {platform_id for platform_id, data in platforms.items() if data.get("name") == "qq_official"}
        if not official_ids:
            return 0
        removed = 0
        for origin in list(self.state["groups"]):
            platform_id = str(origin).split(":", 1)[0]
            if "GroupMessage" not in str(origin) or platform_id not in official_ids:
                del self.state["groups"][origin]
                self._ready_groups.discard(origin)
                removed += 1
        if removed:
            self._save_state()
            logger.warning("[头条新闻] 已清理 %d 个非当前 QQ 官方平台的历史群目标", removed)
        return removed

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
        """Only remember runtime readiness; ordinary messages never subscribe a group."""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if self._is_qq_official_group(origin):
            self._ready_groups.add(origin)

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

    @staticmethod
    def _is_active_message_permission_error(error: object) -> bool:
        """Only match QQ's explicit proactive-message permission rejection."""
        text = re.sub(r"\s+", "", str(error or "")).lower()
        return "主动消息失败" in text and "无权限" in text

    async def _push_news(self, image_path: Path, news_hash: str) -> None:
        candidates = []
        today_text = dt.date.today().isoformat()
        for origin, group in self.state["groups"].items():
            if not bool(group.get("subscribed", False)):
                continue
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
                # AstrBot send_message() succeeds by returning None on the QQ
                # adapter. Only an exception means failure; bool(None) is not
                # a delivery signal.
                await self.context.send_message(origin, chain)
                group["last_delivery"] = "SUCCESS"
                group["last_error"] = ""
                logger.info("[头条新闻] ✅ %s (%d/%d)", origin, index + 1, len(candidates))
            except Exception as exc:
                group["last_delivery"] = "FAILED"
                group["last_error"] = str(exc)[:500] or type(exc).__name__
                if self._is_active_message_permission_error(exc):
                    # Only QQ's explicit permission rejection removes the group
                    # from the active subscription list. Transient failures keep it.
                    group["subscribed"] = False
                    group["unsubscribed_reason"] = "ACTIVE_MESSAGE_PERMISSION_DENIED"
                    group["unsubscribed_at"] = int(time.time())
                    logger.warning(
                        "[头条新闻] 主动消息无权限，已自动移出订阅名单: %s error=%s",
                        origin,
                        exc,
                    )
                else:
                    logger.error(
                        "[头条新闻] ❌ %s 临时发送异常，保留订阅: %s",
                        origin,
                        exc,
                    )
            self._save_state()
            if index < len(candidates) - 1:
                await asyncio.sleep(self.send_interval)

    async def _check_once(self) -> None:
        self._prune_non_official_groups()
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

    async def _subscribe_current_group(self, event: AstrMessageEvent):
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._is_qq_official_group(origin):
            yield event.plain_result("仅支持在 QQ 官方群内订阅新闻。")
            return
        existing = self.state["groups"].get(origin)
        if isinstance(existing, dict) and bool(existing.get("subscribed", False)):
            yield event.plain_result("该群已加入每日新闻订阅，无需重复订阅。")
            return

        self._ready_groups.add(origin)
        try:
            # This message itself is the proactive-send capability probe and
            # the sole success response. Do not send a news card here.
            await self.context.send_message(
                origin,
                MessageChain().message("✅ 新闻订阅成功，今后将向本群主动推送每日60秒新闻。"),
            )
        except Exception as exc:
            logger.warning("[头条新闻] 主动消息订阅测试失败 %s: %s", origin, exc)
            yield event.plain_result(
                "订阅失败，请@群主开启 Bot 的“机器人主动在群聊内发言”功能后重试。"
            )
            return

        now = int(time.time())
        group = self.state["groups"].setdefault(origin, self._new_group_state())
        group.update(
            {
                "subscribed": True,
                "last_seen_at": now,
                "last_delivery": "NEVER",
                "last_error": "",
            }
        )
        group.pop("unsubscribed_reason", None)
        group.pop("unsubscribed_at", None)
        self._save_state()
        logger.info("[头条新闻] 当前群订阅并通过主动消息测试: %s", origin)

    @filter.command("新闻订阅", alias={"订阅新闻"})
    async def subscribe_news_command(self, event: AstrMessageEvent):
        """订阅当前 QQ 官方群，并立即验证一次主动消息。"""
        event.stop_event()
        async for result in self._subscribe_current_group(event):
            yield result

    @filter.command("新闻取消订阅", alias={"取消新闻订阅"})
    async def unsubscribe_news_command(self, event: AstrMessageEvent):
        """仅取消当前 QQ 官方群的每日新闻主动推送。"""
        event.stop_event()
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._is_qq_official_group(origin):
            yield event.plain_result("仅支持在 QQ 官方群内取消新闻订阅。")
            return
        group = self.state["groups"].get(origin)
        if not isinstance(group, dict) or not bool(group.get("subscribed", False)):
            yield event.plain_result("该群当前没有订阅每日新闻。")
            return
        group["subscribed"] = False
        group["last_seen_at"] = int(time.time())
        self._save_state()
        logger.info("[头条新闻] 当前群已取消订阅: %s", origin)
        yield event.plain_result("✅ 已取消本群的每日新闻订阅。")

    @filter.command("新闻", alias={"早报", "news"})
    async def news_command(self, event: AstrMessageEvent, date_text: str | None = None):
        """查看新闻或订阅当前群。用法：/新闻 [YYYYMMDD|订阅此群]"""
        if str(date_text or "").strip() == "订阅此群":
            event.stop_event()
            async for result in self._subscribe_current_group(event):
                yield result
            return
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
        self._prune_non_official_groups()
        subscribed = sum(
            1
            for group in self.state["groups"].values()
            if bool(group.get("subscribed", False))
        )
        success = sum(1 for group in self.state["groups"].values() if group.get("last_delivery") == "SUCCESS")
        failed = sum(1 for group in self.state["groups"].values() if group.get("last_delivery") == "FAILED")
        ready = sum(1 for origin in self.state["groups"] if self._group_ready(origin))
        yield event.plain_result(
            f"📰 头条新闻状态\n"
            f"检查间隔：{self.check_interval} 秒\n"
            f"已订阅官方群：{subscribed}\n"
            f"本次启动已就绪：{ready}\n"
            f"最近成功：{success} / 最近失败：{failed}\n"
            f"最近新闻日期：{self.state.get('last_detected_date') or '尚未检测'}\n"
            f"最近内容哈希：{(self.state.get('last_detected_hash') or '')[:12] or '无'}"
        )

    async def _on_group_del_robot(self, client, event) -> None:
        group_openid = str(getattr(event, "group_openid", "") or "").strip()
        platform = getattr(client, "platform", None)
        if not group_openid or platform is None:
            return
        origin = f"{platform.meta().id}:GroupMessage:{group_openid}"
        removed = self.state["groups"].pop(origin, None)
        self._ready_groups.discard(origin)
        if removed is not None:
            self._save_state()
            logger.warning("[头条新闻] Bot被移出群，已清空订阅目标: %s", origin)
        else:
            logger.info("[头条新闻] Bot被移出未订阅群: %s", origin)

    async def terminate(self):
        qq_group_event_bridge.detach(PLUGIN_NAME)
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
