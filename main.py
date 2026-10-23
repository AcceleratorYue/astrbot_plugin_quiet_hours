"""AstrBot 静默时段插件（astrbot_plugin_quiet_hours）。

在每天的固定时间段内暂停非管理员的 Bot 回复（用于规避 API 价格峰值），
管理员不受限制；非管理员尝试触发时会收到一条可在配置里修改的提醒，
并告知下一次开放使用的时间。

拦截分三层，确保“不回复、不烧 token”：
1. 消息入口：静默期内非管理员的唤醒消息直接拦下（可选严格模式拦下所有消息）；
2. LLM 请求入口：其他触发路径（戳一戳、正则触发、插件主动 request_llm）在
   on_llm_request 处拦截，不会真正发出模型请求；
3. 发送前：兜底清空其他插件生成的回复内容（针对不经过 LLM 的直接回复插件）。

作者：AcceleratorYue
版本：1.1.2
"""

from __future__ import annotations

import asyncio
import json
import re
import time as time_module
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

try:
    from astrbot.core.pipeline.context_utils import call_handler as _call_handler
except Exception:  # pragma: no cover - 兼容旧版本
    _call_handler = None  # type: ignore[assignment]

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # pragma: no cover - 兼容旧版本
    get_astrbot_plugin_data_path = None  # type: ignore[assignment]

try:  # Python 3.9+；Windows 上需要 tzdata（AstrBot 环境通常已自带）
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


PLUGIN_VERSION = "1.1.2"
LOG_PREFIX = "[静默时段]"

STATUS_COMMANDS = ("静默状态", "免打扰状态", "静默查询")
TEST_COMMANDS = ("静默测试",)
ALL_OWN_COMMANDS = set(STATUS_COMMANDS) | set(TEST_COMMANDS)

WEEKDAY_LABELS = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}

DEFAULT_NOTICE = (
    "⏰ 现在是 Bot 的休息时间，暂时不能回复消息。\n"
    "下次开放时间：{next_open}\n"
    "到时候再来找我吧～"
)

DEFAULT_OPTIONS: dict[str, Any] = {
    "enabled": True,
    # 多方案配置（JSON 数组）；为空时回退到旧版 time_windows/active_weekdays
    "schemes": "",
    "time_windows": [],
    "active_weekdays": "1,2,3,4,5,6,7",
    "holiday_enabled": True,
    "holiday_auto_update": True,
    "holiday_update_url": "",
    "timezone": "",
    "scope": "all",
    # 拦截模式：
    #   full         = 完全关闭：静默期其他插件一律不执行（内置高优先级历史持久化除外）
    #   keep_context = 保留上下文/缓存：只允许 context_plugins 白名单里的插件处理消息，
    #                  其余插件一律拦停；所有回复仍然阻止
    "block_mode": "full",
    "context_plugins": [],
    "notice_enabled": True,
    "notice_message": DEFAULT_NOTICE,
    "notice_cooldown_seconds": 300,
    "notice_group_cooldown_seconds": 600,
    "bypass_admins": True,
    "extra_admin_ids": [],
}

HOLIDAY_DATA_FILE = "holidays_cn.json"
HOLIDAY_CACHE_FILE = "holidays_cache.json"
DEFAULT_HOLIDAY_URL = (
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
)

# 窗口字符串支持的分隔符（允许把多个时段写在一个配置项里）
_WINDOW_SPLIT_RE = re.compile(r"[,;，；、\n]+")
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*[-~—–至]\s*(\d{1,2}):(\d{2})\s*$"
)


# ---------------------------------------------------------------------------
# 时间段解析与计算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuietWindow:
    """一个每日重复的静默时段。start == end 表示全天。"""

    start: time
    end: time
    raw: str = ""

    @property
    def full_day(self) -> bool:
        return self.start == self.end

    def label(self) -> str:
        if self.full_day:
            return "全天"
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    def contains(self, moment: datetime) -> bool:
        start_dt, end_dt = self.to_interval(moment)
        return start_dt <= moment < end_dt

    def to_interval(self, anchor: datetime) -> tuple[datetime, datetime]:
        """以 anchor 所在日期为“开始日”，返回这个窗口的绝对起止时间。"""

        start_dt = datetime.combine(anchor.date(), self.start, anchor.tzinfo)
        if self.full_day:
            return start_dt, start_dt + timedelta(days=1)
        end_dt = datetime.combine(anchor.date(), self.end, anchor.tzinfo)
        if self.end <= self.start:
            end_dt += timedelta(days=1)
        return start_dt, end_dt


