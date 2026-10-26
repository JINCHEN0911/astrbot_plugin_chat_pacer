"""聊天节奏 · 私聊消息防抖合并

把连发的几句攒成一个窗口，窗口内没有新消息了才合成一条交给 LLM，
只回一次。撤回的消息会从缓冲里剔掉；整段都撤了就不回。

实现要点：
- 钩子挂在 OnWaitingLLMRequestEvent（拿到 LLM 锁之前）。
- 框架在该钩子结束后会检查 event.is_stopped()，停了就直接 return 不调 LLM
  （见 pipeline/process_stage/method/agent_sub_stages/internal.py）。
- 因此：同批里除最后一条外全部 stop_event()，最后一条把合并文本写回
  event.message_str，走正常流程。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_chat_pacer"
PLUGIN_VERSION = "0.1.0"


def _as_float(raw, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _as_bool(raw, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        t = raw.strip().lower()
        if t in {"1", "true", "yes", "on", "是", "开"}:
            return True
        if t in {"0", "false", "no", "off", "否", "关"}:
            return False
    return default


@register(
    PLUGIN_NAME,
    "Knn",
    "私聊消息防抖合并：连发几句攒成一个窗口再回一次，撤回的消息不计入",
    PLUGIN_VERSION,
)
class ChatPacer(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # umo -> list[(message_id, text)]
        self._buffer: dict[str, list[tuple[str, str]]] = {}
        # umo -> 递增版本号，用来判断自己是不是最后一条
        self._version: dict[str, int] = {}

    # ------------------------------------------------------------ 工具
    def _debug(self, text: str) -> None:
        """把事件写进一个小日志，方便确认撤回通知到底有没有进来。"""
        if not _as_bool(self._cfg("debug_log", False), False):
            return
        try:
            d = Path("data") / "plugin_data" / PLUGIN_NAME
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "pacer.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {text}" + chr(10))
        except Exception:
            pass

    def _cfg(self, key, default):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _window(self) -> float:
        w = _as_float(self._cfg("window_seconds", 4.0), 4.0)
        return max(0.5, min(w, 30.0))

    def _is_target(self, event: AstrMessageEvent) -> bool:
        if not _as_bool(self._cfg("enabled", True), True):
            return False
        try:
            if event.is_private_chat():
                return True
        except Exception:
            return False
        return _as_bool(self._cfg("include_group", False), False)

    # ------------------------------------------------------------ 撤回
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AstrMessageEvent):
        """处理撤回通知：把被撤回的那条从缓冲里剔掉。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice":
            return

        notice_type = str(raw.get("notice_type") or "")
        if "recall" not in notice_type:
            return

        # 撤回不回复：开关关掉就当没这回事
        if not _as_bool(self._cfg("recall_skip", True), True):
            self._debug(f"recall 忽略（开关关闭）{raw.get('message_id')}")
            event.stop_event()
            return

        umo = event.unified_msg_origin
        mid = str(raw.get("message_id") or "")
        buf = list(self._buffer.get(umo) or [])
        before = len(buf)
        kept = [item for item in buf if item[0] != mid]
        if len(kept) != before:
            self._buffer[umo] = kept

        self._debug(
            f"recall umo={umo} mid={mid} 命中={len(kept) != before} 剩余={len(kept)}"
        )
        logger.info("[chat_pacer] 撤回 %s，缓冲剩余 %s 条", mid, len(kept))

        # 通知本身不参与对话
        event.stop_event()

    # ------------------------------------------------------------ 防抖
    @filter.on_waiting_llm_request()
    async def coalesce(self, event: AstrMessageEvent):
        if not self._is_target(event):
            return

        text = (event.message_str or "").strip()
        if not text:
            return

        # 指令类不参与合并
        if text.startswith(("/", "／")):
            return

        umo = event.unified_msg_origin
        mid = str(getattr(event.message_obj, "message_id", "") or "")
        self._buffer.setdefault(umo, []).append((mid, text))
        self._version[umo] = self._version.get(umo, 0) + 1
        mine = self._version[umo]

        await asyncio.sleep(self._window())

        if self._version.get(umo) != mine:
            # 窗口内又来了新消息，这条交给后一条处理
            event.stop_event()
            return

        entries = self._buffer.pop(umo, [])
        self._version[umo] = 0
        merged = "\n".join(t for _, t in entries if t).strip()

        if not merged:
            # 全被撤回了，不回
            logger.info("[chat_pacer] 窗口内消息全部撤回，跳过回复")
            event.stop_event()
            return

        event.message_str = merged
        logger.info(
            "[chat_pacer] 合并 %s 条 → %s 字", len(entries), len(merged)
        )
