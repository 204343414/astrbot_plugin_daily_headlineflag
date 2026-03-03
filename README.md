# 📰 astrbot_plugin_daily_news

每日 60 秒读懂世界 —— 一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的智能新闻推送插件。

> 原项目 [CJSen/astrbot_plugin_daliy_60s_news](https://github.com/CJSen/astrbot_plugin_daliy_60s_news) 已停更，本项目在其基础上完全重写。

## ✨ 特性

- **零配置自动推送**：不需要手动填群号，bot 所在的所有群聊和私聊自动识别
- **智能活跃检测**：只向「有人说话」的会话推送，没人说话的自动休眠，有人说话自动恢复
- **群聊私聊统一逻辑**：昨天有人跟 bot 说过话 → 今天推新闻；没人说话 → 不推，不打扰
- **防风控**：图片只下载一次本地缓存，分散推送，可配置最小间隔
- **自然语言订阅管理**：直接跟 bot 说「我不想看新闻了」即可退订，说「我要订阅新闻」即可恢复
- **多新闻源支持**：支持 vikiboss API、间接 API、直接图片 URL 三种获取方式
- **支持 AstrBot 全平台**：QQ(napcat/Lagrange)、QQ官方bot、微信、Telegram、钉钉、飞书等

## 📦 安装

### 方式一：AstrBot 插件市场（推荐）

在 AstrBot 管理面板的插件市场中搜索 `daily_news` 安装。

### 方式二：手动安装

```bash
cd AstrBot/data/plugins/
git clone https://github.com/eeetechen/astrbot_plugin_daily_news.git

安装后重启 AstrBot，进入管理面板配置插件参数。

⚙️ 配置说明
在 AstrBot 管理面板的插件配置页面中设置：

配置项	说明	默认值
news_type	新闻获取方式：vikiboss_api / indirect / direct	indirect
vikiboss_api	vikiboss API 地址	https://60s-api.viki.moe/v2/60s
indirect	间接 API 地址（返回 JSON 包含图片 URL）	-
img_key	间接 API 返回 JSON 中图片 URL 的键名	imageUrl
date_key	间接 API 返回 JSON 中日期的键名	datatime
direct	直接图片 URL	-
push_start_time	推送窗口开始时间（HH:MM）	07:00
push_end_time	推送窗口结束时间（HH:MM）	07:15
save_days	新闻图片本地保留天数	3
min_push_interval	每个目标之间的最小推送间隔（秒）	5.0

🚀 使用方法
自动推送（核心功能）
你什么都不用配置。 插件启动后自动工作：

有人在群里/私聊说话
        ↓
插件自动记录「这个会话活跃了」
        ↓
第二天早上推送时间到
        ↓
检查：昨天有人说话吗？
  ├─ 有 → 推送新闻 ✅
  └─ 没有 → 跳过，进入休眠 💤
        ↓
休眠中有人说话了 → 自动恢复，明天继续推 ✅

命令
所有用户
命令	说明
/新闻	获取今日新闻
/早报	同上
/news	同上
/新闻 20250901	获取指定日期的新闻
管理员
命令	说明
/新闻管理 status	查看插件状态（活跃/休眠/退订数量）
/新闻管理 list	列出所有推送目标及状态
/新闻管理 push	手动立即推送新闻
/新闻管理 update_news	强制重新下载今日新闻
/新闻管理 clean	清理过期新闻文件
自然语言（通过 LLM 工具调用）
直接跟 bot 说话即可，无需记命令：

「我想订阅每日新闻」 → 订阅推送
「不想看新闻了」「取消新闻推送」 → 取消订阅
「我订阅新闻了吗」 → 查询状态
🛡️ 防风控设计
之前版本因为群发没有限速被腾讯封了 7 天，现在做了以下防护：

图片缓存：每天的新闻图片只下载一次到本地，所有推送复用同一个文件
分散推送：在配置的时间窗口内（默认 07:00~07:15）均匀分布推送
最小间隔：每个目标之间至少间隔 min_push_interval 秒（默认 5 秒）
智能跳过：不活跃的会话自动跳过，减少无意义的消息发送
去重保护：同一天不会重复推送（记录 last_push_date）

🔄 从旧版迁移
如果你之前使用的是 astrbot_plugin_daliy_60s_news（旧版），插件会自动：

读取旧版的 push_groups.json，将已配置的群组迁移到新数据结构中
旧文件会被重命名为 .json.bak 备份
旧版配置中的 groups 字段不再需要，可以删除
❓ 常见问题
Q：插件装上后没有自动推送？

A：插件需要先「发现」会话。只要群里或私聊有人发过消息（任意消息），插件就会记录这个会话。第二天推送时间到了就会推送。

Q：某个群不想推送怎么办？

A：在那个群里跟 bot 说「取消新闻推送」或「不要推新闻了」，bot 会通过 LLM 理解并调用退订工具。

Q：群里好久没人说话，突然有人说了一句，会立即推送吗？

A：不会立即推送。有人说话后会解除休眠状态，第二天的推送时间才会推送。

Q：推送时间可以改吗？

A：可以，在插件配置中修改 push_start_time 和 push_end_time。

📄 致谢
原项目 CJSen/astrbot_plugin_daliy_60s_news
新闻数据来源 vikiboss/60s-api
AstrBot 框架
