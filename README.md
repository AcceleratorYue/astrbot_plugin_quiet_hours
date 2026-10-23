# 静默时段 · astrbot_plugin_quiet_hours

> GitHub：<https://github.com/AcceleratorYue/astrbot_plugin_quiet_hours>
> License：MIT

按**多个静默方案**（像闹钟一样）暂停 AstrBot 的非管理员回复，用来避开 API 价格峰值（省 token）。静默期内：

- **只有管理员**（以及配置里额外豁免的账号）可以正常使用；
- 其他用户真正触发 Bot（@ / 唤醒词 / 引用回复 / 私聊 / 戳 Bot）时，会收到一条**可编辑的提醒**，并自动带上**下一次开放时间**；
- 支持**多方案**：每个方案有自己的时间段、重复星期、以及是否在**法定节假日**启用；
- 支持**法定节假日识别**（内置 2024–2026 放假数据，可联网自动更新），调休补班日按普通工作日处理；
- 静默强度二选一：**完全关闭**（默认，其他插件一律不执行）或 **保留上下文/缓存**（只放行白名单插件的被动处理）；
- 无论哪种模式，回复都不会真正发出，也不会真正请求模型、不产生费用。

## 效果示例

```text
用户：@机器人 在吗
Bot：⏰ 现在是 Bot 的休息时间，暂时不能回复消息。
     下次开放时间：今天 12:00
     到时候再来找我吧～
```

管理员在静默期内发消息、用指令、戳 Bot 均不受影响。

## 多方案配置（schemes）

`schemes` 是一个 **JSON 数组**，每个元素就是一个方案：

```json
[
  {"name": "工作日高峰", "weekdays": "1,2,3,4,5",
   "windows": ["09:00-12:00", "14:00-18:00"], "holidays": "skip"},
  {"name": "夜间宵禁", "weekdays": "",
   "windows": ["23:00-07:00"], "holidays": "run"}
]
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | 方案名，仅用于状态显示 |
| `windows` | 是 | 时间段数组，`HH:MM-HH:MM`；可跨天（如 `23:00-07:00`）；`00:00-00:00` = 全天 |
| `weekdays` | 否 | 重复星期，ISO 编号 `1`=周一 … `7`=周日，逗号分隔；留空 = 每天 |
| `holidays` | 否 | `skip`（默认，法定节假日跳过）或 `run`（法定节假日也启用） |
| `enabled` | 否 | 默认 `true`；设为 `false` 可临时停用某个方案 |

方案之间是**并集**：任意方案生效即为静默。上例的效果是工作日白天高峰静默（节假日跳过）+ 每天夜间宵禁（节假日也执行）。

> 留空 `schemes` 时，插件会退回旧版单方案配置（`time_windows` + `active_weekdays`），老配置无需迁移即可继续用。

## 法定节假日

- 内置 `holidays_cn.json`：2024–2026 年国务院放假安排（含调休补班日）。
- 开启 `holiday_auto_update` 时，插件启动会在后台拉取当年/次年数据并缓存到 `plugin_data/astrbot_plugin_quiet_hours/holidays_cache.json`；拉取失败继续用内置数据。
- 节假日判断用于方案里的 `holidays` 字段：
  - `skip`：法定节假日当天该方案不生效（例如“工作日高峰”在国庆期间自动停用）；
  - `run`：法定节假日也生效（例如“夜间宵禁”天天执行）。
- **调休补班日**（原本是周末但需要上班的日期）不算节假日，按普通工作日处理。
- 关闭 `holiday_enabled` 后，所有日期都只按 `weekdays` 判断。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/静默状态`（别名 `/免打扰状态`、`/静默查询`） | 查看当前是否静默、各方案配置、节假日信息、下次开放时间 |
| `/静默测试`（别名 `/免打扰测试`） | 管理员预览当前提醒文案（不在静默时段会用一个示例时间） |

## 配置项

在 AstrBot WebUI → 插件管理 → 静默时段 中修改，保存后即时生效。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 总开关 |
| `schemes` | 空 | 多方案 JSON 数组，格式见上；为空时回退旧版单方案 |
| `time_windows` | 空 | **旧版**单方案时段，仅在 `schemes` 为空时生效 |
| `active_weekdays` | `1,2,3,4,5,6,7` | **旧版**单方案星期，仅在 `schemes` 为空时生效 |
| `holiday_enabled` | 开 | 是否启用法定节假日识别 |
| `holiday_auto_update` | 开 | 启动时是否联网更新节假日数据（失败自动回退内置数据） |
| `holiday_update_url` | 空 | 自定义节假日数据源；留空用默认源（含 `{year}` 占位符），支持 holiday-cn 与 timor.tech 两种返回格式 |
| `timezone` | 空 | 留空跟随系统时区；也可填 `Asia/Shanghai` 等 IANA 时区 |
| `scope` | `all` | `all`=所有会话；`groups`=仅群聊；`private`=仅私聊 |
| `block_mode` | `full` | `full`=完全关闭（其他插件一律不执行，默认）；`keep_context`=保留上下文/缓存（只放行白名单插件的被动处理） |
| `context_plugins` | 空 | 保留模式下的白名单插件目录名，按实际安装填写，可加多个（如 `astrbot_plugin_context_aware`、`meme_manager`） |
| `notice_enabled` | 开 | 是否发送静默提醒 |
| `notice_message` | 见下 | 提醒文案，支持占位符 |
| `notice_cooldown_seconds` | 300 | 同一用户提醒冷却；0=每次都提醒 |
| `notice_group_cooldown_seconds` | 600 | 同一群/会话提醒冷却，防止不同群友轮流触发刷屏；0=关闭 |
| `bypass_admins` | 开 | 管理员豁免 |
| `extra_admin_ids` | 空 | 额外豁免的 QQ 号 |

