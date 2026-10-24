"""AstrBot 插件：多平台视频解析（astrbot_plugin_resolve）。

移植自工作区 GF 插件 `GF-plugin-video-parser`（`视频解析.ka` + `video-core.mjs`）：

- B 站：官方 web 接口取标题/封面/分 P/DASH 流，选最高画质 id（同画质 AVC 优先、
  再按码率），用 ffmpeg `-c copy` 合并音视频；不重新编码、不降分辨率/帧率。
  合并后超过 QQ 富媒体上限、没装 ffmpeg 或合并失败时，直接发 B 站原页面链接，
  绝不降级到 360P/720P。
- 抖音 / 快手 / 小红书 / 微博 / 西瓜 / 今日头条等：第三方聚合解析接口
  （默认 https://video.zacao.top/api/parse）。
- 解析完成后直接发送原视频；B 站 DASH 音频会分离出来单独发送 MP3，
  其他平台有独立音频地址时也会单独发送音频文件。
- v1.1.0 起取消 `v:<令牌>` / `a:<令牌>` 按钮与一次性消费逻辑，不需要再回复令牌。

与 GF 版的差异：GF 版用两个键盘回调按钮让用户选择原视频/音频；AstrBot 版
改为解析后自动发送，同时把音频从视频中分离出来单独发送。

作者: 桃薰
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:  # pragma: no cover - 依赖 AstrBot 运行时
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    _DATA_ROOT = Path(get_astrbot_data_path())
except Exception:  # pragma: no cover
    _DATA_ROOT = Path("data")

PLUGIN_NAME = "astrbot_plugin_resolve"

BILI_API = "https://api.bilibili.com"
DEFAULT_PARSE_API = "https://video.zacao.top/api/parse"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 单次下载的硬上限，防止异常链接把磁盘写满（GF 版 video-core 同样是 512MB）。
MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
FFMPEG_TIMEOUT = 10 * 60

# 链接末尾常见的中文标点 / 分享文案残留
TRAILING_PUNCT = ")]}>，。！？、；：'\"”’】》"
URL_RE = re.compile(
    r"https?://[^\s<>\"'，。！？、；：）】》\u4e00-\u9fff]+",
    re.I,
)


# 平台识别规则：与 GF 版 `视频解析.ka` / `video-core.mjs` 的平台正则保持一致。
PLATFORM_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("bilibili", "B站", re.compile(r"(^|\.)bilibili\.com$|(^|\.)b23\.tv$|(^|\.)bili2233\.cn$", re.I)),
    ("douyin", "抖音", re.compile(r"(^|\.)douyin\.com$|(^|\.)iesdouyin\.com$", re.I)),
    ("kuaishou", "快手", re.compile(r"(^|\.)kuaishou\.com$|(^|\.)gifshow\.com$|(^|\.)chenzhongtech\.com$", re.I)),
    ("xiaohongshu", "小红书", re.compile(r"(^|\.)xiaohongshu\.com$|(^|\.)xhslink\.com$", re.I)),
    ("weibo", "微博", re.compile(r"(^|\.)weibo\.com$|(^|\.)weibo\.cn$", re.I)),
    ("xigua", "西瓜视频", re.compile(r"(^|\.)ixigua\.com$", re.I)),
    ("toutiao", "今日头条", re.compile(r"(^|\.)toutiao\.com$", re.I)),
    ("acfun", "AcFun", re.compile(r"(^|\.)acfun\.cn$", re.I)),
    ("weishi", "微视", re.compile(r"(^|\.)weishi\.qq\.com$", re.I)),
    ("pipixia", "皮皮虾", re.compile(r"(^|\.)pipix\.com$", re.I)),
    ("meipai", "美拍", re.compile(r"(^|\.)meipai\.com$", re.I)),
    ("xinpianchang", "新片场", re.compile(r"(^|\.)xinpianchang\.com$", re.I)),
    ("haokan", "好看视频", re.compile(r"(^|\.)haokan\.baidu\.com$", re.I)),
    ("lishipin", "梨视频", re.compile(r"(^|\.)lishipin\.com$", re.I)),
    ("huoshan", "火山小视频", re.compile(r"(^|\.)huoshan\.com$", re.I)),
    ("doubao", "豆包", re.compile(r"(^|\.)doubao\.com$", re.I)),
    ("jianying", "剪映", re.compile(r"(^|\.)jianying\.com$", re.I)),
    ("tiktok", "TikTok", re.compile(r"(^|\.)tiktok\.com$", re.I)),
    ("twitter", "X / Twitter", re.compile(r"(^|\.)x\.com$|(^|\.)twitter\.com$", re.I)),
]


class ParseError(Exception):
    """可预期的解析失败（直接展示给用户）。"""


@dataclass
class MediaInfo:
    """一次解析得到的媒体信息。"""

    platform_id: str
    platform: str
    title: str
    link: str
    cover: str = ""
    author: str = ""
    duration: str = ""
    view: str = ""
    desc: str = ""
    bvid: str = ""
    avid: str = ""
    cid: str = ""
    page: int = 1
    video_url: str = ""
    audio_url: str = ""
    audio_format: str = "mp3"
    video_size: int = 0
    video_silent: bool = False
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@register(
    PLUGIN_NAME,
    "桃薰",
    "多平台视频解析：B站原画 DASH 合并、抖音/快手/小红书等聚合解析，"
    "解析后自动发送原视频并分离音频",
    "1.1.0",
)
class ResolvePlugin(Star):
    """多平台视频解析插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = _DATA_ROOT / "plugin_data" / PLUGIN_NAME
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._ffmpeg: str | None = None  # None=未探测，""=不可用，其他=路径
        self._ffmpeg_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        await self._get_client()

    async def terminate(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------------ #
    # 配置 / HTTP 客户端
    # ------------------------------------------------------------------ #
    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value) if value is not None else default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    @property
    def _ua(self) -> str:
        return self._cfg_str("ua", DEFAULT_UA).strip() or DEFAULT_UA

    def _request_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(float(max(5, self._cfg_int("request_timeout", 30))), connect=10.0)

    def _download_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(float(max(30, self._cfg_int("download_timeout", 300))), connect=15.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            kwargs: dict[str, Any] = {
                "timeout": self._request_timeout(),
                "follow_redirects": True,
                "headers": {
                    "User-Agent": self._ua,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            }
            proxy = self._cfg_str("proxy", "").strip()
            if proxy:
                try:  # httpx >= 0.26
                    self._client = httpx.AsyncClient(proxy=proxy, **kwargs)
                except TypeError:  # httpx <= 0.25
                    self._client = httpx.AsyncClient(proxies=proxy, **kwargs)
            else:
                self._client = httpx.AsyncClient(**kwargs)
            return self._client

    # ------------------------------------------------------------------ #
    # 消息入口
    # ------------------------------------------------------------------ #
    @filter.regex(
        r"^\s*[/／]?\s*(?:解析帮助|视频解析帮助|解析help)\s*$"
        r"|^\s*[/／]?\s*(?:解析|视频解析|解析视频)(?:\s|$)"
        r"|https?://"
        r"|^\s*(?:BV[0-9A-Za-z]{10}|av\d+)\s*$"
    )
    async def on_message(self, event: AstrMessageEvent) -> None:
        """处理帮助、显式「解析」和裸链接；解析后自动发送视频与音频。"""
        try:
            text = (event.message_str or "").strip()
            # 兼容 AstrBot 用户习惯的 /指令 写法；message_str 已被平台唤醒阶段
            # 去掉前缀时这里的替换不会产生副作用。
            text = re.sub(r"^[/／]\s*", "", text)
            if not text:
                return

            if text.lower() in ("解析帮助", "视频解析帮助", "解析help"):
                self._claim_event(event)
                await self._send_text(event, self._help_text())
                return

            explicit = bool(re.match(r"^\s*(?:解析|视频解析|解析视频)\s*", text))
            if not explicit and not self._cfg_bool("auto_parse_link", True):
                return

            url = self._extract_url(text)
            if not url:
                if explicit:
                    self._claim_event(event)
                    await self._send_text(
                        event,
                        "请在「解析」后面附上视频链接，例如：\n"
                        "解析 https://www.bilibili.com/video/BV1xx411c7mD\n\n"
                        "也可以直接发送链接。",
                    )
                return

            platform = self._detect_platform(url)
            if platform is None:
                if explicit:
                    self._claim_event(event)
                    await self._send_text(
                        event,
                        "暂不支持这个平台的链接。\n"
                        "目前支持：B站、抖音、快手、小红书、微博、西瓜、今日头条、"
                        "皮皮虾、微视等。",
                    )
                return

            self._claim_event(event)
            await self._handle_parse(event, url, platform)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 处理消息失败: {exc}")
            try:
                await self._send_text(event, "解析插件处理失败，请稍后重试或查看 AstrBot 日志。")
            except Exception:  # noqa: BLE001
                pass

    def _claim_event(self, event: AstrMessageEvent) -> None:
        """标记事件已处理，避免消息继续进入 LLM。"""
        event.stop_event()
        try:
            event.should_call_llm(False)
        except AttributeError:  # 兼容旧版 AstrBot
            pass

    # ------------------------------------------------------------------ #
    # 帮助 / 卡片
    # ------------------------------------------------------------------ #
    def _help_text(self) -> str:
        return (
            "🎬 多平台视频解析\n\n"
            "用法：\n"
            "解析 https://www.bilibili.com/video/BV...\n"
            "解析 https://v.douyin.com/.../\n"
            "也可以直接发送链接。\n\n"
            "支持：B站、抖音、快手、小红书、微博、西瓜、今日头条、皮皮虾、微视等。\n\n"
            "解析后会自动发送原视频；B站会额外把音频分离出来单独发送 MP3，\n"
            "其他平台有独立音频地址时也会单独发送音频文件。\n"
            "不需要再点按钮或回复令牌。\n\n"
            "B站：官方接口 + DASH 原画，ffmpeg -c copy 合并，不降分辨率/帧率；\n"
            "其他平台：聚合解析接口。"
        )

    async def _send_caption(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> None:
        ok, error = await self._send_text(event, self._caption_text(cache))
        if not ok:
            logger.warning(f"[{PLUGIN_NAME}] 发送解析信息失败: {error}")
            return
        cover = str(cache.get("cover") or "")
        if (
            cover.startswith("http://") or cover.startswith("https://")
        ) and self._cfg_bool("send_cover", False):
            sent, error = await self._send_chain(event, [Comp.Image.fromURL(cover)])
            if not sent:
                logger.debug(f"[{PLUGIN_NAME}] 封面发送失败（忽略）: {error}")

    def _caption_text(self, cache: dict[str, Any]) -> str:
        title = str(cache.get("title") or "未命名作品")
        title = title.replace("[", "［").replace("]", "］")
        lines = [f"🎬 {title}"]

        platform = str(cache.get("platform") or "")
        author = str(cache.get("author") or "")
        duration = str(cache.get("duration") or "")
        view = str(cache.get("view") or "")
        images = cache.get("images") or []

        if platform:
            lines.append(f"平台：{platform}")
        if author:
            lines.append(f"作者：{author}")
        if duration:
            lines.append(f"时长：{duration}")
        if view:
            lines.append(f"播放：{view}")
        if images:
            lines.append(f"图集：{len(images)} 张")

        lines.append("")
        if self._has_video(cache):
            lines.append("正在发送原视频…")
        if self._has_audio(cache):
            lines.append("音频将分离后单独发送。")
        return "\n".join(lines)

    @staticmethod
    def _has_video(cache: dict[str, Any]) -> bool:
        if (
            cache.get("platform_id") == "bilibili"
            and cache.get("bvid")
            and cache.get("cid")
            and not cache.get("video_silent")
        ):
            return True
        if cache.get("video_url") and not cache.get("video_silent"):
            return True
        if cache.get("images"):
            return True
        # B 站即使只能回退到原页面链接，也保留「原视频」入口
        return cache.get("platform_id") == "bilibili"

    @staticmethod
    def _has_audio(cache: dict[str, Any]) -> bool:
        # 只有解析阶段确实拿到了音频地址才自动发送音频；
        # B 站 DASH 音频地址会写入 audio_url。
        return bool(cache.get("audio_url"))

    # ------------------------------------------------------------------ #
    # 发送封装
    # ------------------------------------------------------------------ #
    async def _send_chain(
        self, event: AstrMessageEvent, components: list[Any]
    ) -> tuple[bool, str]:
        try:
            message = MessageChain(chain=list(components))
            # 卡片本身是纯文本，强制按纯文本发送，避免 QQ 官方机器人
            # 未开通 Markdown 权限时整条消息失败；旧版 AstrBot 没有该方法则跳过。
            try:
                message.use_markdown(False)
            except AttributeError:
                pass
            await event.send(message)
            return True, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 发送消息失败: {exc}")
            return False, str(exc)

    async def _send_text(self, event: AstrMessageEvent, text: str) -> tuple[bool, str]:
        return await self._send_chain(event, [Comp.Plain(text)])

    # ------------------------------------------------------------------ #
    # 解析入口
    # ------------------------------------------------------------------ #
    async def _handle_parse(
        self,
        event: AstrMessageEvent,
        url: str,
        platform: tuple[str, str],
    ) -> None:
        platform_id, platform_name = platform
        logger.info(f"[{PLUGIN_NAME}] 开始解析 {platform_name}: {url[:120]}")
        try:
            media = await self._parse_media(url, platform_id, platform_name)
        except ParseError as exc:
            await self._send_text(event, f"解析失败：{exc}")
            return
        except httpx.TimeoutException:
            await self._send_text(event, "解析超时：接口或视频地址响应太慢，请稍后重试。")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 解析异常: {exc}")
            await self._send_text(event, "解析失败：插件内部错误，请稍后重试或查看日志。")
            return

        # 只需要一个本次发送的短 ID 用于本地媒体文件名；不再生成用户令牌，
        # 也不再写解析缓存。
        session_id = self._new_session_id()
        cache = media.to_dict()
        cache.update({"token": session_id, "created": int(time.time())})
        self._cleanup_cache()

        await self._send_caption(event, cache)
        await self._auto_send(event, cache)

    async def _auto_send(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> None:
        """取消按钮后：解析完成立即发送原视频，并把音频分离出来单独发送。"""
        try:
            ok, error = await self._deliver_video(event, cache)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 自动发送视频失败: {exc}")
            ok, error = False, f"视频发送失败：{exc}"
        if not ok and self._has_video(cache):
            await self._send_text(event, error or "视频发送失败，请稍后重试。")

        if not cache.get("audio_url"):
            return
        try:
            ok, error = await self._deliver_audio(event, cache)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 自动发送音频失败: {exc}")
            ok, error = False, f"音频发送失败：{exc}"
        if not ok and "没有可提取的音频" not in (error or ""):
            await self._send_text(event, error or "音频发送失败，请稍后重试。")

    async def _parse_media(
        self, url: str, platform_id: str, platform_name: str
    ) -> MediaInfo:
        if platform_id == "bilibili":
            return await self._parse_bilibili(url)
        return await self._parse_third_party(url, platform_id, platform_name)

    # ------------------------------------------------------------------ #
    # 链接 / 平台识别
    # ------------------------------------------------------------------ #
    def _extract_url(self, text: str) -> str:
        match = URL_RE.search(text)
        if match:
            url = match.group(0)
        else:
            match = re.search(r"((?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+)", text, re.I)
            if match:
                url = "https://" + match.group(1)
            else:
                match = re.fullmatch(r"\s*(BV[0-9A-Za-z]{10})\s*", text, re.I)
                if match:
                    url = "https://www.bilibili.com/video/" + match.group(1)
                else:
                    match = re.fullmatch(r"\s*(av\d+)\s*", text, re.I)
                    if match:
                        url = "https://www.bilibili.com/video/" + match.group(1)
                    else:
                        return ""

        url = url.replace("&amp;", "&").strip()
        url = url.rstrip(TRAILING_PUNCT)
        # 去掉英文句末标点；只去末尾，避免破坏域名里的点
        url = re.sub(r"[.,;!?]+$", "", url)
        return url

    @staticmethod
    def _detect_platform(url: str) -> tuple[str, str] | None:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return None
        if not host:
            return None
        for platform_id, platform_name, pattern in PLATFORM_RULES:
            if pattern.search(host):
                return platform_id, platform_name
        return None

    # ------------------------------------------------------------------ #
    # 本地媒体文件
    # ------------------------------------------------------------------ #
    def _new_session_id(self) -> str:
        """生成本次发送的短 ID，仅用于本地媒体文件名。"""
        return f"{int(time.time() * 1000):x}{secrets.token_hex(4)}"

    def _cleanup_cache(self) -> None:
        """清理旧版 v1.0 的解析缓存与孤儿媒体文件。"""
        ttl = 24 * 3600
        now = time.time()
        suffixes = (
            "-video.mp4",
            "-audio.mp3",
            "-audio.m4a",
            "-videoSrc.m4s",
            "-audioSrc.m4s",
        )
        try:
            paths = list(self.cache_dir.glob("*.json"))
        except OSError:
            return
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                created = float(data.get("created") or 0)
            except (OSError, ValueError, TypeError):
                continue
            if created and now - created <= ttl:
                continue
            token = path.stem
            for suffix in suffixes:
                (self.cache_dir / f"{token}{suffix}").unlink(missing_ok=True)
            path.unlink(missing_ok=True)

        # 没有 JSON 引用的媒体文件最多保留 24 小时
        cutoff = now - 24 * 3600
        try:
            entries = list(self.cache_dir.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.is_file() or path.suffix == ".json" or path.name.endswith(".part"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 自动发送视频
    # ------------------------------------------------------------------ #
    async def _deliver_video(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        if (
            cache.get("platform_id") == "bilibili"
            and cache.get("bvid")
            and cache.get("cid")
            and not cache.get("video_silent")
        ):
            return await self._deliver_bili_video(event, cache)

        if cache.get("video_url") and not cache.get("video_silent"):
            return await self._deliver_remote_video(event, cache)

        if cache.get("images"):
            return await self._deliver_images(event, cache)

        if cache.get("platform_id") == "bilibili":
            link = str(cache.get("link") or "")
            await self._send_text(
                event,
                "这条 B 站解析没有建立原画会话，已改为发送原页面链接：\n" + link,
            )
            return True, ""

        return False, "这条解析没有可发送的视频或图片。"

    async def _deliver_images(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        images = [str(u) for u in (cache.get("images") or []) if str(u).startswith("http")]
        limit = max(1, min(9, self._cfg_int("image_limit", 9)))
        images = images[:limit]
        if not images:
            return False, "这条解析的图集地址为空，请重新发送链接解析。"
        ok, error = await self._send_chain(
            event, [Comp.Image.fromURL(url) for url in images]
        )
        if ok:
            return True, ""
        return (
            False,
            "图片发送失败："
            + error
            + "\n图片直链：\n"
            + "\n".join(images),
        )

    async def _deliver_remote_video(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        video_url = str(cache.get("video_url") or "")
        link = str(cache.get("link") or "")
        platform_id = str(cache.get("platform_id") or "")
        limit_mb = max(1, min(64, self._cfg_int("video_limit_mb", 60)))
        limit = limit_mb * 1024 * 1024

        size = 0
        try:
            size = int(cache.get("video_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            size = await self._probe_size(video_url, platform_id)

        if size > limit:
            await self._send_text(
                event,
                f"视频约 {size / 1048576:.1f} MB，超过 QQ 富媒体上限 {limit_mb}MB。\n"
                f"已改为发送直链，可直接下载或播放：\n{video_url}\n\n原页面：\n{link}",
            )
            return True, ""

        if re.search(r"\.m3u8(?:[?#]|$)", video_url, re.I):
            await self._send_text(
                event,
                "这个平台返回的是 m3u8 流，无法直接上传为 QQ 视频，已改为发送直链：\n"
                + video_url,
            )
            return True, ""

        ok, error = await self._send_chain(event, [Comp.Video.fromURL(video_url)])
        if ok:
            return True, ""
        return (
            False,
            "视频发送失败："
            + error
            + "\n直链可能已过期，请重新发送链接解析。\n\n直链：\n"
            + video_url,
        )

    # ------------------------------------------------------------------ #
    # 自动发送分离音频
    # ------------------------------------------------------------------ #
    async def _deliver_audio(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        # B站官方接口成功时走 DASH 音轨（bvid+cid 存在）；
        # 如果官方接口失败、只剩聚合接口的 audio_url，就按普通远程音频处理。
        if (
            cache.get("platform_id") == "bilibili"
            and cache.get("bvid")
            and cache.get("cid")
        ):
            return await self._deliver_bili_audio(event, cache)
        if cache.get("audio_url"):
            return await self._deliver_remote_audio(event, cache)
        if cache.get("platform_id") == "bilibili":
            return await self._deliver_bili_audio(event, cache)
        return False, "这个作品没有可提取的音频。"

    async def _deliver_remote_audio(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        token = str(cache["token"])
        audio_url = str(cache.get("audio_url") or "")
        platform_id = str(cache.get("platform_id") or "")
        audio_format = str(cache.get("audio_format") or "mp3").lower()
        headers = self._media_headers(audio_url, platform_id)

        if audio_format == "mp3":
            try:
                path = await self._download_file(
                    audio_url,
                    self.cache_dir / f"{token}-audio.mp3",
                    headers=headers,
                    timeout=self._download_timeout(),
                    max_bytes=MAX_AUDIO_BYTES,
                )
                ok, error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.mp3", file=str(path))],
                )
                if ok:
                    return True, ""
                logger.warning(f"[{PLUGIN_NAME}] MP3 文件发送失败，改用直链: {error}")
                ok, url_error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.mp3", url=audio_url)],
                )
                if ok:
                    return True, ""
                return (
                    False,
                    f"音频发送失败：{error}；{url_error}\n直链：\n{audio_url}",
                )
            except ParseError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 音频下载失败，改用直链: {exc}")
                ok, error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.mp3", url=audio_url)],
                )
                if ok:
                    return True, ""
                return False, f"音频下载/发送失败：{exc}；{error}\n直链：\n{audio_url}"

        # m4a / 其他格式：先试备用转码接口，再试本机 ffmpeg，最后按原格式发送
        converted = await self._convert_via_api(audio_url, token)
        if converted:
            try:
                path = await self._download_file(
                    converted,
                    self.cache_dir / f"{token}-audio.mp3",
                    headers=headers,
                    timeout=self._download_timeout(),
                    max_bytes=MAX_AUDIO_BYTES,
                )
                ok, error = await self._send_chain(
                    event, [Comp.File(name=f"MK-{token}.mp3", file=str(path))]
                )
                if ok:
                    return True, ""
                logger.warning(f"[{PLUGIN_NAME}] 转码音频发送失败: {error}")
            except ParseError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 转码结果下载失败: {exc}")

        ffmpeg = await self._ensure_ffmpeg()
        source: Path | None = None
        try:
            source = await self._download_file(
                audio_url,
                self.cache_dir / f"{token}-audioSrc.m4s",
                headers=headers,
                timeout=self._download_timeout(),
                max_bytes=MAX_AUDIO_BYTES,
            )
        except ParseError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 源音频下载失败: {exc}")

        if source is not None and ffmpeg:
            mp3_path = self.cache_dir / f"{token}-audio.mp3"
            try:
                await self._to_mp3(source, mp3_path)
                ok, error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.mp3", file=str(mp3_path))],
                )
                if ok:
                    return True, ""
                logger.warning(f"[{PLUGIN_NAME}] MP3 发送失败: {error}")
            except ParseError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 导出 MP3 失败: {exc}")

        if source is not None:
            m4a_path = self.cache_dir / f"{token}-audio.m4a"
            try:
                if ffmpeg:
                    await self._to_m4a(source, m4a_path)
                else:
                    shutil.copyfile(source, m4a_path)
            except (OSError, ParseError) as exc:
                logger.warning(f"[{PLUGIN_NAME}] 生成 m4a 失败: {exc}")
                m4a_path = None
            if m4a_path is not None and m4a_path.exists():
                ok, error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.m4a", file=str(m4a_path))],
                )
                if ok:
                    await self._send_text(
                        event,
                        "这个平台的源音频是 m4a，已按原格式发送。"
                        "如需 MP3，请安装 ffmpeg 或配置转码接口后重新解析。",
                    )
                    return True, ""
                logger.warning(
                    f"[{PLUGIN_NAME}] m4a 文件发送失败，改用音频直链: {error}"
                )

        # 最后兜底：把接口音频 URL 直接交给平台下载
        ok, error = await self._send_chain(
            event, [Comp.File(name=f"MK-{token}.m4a", url=audio_url)]
        )
        if ok:
            return True, ""
        return (
            False,
            "音频发送失败："
            + error
            + "\n直链可能已过期，请重新发送链接解析。\n\n直链：\n"
            + audio_url,
        )

    # ------------------------------------------------------------------ #
    # B 站：官方接口 + DASH 原画
    # ------------------------------------------------------------------ #
    async def _parse_bilibili(self, url: str) -> MediaInfo:
        try:
            return await self._parse_bilibili_official(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] B站官方接口失败，尝试聚合接口: {exc}")
            try:
                media = await self._parse_third_party(url, "bilibili", "B站")
            except Exception as third_exc:  # noqa: BLE001
                raise ParseError(
                    f"B站解析失败：{exc}；聚合接口：{third_exc}"
                ) from third_exc
            # 聚合接口拿到的 B 站直链通常是纯视频流（没有音轨），
            # 与 GF 版 video-core 的 `视频无音=1` 行为一致：视频按钮改发原页面链接。
            media.video_silent = True
            media.platform = "B站"
            return media

    async def _parse_bilibili_official(self, url: str) -> MediaInfo:
        resolved = await self._resolve_b23(url)
        bvid, avid, page = self._bili_ids(resolved)
        if not bvid and not avid:
            raise ParseError("无法从链接中识别 BV/av 号")

        view_params: dict[str, str] = {"bvid": bvid} if bvid else {"aid": avid}
        view = await self._bili_api(
            "/x/web-interface/view", view_params, referer=resolved
        )

        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        index = page - 1
        page_info = (
            pages[index]
            if 0 <= index < len(pages)
            else (pages[0] if pages else {})
        )
        cid = str(page_info.get("cid") or view.get("cid") or "")
        if not cid:
            raise ParseError("B站接口没有返回 cid")

        play_params = {
            "cid": cid,
            "qn": "80",
            "fnval": "4048",
            "fnver": "0",
            "fourk": "1",
            "platform": "pc",
            "try_look": "1",
        }
        if bvid:
            play_params["bvid"] = bvid
        else:
            play_params["avid"] = avid
        play = await self._bili_api(
            "/x/player/playurl", play_params, referer=resolved
        )
        return self._bili_media_from(resolved, view, play, page)

    def _bili_media_from(
        self,
        url: str,
        view: dict[str, Any],
        play: dict[str, Any],
        page: int,
    ) -> MediaInfo:
        pages = view.get("pages") if isinstance(view.get("pages"), list) else []
        index = max(0, page - 1)
        page_info = (
            pages[index]
            if index < len(pages)
            else (pages[0] if pages else {})
        )
        title = str(view.get("title") or "").strip() or "B站视频"
        if len(pages) > 1 and index < len(pages) and page_info.get("part"):
            title = f"{title} - P{page} {page_info.get('part')}"

        cid = str(page_info.get("cid") or view.get("cid") or "")
        if not cid:
            raise ParseError("B站接口没有返回 cid")

        dash = play.get("dash") if isinstance(play, dict) else {}
        if not isinstance(dash, dict):
            dash = {}
        video_stream = self._pick_dash_video(dash.get("video"))
        audio_stream = self._pick_dash_audio(dash.get("audio"))
        if video_stream is None and audio_stream is None:
            raise ParseError("B站没有返回可用的视频流（可能是会员/地区/付费视频）")

        video_urls = self._stream_urls(video_stream)
        audio_urls = self._stream_urls(audio_stream)
        owner = view.get("owner") if isinstance(view.get("owner"), dict) else {}
        stat = view.get("stat") if isinstance(view.get("stat"), dict) else {}
        duration = page_info.get("duration") or view.get("duration") or 0

        bvid = str(view.get("bvid") or "")
        avid = str(view.get("aid") or "")
        if not bvid or not avid:
            fallback_bv, fallback_av, _ = self._bili_ids(url)
            bvid = bvid or fallback_bv
            avid = avid or fallback_av
        media = MediaInfo(
            platform_id="bilibili",
            platform="B站",
            title=title,
            link=url,
            cover=self._upgrade_https(str(view.get("pic") or "")),
            author=str(owner.get("name") or ""),
            duration=self._format_duration(duration),
            view=str(stat.get("view") or ""),
            desc=str(view.get("desc") or ""),
            bvid=bvid,
            avid=avid,
            cid=cid,
            page=page,
            video_url=video_urls[0] if video_urls else "",
            audio_url=audio_urls[0] if audio_urls else "",
            audio_format="m4a",
            video_silent=False,
        )
        return media

    @staticmethod
    def _bili_ids(url: str) -> tuple[str, str, int]:
        bv_match = re.search(r"/video/(BV[0-9A-Za-z]+)", url, re.I)
        av_match = re.search(r"/video/av(\d+)", url, re.I)
        page_match = re.search(r"[?&]p=(\d+)", url)
        page = int(page_match.group(1)) if page_match else 1
        return (
            bv_match.group(1) if bv_match else "",
            av_match.group(1) if av_match else "",
            max(1, page),
        )

    async def _resolve_b23(self, url: str) -> str:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return url
        if host not in ("b23.tv", "bili2233.cn"):
            return url
        try:
            client = await self._get_client()
            response = await client.get(
                url, headers=self._bili_headers(), timeout=self._request_timeout()
            )
            final_url = str(response.url)
            if re.search(r"bilibili\.com/video", final_url, re.I):
                return final_url
            match = re.search(
                r"https?://(?:www\.)?bilibili\.com/video/[^\"'<\s]+",
                response.text,
                re.I,
            )
            if match:
                return match.group(0)
            return final_url or url
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 解析 b23 短链失败: {exc}")
            return url

    def _bili_headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._ua,
            "Referer": referer or "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookie = self._cfg_str("bili_cookie", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _bili_api(
        self,
        path: str,
        params: dict[str, Any],
        referer: str | None = None,
    ) -> dict[str, Any]:
        try:
            client = await self._get_client()
            response = await client.get(
                BILI_API + path,
                params=params,
                headers=self._bili_headers(referer),
                timeout=self._request_timeout(),
            )
        except httpx.HTTPError as exc:
            raise ParseError(f"B站接口请求失败：{exc}") from exc

        if response.status_code == 412:
            raise ParseError(
                "B站接口拒绝访问（412）。可以稍后重试、换网络，"
                "或在插件配置里填写 bili_cookie（浏览器登录 B 站后的 Cookie）。"
            )
        if response.status_code != 200:
            raise ParseError(f"B站接口 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError("B站接口返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise ParseError("B站接口返回格式异常")
        code = payload.get("code", 0)
        if code != 0:
            message = str(payload.get("message") or "").strip()
            raise ParseError(f"B站接口 code={code} {message}".strip())
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ParseError("B站接口没有返回数据")
        return data

    @staticmethod
    def _pick_dash_video(streams: Any) -> dict[str, Any] | None:
        if not isinstance(streams, list):
            return None
        items = [item for item in streams if isinstance(item, dict)]
        if not items:
            return None
        # 先锁定最高画质 id，绝不为了编码兼容性降低分辨率；
        # 同一画质内优先 AVC（codecid=7），再按码率取最高。
        max_id = max(ResolvePlugin._to_int(item.get("id")) for item in items)
        top = [
            item
            for item in items
            if ResolvePlugin._to_int(item.get("id")) == max_id
        ]
        avc = [
            item for item in top if ResolvePlugin._to_int(item.get("codecid")) == 7
        ]
        pool = avc or top
        return max(pool, key=lambda item: ResolvePlugin._to_int(item.get("bandwidth")))

    @staticmethod
    def _pick_dash_audio(streams: Any) -> dict[str, Any] | None:
        if not isinstance(streams, list):
            return None
        items = [item for item in streams if isinstance(item, dict)]
        if not items:
            return None

        def rank(item: dict[str, Any]) -> tuple[int, int]:
            audio_id = ResolvePlugin._to_int(item.get("id"))
            priority = 3 if audio_id == 30280 else 2 if audio_id == 30232 else 1
            return priority, ResolvePlugin._to_int(item.get("bandwidth"))

        return max(items, key=rank)

    @staticmethod
    def _stream_urls(stream: dict[str, Any] | None) -> list[str]:
        if not isinstance(stream, dict):
            return []
        base = stream.get("baseUrl") or stream.get("base_url") or ""
        backups = stream.get("backupUrl") or stream.get("backup_url") or []
        if not isinstance(backups, list):
            backups = []
        urls: list[str] = []
        for value in [base, *backups]:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value.startswith("//"):
                value = "https:" + value
            if value.startswith("http://"):
                value = "https://" + value[len("http://") :]
            if value.startswith("https://"):
                urls.append(value)
        return list(dict.fromkeys(urls))

    async def _bili_play(self, cache: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cid": cache.get("cid") or "",
            "qn": "80",
            "fnval": "4048",
            "fnver": "0",
            "fourk": "1",
            "platform": "pc",
            "try_look": "1",
        }
        bvid = str(cache.get("bvid") or "")
        avid = str(cache.get("avid") or "")
        if bvid:
            params["bvid"] = bvid
        elif avid:
            params["avid"] = avid
        else:
            raise ParseError("缓存里没有 B 站 bvid/avid")
        return await self._bili_api(
            "/x/player/playurl", params, referer=str(cache.get("link") or "")
        )

    async def _deliver_bili_video(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        token = str(cache["token"])
        link = str(cache.get("link") or "")
        ffmpeg = await self._ensure_ffmpeg()
        if not ffmpeg:
            await self._send_text(
                event,
                "本机没有安装 ffmpeg，无法合并 B 站原画音视频。\n"
                "已改为发送原页面链接（原画质/原帧率）：\n" + link,
            )
            return True, ""

        video_stream: dict[str, Any] | None = None
        audio_stream: dict[str, Any] | None = None
        try:
            play = await self._bili_play(cache)
            dash = play.get("dash") if isinstance(play, dict) else {}
            if isinstance(dash, dict):
                video_stream = self._pick_dash_video(dash.get("video"))
                audio_stream = self._pick_dash_audio(dash.get("audio"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 重新获取 B 站播放流失败: {exc}")

        if video_stream is None and cache.get("video_url"):
            video_stream = {"baseUrl": cache.get("video_url")}
        if audio_stream is None and cache.get("audio_url"):
            audio_stream = {"baseUrl": cache.get("audio_url")}

        if video_stream is None:
            await self._send_text(
                event,
                "B 站没有返回可用的原画视频流，已改为发送原页面链接：\n" + link,
            )
            return True, ""

        try:
            video_path = await self._download_bili_stream(
                video_stream, token, "videoSrc"
            )
            audio_path = (
                await self._download_bili_stream(audio_stream, token, "audioSrc")
                if audio_stream is not None
                else None
            )
            output = self.cache_dir / f"{token}-video.mp4"
            if audio_path is not None:
                await self._merge_av(video_path, audio_path, output)
            else:
                await self._remux_video(video_path, output)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] B 站原画合并失败: {exc}")
            await self._send_text(
                event,
                f"B 站原画合并失败（{exc}），已改为发送原页面链接：\n{link}",
            )
            return True, ""

        size = output.stat().st_size if output.exists() else 0
        limit_mb = max(1, min(64, self._cfg_int("bili_limit_mb", 64)))
        limit = limit_mb * 1024 * 1024
        if size > limit:
            output.unlink(missing_ok=True)
            await self._send_text(
                event,
                f"B 站原画合并后约 {size / 1048576:.1f} MB，"
                f"超过 QQ 富媒体上限 {limit_mb}MB。\n"
                "已改为发送原页面链接（原画质/原帧率）：\n" + link,
            )
            return True, ""

        ok, error = await self._send_chain(
            event, [Comp.Video.fromFileSystem(str(output))]
        )
        if ok:
            return True, ""
        # 与 GF 版一致：原画发送失败不再降画质，直接补发原页面链接
        await self._send_text(
            event,
            f"原画视频发送失败（{error}），已改为发送原页面链接：\n{link}",
        )
        return True, ""

    async def _deliver_bili_audio(
        self, event: AstrMessageEvent, cache: dict[str, Any]
    ) -> tuple[bool, str]:
        token = str(cache["token"])
        ffmpeg = await self._ensure_ffmpeg()

        audio_stream: dict[str, Any] | None = None
        try:
            play = await self._bili_play(cache)
            dash = play.get("dash") if isinstance(play, dict) else {}
            if isinstance(dash, dict):
                audio_stream = self._pick_dash_audio(dash.get("audio"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 重新获取 B 站音频流失败: {exc}")
        if audio_stream is None and cache.get("audio_url"):
            audio_stream = {"baseUrl": cache.get("audio_url")}
        if audio_stream is None:
            return False, "这个作品没有可提取的音频。"

        try:
            source = await self._download_bili_stream(audio_stream, token, "audioSrc")
        except ParseError as exc:
            return False, f"音频下载失败：{exc}"

        if ffmpeg:
            mp3_path = self.cache_dir / f"{token}-audio.mp3"
            try:
                await self._to_mp3(source, mp3_path)
                ok, error = await self._send_chain(
                    event,
                    [Comp.File(name=f"MK-{token}.mp3", file=str(mp3_path))],
                )
                if ok:
                    return True, ""
                logger.warning(
                    f"[{PLUGIN_NAME}] B 站 MP3 发送失败，改发 m4a: {error}"
                )
            except ParseError as exc:
                logger.warning(f"[{PLUGIN_NAME}] B 站音频导出 MP3 失败: {exc}")

        # 没有 ffmpeg 或 MP3 导出失败：按 m4a 原格式发送
        m4a_path = self.cache_dir / f"{token}-audio.m4a"
        try:
            if ffmpeg:
                await self._to_m4a(source, m4a_path)
            else:
                shutil.copyfile(source, m4a_path)
        except (OSError, ParseError) as exc:
            logger.warning(f"[{PLUGIN_NAME}] B 站音频转 m4a 失败: {exc}")
            return False, f"音频转码失败：{exc}"

        ok, error = await self._send_chain(
            event, [Comp.File(name=f"MK-{token}.m4a", file=str(m4a_path))]
        )
        if not ok:
            return False, f"音频发送失败：{error}"
        if ffmpeg:
            await self._send_text(
                event,
                "MP3 转码失败，已按 m4a 原格式发送。请检查本机 ffmpeg 是否可用。",
            )
        else:
            await self._send_text(
                event,
                "本机没有 ffmpeg，无法导出 MP3，已按 m4a 原格式发送。",
            )
        return True, ""

    async def _download_bili_stream(
        self,
        stream: dict[str, Any],
        token: str,
        key: str,
        suffix: str = ".m4s",
    ) -> Path:
        urls = self._stream_urls(stream)
        if not urls:
            raise ParseError("B站没有返回下载地址")
        dest = self.cache_dir / f"{token}-{key}{suffix}"
        # 同一次自动发送中，视频合并和音频导出会用到同一份 DASH 音轨，
        # 已经下载成功就直接复用，避免重复下载。
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        last_error: Exception | None = None
        for attempt in range(2):
            for url in urls:
                try:
                    return await self._download_file(
                        url,
                        dest,
                        headers=self._bili_headers(),
                        timeout=self._download_timeout(),
                        max_bytes=MAX_MEDIA_BYTES,
                    )
                except ParseError as exc:
                    last_error = exc
                    logger.warning(
                        f"[{PLUGIN_NAME}] B站流下载失败（第 {attempt + 1} 轮）: {exc}"
                    )
        raise ParseError(f"B站流下载失败：{last_error or '未知错误'}")

    # ------------------------------------------------------------------ #
    # 第三方聚合解析
    # ------------------------------------------------------------------ #
    async def _parse_third_party(
        self, url: str, platform_id: str, platform_name: str
    ) -> MediaInfo:
        api = self._cfg_str("parse_api", DEFAULT_PARSE_API).strip()
        if not api:
            raise ParseError("未配置第三方解析接口（parse_api）")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": self._ua,
            "Accept": "application/json, text/plain, */*",
        }
        api_key = self._cfg_str("parse_api_key", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key

        try:
            client = await self._get_client()
            response = await client.post(
                api,
                json={"text": url},
                headers=headers,
                timeout=self._request_timeout(),
            )
        except httpx.HTTPError as exc:
            raise ParseError(f"解析接口请求失败：{exc}") from exc

        if response.status_code != 200:
            raise ParseError(f"解析接口 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError("解析接口返回的不是 JSON") from exc

        return self._media_from_third_party(
            payload, url, platform_id, platform_name
        )

    def _media_from_third_party(
        self,
        payload: Any,
        url: str,
        platform_id: str,
        platform_name: str,
    ) -> MediaInfo:
        if not isinstance(payload, dict):
            raise ParseError("解析接口返回格式异常")

        code = payload.get("code")
        if code is not None and str(code) not in ("0", "200", "200.0"):
            message = str(payload.get("message") or payload.get("msg") or "").strip()
            raise ParseError(f"接口返回 {code}：{message or '未知错误'}")

        if payload.get("success") is False or payload.get("succ") is False:
            message = str(payload.get("message") or payload.get("msg") or "").strip()
            raise ParseError(f"解析失败：{message or '接口未返回内容'}")

        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            if any(
                key in payload
                for key in ("video_url", "source_video_url", "image_list", "title")
            ):
                data = payload
            else:
                raise ParseError("解析接口没有返回数据")

        title = self._truncate(self._as_text(data.get("title")) or "未命名作品", 60)
        cover = self._upgrade_https(
            self._normalize_url(self._as_text(data.get("cover_url")))
        )
        if not cover.startswith(("http://", "https://")):
            cover = ""
        video_url = self._upgrade_https(
            self._first_url(data.get("video_url"), data.get("source_video_url"))
        )
        audio_url = self._upgrade_https(
            self._first_url(data.get("audio_url"), data.get("source_audio_url"))
        )
        images = self._normalize_images(data.get("image_list"))

        author = data.get("author")
        if isinstance(author, dict):
            author_text = self._as_text(author.get("nickname") or author.get("name"))
        else:
            author_text = self._as_text(author)
        author_text = self._truncate(author_text, 30)

        platform_text = self._as_text(data.get("platform")) or platform_name
        if platform_id == "bilibili":
            platform_text = "B站"

        size = self._to_int(data.get("size"))
        audio_format = "m4a" if re.search(r"\.(m4a|mp4)(?:[?#]|$)", audio_url, re.I) else "mp3"
        duration = self._format_duration(data.get("duration"))

        if not video_url and not images and not audio_url:
            raise ParseError(
                "接口没有解析出可发送的视频、图集或音频"
                "（可能被风控、链接失效或该平台暂不支持）"
            )

        return MediaInfo(
            platform_id=platform_id,
            platform=platform_text,
            title=title,
            link=url,
            cover=cover,
            author=author_text,
            duration=duration,
            video_url=video_url,
            audio_url=audio_url,
            audio_format=audio_format,
            video_size=size,
            images=images,
            video_silent=False,
        )

    def _normalize_images(self, raw: Any) -> list[str]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        limit = max(1, min(9, self._cfg_int("image_limit", 9)))
        images: list[str] = []
        for item in raw:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = (
                    item.get("url")
                    or item.get("src")
                    or item.get("image_url")
                    or ""
                )
            else:
                continue
            normalized = self._normalize_url(self._as_text(value))
            if normalized and normalized.startswith("http"):
                images.append(normalized)
            if len(images) >= limit:
                break
        return list(dict.fromkeys(images))

    def _normalize_url(self, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if value.startswith("//"):
            return "https:" + value
        if value.startswith("/"):
            return self._api_origin() + value
        return value

    def _api_origin(self) -> str:
        api = self._cfg_str("parse_api", DEFAULT_PARSE_API).strip()
        try:
            parts = urlsplit(api)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except ValueError:
            pass
        return "https://video.zacao.top"

    # ------------------------------------------------------------------ #
    # HTTP 下载 / 探测
    # ------------------------------------------------------------------ #
    async def _download_file(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str],
        timeout: httpx.Timeout,
        max_bytes: int,
    ) -> Path:
        if not re.match(r"^https?://", url, re.I):
            raise ParseError("下载地址不是 http(s)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.unlink(missing_ok=True)
        total = 0
        try:
            client = await self._get_client()
            async with client.stream(
                "GET",
                url,
                headers={**headers, "Accept": "*/*"},
                timeout=timeout,
                follow_redirects=True,
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise ParseError(
                        f"下载失败 HTTP {response.status_code}：{body[:160]!r}"
                    )
                content_type = (response.headers.get("content-type") or "").lower()
                if (
                    content_type.startswith("text/")
                    or "json" in content_type
                    or "html" in content_type
                ):
                    body = await response.aread()
                    raise ParseError(
                        "下载地址返回的不是媒体"
                        f"（Content-Type: {content_type or '未知'}）：{body[:120]!r}"
                    )
                declared = self._to_int(response.headers.get("content-length"))
                if declared > max_bytes:
                    raise ParseError(
                        f"文件超过上限 {max_bytes // 1048576}MB"
                        f"（声明 {declared // 1048576}MB）"
                    )
                with tmp.open("wb") as file_obj:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise ParseError(
                                f"文件超过上限 {max_bytes // 1048576}MB"
                            )
                        file_obj.write(chunk)
            if total <= 0:
                raise ParseError("下载内容为空")
            tmp.replace(dest)
            return dest
        except httpx.TimeoutException as exc:
            raise ParseError("下载超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise ParseError(f"下载失败：{exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)

    async def _probe_size(self, url: str, platform_id: str = "") -> int:
        if not url:
            return 0
        headers = self._media_headers(url, platform_id)
        client = await self._get_client()
        try:
            response = await client.head(
                url, headers=headers, timeout=self._request_timeout()
            )
            length = self._to_int(response.headers.get("content-length"))
            if response.status_code < 400 and length > 0:
                return length
        except Exception:  # noqa: BLE001
            pass
        try:
            async with client.stream(
                "GET",
                url,
                headers={**headers, "Range": "bytes=0-0"},
                timeout=self._download_timeout(),
            ) as response:
                content_range = response.headers.get("content-range") or ""
                match = re.search(r"/(\d+)\s*$", content_range)
                if match:
                    return int(match.group(1))
                length = self._to_int(response.headers.get("content-length"))
                return length if response.status_code < 400 else 0
        except Exception:  # noqa: BLE001
            return 0

    def _media_headers(self, url: str, platform_id: str = "") -> dict[str, str]:
        headers = {"User-Agent": self._ua, "Accept": "*/*"}
        low = url.lower()
        if (
            "bilibili" in low
            or "bilivideo" in low
            or "hdslb" in low
            or platform_id == "bilibili"
        ):
            headers["Referer"] = "https://www.bilibili.com/"
        elif "douyin" in low or "douyinvod" in low:
            headers["Referer"] = "https://www.douyin.com/"
        elif "kuaishou" in low:
            headers["Referer"] = "https://www.kuaishou.com/"
        return headers

    # ------------------------------------------------------------------ #
    # 转码 / ffmpeg
    # ------------------------------------------------------------------ #
    async def _convert_via_api(self, audio_url: str, token: str) -> str:
        api = self._cfg_str("convert_api", "").strip()
        if not api:
            return ""
        try:
            client = await self._get_client()
            response = await client.post(
                api,
                json={"url": audio_url, "name": f"MK-{token}"},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": self._ua,
                },
                timeout=self._request_timeout(),
            )
            if response.status_code != 200:
                return ""
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"[{PLUGIN_NAME}] 备用转码接口失败: {exc}")
            return ""
        if not isinstance(payload, dict):
            return ""
        if payload.get("ok") is False:
            return ""
        for key in ("url", "mp3", "file"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            value = data.get("url")
            if isinstance(value, str) and value.startswith("http"):
                return value
        return ""

    async def _ensure_ffmpeg(self) -> str:
        if self._ffmpeg is not None:
            return self._ffmpeg
        async with self._ffmpeg_lock:
            if self._ffmpeg is not None:
                return self._ffmpeg

            candidates: list[str] = []
            configured = self._cfg_str("ffmpeg_path", "").strip()
            if configured:
                candidates.append(configured)
            for env_name in ("FFMPEG_PATH", "FFMPEG"):
                value = os.environ.get(env_name, "").strip()
                if value:
                    candidates.append(value)
            which = shutil.which("ffmpeg")
            if which:
                candidates.append(which)
            candidates.extend(
                [
                    r"C:\ffmpeg\bin\ffmpeg.exe",
                    r"C:\ffmpeg\ffmpeg.exe",
                    r"D:\ffmpeg\bin\ffmpeg.exe",
                    r"D:\ffmpeg\ffmpeg.exe",
                    "/usr/bin/ffmpeg",
                    "/usr/local/bin/ffmpeg",
                    "/opt/homebrew/bin/ffmpeg",
                ]
            )

            seen: set[str] = set()
            for candidate in candidates:
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                if ("/" in candidate or "\\" in candidate) and not Path(
                    candidate
                ).exists():
                    continue
                try:
                    process = await asyncio.create_subprocess_exec(
                        candidate,
                        "-version",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except (OSError, ValueError):
                    continue
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
                    continue
                if process.returncode == 0:
                    self._ffmpeg = candidate
                    logger.info(f"[{PLUGIN_NAME}] 使用 ffmpeg: {candidate}")
                    return candidate

            self._ffmpeg = ""
            logger.warning(
                f"[{PLUGIN_NAME}] 未找到 ffmpeg，将无法合并 B 站音视频/导出 MP3"
            )
            return ""

    async def _run_ffmpeg(self, args: list[str], timeout: int = FFMPEG_TIMEOUT) -> None:
        ffmpeg = await self._ensure_ffmpeg()
        if not ffmpeg:
            raise ParseError("本机没有可用的 ffmpeg")

        # 与 GF 版 video-core 一致：ffmpeg 的错误输出写到临时日志文件，
        # 不通过管道读取，避免长日志占内存/管道阻塞。
        log_path = self.cache_dir / f".ffmpeg-{os.getpid()}-{time.time_ns()}.log"
        process = None
        try:
            with log_path.open("wb") as log_file:
                try:
                    process = await asyncio.create_subprocess_exec(
                        ffmpeg,
                        *args,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=log_file,
                    )
                except (OSError, ValueError) as exc:
                    raise ParseError(f"无法启动 ffmpeg：{exc}") from exc
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
                    raise ParseError(f"ffmpeg 超时（{timeout // 60}分钟）") from exc

            if process.returncode != 0:
                try:
                    message = log_path.read_text(
                        encoding="utf-8", errors="ignore"
                    )[-700:]
                except OSError:
                    message = ""
                raise ParseError(
                    f"ffmpeg 退出码 {process.returncode}：{message or '没有错误输出'}"
                )
        finally:
            log_path.unlink(missing_ok=True)

    async def _merge_av(self, video: Path, audio: Path, output: Path) -> None:
        output.unlink(missing_ok=True)
        common = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
        try:
            await self._run_ffmpeg(
                [
                    *common,
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
        except ParseError as exc:
            logger.warning(f"[{PLUGIN_NAME}] DASH 直接合并失败，尝试转码音频: {exc}")
            output.unlink(missing_ok=True)
            await self._run_ffmpeg(
                [
                    *common,
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
        if not output.exists() or output.stat().st_size <= 0:
            raise ParseError("ffmpeg 没有生成视频文件")

    async def _remux_video(self, video: Path, output: Path) -> None:
        output.unlink(missing_ok=True)
        await self._run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        if not output.exists() or output.stat().st_size <= 0:
            raise ParseError("ffmpeg 没有生成视频文件")

    async def _to_mp3(self, source: Path, output: Path) -> None:
        output.unlink(missing_ok=True)
        await self._run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(output),
            ]
        )
        if not output.exists() or output.stat().st_size <= 0:
            raise ParseError("ffmpeg 没有生成 MP3 文件")

    async def _to_m4a(self, source: Path, output: Path) -> None:
        output.unlink(missing_ok=True)
        await self._run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output),
            ]
        )
        if not output.exists() or output.stat().st_size <= 0:
            raise ParseError("ffmpeg 没有生成 m4a 文件")

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def _first_url(cls, *values: Any) -> str:
        for value in values:
            candidates = value if isinstance(value, (list, tuple)) else [value]
            for candidate in candidates:
                text = cls._as_text(candidate)
                if text.startswith("//"):
                    text = "https:" + text
                if text.startswith("http://") or text.startswith("https://"):
                    return text
        return ""

    @staticmethod
    def _upgrade_https(url: str) -> str:
        url = url.strip()
        if url.startswith("http://"):
            return "https://" + url[len("http://") :]
        if url.startswith("//"):
            return "https:" + url
        return url

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        if seconds is None or seconds == "":
            return ""
        if isinstance(seconds, str):
            text = seconds.strip()
            if re.fullmatch(r"\d+(?::\d+){1,2}", text):
                return text
            try:
                value = float(text)
            except ValueError:
                return ""
        else:
            try:
                value = float(seconds)
            except (TypeError, ValueError):
                return ""
        if value <= 0:
            return ""
        total = int(round(value))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = str(text or "").replace("\n", " ").replace("\r", " ").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "…"
