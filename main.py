import asyncio
import datetime
import os
import time
import traceback
from pathlib import Path
from typing import Tuple

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
import json
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

SAVED_NEWS_DIR = Path("data", "plugin_data", "astrbot_plugin_daily_60s_news", "news")
SAVED_NEWS_DIR.mkdir(parents=True, exist_ok=True)


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _parse_msg_origin(unified_id: str) -> dict:
    """
    解析 unified_msg_origin，提取平台、消息类型、ID
    格式: 前缀:中缀:后缀
    例: aiocqhttp:GroupMessage:987654321
        wechatpadpro:GroupMessage:123456789@chatroom
    """
    parts = unified_id.split(":", 2)  # 最多分3段（后缀可能包含冒号）
    result = {
        "platform": parts[0] if len(parts) > 0 else "unknown",
        "msg_type": parts[1] if len(parts) > 1 else "unknown",
        "target_id": parts[2] if len(parts) > 2 else "unknown",
    }
    return result


def _is_group(unified_id: str) -> bool:
    """判断是否为群聊"""
    return "GroupMessage" in unified_id


def _is_private(unified_id: str) -> bool:
    """判断是否为私聊"""
    return "FriendMessage" in unified_id


def _is_other(unified_id: str) -> bool:
    """判断是否为其他类型"""
    return "OtherMessage" in unified_id


def _get_type_label(unified_id: str) -> str:
    """获取可读的类型标签"""
    if _is_group(unified_id):
        return "群聊"
    elif _is_private(unified_id):
        return "私聊"
    elif _is_other(unified_id):
        return "其他"
    else:
        return "未知"