### 提醒文案占位符

| 占位符 | 含义 | 示例 |
| --- | --- | --- |
| `{next_open}` | 下次开放的完整时间 | `今天 12:00` / `明天 08:00` |
| `{next_open_time}` | 下次开放的时刻 | `12:00` |
| `{next_open_day}` | 下次开放是今天/明天/后天/几月几日 | `今天` |
| `{window}` | 当前静默区间 | `08:00-12:00` |
| `{window_end}` | 当前静默结束时刻 | `12:00` |

默认文案：

```text
⏰ 现在是 Bot 的休息时间，暂时不能回复消息。
下次开放时间：{next_open}
到时候再来找我吧～
```

文案里可以直接写 `\n` 换行。

## 常见配置示例

工作日高峰 + 每天夜间宵禁：

```json
[
  {"name": "工作日高峰", "weekdays": "1,2,3,4,5",
   "windows": ["09:00-12:00", "14:00-18:00"], "holidays": "skip"},
  {"name": "夜间宵禁", "weekdays": "",
   "windows": ["23:00-07:00"], "holidays": "run"}
]
```

只在工作日白天静默（旧版写法仍可用）：

```yaml
active_weekdays: "1,2,3,4,5"
time_windows:
  - 09:00-12:00
  - 14:00-18:00
```

全天静默（只留管理员）：

```json
[{"name": "全天静默", "windows": ["00:00-00:00"], "holidays": "run"}]
```

只在周末静默：

```json
[{"name": "周末静默", "weekdays": "6,7", "windows": ["08:00-22:00"], "holidays": "skip"}]
```

## 工作原理

1. **触发判断**：只认 `is_at_or_wake_command`（@Bot / 唤醒词 / 引用回复 / 私聊）与**戳 Bot 自己**的戳一戳。
   - 不能用 `is_wake`：AstrBot 会对每个通过过滤器的 handler 都把 `is_wake` 置为 True，普通群消息也会变成 True，会导致“群里任何人说话都收到提醒”。
   - 戳一戳要看 `target_id == bot 自己`；群友之间互戳不提醒（但仍会拦停，不产生回复）。
2. **方案计算**：把每个方案按 `weekdays` 与节假日规则展开成绝对时间区间，多个方案取并集，得到当前是否静默、本次静默结束时间、下一次静默开始时间。
3. **静默强度**：
   - `keep_context`：从当前事件的激活处理器里挑出 `context_plugins` 白名单插件的**被动处理器**（跳过它们的指令处理器），手动执行、丢弃它们可能产生的回复，然后 `stop_event` 拦下其余全部插件；
   - `full`：直接 `stop_event`，其他插件一个都不执行。
4. **回复拦截**：把事件的 `send` / `send_streaming` 换成屏蔽版本；`on_llm_request` / `on_llm_response` 静默拦下所有模型请求；`on_decorating_result` 清空其他插件通过事件结果返回的回复。
5. **提醒**：只在真正触发时发送，用户级冷却 300 秒 + 群级冷却 600 秒叠加；提醒本身用原始 `send` 发出。

> 说明：AstrBot 内置的群消息历史持久化处理器优先级高于本插件，会在拦截前写入历史（两种模式都一样）；这是 AstrBot 用来做上下文注入的底层存储，无法也不建议拦截。

## 安装

### 从 GitHub 安装

```bash
cd data/plugins
git clone https://github.com/AcceleratorYue/astrbot_plugin_quiet_hours.git
```

然后在 AstrBot WebUI 重载插件（或重启 AstrBot）。

### 手动安装

把 `astrbot_plugin_quiet_hours` 目录（含 `main.py`、`metadata.yaml`、`_conf_schema.json`、`holidays_cn.json`）放到 AstrBot 的 `data/plugins/` 下，重载插件即可；无第三方依赖。

## 兼容性

- **AstrBot**：`>=4.16,<5`（4.16.0 源码已核对本插件用到的接口；`keep_context` 手动调用白名单处理器依赖 AstrBot 内部机制，4.x 内使用，5.0 前需重新适配）。
- **平台**：静默/提醒/回复拦截与平台无关；**戳一戳**检测只对 OneBot v11（QQ 的 napcat / aiocqhttp 适配器）的 `notice/notify/poke` 事件生效，其他平台只是不响应戳一戳，不影响主功能。
- **地区**：内置节假日为**中国大陆**放假安排；其他国家/地区可关闭 `holiday_enabled`，或把 `holiday_update_url` 指向自己的数据源（支持 holiday-cn / timor.tech 格式）。
- **语言**：命令与文案为中文；提醒文案可在配置里自行修改。
- **无个人依赖**：不依赖特定 QQ 号、群号、唤醒词、人设或路径；默认配置下插件不产生任何静默效果，需自行填写 `schemes` 才会生效。

## 已知限制

- 定时任务类的主动推送（不经过当前用户消息事件、或使用独立事件对象）不受本插件控制。
- 节假日数据来自 [NateScarlet/holiday-cn](https://github.com/NateScarlet/holiday-cn)（国务院放假通知整理）；次年安排通常 11–12 月发布，发布前该年数据为空，届时自动更新会补上。
- `keep_context` 模式只放行白名单插件的被动处理；白名单插件若自己使用独立发送通道（不经过 `event.send`）仍可能漏发（极少数插件）。
- `full` 模式下静默期没有任何插件副作用；`keep_context` 下白名单插件仍会产生副作用（例如写上下文、图片入库），请只把确实需要的插件放进白名单。
- 管理员的豁免以 AstrBot 管理员列表为准，QQ 群管理员不等于 AstrBot 管理员。
