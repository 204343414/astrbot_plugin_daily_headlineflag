import asyncio
import datetime
import os
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


@register(
    "每日60s读懂世界",
    "eaton",
    "AstrBot 每日60s新闻插件。自动检测活跃会话推送，支持命令获取。",
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
        # data_file 存储所有会话的推送状态
        # 结构:
        # {
        #   "targets": {
        #     "unified_id": {
        #       "active_after_push": true/false,  # 上次推送后是否有人说话
        #       "dormant": false,                  # 是否休眠（没人说话就休眠）
        #       "unsubscribed": false,             # 是否主动退订
        #       "last_push_date": "2025-01-01"     # 上次推送日期
        #     }
        #   }
        # }
        self.data_file = SAVED_NEWS_DIR / "news_push_data.json"
        self._data = {"targets": {}}
        self._load_data()

        logger.info(f"[每日新闻] 插件已加载，已知目标: {len(self._data['targets'])} 个")
        logger.info(f"[每日新闻] 推送窗口: {self.push_start_time} ~ {self.push_end_time}")

        # 启动定时任务
        self._monitoring_task = asyncio.create_task(self._daily_task())

    # ==================== 数据持久化 ====================

    def _load_data(self):
        """加载推送数据"""
        if self.data_file.exists():
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                if "targets" not in self._data:
                    self._data["targets"] = {}
            except Exception as e:
                logger.error(f"加载推送数据失败: {e}")
                self._data = {"targets": {}}

        # 兼容旧版: 迁移 push_groups.json
        old_file = SAVED_NEWS_DIR / "push_groups.json"
        if old_file.exists():
            try:
                with open(old_file, "r", encoding="utf-8") as f:
                    old_groups = json.load(f)
                count = 0
                for g in old_groups:
                    if g not in self._data["targets"]:
                        self._data["targets"][g] = {
                            "active_after_push": True,
                            "dormant": False,
                            "unsubscribed": False,
                            "last_push_date": "",
                        }
                        count += 1
                if count > 0:
                    self._save_data()
                    logger.info(f"[每日新闻] 从旧版迁移了 {count} 个目标")
                old_file.rename(old_file.with_suffix(".json.bak"))
            except Exception as e:
                logger.error(f"迁移旧数据失败: {e}")

    def _save_data(self):
        """保存推送数据"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存推送数据失败: {e}")

    def _get_target(self, unified_id: str) -> dict:
        """获取目标信息，不存在则自动创建"""
        if unified_id not in self._data["targets"]:
            self._data["targets"][unified_id] = {
                "active_after_push": False,  # 新发现的，还没说过话呢（等下面记录说话再设True）
                "dormant": True,             # 默认休眠，说了话才激活
                "unsubscribed": False,
                "last_push_date": "",
            }
            self._save_data()
            logger.info(f"[每日新闻] 发现新会话: {unified_id}")
        return self._data["targets"][unified_id]

    # ==================== 活跃度检测 ====================

    def _mark_active(self, unified_id: str):
        """
        标记某个会话有人说话了。
        - 如果在休眠中 → 解除休眠，下次推送时会推
        - active_after_push 设为 True
        """
        target = self._get_target(unified_id)
        changed = False

        if not target.get("active_after_push"):
            target["active_after_push"] = True
            changed = True

        if target.get("dormant"):
            target["dormant"] = False
            changed = True
            logger.info(f"[每日新闻] {unified_id} 有人说话，从休眠中恢复")

        if changed:
            self._save_data()

    @filter.regex(r"[\s\S]*")
    async def _catch_all_messages(self, event: AstrMessageEvent):
        """
        监听所有消息，记录活跃度。
        不产生回复，不干扰其他插件。
        """
        unified_id = event.unified_msg_origin
        self._mark_active(unified_id)
        # 不yield，不回复

    # ==================== LLM工具 ====================

    @llm_tool(name="subscribe_news")
    async def subscribe_news(self, event: AstrMessageEvent) -> str:
        """订阅每日新闻推送。当用户说想要接收每日新闻、订阅新闻等意图时调用此工具。"""
        unified_id = event.unified_msg_origin
        target = self._get_target(unified_id)

        if not target.get("unsubscribed") and not target.get("dormant"):
            return "当前会话已经在接收每日新闻推送了，无需重复订阅。"

        target["unsubscribed"] = False
        target["dormant"] = False
        target["active_after_push"] = True
        self._save_data()
        return f"订阅成功！将在每天 {self.push_start_time} ~ {self.push_end_time} 之间推送新闻。"

    @llm_tool(name="unsubscribe_news")
    async def unsubscribe_news(self, event: AstrMessageEvent) -> str:
        """取消订阅每日新闻推送。当用户说不想接收新闻、取消订阅、退订等意图时调用此工具。"""
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
        unified_id = event.unified_msg_origin
        target = self._get_target(unified_id)

        if target.get("unsubscribed"):
            return "当前会话已退订每日新闻。如需恢复，请告诉我。"
        elif target.get("dormant"):
            return (
                "当前会话因之前无人发言已暂停推送。"
                "现在你说话了，明天将恢复推送。"
            )
        else:
            return f"当前会话正在接收推送，时间为每天 {self.push_start_time} ~ {self.push_end_time}。"

    # ==================== 命令 ====================

    @filter.command_group("新闻管理")
    def mnews(self):
        pass

    @filter.command("新闻", alias={"早报", "news"})
    async def daily_60s_news(self, event: AstrMessageEvent, date_str=None):
        """获取60s新闻。用法: /新闻 或 /新闻 20250901"""
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
        sleep_time = self._calculate_sleep_time()
        hours = int(sleep_time / 3600)
        minutes = int((sleep_time % 3600) / 60)

        targets = self._data.get("targets", {})
        total = len(targets)
        active = sum(
            1 for t in targets.values()
            if not t.get("unsubscribed") and not t.get("dormant")
        )
        dormant = sum(
            1 for t in targets.values()
            if t.get("dormant") and not t.get("unsubscribed")
        )
        unsub = sum(1 for t in targets.values() if t.get("unsubscribed"))

        yield event.plain_result(
            f"📰 每日新闻插件状态\n"
            f"推送窗口: {self.push_start_time} ~ {self.push_end_time}\n"
            f"下次推送: {hours}小时{minutes}分钟后\n"
            f"━━━━━━━━━━\n"
            f"已知会话: {total}\n"
            f"  ✅ 活跃(明天会推): {active}\n"
            f"  💤 休眠(没人说话): {dormant}\n"
            f"  ❌ 主动退订: {unsub}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("list")
    async def list_targets(self, event: AstrMessageEvent):
        """列出所有推送目标（管理员）"""
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
            last = info.get("last_push_date", "从未")
            lines.append(f"{status} | 上次推送:{last}\n  {uid}")

        # 防止消息过长，分段发送
        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:2000] + "\n...(内容过长已截断)"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("clean")
    async def clean_news(self, event: AstrMessageEvent):
        """清理过期新闻文件（管理员）"""
        await self._delete_expired_news_files()
        yield event.plain_result(f"已清理 {self.config.save_days} 天前的过期新闻文件。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("push")
    async def push_news(self, event: AstrMessageEvent):
        """手动推送新闻（管理员）"""
        await self._send_daily_news_to_all()
        yield event.plain_result("手动推送完成。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mnews.command("update_news")
    async def update_news_files(self, event: AstrMessageEvent):
        """强制重新下载今日新闻（管理员）"""
        await self._force_update_news()
        yield event.plain_result("今日新闻已重新下载。")

    # ==================== 新闻获取 ====================

    async def _force_update_news(self):
        image_path, _ = self._get_news_file_path()
        # 删掉旧文件强制重下
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
                                # 回退到 vikiboss
                                fallback = f"https://60s-api.viki.moe/v2/60s?date={date}&encoding=image-proxy"
                                logger.info(f"[每日新闻] 间接API日期不匹配，回退: {fallback}")
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

    async def _download_image(self, url: str, path: str, timeout: int = 10) -> Tuple[str, bool]:
        """下载图片到本地"""
        logger.info(f"[每日新闻] 下载图片: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    content = await response.read()
                    with open(path, "wb") as f:
                        f.write(content)
                    return path, True
                else:
                    raise Exception(f"HTTP {response.status}")

    # ==================== 推送核心逻辑 ====================

    async def _send_daily_news_to_all(self):
        """
        推送逻辑（群聊和私聊统一规则）：
        
        1. 主动退订的 → 跳过
        2. 今天已经推过的 → 跳过
        3. 上次推送后有人说话(active_after_push=True) → 推送
        4. 上次推送后没人说话 → 标记休眠，跳过
        5. 从未推送过但有人说过话 → 推送
        """
        targets = self._data.get("targets", {})
        if not targets:
            logger.info("[每日新闻] 没有任何已知目标")
            return

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")

        # 先缓存好新闻图片（只下载一次）
        news_path, success = await self._get_image_news()
        if not success:
            logger.error(f"[每日新闻] 获取新闻失败: {news_path}")
            return

        # 筛选要推送的目标
        push_list = []
        for uid, info in targets.items():
            # 今天已推过
            if info.get("last_push_date") == today_str:
                continue

            # 主动退订
            if info.get("unsubscribed"):
                continue

            # 检查活跃度（群聊和私聊统一逻辑）
            if info.get("active_after_push"):
                push_list.append(uid)
            else:
                # 没人说话，进入/保持休眠
                if not info.get("dormant"):
                    info["dormant"] = True
                    logger.info(f"[每日新闻] {uid} 无人发言，进入休眠")

        self._save_data()

        if not push_list:
            logger.info("[每日新闻] 没有活跃目标需要推送")
            return

        # 计算分散推送间隔
        start_h, start_m = map(int, self.push_start_time.split(":"))
        end_h, end_m = map(int, self.push_end_time.split(":"))
        window_seconds = max((end_h * 60 + end_m - start_h * 60 - start_m) * 60, 60)

        count = len(push_list)
        interval = window_seconds / count if count > 1 else 0
        interval = max(interval, self.min_push_interval)  # 最小间隔

        logger.info(f"[每日新闻] 开始推送，{count} 个目标，间隔 {interval:.1f}s")

        for index, uid in enumerate(push_list):
            try:
                chain = MessageChain().message("每日新闻播报：").file_image(news_path)
                await self.context.send_message(uid, chain)
                logger.info(f"[每日新闻] ✅ {uid} ({index + 1}/{count})")

                # 更新状态
                info = self._data["targets"].get(uid, {})
                info["last_push_date"] = today_str
                info["active_after_push"] = False  # 重置！等有人说话再变True
                info["dormant"] = False
                self._save_data()

                # 分散推送
                if index < count - 1 and interval > 0:
                    await asyncio.sleep(interval)

            except Exception as e:
                logger.error(f"[每日新闻] ❌ {uid} 推送失败: {e}")
                await asyncio.sleep(5)

    # ==================== 定时任务 ====================

    def _calculate_sleep_time(self) -> float:
        now = datetime.datetime.now()
        hour, minute = map(int, self.push_start_time.split(":"))
        next_push = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_push <= now:
            next_push += datetime.timedelta(days=1)
        return (next_push - now).total_seconds()

    async def _delete_expired_news_files(self):
        save_days = self.config.get("save_days", 3)
        if save_days <= 0:
            return
        for filename in os.listdir(self.news_path):
            if not filename.endswith(".jpeg"):
                continue
            try:
                file_date = datetime.datetime.strptime(filename[:8], "%Y%m%d").date()
                if (datetime.date.today() - file_date).days >= save_days:
                    os.remove(os.path.join(self.news_path, filename))
                    logger.info(f"[每日新闻] 清理过期文件: {filename}")
            except Exception:
                continue

    async def _daily_task(self):
        while True:
            try:
                sleep_time = self._calculate_sleep_time()
                logger.info(f"[每日新闻] 下次推送: {sleep_time / 3600:.2f} 小时后")
                await asyncio.sleep(sleep_time)

                # 到点了
                await self._get_image_news()  # 预下载缓存
                await self._delete_expired_news_files()
                await self._send_daily_news_to_all()

                await asyncio.sleep(60)  # 防止同一分钟重复触发
            except asyncio.CancelledError:
                logger.info("[每日新闻] 定时任务已取消")
                break
            except Exception as e:
                logger.error(f"[每日新闻] 定时任务出错: {e}")
                traceback.print_exc()
                await asyncio.sleep(300)

    async def terminate(self):
        if self._monitoring_task:
            self._monitoring_task.cancel()
        self._save_data()
        logger.info("[每日新闻] 插件已停止")