def parse_time_token(token: str) -> time | None:
    """解析 HH:MM。支持 24:00（等价于次日 00:00）。"""

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", token.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24 and minute == 0:
        return time(0, 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_windows(raw: Any) -> list[QuietWindow]:
    """把配置里的时段列表解析成 QuietWindow 列表（非法项忽略）。"""

    items: list[str] = []
    if isinstance(raw, str):
        items = _WINDOW_SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        for element in raw:
            if isinstance(element, str):
                items.extend(_WINDOW_SPLIT_RE.split(element))
    windows: list[QuietWindow] = []
    seen: set[tuple[time, time]] = set()
    for item in items:
        text = item.strip()
        if not text:
            continue
        match = _WINDOW_RE.match(text)
        if not match:
            logger.warning(f"{LOG_PREFIX} 忽略无法解析的时段：{text!r}（应为 HH:MM-HH:MM）")
            continue
        start = parse_time_token(f"{match.group(1)}:{match.group(2)}")
        end = parse_time_token(f"{match.group(3)}:{match.group(4)}")
        if start is None or end is None:
            logger.warning(f"{LOG_PREFIX} 忽略非法时段：{text!r}")
            continue
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        windows.append(QuietWindow(start=start, end=end, raw=text))
    return windows


def parse_weekdays(raw: Any) -> set[int]:
    """把 "1,2,3,4,5" 或列表解析成 ISO 星期集合；非法/空返回全集。"""

    values: list[str] = []
    if isinstance(raw, str):
        values = re.split(r"[,;，；、\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        values = [str(v) for v in raw]
    days: set[int] = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        try:
            day = int(value)
        except ValueError:
            continue
        if 1 <= day <= 7:
            days.add(day)
    if not days:
        return {1, 2, 3, 4, 5, 6, 7}
    return days


def build_intervals(
    now: datetime,
    windows: list[QuietWindow],
    active_days: set[int],
    *,
    days_back: int = 1,
    days_ahead: int = 8,
) -> list[tuple[datetime, datetime]]:
    """生成 now 附近若干天内所有静默区间，并合并重叠/相邻区间。"""

    if not windows:
        return []
    intervals: list[tuple[datetime, datetime]] = []
    today = now.date()
    for offset in range(-days_back, days_ahead):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in active_days:
            continue
        anchor = now.replace(
            year=day.year, month=day.month, day=day.day, hour=0, minute=0, second=0, microsecond=0
        )
        for window in windows:
            intervals.append(window.to_interval(anchor))
    intervals.sort(key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = []
    for start_dt, end_dt in intervals:
        if merged and start_dt <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_dt))
        else:
            merged.append((start_dt, end_dt))
    return merged


@dataclass
class QuietState:
    """某一时刻的静默状态。"""

    active: bool = False
    interval: tuple[datetime, datetime] | None = None
    next_open: datetime | None = None
    next_quiet: tuple[datetime, datetime] | None = None


def query_quiet_state(
    now: datetime, windows: list[QuietWindow], active_days: set[int]
) -> QuietState:
    """计算当前是否静默、下次开放时间、下一次静默开始时间。"""

    intervals = build_intervals(now, windows, active_days)
    current = next(((s, e) for s, e in intervals if s <= now < e), None)
    future = [(s, e) for s, e in intervals if s > now]
    return QuietState(
        active=current is not None,
        interval=current,
        next_open=current[1] if current else None,
        next_quiet=future[0] if future else None,
    )


def format_day_label(target: date, today: date) -> str:
    """今天 / 明天 / 后天 / 几月几日。"""

    delta = (target - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return f"{target.month}月{target.day}日"


def format_moment(moment: datetime, now: datetime) -> str:
    return f"{format_day_label(moment.date(), now.date())} {moment:%H:%M}"


def format_weekdays(days: set[int]) -> str:
    if days >= {1, 2, 3, 4, 5, 6, 7}:
        return "每天"
    return "、".join(WEEKDAY_LABELS[d] for d in sorted(days))


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return "不到 1 分钟"


# ---------------------------------------------------------------------------
# 多静默方案与法定节假日
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuietScheme:
    """一个静默方案：静默时间段 + 每周重复 + 法定节假日行为。"""

    name: str
    enabled: bool
    weekdays: frozenset[int]  # 空集合 / 1-7 = 每天
    windows: tuple[QuietWindow, ...]
    holidays: str  # skip = 法定节假日跳过；run = 法定节假日也启用

    def applies_on(
        self,
        day: date,
        holidays: dict[date, tuple[str, bool]],
        holiday_enabled: bool,
    ) -> bool:
        if not self.enabled or not self.windows:
            return False
        info = holidays.get(day) if holiday_enabled else None
        if info is not None and info[1]:
            # 法定节假日（休息日）：由方案自己决定是否启用
            return self.holidays == "run"
        if self.weekdays and day.isoweekday() not in self.weekdays:
            return False
        return True

    def window_text(self) -> str:
        return "、".join(window.label() for window in self.windows)

    def weekday_text(self) -> str:
        return "每天" if len(self.weekdays) >= 7 else format_weekdays(set(self.weekdays))

    def holiday_text(self) -> str:
        return "法定节假日也启用" if self.holidays == "run" else "法定节假日跳过"


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开", "开启"}
    if value is None:
        return default
    return bool(value)


def _parse_scheme_item(item: Any, index: int) -> QuietScheme | None:
    if not isinstance(item, dict):
        logger.warning(f"{LOG_PREFIX} 忽略第 {index} 个静默方案：不是 JSON 对象")
        return None
    name = str(item.get("name") or f"方案{index}").strip() or f"方案{index}"
    windows = parse_windows(
        item.get("windows") or item.get("time_windows") or item.get("periods") or []
    )
    weekdays = parse_weekdays(item.get("weekdays", item.get("active_weekdays", "")))
    holiday_mode = str(item.get("holidays", item.get("holiday", "skip"))).strip().lower()
    if holiday_mode in {"run", "on", "true", "1", "启用", "开启", "是"}:
        holiday_mode = "run"
    else:
        holiday_mode = "skip"
    if not windows:
        logger.warning(
            f"{LOG_PREFIX} 忽略静默方案「{name}」：windows 为空或格式不正确（应形如 09:00-12:00）"
        )
        return None
    return QuietScheme(
        name=name,
        enabled=_as_bool(item.get("enabled", True)),
        weekdays=frozenset(weekdays),
        windows=tuple(windows),
        holidays=holiday_mode,
    )


def parse_schemes(
    raw: Any, legacy_windows: Any = None, legacy_days: Any = None
) -> list[QuietScheme]:
    """解析 schemes（JSON 数组）；为空时把旧版单方案配置迁移成一个方案。"""

    items: list[Any] = []
    if isinstance(raw, str) and raw.strip():
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} schemes 不是合法 JSON，已忽略：{exc}")
        if isinstance(parsed, dict):
            parsed = parsed.get("schemes") or [parsed]
        if isinstance(parsed, list):
            items = parsed
        elif parsed is not None:
            logger.warning(f"{LOG_PREFIX} schemes 应为 JSON 数组，已忽略")
    elif isinstance(raw, list):
        items = raw

    schemes: list[QuietScheme] = []
    for index, item in enumerate(items, start=1):
        scheme = _parse_scheme_item(item, index)
        if scheme is not None:
            schemes.append(scheme)
    if schemes:
        return schemes

    # 旧版单方案兼容：time_windows + active_weekdays
    windows = parse_windows(legacy_windows or [])
    if windows:
        return [
            QuietScheme(
                name="默认方案",
                enabled=True,
                weekdays=frozenset(parse_weekdays(legacy_days or "")),
                windows=tuple(windows),
                holidays="skip",
            )
        ]
    return []


def _holiday_entries(payload: Any) -> list[tuple[date, str, bool]]:
    """解析节假日数据，兼容 holiday-cn、timor.tech 与精简数组格式。"""

    entries: list[tuple[date, str, bool]] = []
    days: Any = payload
    if isinstance(payload, dict):
        days = payload.get("days")
        if not isinstance(days, list):
            # timor.tech 格式：{"holiday": {"01-01": {"date": "...", "holiday": true, ...}}}
            holiday_map = payload.get("holiday")
            if isinstance(holiday_map, dict):
                for item in holiday_map.values():
                    if not isinstance(item, dict):
                        continue
                    raw_date = item.get("date")
                    name = item.get("name", "")
                    is_off = item.get("holiday", item.get("isOffDay"))
                    try:
                        day = date.fromisoformat(str(raw_date)[:10])
                    except Exception:
                        continue
                    entries.append((day, str(name or ""), bool(is_off)))
            return entries
    if not isinstance(days, list):
        return entries
    for item in days:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            raw_date, name, is_off = item[0], item[1], item[2]
        elif isinstance(item, dict):
            raw_date = item.get("date")
            name = item.get("name", "")
            is_off = item.get("isOffDay", item.get("holiday"))
        else:
            continue
        try:
            day = date.fromisoformat(str(raw_date)[:10])
        except Exception:
            continue
        entries.append((day, str(name or ""), bool(is_off)))
    return entries


def load_bundled_holidays() -> dict[date, tuple[str, bool]]:
    """读取插件内置的节假日数据（离线可用）。"""

    path = Path(__file__).with_name(HOLIDAY_DATA_FILE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"{LOG_PREFIX} 读取内置节假日数据失败：{exc}")
        return {}
    data: dict[date, tuple[str, bool]] = {}
    years = payload.get("years") if isinstance(payload, dict) else None
    if isinstance(years, dict):
        for days in years.values():
            for day, name, is_off in _holiday_entries({"days": days}):
                data[day] = (name, is_off)
    return data


def merge_holiday_payload(
    data: dict[date, tuple[str, bool]], payload: Any
) -> int:
    entries = _holiday_entries(payload)
    for day, name, is_off in entries:
        data[day] = (name, is_off)
    return len(entries)


def _window_contains(window: QuietWindow, now: datetime) -> bool:
    for offset in (0, -1):
        day = (now + timedelta(days=offset)).date()
        start_dt = datetime.combine(day, window.start).replace(tzinfo=now.tzinfo)
        end_dt = datetime.combine(day, window.end).replace(tzinfo=now.tzinfo)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if start_dt <= now < end_dt:
            return True
    return False


def active_scheme_names(
    now: datetime,
    schemes: list[QuietScheme],
    holidays: dict[date, tuple[str, bool]] | None = None,
    holiday_enabled: bool = True,
) -> list[str]:
    holidays = holidays or {}
    names: list[str] = []
    for scheme in schemes:
        if not scheme.enabled:
            continue
        active = False
        for offset in (0, -1):
            day = (now + timedelta(days=offset)).date()
            if not scheme.applies_on(day, holidays, holiday_enabled):
                continue
            if any(_window_contains(window, now) for window in scheme.windows):
                active = True
                break
        if active:
            names.append(scheme.name)
    return names


def build_scheme_intervals(
    now: datetime,
    schemes: list[QuietScheme],
    holidays: dict[date, tuple[str, bool]] | None = None,
    holiday_enabled: bool = True,
    max_days: int = 4,
) -> list[tuple[datetime, datetime]]:
    """把各方案展开成绝对时间区间（覆盖前后几天，处理跨天窗口）。"""

    holidays = holidays or {}
    start_day = (now - timedelta(days=2)).date()
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(max_days + 3):
        day = start_day + timedelta(days=offset)
        for scheme in schemes:
            if not scheme.applies_on(day, holidays, holiday_enabled):
                continue
            for window in scheme.windows:
                start_dt = datetime.combine(day, window.start).replace(tzinfo=now.tzinfo)
                end_dt = datetime.combine(day, window.end).replace(tzinfo=now.tzinfo)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                intervals.append((start_dt, end_dt))
    intervals.sort(key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = []
    for start_dt, end_dt in intervals:
        if merged and start_dt <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_dt))
        else:
            merged.append((start_dt, end_dt))
    return merged