@register(
    "每日60s读懂世界",
    "eaton",
    "AstrBot 每日60s新闻插件。自动检测活跃会话推送，群聊私聊通用。",
    "0.0.5",
)
class Daily60sNewsPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 新闻源配置
        self.news_type = config.get("news_type", "indirect")
        self.news_path = SAVED_NEWS_DIR
        if self.news_type == "vikiboss_api":
            self.api = self.config.vikiboss_api
        elif self.news_type == "indirect":
            self.api = self.config.indirect
            self.img_key = self.config.get("img_key", "imageUrl")
            self.date_key = self.config.get("date_key", "datatime")
        elif self.news_type == "direct":
            self.img_url = self.config.direct

        # 推送时间窗口
        self.push_start_time = self.config.get("push_start_time", "07:00")
        self.push_end_time = self.config.get("push_end_time", "07:15")
        self.push_time = self.config.get("push_time", self.push_start_time)
        self.min_push_interval = self.config.get("min_push_interval", 5.0)

        # ========= 核心数据 =========
        self.data_file = SAVED_NEWS_DIR / "news_push_data.json"
        self._data = {"targets": {}}
        self._load_data()

        # ========= 内存活跃集合（解决 Bug4：减少文件I/O） =========
        # 只在内存中记录，定时批量写入文件
        self._pending_active: set = set()
        self._last_save_time: float = time.time()
        self._save_interval: float = 60.0  # 最多60秒写一次文件

        logger.info(f"[每日新闻] 已加载，已知目标: {len(self._data['targets'])} 个")
        logger.info(f"[每日新闻] 推送窗口: {self.push_start_time} ~ {self.push_end_time}")

        self._monitoring_task = asyncio.create_task(self._daily_task())

    # ==================== 数据持久化 ====================

    def _load_data(self):
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if "targets" not in self._data:
                    self._data["targets"] = {}
            except Exception as e:
                logger.error(f"加载推送数据失败: {e}")
                self._data = {"targets": {}}

        # 兼容旧版迁移
        old_file = SAVED_NEWS_DIR / "push_groups.json"
        if old_file.exists():
            try:
                with open(old_file, "r", encoding="utf-8") as f:
                    old_groups = json.load(f)
                count = 0
                for g in old_groups:
                    if g not in self._data["targets"]:
                        self._data["targets"][g] = self._make_default_target(g, pre_active=True)
                        count += 1
                if count > 0:
                    self._save_data()
                    logger.info(f"[每日新闻] 从旧版迁移了 {count} 个目标")
                old_file.rename(old_file.with_suffix(".json.bak"))
            except Exception as e:
                logger.error(f"迁移旧数据失败: {e}")

    def _save_data(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            self._last_save_time = time.time()
        except Exception as e:
            logger.error(f"保存推送数据失败: {e}")

    def _save_data_throttled(self):
        """
        节流保存：距离上次保存超过 _save_interval 秒才真正写文件
        解决 Bug4：避免每条消息都写文件
        """
        now = time.time()
        if now - self._last_save_time >= self._save_interval:
            self._save_data()

    def _flush_pending_active(self):
        """
        将内存中的活跃记录批量写入数据
        在推送前、插件卸载时调用
        """
        if not self._pending_active:
            return
        for uid in self._pending_active:
            if uid in self._data["targets"]:
                self._data["targets"][uid]["active_after_push"] = True
                if self._data["targets"][uid].get("dormant"):
                    self._data["targets"][uid]["dormant"] = False
                    logger.info(f"[每日新闻] {_get_type_label(uid)} {uid} 从休眠恢复")
        self._pending_active.clear()
        self._save_data()

    def _make_default_target(self, unified_id: str, pre_active: bool = False) -> dict:
        """
        创建默认的目标数据结构
        pre_active: True 表示创建时就标记为活跃（用于迁移等场景）
        """
        parsed = _parse_msg_origin(unified_id)
        return {
            "platform": parsed["platform"],
            "msg_type": parsed["msg_type"],      # GroupMessage / FriendMessage / OtherMessage
            "target_id": parsed["target_id"],
            "active_after_push": pre_active,
            "dormant": not pre_active,            # 新发现的默认休眠，等说话再激活
            "unsubscribed": False,
            "last_push_date": "",
        }

    def _get_target(self, unified_id: str) -> dict:
        """获取目标信息，不存在则自动创建"""
        if unified_id not in self._data["targets"]:
            # Bug6 防护：OtherMessage 类型也记录，但推送时会跳过
            self._data["targets"][unified_id] = self._make_default_target(unified_id)
            self._save_data()
            type_label = _get_type_label(unified_id)
            logger.info(f"[每日新闻] 发现新会话 [{type_label}]: {unified_id}")
        return self._data["targets"][unified_id]

    # ==================== 活跃度检测（重写方案，解决 Bug2/3） ====================

    def _mark_active(self, unified_id: str):
        """
        标记某个会话有人说话。
        只操作内存，不立即写文件（解决 Bug4）。
        """
        # 确保目标存在
        self._get_target(unified_id)

        # 加入待处理集合
        self._pending_active.add(unified_id)

        # 节流保存
        self._save_data_throttled()

    # ---- 方案A：通过 on_decorating_result 钩子 ----
    # 优点：不干扰其他插件和 LLM
    # 缺点：只有触发了 bot 回复的消息才会被捕获
    #        纯群聊闲聊（没人 @bot）不会被记录
    @filter.on_decorating_result()
    async def _on_any_result(self, event: AstrMessageEvent):
        """
        每当有消息产生了回复结果时触发。
        用于记录活跃度，不修改回复内容。
        """
        if event and hasattr(event, "unified_msg_origin"):
            uid = event.unified_msg_origin
            # Bug7 防护：忽略 bot 自己
            sender_id = str(event.get_sender_id()) if hasattr(event, "get_sender_id") else ""
            if sender_id and sender_id == str(getattr(self.context, "bot_id", "")):
                return
            self._mark_active(uid)

    # ---- 方案B：命令/工具触发时顺带记录 ----
    # 作为方案A的补充，确保用户主动交互时一定被记录
    def _mark_active_from_event(self, event: AstrMessageEvent):
        """从事件中提取 unified_id 并标记活跃"""
        if event and hasattr(event, "unified_msg_origin"):
            self._mark_active(event.unified_msg_origin)

    # ==================== LLM 工具 ====================

    @llm_tool(name="subscribe_news")
    async def subscribe_news(self, event: AstrMessageEvent) -> str:
        """订阅每日新闻推送。当用户说想要接收每日新闻、订阅新闻等意图时调用此工具。"""
        self._mark_active_from_event(event)
        unified_id = event.unified_msg_origin
        target = self._get_target(unified_id)

        # Bug6 防护
        if _is_other(unified_id):
            return "当前会话类型不支持新闻推送。"

        if not target.get("unsubscribed") and not target.get("dormant"):
            return "当前会话已经在接收每日新闻推送了，无需重复订阅。"

        target["unsubscribed"] = False
        target["dormant"] = False
        target["active_after_push"] = True
        self._save_data()

        type_label = _get_type_label(unified_id)
        return f"订阅成功！当前{type_label}将在每天 {self.push_start_time} ~ {self.push_end_time} 收到新闻推送。"

    @llm_tool(name="unsubscribe_news")
    async def unsubscribe_news(self, event: AstrMessageEvent) -> str:
        """取消订阅每日新闻推送。当用户说不想接收新闻、取消订阅、退订等意图时调用此工具。"""
        self._mark_active_from_event(event)
        unified_id = event.unified_msg_origin
        target = self._get_target(unified_id)

        if target.get("unsubscribed"):
            return "当前会话已经取消了每日新闻推送。"

        target["unsubscribed"] = True
        self._save_data()
        return "取消成功！将不再推送每日新闻。如需重新订阅，随时告诉我。"

    @llm_tool(name="check_news_subscription")
    async def check_news_subscription(self, event: AstrMessageEvent) -> str:
        """查询当前新闻推送状态。"""
        self._mark_active_from_event(event)
        unified_id = event.unified_msg_origin
        target = self._get_target(unified_id)
        type_label = _get_type_label(unified_id)

        if target.get("unsubscribed"):
            return f"当前{type_label}已退订每日新闻。如需恢复，请告诉我。"
        elif target.get("dormant"):
            return (
                f"当前{type_label}因之前无人与bot互动已暂停推送。"
                f"现在你说话了，明天将恢复推送。"
            )
        else:
            return f"当前{type_label}正在接收推送，时间为每天 {self.push_start_time} ~ {self.push_end_time}。"

    # ==================== 命令 ====================

    @filter.command_group("新闻管理")
    def mnews(self):
        pass

    @filter.command("新闻", alias={"早报", "news"})
    async def daily_60s_news(self, event: AstrMessageEvent, date_str=None):
        """获取60s新闻。用法: /新闻 或 /新闻 20250901"""
        self._mark_active_from_event(event)

        if date_str is not None:
            try:
                target_date = datetime.datetime.strptime(str(date_str), "%Y%m%d")
            except ValueError:
                yield event.plain_result("日期格式错误，请用 YYYYMMDD，例如: /新闻 20250901")
                return
        else:
            target_date = None

        news_path, success = await self._get_image_news(target_date)
        if not success:
            yield event.plain_result(news_path)
            return
        yield event.image_result(news_path)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("status")
    async def check_status(self, event: AstrMessageEvent):
        """查看插件状态（管理员）"""
        self._mark_active_from_event(event)
        sleep_time = self._calculate_sleep_time()
        hours = int(sleep_time / 3600)
        minutes = int((sleep_time % 3600) / 60)

        targets = self._data.get("targets", {})
        total = len(targets)

        # 分类统计
        group_active = group_dormant = group_unsub = 0
        private_active = private_dormant = private_unsub = 0
        other_count = 0

        for uid, t in targets.items():
            if _is_other(uid):
                other_count += 1
                continue

            unsub = t.get("unsubscribed", False)
            dormant = t.get("dormant", False)

            if _is_group(uid):
                if unsub:
                    group_unsub += 1
                elif dormant:
                    group_dormant += 1
                else:
                    group_active += 1
            elif _is_private(uid):
                if unsub:
                    private_unsub += 1
                elif dormant:
                    private_dormant += 1
                else:
                    private_active += 1

        yield event.plain_result(
            f"📰 每日新闻插件状态\n"
            f"推送窗口: {self.push_start_time} ~ {self.push_end_time}\n"
            f"下次推送: {hours}小时{minutes}分钟后\n"
            f"待写入活跃记录: {len(self._pending_active)} 条\n"
            f"━━━━━━━━━━━━━━\n"
            f"👥 群聊 (共 {group_active + group_dormant + group_unsub})\n"
            f"  ✅ 活跃: {group_active}\n"
            f"  💤 休眠: {group_dormant}\n"
            f"  ❌ 退订: {group_unsub}\n"
            f"👤 私聊 (共 {private_active + private_dormant + private_unsub})\n"
            f"  ✅ 活跃: {private_active}\n"
            f"  💤 休眠: {private_dormant}\n"
            f"  ❌ 退订: {private_unsub}\n"
            f"❓ 其他: {other_count} (不推送)"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("list")
    async def list_targets(self, event: AstrMessageEvent):
        """列出所有推送目标（管理员）"""
        self._mark_active_from_event(event)
        targets = self._data.get("targets", {})
        if not targets:
            yield event.plain_result("暂无任何推送目标。")
            return

        lines = ["📋 推送目标列表：\n"]
        for uid, info in targets.items():
            if info.get("unsubscribed"):
                status = "❌退订"
            elif info.get("dormant"):
                status = "💤休眠"
            else:
                status = "✅活跃"

            type_label = _get_type_label(uid)
            platform = info.get("platform", "?")
            last = info.get("last_push_date", "从未")
            lines.append(f"  {status} [{type_label}] {platform} | 上次:{last}\n    {uid}")

        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:2000] + "\n...(过长已截断)"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("clean")
    async def clean_news(self, event: AstrMessageEvent):
        """清理过期新闻文件（管理员）"""
        self._mark_active_from_event(event)
        count = await self._delete_expired_news_files()
        yield event.plain_result(f"已清理 {count} 个过期新闻文件。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("push")
    async def push_news(self, event: AstrMessageEvent):
        """手动推送新闻（管理员）"""
        self._mark_active_from_event(event)
        result = await self._send_daily_news_to_all()
        yield event.plain_result(f"推送完成。{result}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("update_news")
    async def update_news_files(self, event: AstrMessageEvent):
        """强制重新下载今日新闻（管理员）"""
        self._mark_active_from_event(event)
        await self._force_update_news()
        yield event.plain_result("今日新闻已重新下载。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("reset")
    async def reset_target(self, event: AstrMessageEvent, target_uid: str = None):
        """
        重置某个目标的状态（管理员）
        用法: /新闻管理 reset <unified_id>
        不填则重置当前会话
        """
        self._mark_active_from_event(event)
        uid = target_uid if target_uid else event.unified_msg_origin

        if uid not in self._data["targets"]:
            yield event.plain_result(f"未找到目标: {uid}")
            return

        self._data["targets"][uid] = self._make_default_target(uid, pre_active=True)
        self._save_data()
        yield event.plain_result(f"已重置: {uid}")

    # ==================== 新闻获取 ====================

    async def _force_update_news(self):
        image_path, _ = self._get_news_file_path()
        if _file_exists(image_path):
            os.remove(image_path)
        await self._download_news(path=image_path)

    def _get_news_file_path(self, target_date: datetime.datetime = None) -> Tuple[str, str]:
        if target_date is None:
            target_date = datetime.datetime.now()
        current_date = target_date.strftime("%Y%m%d")
        name = f"{current_date}.jpeg"
        path = os.path.join(self.news_path, name)
        return path, name

    async def _get_image_news(self, target_date: datetime.datetime = None) -> Tuple[str, bool]:
        path, _ = self._get_news_file_path(target_date)
        if _file_exists(path):
            return path, True
        return await self._download_news(path, target_date)

    async def _download_image(self, url: str, path: str, timeout: int = 10) -> Tuple[str, bool]:
        """下载图片到本地"""
        logger.info(f"[每日新闻] 下载: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(path, "wb") as f:
                        f.write(content)
                    return path, True
                else:
                    raise Exception(f"HTTP {response.status}")

    async def _download_news(self, path: str, target_date: datetime.datetime = None):
        retries = 3
        timeout = 10
        if target_date is None:
            target_date = datetime.datetime.now()
        date = target_date.strftime("%Y-%m-%d")

        for attempt in range(retries):
            try:
                if self.news_type == "vikiboss_api":
                    url = f"https://60s-api.viki.moe/v2/60s?date={date}&encoding=image-proxy"
                    return await self._download_image(url, path, timeout)

                elif self.news_type == "indirect":
                    url = self.api
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=timeout) as response:
                            if response.status != 200:
                                raise Exception(f"API请求失败: HTTP {response.status}")
                            data = await response.json()
                            if data.get("code") != 200:
                                raise Exception(f"API错误: {data.get('msg', '未知')}")

                            api_date = data.get(self.date_key)
                            today = datetime.datetime.now().strftime("%Y-%m-%d")
                            if today != api_date:
                                fallback = f"https://60s-api.viki.moe/v2/60s?date={date}&encoding=image-proxy"
                                logger.info(f"[每日新闻] API日期不匹配({api_date})，回退vikiboss")
                                return await self._download_image(fallback, path, timeout)

                            image_url = data.get(self.img_key)
                            if not image_url:
                                raise Exception("响应中未找到图片URL")
                            return await self._download_image(image_url, path, timeout)

                elif self.news_type == "direct":
                    return await self._download_image(self.img_url, path, timeout)

            except Exception as e:
                logger.error(f"[每日新闻] 下载失败 {attempt + 1}/{retries}: {e}")
                if attempt == retries - 1:
                    return f"新闻获取失败: {e}", False
                await asyncio.sleep(2)
        return "新闻获取失败: 未知错误", False

    # ==================== 推送核心逻辑 ====================

    async def _send_daily_news_to_all(self) -> str:
        """
        推送逻辑（群聊和私聊完全统一）：

        1. OtherMessage 类型 → 跳过（Bug6 防护）
        2. 主动退订的 → 跳过
        3. 今天已推过的 → 跳过
        4. 上次推送后有人说话 → 推送
        5. 上次推送后没人说话 → 标记休眠，跳过
        """
        # 先把内存中的活跃记录刷入数据
        self._flush_pending_active()

        targets = self._data.get("targets", {})
        if not targets:
            logger.info("[每日新闻] 没有任何已知目标")
            return "没有任何已知目标。"

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")

        # 预下载新闻（只下载一次）
        news_path, success = await self._get_image_news()
        if not success:
            logger.error(f"[每日新闻] 获取新闻失败: {news_path}")
            return f"获取新闻失败: {news_path}"

        # 筛选推送目标
        push_list = []
        skip_other = 0
        skip_unsub = 0
        skip_already = 0
        skip_dormant = 0
        newly_dormant = 0

        for uid, info in targets.items():
            # Bug6: 跳过 OtherMessage
            if _is_other(uid):
                skip_other += 1
                continue

            # 今天已推过
            if info.get("last_push_date") == today_str:
                skip_already += 1
                continue

            # 主动退订
            if info.get("unsubscribed"):
                skip_unsub += 1
                continue

            # 核心判断：上次推送后有人说话吗？
            if info.get("active_after_push"):
                push_list.append(uid)
            else:
                if not info.get("dormant"):
                    info["dormant"] = True
                    newly_dormant += 1
                    type_label = _get_type_label(uid)
                    logger.info(f"[每日新闻] [{type_label}] {uid} 无人互动，进入休眠")
                skip_dormant += 1

        self._save_data()

        logger.info(
            f"[每日新闻] 筛选结果: "
            f"推送={len(push_list)} "
            f"休眠={skip_dormant}(新增{newly_dormant}) "
            f"退订={skip_unsub} "
            f"已推={skip_already} "
            f"其他={skip_other}"
        )

        if not push_list:
            return f"没有活跃目标。休眠:{skip_dormant} 退订:{skip_unsub}"

        # 分散推送
        start_h, start_m = map(int, self.push_start_time.split(":"))
        end_h, end_m = map(int, self.push_end_time.split(":"))
        window_seconds = max((end_h * 60 + end_m - start_h * 60 - start_m) * 60, 60)

        count = len(push_list)
        interval = window_seconds / count if count > 1 else 0
        interval = max(interval, self.min_push_interval)

        logger.info(f"[每日新闻] 开始推送 {count} 个目标，间隔 {interval:.1f}s")

        success_count = 0
        fail_count = 0

        for index, uid in enumerate(push_list):
            type_label = _get_type_label(uid)
            try:
                chain = MessageChain().message("每日新闻播报：").file_image(news_path)
                await self.context.send_message(uid, chain)

                # 更新状态
                info = self._data["targets"].get(uid, {})
                info["last_push_date"] = today_str
                info["active_after_push"] = False  # 重置，等下次有人说话
                info["dormant"] = False

                success_count += 1
                logger.info(f"[每日新闻] ✅ [{type_label}] {uid} ({index + 1}/{count})")

                # 分散推送
                if index < count - 1 and interval > 0:
                    await asyncio.sleep(interval)

            except Exception as e:
                fail_count += 1
                logger.error(f"[每日新闻] ❌ [{type_label}] {uid} 失败: {e}")
                await asyncio.sleep(3)

        self._save_data()

        result = f"成功:{success_count} 失败:{fail_count} 休眠:{skip_dormant} 退订:{skip_unsub}"
        logger.info(f"[每日新闻] 推送完成 - {result}")
        return result

    # ==================== 定时任务 ====================

    def _calculate_sleep_time(self) -> float:
        now = datetime.datetime.now()
        hour, minute = map(int, self.push_start_time.split(":"))
        next_push = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_push <= now:
            next_push += datetime.timedelta(days=1)
        return (next_push - now).total_seconds()

    async def _delete_expired_news_files(self) -> int:
        """删除过期新闻文件，返回删除数量"""
        save_days = self.config.get("save_days", 3)
        if save_days <= 0:
            return 0
        count = 0
        for filename in os.listdir(self.news_path):
            if not filename.endswith(".jpeg"):
                continue
            try:
                file_date = datetime.datetime.strptime(filename[:8], "%Y%m%d").date()
                if (datetime.date.today() - file_date).days >= save_days:
                    os.remove(os.path.join(self.news_path, filename))
                    logger.info(f"[每日新闻] 清理: {filename}")
                    count += 1
            except Exception:
                continue
        return count

    async def _daily_task(self):
        """
        定时任务，使用短间隔轮询代替长 sleep（解决 Bug8）
        """
        while True:
            try:
                sleep_time = self._calculate_sleep_time()
                logger.info(f"[每日新闻] 下次推送: {sleep_time / 3600:.2f} 小时后")

                # 分段 sleep，每10分钟醒一次检查（解决 Bug8：sleep 漂移）
                while sleep_time > 0:
                    chunk = min(sleep_time, 600)  # 最多睡10分钟
                    await asyncio.sleep(chunk)
                    sleep_time = self._calculate_sleep_time()
                    # 顺便刷新内存中的活跃记录
                    self._flush_pending_active()
                    if sleep_time <= 5:
                        break

                # 到点了
                logger.info("[每日新闻] 推送时间到，开始执行...")
                await self._get_image_news()
                await self._delete_expired_news_files()
                await self._send_daily_news_to_all()

                # 防止同一分钟重复触发
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("[每日新闻] 定时任务已取消")
                break
            except Exception as e:
                logger.error(f"[每日新闻] 定时任务出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(300)

    async def terminate(self):
        """插件卸载"""
        if self._monitoring_task:
            self._monitoring_task.cancel()
        self._flush_pending_active()
        self._save_data()
        logger.info("[每日新闻] 插件已停止")
