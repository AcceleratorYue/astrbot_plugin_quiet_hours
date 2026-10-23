"""静默时段插件自测脚本（开发用，不参与插件运行）。

覆盖：
1. 时间段解析、静默方案解析（多方案 + 旧版兼容）、法定节假日数据；
2. 多方案状态计算（并集、跨天、节假日 skip/run、下次开放/下次静默）；
3. 两种静默强度：完全关闭 / 保留上下文缓存（白名单插件）；
4. 唤醒判断、戳一戳目标、提醒冷却、回复拦截与管理员豁免；
5. AstrBot 加载器校验（metadata、插件类探测、配置 schema）。

运行（需要装有 astrbot 的 Python 环境）：

    python tests/test_quiet_hours.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parents[1]
sys.path.insert(0, str(PLUGIN_DIR.parent))

import astrbot_plugin_quiet_hours.main as qh  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 10, 21, 10, 0, tzinfo=TZ)  # 周三，非节假日
BOT_ID = "10000"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _StubContext:
    """Star.__init__ 只用到这个占位对象。"""


class _FakeResult:
    def __init__(self) -> None:
        self.chain = ["原始回复"]


class FakeEvent:
    """只实现插件用到的 AstrMessageEvent 接口。"""

    def __init__(
        self,
        *,
        admin: bool = False,
        wake: bool = True,
        text: str = "在吗",
        group: str | None = "123456",
        private: bool = False,
        sender: str = "10001",
        umo: str = "test:GroupMessage:123456",
        self_id: str = BOT_ID,
    ) -> None:
        self._admin = admin
        self._group = group
        self._private = private
        self._sender = sender
        self._self_id = self_id
        self.is_at_or_wake_command = wake
        self.is_wake = wake
        self.message_str = text
        self.unified_msg_origin = umo
        self.sent: list = []
        self.stopped = False
        self.extras: dict = {}
        self.result = None

    def is_admin(self) -> bool:
        return self._admin

    def get_group_id(self):
        return self._group

    def is_private_chat(self) -> bool:
        return self._private

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self_id

    def is_wake_up(self) -> bool:
        return self.is_wake

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def stop_event(self) -> None:
        self.stopped = True

    def set_extra(self, key, value) -> None:
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_result(self, result) -> None:
        self.result = result

    def get_result(self):
        return self.result

    def plain_result(self, text):
        return ("plain", text)

    def sent_texts(self) -> list[str]:
        texts = []
        for chain in self.sent:
            for component in getattr(chain, "chain", []) or []:
                text = getattr(component, "text", None)
                if text:
                    texts.append(text)
        return texts


def make_plugin(overrides: dict | None = None):
    plugin = object.__new__(qh.QuietHoursPlugin)
    # 测试默认关闭群级冷却，避免影响单用户提醒相关断言；群级冷却单独测试
    defaults = {
        **qh.DEFAULT_OPTIONS,
        "notice_group_cooldown_seconds": 0,
    }
    plugin._config = {**defaults, **(overrides or {})}
    plugin._notified = {}
    plugin._notified_group = {}
    plugin._snapshot_key = None
    plugin._snapshot_value = None
    plugin._now = lambda: FIXED_NOW
    plugin._holidays = qh.load_bundled_holidays()
    plugin._holiday_cache_path = None
    plugin._holiday_refresh_task = None
    return plugin


def fake_handler(module_path: str, name: str, fn, command: bool = False):
    filters = [qh.CommandFilter("测试指令")] if command else []
    return SimpleNamespace(
        handler_module_path=module_path,
        handler_full_name=f"{module_path}_{name}",
        handler_name=name,
        event_filters=filters,
        handler=fn,
    )


def install_fake_call_handler():
    """把 AstrBot 的 call_handler 换成直接调用处理器（仅测试）。"""

    async def _fake_call_handler(event, handler, **params):
        if False:  # pragma: no cover - 让它成为 async generator
            yield None
        await handler(event)

    qh._call_handler = _fake_call_handler


SCHEMES_JSON = json.dumps(
    [
        {
            "name": "工作日高峰",
            "weekdays": "1,2,3,4,5",
            "windows": ["09:00-12:00", "14:00-18:00"],
            "holidays": "skip",
        },
        {
            "name": "夜间宵禁",
            "weekdays": "",
            "windows": ["23:00-07:00"],
            "holidays": "run",
        },
    ],
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# 1. 时间段 / 方案解析
# ---------------------------------------------------------------------------


def test_parse_windows() -> None:
    windows = qh.parse_windows(
        [
            "08:00-12:00",
            "14:00~18:00",
            "23:00-08:00",
            "00:00-00:00",
            "08:00 - 12:00",
            "bad-value",
            "25:00-26:00",
        ]
    )
    labels = [window.label() for window in windows]
    assert "08:00-12:00" in labels
    assert "14:00-18:00" in labels
    assert "23:00-08:00" in labels
    assert "全天" in labels
    assert len(labels) == 4, labels

    combined = qh.parse_windows("08:00-12:00, 14:00-18:00\n19:00-20:00")
    assert len(combined) == 3, [w.label() for w in combined]
    print("✅ 时段解析：", labels, "+ 单字符串多时段")


def test_parse_weekdays() -> None:
    assert qh.parse_weekdays("") == {1, 2, 3, 4, 5, 6, 7}
    assert qh.parse_weekdays("1,3,5") == {1, 3, 5}
    assert qh.parse_weekdays([4, "6"]) == {4, 6}
    assert qh.parse_weekdays("bad") == {1, 2, 3, 4, 5, 6, 7}
    print("✅ 星期解析：按 ISO 星期过滤，空值=每天")


def test_parse_schemes() -> None:
    schemes = qh.parse_schemes(SCHEMES_JSON)
    assert [scheme.name for scheme in schemes] == ["工作日高峰", "夜间宵禁"]
    assert schemes[0].weekdays == frozenset({1, 2, 3, 4, 5})
    assert schemes[0].holidays == "skip"
    assert schemes[1].weekdays == frozenset({1, 2, 3, 4, 5, 6, 7})
    assert schemes[1].holidays == "run"
    assert schemes[0].window_text() == "09:00-12:00、14:00-18:00"

    # 旧版单方案兼容
    legacy = qh.parse_schemes("", ["08:00-12:00"], "1,2,3")
    assert len(legacy) == 1
    assert legacy[0].name == "默认方案"
    assert legacy[0].weekdays == frozenset({1, 2, 3})
    assert legacy[0].holidays == "skip"

    # 非法 JSON：回退旧版
    bad = qh.parse_schemes("{not-json", ["08:00-12:00"], "")
    assert len(bad) == 1 and bad[0].name == "默认方案"

    # 方案里的 enabled=false 也允许保留（只是不生效）
    disabled = qh.parse_schemes(
        json.dumps([{"name": "停用", "windows": ["01:00-02:00"], "enabled": False}])
    )
    assert disabled[0].enabled is False
    print("✅ 方案解析：多方案 / 旧版兼容 / 非法 JSON 回退")


def test_holiday_data() -> None:
    data = qh.load_bundled_holidays()
    assert len(data) > 100, len(data)
    assert data[date(2026, 10, 1)] == ("国庆节", True)
    assert data[date(2026, 10, 10)] == ("国庆节", False)  # 调休补班
    assert data[date(2025, 1, 1)][0] == "元旦"
    assert data[date(2024, 2, 10)] == ("春节", True)
    print(f"✅ 节假日数据：内置 {len(data)} 天（含调休补班）")


def test_holiday_payload_formats() -> None:
    """自定义数据源兼容 holiday-cn 与 timor.tech 两种格式。"""

    holiday_cn = qh._holiday_entries(
        {"days": [{"date": "2026-01-01", "name": "元旦", "isOffDay": True}]}
    )
    assert holiday_cn == [(date(2026, 1, 1), "元旦", True)]

    timor = qh._holiday_entries(
        {
            "code": 0,
            "holiday": {
                "01-01": {"holiday": True, "name": "元旦", "date": "2026-01-01"},
                "10-10": {"holiday": False, "name": "国庆节后补班", "date": "2026-10-10"},
            },
        }
    )
    assert (date(2026, 1, 1), "元旦", True) in timor
    assert (date(2026, 10, 10), "国庆节后补班", False) in timor

    compact = qh._holiday_entries([["2026-01-02", "元旦", True]])
    assert compact == [(date(2026, 1, 2), "元旦", True)]
    print("✅ 节假日数据源：holiday-cn / timor.tech / 精简数组 均支持")


def test_module_root() -> None:
    """白名单插件名识别：兼容不同模块布局。"""

    root = qh.QuietHoursPlugin._module_root
    assert (
        root("data.plugins.astrbot_plugin_context_aware.main")
        == "astrbot_plugin_context_aware"
    )
    assert root("astrbot.builtin_stars.astrbot.main") == "astrbot"
    assert root("some.other.plugin_dir.main") == "plugin_dir"
    assert root("plain_plugin") == "plain_plugin"
    assert root("") == ""
    print("✅ 白名单插件名：data.plugins / 内置 / 任意布局 均可识别")


# ---------------------------------------------------------------------------
# 2. 多方案状态计算
# ---------------------------------------------------------------------------


def test_scheme_state() -> None:
    schemes = qh.parse_schemes(SCHEMES_JSON)
    holidays = qh.load_bundled_holidays()

    # 普通工作日 10:00：命中工作日高峰
    state = qh.query_scheme_state(FIXED_NOW, schemes, holidays, True)
    assert state.active and state.interval is not None
    assert state.interval[1] == datetime(2026, 10, 21, 12, 0, tzinfo=TZ)
    assert state.next_quiet is not None
    assert state.next_quiet[0] == datetime(2026, 10, 21, 14, 0, tzinfo=TZ)

    # 同一时刻的方案名
    names = qh.active_scheme_names(FIXED_NOW, schemes, holidays, True)
    assert names == ["工作日高峰"], names

    # 国庆假期 10:00：工作日高峰 skip，只剩夜间宵禁（此时未到点）
    holiday_noon = datetime(2026, 10, 1, 10, 0, tzinfo=TZ)
    state = qh.query_scheme_state(holiday_noon, schemes, holidays, True)
    assert not state.active
    assert state.next_quiet is not None and state.next_quiet[0] == datetime(
        2026, 10, 1, 23, 0, tzinfo=TZ
    )

    # 国庆假期 23:30：夜间宵禁 run，跨天到次日 07:00
    holiday_night = datetime(2026, 10, 1, 23, 30, tzinfo=TZ)
    state = qh.query_scheme_state(holiday_night, schemes, holidays, True)
    assert state.active and state.interval is not None
    assert state.interval[1] == datetime(2026, 10, 2, 7, 0, tzinfo=TZ)

    # 周六非节假日：工作日高峰不生效，夜间宵禁照常
    weekend = datetime(2026, 10, 17, 10, 0, tzinfo=TZ)
    state = qh.query_scheme_state(weekend, schemes, holidays, True)
    assert not state.active
    assert state.next_quiet is not None and state.next_quiet[0] == datetime(
        2026, 10, 17, 23, 0, tzinfo=TZ
    )

    # 关闭节假日识别后：国庆 10:00 会被当成普通周四，工作日高峰生效
    state = qh.query_scheme_state(holiday_noon, schemes, holidays, False)
    assert state.active

    # 方案并集：两个方案相邻/重叠时合并成一段
    adjacent = qh.parse_schemes(
        json.dumps(
            [
                {"name": "A", "windows": ["09:00-12:00"]},
                {"name": "B", "windows": ["11:00-14:00"]},
            ]
        )
    )
    state = qh.query_scheme_state(FIXED_NOW, adjacent, holidays, True)
    assert state.interval is not None
    assert state.interval[1] == datetime(2026, 10, 21, 14, 0, tzinfo=TZ)
    print("✅ 多方案状态：并集 / 跨天 / 节假日 skip 与 run / 关闭节假日识别")


def test_format_helpers() -> None:
    today = date(2026, 10, 20)
    assert qh.format_day_label(today, today) == "今天"
    assert qh.format_day_label(today + timedelta(days=1), today) == "明天"
    assert qh.format_day_label(today + timedelta(days=2), today) == "后天"
    assert qh.format_day_label(today + timedelta(days=5), today) == "10月25日"
    assert qh.format_weekdays({1, 2, 3, 4, 5, 6, 7}) == "每天"
    assert qh.format_weekdays({1, 3}) == "周一、周三"
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=TZ)
    assert qh.format_moment(moment, FIXED_NOW) == "今天 12:00"
    print("✅ 文案格式化：今天/明天/后天/日期、星期、时刻")


# ---------------------------------------------------------------------------
# 3. 触发判断 / 静默强度 / 白名单
# ---------------------------------------------------------------------------


def test_wake_detection() -> None:
    """回归：AstrBot 会给每个通过过滤器的 handler 置 is_wake=True，不能据此提醒。"""

    plugin = make_plugin({"time_windows": ["09:00-12:00"]})

    plain = FakeEvent(wake=False, sender="41004")
    plain.is_wake = True  # 模拟框架副作用
    asyncio.run(plugin.guard_message(plain))
    assert not plain.sent, "普通群消息不应触发静默提醒"
    assert plain.stopped, "静默期普通消息也不应交给其他插件"

    wake = FakeEvent(wake=True, sender="41005")
    asyncio.run(plugin.guard_message(wake))
    assert wake.stopped and len(wake.sent) == 1, (wake.stopped, wake.sent)

    full = make_plugin({"time_windows": ["09:00-12:00"], "block_mode": "full"})
    plain2 = FakeEvent(wake=False, sender="41006")
    plain2.is_wake = True
    asyncio.run(full.guard_message(plain2))
    assert plain2.stopped and not plain2.sent
    print("✅ 唤醒判断：普通消息不提醒，只有 @/唤醒词才提醒")


def test_block_modes() -> None:
    """keep_context 只跑白名单插件的被动处理器；full 一个都不跑。"""

    install_fake_call_handler()
    ran: list[str] = []

    async def _ctx_handler(event):
        ran.append("context")

    async def _meme_handler(event):
        ran.append("meme")

    async def _ctx_command(event):
        ran.append("context_command")

    context_handler = fake_handler(
        "data.plugins.astrbot_plugin_context_aware.main", "on_message", _ctx_handler
    )
    meme_handler = fake_handler(
        "data.plugins.meme_manager.main", "handle_upload_image", _meme_handler
    )
    context_command = fake_handler(
        "data.plugins.astrbot_plugin_context_aware.main",
        "cmd_short_memory",
        _ctx_command,
        command=True,
    )

    def bind(event):
        event.set_extra(
            "activated_handlers", [context_handler, meme_handler, context_command]
        )
        event.set_extra("handlers_parsed_params", {})

    plugin = make_plugin(
        {
            "time_windows": ["09:00-12:00"],
            "block_mode": "keep_context",
            "context_plugins": ["astrbot_plugin_context_aware"],
        }
    )
    event = FakeEvent(wake=False, sender="41501")
    bind(event)
    asyncio.run(plugin.guard_message(event))
    assert ran == ["context"], ran
    assert event.stopped and not event.sent
    assert event.get_extra("_quiet_hours_send_guard") is True

    ran.clear()
    full = make_plugin({"time_windows": ["09:00-12:00"], "block_mode": "full"})
    event2 = FakeEvent(wake=False, sender="41502")
    bind(event2)
    asyncio.run(full.guard_message(event2))
    assert ran == [], ran
    assert event2.stopped and not event2.sent

    ran.clear()
    wake = FakeEvent(wake=True, sender="41503")
    bind(wake)
    asyncio.run(plugin.guard_message(wake))
    assert ran == ["context"], ran
    assert wake.stopped and len(wake.sent) == 1

    print("✅ 静默强度：keep_context 仅放行白名单被动处理器；full 全部拦停")


def test_poke_targets() -> None:
    """只有戳 bot 自己才提醒；群友互戳不提醒。"""

    plugin = make_plugin(
        {"time_windows": ["09:00-12:00"], "notice_cooldown_seconds": 0}
    )

    def poke_event(target, sender="41601", wake=False):
        event = FakeEvent(wake=wake, sender=sender)
        event.message_obj = SimpleNamespace(
            raw_message={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": sender,
                "target_id": target,
            }
        )
        return event

    to_bot = poke_event(BOT_ID)
    asyncio.run(plugin.guard_message(to_bot))
    assert to_bot.stopped and len(to_bot.sent) == 1

    other = poke_event("123456", sender="41602")
    asyncio.run(plugin.guard_message(other))
    assert other.stopped and not other.sent

    missing = FakeEvent(wake=False, sender="41603")
    missing.message_obj = SimpleNamespace(
        raw_message={"post_type": "notice", "notice_type": "notify", "sub_type": "poke"}
    )
    asyncio.run(plugin.guard_message(missing))
    assert missing.stopped and not missing.sent

    admin = poke_event(BOT_ID, sender="10001")
    admin._admin = True
    asyncio.run(plugin.guard_message(admin))
    assert not admin.stopped and not admin.sent
    print("✅ 戳一戳：只响应戳 bot，群友互戳不提醒")


def test_group_cooldown() -> None:
    """群级冷却：不同群友轮流触发时，一个群只提醒一次。"""

    plugin = make_plugin(
        {
            "time_windows": ["09:00-12:00"],
            "notice_cooldown_seconds": 300,
            "notice_group_cooldown_seconds": 600,
        }
    )

    first = FakeEvent(sender="42001")
    asyncio.run(plugin.guard_message(first))
    assert first.stopped and len(first.sent) == 1

    second = FakeEvent(sender="42002")
    asyncio.run(plugin.guard_message(second))
    assert second.stopped and not second.sent

    other_group = FakeEvent(sender="42003", group="999999", umo="test:GroupMessage:999999")
    asyncio.run(plugin.guard_message(other_group))
    assert other_group.stopped and len(other_group.sent) == 1

    no_group_cd = make_plugin(
        {
            "time_windows": ["09:00-12:00"],
            "notice_cooldown_seconds": 300,
            "notice_group_cooldown_seconds": 0,
        }
    )
    a = FakeEvent(sender="43001")
    b = FakeEvent(sender="43002")
    asyncio.run(no_group_cd.guard_message(a))
    asyncio.run(no_group_cd.guard_message(b))
    assert len(a.sent) == 1 and len(b.sent) == 1
    print("✅ 群级提醒冷却：同群只提醒一次，跨群独立")


# ---------------------------------------------------------------------------
# 4. 回复拦截
# ---------------------------------------------------------------------------


def test_guard_message() -> None:
    install_fake_call_handler()
    plugin = make_plugin({"time_windows": ["09:00-12:00"]})

    event = FakeEvent()
    asyncio.run(plugin.guard_message(event))
    assert event.stopped
    texts = event.sent_texts()
    assert len(texts) == 1 and "下次开放时间" in texts[0] and "今天 12:00" in texts[0], texts

    event2 = FakeEvent()
    asyncio.run(plugin.guard_message(event2))
    assert event2.stopped and not event2.sent

    event3 = FakeEvent(sender="20002")
    asyncio.run(plugin.guard_message(event3))
    assert event3.stopped and len(event3.sent) == 1

    admin = FakeEvent(admin=True)
    asyncio.run(plugin.guard_message(admin))
    assert not admin.stopped and not admin.sent

    passive = FakeEvent(wake=False, sender="30003")
    asyncio.run(plugin.guard_message(passive))
    assert passive.stopped and not passive.sent

    async def _try_direct_send(ev):
        await ev.send("插件直发的消息")

    asyncio.run(_try_direct_send(passive))
    assert passive.sent == [], "静默期插件直接发送应被屏蔽"
    assert passive.get_extra("_quiet_hours_send_guard") is True

    async def _try_send_after_notice(ev):
        await ev.send("其他插件的直发消息")

    asyncio.run(_try_send_after_notice(event))
    assert len(event.sent) == 1, event.sent

    plugin_groups = make_plugin({"time_windows": ["09:00-12:00"], "scope": "groups"})
    private_event = FakeEvent(private=True, group=None, umo="aiocqhttp:FriendMessage:10001")
    asyncio.run(plugin_groups.guard_message(private_event))
    assert not private_event.stopped

    own = FakeEvent(text="静默状态", sender="40004")
    asyncio.run(plugin.guard_message(own))
    assert not own.stopped

    print("✅ 消息入口拦截：提醒/冷却/管理员/范围/自身指令")


def test_guard_llm_and_result() -> None:
    plugin = make_plugin({"time_windows": ["09:00-12:00"]})

    event = FakeEvent(sender="50005")
    asyncio.run(plugin.guard_llm_request(event, None))
    assert event.stopped and not event.sent
    assert event.get_extra("agent_stop_requested") is True

    admin = FakeEvent(admin=True)
    asyncio.run(plugin.guard_llm_request(admin, None))
    assert not admin.stopped

    response_event = FakeEvent(sender="52005")
    response = SimpleNamespace(
        completion_text="不该发出的回复", result_chain=SimpleNamespace(chain=["x"])
    )
    asyncio.run(plugin.guard_llm_response(response_event, response))
    assert response_event.stopped
    assert response.completion_text == "" and response.result_chain.chain == []

    result_event = FakeEvent(sender="60006")
    result_event.result = _FakeResult()
    asyncio.run(plugin.guard_result(result_event))
    assert result_event.stopped and result_event.result.chain == []

    allowed = FakeEvent(text="静默状态", sender="70007")
    allowed.set_extra("_quiet_hours_allow_result", True)
    allowed.result = _FakeResult()
    asyncio.run(plugin.guard_result(allowed))
    assert not allowed.stopped and allowed.result.chain

    admin2 = FakeEvent(admin=True)
    admin2.result = _FakeResult()
    asyncio.run(plugin.guard_result(admin2))
    assert not admin2.stopped and admin2.result.chain
    print("✅ LLM 请求/响应与发送前兜底：均生效")


async def test_commands() -> None:
    plugin = make_plugin(
        {"time_windows": ["09:00-12:00"], "block_mode": "keep_context"}
    )

    status_event = FakeEvent(sender="80008")
    results = [item async for item in plugin.cmd_status(status_event)]
    text = results[0][1]
    assert "静默" in text and "下次开放" in text and "今天 12:00" in text, text
    assert "静默强度" in text and "保留上下文/缓存" in text, text
    assert "方案「默认方案」" in text, text
    assert "保留插件：无" in text, text
    assert "法定节假日识别：开" in text, text

    # 默认配置（full）下的状态文案
    full_plugin = make_plugin({"time_windows": ["09:00-12:00"]})
    full_status = FakeEvent(sender="80009")
    results = [item async for item in full_plugin.cmd_status(full_status)]
    assert "静默强度：完全关闭" in results[0][1], results[0][1]

    denied = FakeEvent(sender="90009")
    results = [item async for item in plugin.cmd_test_notice(denied)]
    assert "管理员" in results[0][1]

    admin = FakeEvent(admin=True)
    results = [item async for item in plugin.cmd_test_notice(admin)]
    assert "静默提醒预览" in results[0][1] and "下次开放时间" in results[0][1]
    print("✅ 指令：状态查询（多方案/节假日） / 提醒预览（权限）")


def test_notice_template() -> None:
    plugin = make_plugin(
        {
            "time_windows": ["09:00-12:00"],
            "notice_message": "休息中，{next_open_day} {next_open_time}（{window}）恢复。",
        }
    )
    schemes = plugin._config_snapshot()[2]
    state = qh.query_scheme_state(FIXED_NOW, schemes, plugin._holidays, True)
    text = plugin._render_notice(state, FIXED_NOW)
    assert text == "休息中，今天 12:00（09:00-12:00）恢复。", text
    print("✅ 提醒模板占位符：", text)


# ---------------------------------------------------------------------------
# 5. AstrBot 加载器校验
# ---------------------------------------------------------------------------


def test_loader() -> None:
    from astrbot.core.config import AstrBotConfig
    from astrbot.core.star.star_manager import PluginManager

    metadata = PluginManager._load_plugin_metadata(str(PLUGIN_DIR))
    assert metadata is not None
    assert metadata.name == "astrbot_plugin_quiet_hours", metadata.name
    assert metadata.display_name == "静默时段", metadata.display_name
    assert metadata.version == "1.1.2", metadata.version

    classes = PluginManager._get_classes(sys.modules[qh.__name__])
    assert classes == ["QuietHoursPlugin"], classes

    schema = PluginManager._load_plugin_config_schema(str(PLUGIN_DIR / "_conf_schema.json"))
    assert isinstance(schema, dict)
    for key in ("schemes", "holiday_enabled", "holiday_auto_update", "block_mode"):
        assert key in schema, key
    assert schema["block_mode"]["default"] == "full"
    assert schema["context_plugins"]["default"] == []

    runtime = PLUGIN_DIR / "_test_runtime"
    runtime.mkdir(exist_ok=True)
    config_path = runtime / "cfg.json"
    if config_path.exists():
        config_path.unlink()
    try:
        config = AstrBotConfig(config_path=str(config_path), schema=schema)
        assert config.get("block_mode") == "full"
        assert config.get("context_plugins") == []
        assert config.get("holiday_enabled") is True
        assert config.get("schemes") == ""
        plugin = qh.QuietHoursPlugin(_StubContext(), config)
        assert plugin._quiet_now().active is False
    finally:
        import shutil

        shutil.rmtree(runtime, ignore_errors=True)

    print("✅ 加载器校验：metadata / 插件类 / 配置 schema / 默认值")


def main() -> int:
    try:
        test_parse_windows()
        test_parse_weekdays()
        test_parse_schemes()
        test_holiday_data()
        test_holiday_payload_formats()
        test_module_root()
        test_scheme_state()
        test_format_helpers()
        test_wake_detection()
        test_block_modes()
        test_poke_targets()
        test_group_cooldown()
        test_guard_message()
        test_guard_llm_and_result()
        asyncio.run(test_commands())
        test_notice_template()
        test_loader()
    except AssertionError:
        traceback.print_exc()
        return 1
    print("\n全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