def query_scheme_state(
    now: datetime,
    schemes: list[QuietScheme],
    holidays: dict[date, tuple[str, bool]] | None = None,
    holiday_enabled: bool = True,
) -> QuietState:
    """按多个方案计算静默状态；方案之间取并集。"""

    holidays = holidays or {}
    intervals = build_scheme_intervals(
        now, schemes, holidays, holiday_enabled, max_days=4
    )
    current = next(((s, e) for s, e in intervals if s <= now < e), None)
    future = next(((s, e) for s, e in intervals if s > now), None)
    if current is None and future is None:
        # 方案可能很稀疏（例如只在法定节假日启用），扩大搜索范围
        intervals = build_scheme_intervals(
            now, schemes, holidays, holiday_enabled, max_days=400
        )
        current = next(((s, e) for s, e in intervals if s <= now < e), None)
        future = next(((s, e) for s, e in intervals if s > now), None)
    return QuietState(
        active=current is not None,
        interval=current,
        next_open=current[1] if current else None,
        next_quiet=future,
    )


def holiday_text(holidays: dict[date, tuple[str, bool]], day: date) -> str:
    info = holidays.get(day)
    if not info:
        return ""
    name, is_off = info
    label = name or "节假日"
    return f"{label}（{'休' if is_off else '调休上班'}）"


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class QuietHoursPlugin(Star):
    """固定时段静默：非管理员不回复，管理员照常使用。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self._config = config
        self._notified: dict[str, float] = {}
        self._notified_group: dict[str, float] = {}
        self._snapshot_key: tuple | None = None
        self._snapshot_value: tuple | None = None
        self._holidays: dict[date, tuple[str, bool]] = load_bundled_holidays()
        self._holiday_cache_path = self._resolve_cache_path()
        self._load_holiday_cache()
        self._holiday_refresh_task: asyncio.Task | None = None
        logger.info(f"{LOG_PREFIX} 插件 v{PLUGIN_VERSION} 已加载")

    async def initialize(self) -> None:
        """插件启用后自动更新当年/次年法定节假日数据（后台，失败不影响使用）。"""

        if not self._bool_opt("holiday_enabled"):
            return
        if not self._bool_opt("holiday_auto_update"):
            return
        self._holiday_refresh_task = asyncio.create_task(self._refresh_holidays())

    def _resolve_cache_path(self) -> Path | None:
        try:
            if get_astrbot_plugin_data_path is not None:
                base = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_quiet_hours"
            else:  # pragma: no cover - 兼容旧版本
                base = Path(__file__).with_name("data")
            base.mkdir(parents=True, exist_ok=True)
            return base / HOLIDAY_CACHE_FILE
        except Exception:
            return None

    def _load_holiday_cache(self) -> None:
        path = self._holiday_cache_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 读取节假日缓存失败：{exc}")
            return
        count = merge_holiday_payload(self._holidays, payload)
        logger.debug(f"{LOG_PREFIX} 已从缓存合入 {count} 条节假日记录")

    async def _refresh_holidays(self) -> None:
        template = str(self._opt("holiday_update_url") or "").strip() or DEFAULT_HOLIDAY_URL
        current_year = self._now().year
        merged: dict[date, tuple[str, bool]] = {}
        for year in (current_year - 1, current_year, current_year + 1):
            url = template.format(year=year) if "{year}" in template else template
            payload = await self._fetch_holiday_payload(url)
            if payload is None:
                continue
            count = merge_holiday_payload(merged, payload)
            if count:
                logger.info(f"{LOG_PREFIX} 已获取 {year} 年节假日数据（{count} 条）")
        if not merged:
            logger.debug(f"{LOG_PREFIX} 节假日自动更新失败，继续使用内置数据")
            return
        self._holidays.update(merged)
        path = self._holiday_cache_path
        if path is not None:
            try:
                payload = {
                    "updated": self._now().isoformat(timespec="seconds"),
                    "days": [
                        [day.isoformat(), name, is_off]
                        for day, (name, is_off) in sorted(merged.items())
                    ],
                }
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 写入节假日缓存失败：{exc}")

    @staticmethod
    async def _fetch_holiday_payload(url: str) -> Any:
        def _fetch():
            request = urllib.request.Request(
                url, headers={"User-Agent": "astrbot_plugin_quiet_hours"}
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 拉取节假日数据失败（{url}）：{exc}")
            return None

    # -- 配置读取 --------------------------------------------------------

    def _opt(self, key: str) -> Any:
        default = DEFAULT_OPTIONS.get(key)
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _bool_opt(self, key: str) -> bool:
        value = self._opt(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开", "开启"}
        return bool(value)

    def _int_opt(self, key: str) -> int:
        try:
            return int(self._opt(key))
        except (TypeError, ValueError):
            return int(DEFAULT_OPTIONS[key])  # type: ignore[arg-type]

    def _list_opt(self, key: str) -> list[str]:
        value = self._opt(key)
        if isinstance(value, str):
            return [item for item in re.split(r"[,;，；、\s]+", value) if item]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
        return []

    # -- 时间与状态 ------------------------------------------------------

    def _timezone(self) -> tzinfo | None:
        name = str(self._opt("timezone") or "").strip()
        if not name:
            return None  # 跟随系统本地时区
        if ZoneInfo is None:
            logger.warning(f"{LOG_PREFIX} 当前环境没有 zoneinfo，改用系统本地时区")
            return None
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            logger.warning(f"{LOG_PREFIX} 无法识别的时区：{name}，改用系统本地时区")
            return None

    def _now(self) -> datetime:
        tz = self._timezone()
        if tz is not None:
            return datetime.now(tz)
        return datetime.now().astimezone()

    def _config_snapshot(
        self,
    ) -> tuple[bool, str, list[QuietScheme], str, tuple[str, ...]]:
        """解析并缓存静默方案；配置变化后自动重新解析（避免每条消息重复解析）。"""

        raw_schemes = self._opt("schemes")
        raw_windows = self._opt("time_windows")
        raw_days = self._opt("active_weekdays")
        raw_scope = self._opt("scope")
        raw_context = self._opt("context_plugins")
        enabled = self._bool_opt("enabled")
        mode = str(self._opt("block_mode") or "keep_context").strip().lower()
        if mode not in {"full", "keep_context"}:
            mode = "keep_context"
        key = (
            enabled,
            repr(raw_schemes),
            repr(raw_windows),
            repr(raw_days),
            repr(raw_scope),
            mode,
            repr(raw_context),
        )
        if key == self._snapshot_key and self._snapshot_value is not None:
            return self._snapshot_value

        schemes = parse_schemes(raw_schemes, raw_windows, raw_days)
        scope = str(raw_scope or "all").strip().lower()
        context_plugins = tuple(self._list_opt("context_plugins"))
        value = (enabled, mode, schemes, scope, context_plugins)
        self._snapshot_key = key
        self._snapshot_value = value
        return value

    def _quiet_now(self) -> QuietState:
        enabled, _mode, schemes, _scope, _context = self._config_snapshot()
        holidays_enabled = self._bool_opt("holiday_enabled")
        if not enabled or not any(scheme.enabled for scheme in schemes):
            return QuietState()
        return query_scheme_state(
            self._now(),
            schemes,
            self._holidays,
            holidays_enabled,
        )

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        scope = str(self._opt("scope") or "all").strip().lower()
        if scope == "groups":
            try:
                return bool(event.get_group_id())
            except Exception:
                return False
        if scope == "private":
            try:
                return bool(event.is_private_chat())
            except Exception:
                return False
        return True

    # -- 身份判断 --------------------------------------------------------

    def _is_bot_admin(self, event: AstrMessageEvent) -> bool:
        """是否是 AstrBot 管理员（或配置里额外的管理员 ID）。"""

        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender = ""
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            pass
        return bool(sender) and sender in set(self._list_opt("extra_admin_ids"))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """静默期是否豁免：管理员 + 开关。"""

        if not self._bool_opt("bypass_admins"):
            return False
        return self._is_bot_admin(event)

    @staticmethod
    def _is_wake_attempt(event: AstrMessageEvent) -> bool:
        """是否是用户主动唤醒（@ / 唤醒词 / 引用回复 / 私聊）。

        注意：不能用 ``event.is_wake``。AstrBot 的唤醒阶段会对**每个通过了
        过滤器**的 handler 都把 ``event.is_wake`` 置为 True，普通群消息也会
        变成 True，用它判断会导致“群里任何人说话都收到静默提醒”。
        只有 ``is_at_or_wake_command`` 是在真正检测到 @ / 唤醒词时才会设置。
        """

        return bool(getattr(event, "is_at_or_wake_command", False))

    @staticmethod
    def _is_own_command(event: AstrMessageEvent) -> bool:
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return False
        first = re.split(r"\s+", text, maxsplit=1)[0]
        return first in ALL_OWN_COMMANDS

    @staticmethod
    def _is_poke_event(event: AstrMessageEvent) -> bool:
        """是否是“戳了 bot 自己”的戳一戳事件。

        OneBot v11 的 notify/poke 事件里，``target_id`` 才是被戳的人；
        群友之间互戳也会产生 poke 事件，不能一律当成在戳 bot。
        拿不到 target_id 时保守放行（不提醒、不拦截）。
        """

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        if not (
            raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke"
        ):
            return False
        target = raw.get("target_id", raw.get("target"))
        if target is None:
            return False
        try:
            return str(target) == str(event.get_self_id())
        except Exception:
            return False

    # -- 提醒 -------------------------------------------------------------

    def _render_notice(self, state: QuietState, now: datetime) -> str:
        template = str(self._opt("notice_message") or DEFAULT_NOTICE)
        interval = state.interval
        if state.next_open is not None:
            next_open = format_moment(state.next_open, now)
            next_open_time = f"{state.next_open:%H:%M}"
            next_open_day = format_day_label(state.next_open.date(), now.date())
        else:
            next_open = "另行通知"
            next_open_time = "待定"
            next_open_day = "待定"
        window_text = "静默时段"
        window_end = next_open_time
        if interval is not None:
            window_text = f"{interval[0]:%H:%M}-{interval[1]:%H:%M}"
            window_end = f"{interval[1]:%H:%M}"
        values = {
            "next_open": next_open,
            "next_open_time": next_open_time,
            "next_open_day": next_open_day,
            "window": window_text,
            "window_end": window_end,
        }
        text = template
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        # 配置界面里写 \n 也能换行
        return text.replace("\\n", "\n").strip()

    def _notice_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._bool_opt("notice_enabled"):
            return False
        user_cooldown = max(0, self._int_opt("notice_cooldown_seconds"))
        group_cooldown = max(0, self._int_opt("notice_group_cooldown_seconds"))
        try:
            umo = str(event.unified_msg_origin or "")
            sender = str(event.get_sender_id() or "")
        except Exception:
            umo, sender = "", ""
        now = time_module.monotonic()
        user_key = f"{umo}|{sender}"
        if user_cooldown > 0 and now - self._notified.get(user_key, 0.0) < user_cooldown:
            return False
        if group_cooldown > 0 and now - self._notified_group.get(umo, 0.0) < group_cooldown:
            return False
        if user_cooldown > 0:
            self._notified[user_key] = now
        if group_cooldown > 0:
            self._notified_group[umo] = now
        if len(self._notified) > 2000:  # 防止长时间运行无限增长
            cutoff = now - max(user_cooldown, group_cooldown, 3600) * 2
            self._notified = {
                k: v for k, v in self._notified.items() if v >= cutoff
            }
            self._notified_group = {
                k: v for k, v in self._notified_group.items() if v >= cutoff
            }
        return True

    async def _send_notice(self, event: AstrMessageEvent, state: QuietState) -> None:
        if not self._notice_allowed(event):
            return
        text = self._render_notice(state, self._now())
        if not text:
            return
        # 如果已经装上了“禁止直接发送”的保护，这里用原始 send 发提醒
        sender = event.get_extra("_quiet_hours_original_send") or event.send
        try:
            await sender(MessageChain([Plain(text)]))
        except Exception as exc:  # pragma: no cover - 平台发送失败不影响拦截
            logger.warning(f"{LOG_PREFIX} 发送静默提醒失败：{exc}")

    @staticmethod
    def _install_send_guard(event: AstrMessageEvent) -> None:
        """把当前事件的 send / send_streaming 换成静默版本。

        有些插件（表情包、图片搜索、签到等）不经过事件结果、直接调用
        ``event.send()`` 发消息，光靠 decorating 阶段拦不住。这里在静默期
        把非管理员事件的直接发送全部屏蔽掉；我们自己的提醒使用原始 send。
        """

        if event.get_extra("_quiet_hours_send_guard", False):
            return
        original_send = getattr(event, "send", None)
        if not callable(original_send):
            return

        async def _blocked_send(message_chain, *args, **kwargs):
            try:
                umo = event.unified_msg_origin
                sender = event.get_sender_id()
            except Exception:
                umo, sender = "", ""
            logger.debug(
                f"{LOG_PREFIX} 已拦截静默期的直接发送：sender={sender} umo={umo}"
            )
            return None

        async def _blocked_send_streaming(generator, *args, **kwargs):
            logger.debug(f"{LOG_PREFIX} 已拦截静默期的流式发送")
            return None

        event.set_extra("_quiet_hours_original_send", original_send)
        event.set_extra("_quiet_hours_send_guard", True)
        try:
            event.send = _blocked_send  # type: ignore[method-assign]
        except Exception:
            return
        if callable(getattr(event, "send_streaming", None)):
            try:
                event.send_streaming = _blocked_send_streaming  # type: ignore[method-assign]
            except Exception:
                pass

    # -- 静默模式与白名单 --------------------------------------------------

    @staticmethod
    def _module_root(module_path: str | None) -> str:
        """从 handler 的模块路径推导插件根名。

        常见形态：``data.plugins.<目录>.main``（第三方插件）与
        ``astrbot.builtin_stars.<目录>.main``（内置插件）。为兼容其他加载
        布局，若路径以 ``main`` 结尾则取它前面一段作为插件名。
        """

        path = module_path or ""
        for prefix in ("data.plugins.", "astrbot.builtin_stars."):
            if path.startswith(prefix):
                rest = path[len(prefix) :]
                return rest.split(".")[0] if rest else ""
        parts = [part for part in path.split(".") if part]
        if len(parts) >= 2 and parts[-1] == "main":
            return parts[-2]
        return parts[-1] if parts else ""

    def _select_context_handlers(self, event: AstrMessageEvent) -> list:
        """在“保留上下文/缓存”模式下，挑出白名单插件的被动消息处理器。"""

        _enabled, mode, _schemes, _scope, context_plugins = self._config_snapshot()
        if mode != "keep_context" or not context_plugins:
            return []
        allowed = set(context_plugins)
        try:
            activated = event.get_extra("activated_handlers") or []
        except Exception:
            activated = []
        selected = []
        for handler in activated:
            module_path = getattr(handler, "handler_module_path", "") or ""
            if self._module_root(module_path) not in allowed:
                continue
            # 白名单插件的指令不在静默期执行，只让它做被动记录/缓存
            if any(
                isinstance(event_filter, (CommandFilter, CommandGroupFilter))
                for event_filter in getattr(handler, "event_filters", []) or []
            ):
                continue
            selected.append(handler)
        return selected

    async def _run_context_handlers(
        self, event: AstrMessageEvent, handlers: list
    ) -> None:
        """手动执行白名单插件的被动处理器，并丢弃它们可能产生的回复。"""

        if not handlers or _call_handler is None:
            return
        try:
            params_map = event.get_extra("handlers_parsed_params") or {}
        except Exception:
            params_map = {}
        for handler in handlers:
            params = params_map.get(getattr(handler, "handler_full_name", ""), {})
            try:
                async for _ in _call_handler(event, handler.handler, **params):
                    pass  # 绝不外发
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 运行白名单插件处理器失败：{exc}")

    # -- 多层拦截 ---------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000000)
    async def guard_message(self, event: AstrMessageEvent) -> None:
        """第一层：消息入口拦下静默期的非管理员消息。"""

        if self._is_admin(event):
            return
        if not self._in_scope(event):
            return
        if self._is_own_command(event):
            return
        state = self._quiet_now()
        if not state.active:
            return

        _enabled, mode, _schemes, _scope, _context_plugins = (
            self._config_snapshot()
        )
        context_handlers = self._select_context_handlers(event)
        is_trigger = self._is_wake_attempt(event) or self._is_poke_event(event)

        if is_trigger:
            await self._send_notice(event, state)
            # 保留上下文时，连唤醒消息也交给白名单插件记录
            await self._run_context_handlers(event, context_handlers)
            event.stop_event()
            self._install_send_guard(event)
            return

        if mode == "keep_context":
            # 只放行白名单插件做被动记录/缓存，其余插件全部拦停
            await self._run_context_handlers(event, context_handlers)
            event.stop_event()
            self._install_send_guard(event)
            return

        # full：完全关闭，其他插件一律不执行
        event.stop_event()
        self._install_send_guard(event)

    @filter.on_llm_request(priority=1000000)
    async def guard_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """第二层：兜住戳一戳、正则触发、插件主动 request_llm 等路径。

        这里的请求通常来自“自动回复”（AstrBot 主动回复、正则触发等），
        并不是用户主动唤醒，所以**静默拦截，不发提醒**，避免群里多人聊天时
        被动触发的 LLM 请求反复刷提醒。
        """

        if self._is_admin(event):
            return
        if not self._in_scope(event):
            return
        state = self._quiet_now()
        if not state.active:
            return
        self._install_send_guard(event)
        try:
            event.set_extra("agent_stop_requested", True)
        except Exception:
            pass
        event.stop_event()

    @filter.on_llm_response(priority=1000000)
    async def guard_llm_response(self, event: AstrMessageEvent, resp: Any) -> None:
        """兜底：万一 LLM 请求还是跑完了，清空响应并拦住发送。"""

        if self._is_admin(event):
            return
        if not self._in_scope(event):
            return
        state = self._quiet_now()
        if not state.active:
            return
        try:
            if hasattr(resp, "completion_text"):
                resp.completion_text = ""
        except Exception:
            pass
        try:
            chain = getattr(resp, "result_chain", None)
            if chain is not None and hasattr(chain, "chain"):
                chain.chain = []
        except Exception:
            pass
        self._install_send_guard(event)
        try:
            event.set_extra("agent_stop_requested", True)
        except Exception:
            pass
        event.stop_event()

    @filter.on_decorating_result(priority=1000000)
    async def guard_result(self, event: AstrMessageEvent) -> None:
        """第三层：兜底清空其他插件产生的回复（不经过 LLM 的直接回复）。"""

        if self._is_admin(event):
            return
        if not self._in_scope(event):
            return
        try:
            if event.get_extra("_quiet_hours_allow_result", False):
                return
        except Exception:
            pass
        state = self._quiet_now()
        if not state.active:
            return
        result = event.get_result()
        if result is not None:
            try:
                result.chain = []
            except Exception:
                pass
        event.stop_event()

    # -- 指令 -------------------------------------------------------------

    @filter.command(
        STATUS_COMMANDS[0],
        alias=[name for name in STATUS_COMMANDS[1:]],
    )
    async def cmd_status(self, event: AstrMessageEvent):
        """查看静默时段状态与下一次开放时间。"""

        event.set_extra("_quiet_hours_allow_result", True)
        enabled, mode, schemes, scope, context_plugins = self._config_snapshot()
        now = self._now()
        holidays_enabled = self._bool_opt("holiday_enabled")
        state = query_scheme_state(now, schemes, self._holidays, holidays_enabled)
        tz_name = str(self._opt("timezone") or "").strip() or "系统本地时区"

        if not enabled:
            yield event.plain_result("静默时段：已关闭（enabled=false）")
            return
        if not any(scheme.enabled for scheme in schemes):
            yield event.plain_result(
                "静默时段：未配置静默方案。请在 WebUI 插件配置里填写 schemes（JSON 数组），"
                "或使用旧版 time_windows/active_weekdays。"
            )
            return

        scope_text = {"all": "所有会话", "groups": "仅群聊", "private": "仅私聊"}.get(
            scope, scope
        )
        mode_text = "完全关闭" if mode == "full" else "保留上下文/缓存"
        lines = ["静默时段状态"]
        today_holiday = holiday_text(self._holidays, now.date()) if holidays_enabled else ""
        if today_holiday:
            lines.append(f"· 今天：{today_holiday}")
        if holidays_enabled:
            auto_text = "开" if self._bool_opt("holiday_auto_update") else "关"
            lines.append(
                f"· 法定节假日识别：开（内置 {len(self._holidays)} 天数据；自动更新：{auto_text}）"
            )
            for offset in range(1, 121):
                day = now.date() + timedelta(days=offset)
                info = self._holidays.get(day)
                if info is not None and info[1]:
                    lines.append(
                        f"· 下个法定节假日：{format_day_label(day, now.date())}（{info[0]}）"
                    )
                    break
        else:
            lines.append("· 法定节假日识别：关（仅按星期判断）")
        for scheme in schemes:
            if not scheme.enabled:
                continue
            lines.append(
                f"· 方案「{scheme.name}」：{scheme.weekday_text()}；"
                f"{scheme.window_text()}；{scheme.holiday_text()}"
            )
        lines.append(f"· 范围：{scope_text}；静默强度：{mode_text}；时区：{tz_name}")
        lines.append(f"· 管理员豁免：{'开' if self._bool_opt('bypass_admins') else '关'}")
        if mode == "keep_context":
            lines.append(
                "· 保留插件：" + ("、".join(context_plugins) if context_plugins else "无")
            )
        if state.active and state.interval is not None:
            start_dt, end_dt = state.interval
            remaining = (end_dt - now).total_seconds()
            active_names = active_scheme_names(now, schemes, self._holidays, holidays_enabled)
            current = "静默中"
            if active_names:
                current += f"（{'、'.join(active_names)}）"
            lines.append(
                f"· 当前：{current}，本次静默至 {format_moment(end_dt, now)}，还剩 {format_duration(remaining)}"
            )
            lines.append(f"· 下次开放：{format_moment(end_dt, now)}")
        else:
            lines.append("· 当前：正常回复")
            if state.next_quiet is not None:
                start_dt, _end_dt = state.next_quiet
                lines.append(f"· 下次静默开始：{format_moment(start_dt, now)}")
        yield event.plain_result("\n".join(lines))

    @filter.command(TEST_COMMANDS[0], alias=["免打扰测试"])
    async def cmd_test_notice(self, event: AstrMessageEvent):
        """（管理员）预览静默提醒的最终文案。"""

        event.set_extra("_quiet_hours_allow_result", True)
        if not self._is_bot_admin(event):
            yield event.plain_result("该指令需要管理员权限。")
            return
        now = self._now()
        _enabled, _mode, schemes, _scope, _context = self._config_snapshot()
        state = query_scheme_state(
            now, schemes, self._holidays, self._bool_opt("holiday_enabled")
        )
        if state.interval is None:
            # 不在静默时段也给出一个示例时间，方便预览模板
            state = QuietState(
                active=True,
                interval=(now, now + timedelta(hours=1)),
                next_open=now + timedelta(hours=1),
            )
        text = self._render_notice(state, now)
        yield event.plain_result(f"静默提醒预览：\n——\n{text}\n——")

    async def terminate(self) -> None:
        task = self._holiday_refresh_task
        if task is not None and not task.done():
            task.cancel()
        logger.info(f"{LOG_PREFIX} 插件已卸载")
