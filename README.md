# 多平台视频解析（astrbot_plugin_resolve）

AstrBot 插件。移植自工作区的 **GF 视频解析插件**（`GF-plugin-video-parser/视频解析.ka` +
`video-core.mjs`），把 QQ 官方机器人 `.ka` 脚本里的解析、缓存、一次性按钮、B站原画合并、
第三方聚合解析等逻辑改写为 AstrBot 的 Python 插件。

- 发送 **B站 / 抖音 / 快手 / 小红书 / 微博 / 西瓜 / 今日头条 / 皮皮虾 / 微视** 等平台的视频链接；
- 解析后回复一张信息卡片，并给出两个一次性令牌：
  - `v:<令牌>`：发送原视频或图集原图；
  - `a:<令牌>`：发送音频文件（B站输出 MP3）。
- **每条解析里，每个按钮只能成功使用一次**；默认只有发起解析的人能用。
- B站走官方接口 + DASH 原画：ffmpeg `-c copy` 合并，**不重新编码、不降分辨率、不降帧率**；
  合并后超过上限、没装 ffmpeg 或合并失败时，直接发 B站原页面链接，不会偷偷降画质。
- 其他平台走第三方聚合接口（默认 `https://video.zacao.top/api/parse`）。

---

## 一、文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | 插件主体：消息入口、解析、缓存、令牌、B站合并、第三方解析、ffmpeg 调用 |
| `metadata.yaml` | AstrBot 插件元数据 |
| `_conf_schema.json` | WebUI 可视化配置项 |
| `requirements.txt` | 依赖 `httpx`（AstrBot 通常已自带） |
| `README.md` | 本文件 |

解析缓存与媒体文件保存在：

```text
AstrBot/data/plugin_data/astrbot_plugin_resolve/cache/
```

不会写到插件目录，重装/更新插件不会丢数据。

---

## 二、安装

1. 把整个 `astrbot_plugin_resolve` 文件夹放进 `AstrBot/data/plugins/`；
2. 在 WebUI「插件」页刷新/重载；
3. 打开插件配置，按需修改解析接口、上限、权限等；
4. B站原画合并和 MP3 导出需要 **ffmpeg**：
   - Windows 可把 `ffmpeg.exe` 放进 `C:\ffmpeg\bin\`，或加入 PATH；
   - 也可以在插件配置 `ffmpeg_path` 填绝对路径；
   - 没有 ffmpeg 时，B站视频会直接发原页面链接，音频会按 m4a 原格式发送。

---

## 三、使用

### 1. 解析

```text
解析 https://www.bilibili.com/video/BV1xx411c7mD
解析 https://v.douyin.com/xxxxxx/
视频解析 https://www.kuaishou.com/short-video/xxxxx
```

也可以直接发送链接（默认开启 `auto_parse_link`）：

```text
https://www.bilibili.com/video/BV1xx411c7mD
```

支持直接发 `BV号` / `av号`，以及 `b23.tv` 短链。

### 2. 获取内容

解析成功后会收到类似：

```text
🎬 视频标题
平台：B站
作者：UP主
时长：3:25
播放：123456

回复 v:ab12cd34ef56 获取原视频/原图
回复 a:ab12cd34ef56 获取音频文件
每个按钮每条解析只能点一次，解析结果 2 小时内有效。
（仅发起人可用）
```

然后发送：

```text
v:ab12cd34ef56
a:ab12cd34ef56
```

- `v:` 发送原视频；图集平台发送图片；B站无法合并时发送原页面链接。
- `a:` 发送音频文件；B站由 ffmpeg 从 DASH 音轨导出 `.mp3`。
- 同一个令牌的 `v:`、`a:` 各自独立，各能成功一次；再点会提示已经用过。
- 默认只有发起解析的人能用；`owner_only` 关闭后只要拿到令牌就能用（仍各一次）。

### 3. 帮助

```text
解析帮助
```

---

## 四、配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `parse_api` | `https://video.zacao.top/api/parse` | 第三方聚合解析接口，需支持 `POST {"text":"链接"}` |
| `parse_api_key` | 空 | 聚合接口 `X-API-Key`；留空匿名调用（默认每 IP 每小时 30 次） |
| `video_limit_mb` | 60 | 第三方视频超过此大小直接发直链，避免富媒体上传超时 |
| `bili_limit_mb` | 64 | B站原画合并后超过此大小直接发原页面链接，不降画质 |
| `owner_only` | 开 | 仅发起人可用 |
| `cache_hours` | 2 | 解析结果有效期 |
| `auto_parse_link` | 开 | 直接发送链接也解析 |
| `send_cover` | 开 | 卡片后附带封面图 |
| `image_limit` | 9 | 图集最多发送张数 |
| `ffmpeg_path` | 空 | ffmpeg 路径，留空自动查找 |
| `convert_api` | 空 | 可选备用 m4a→mp3 转码接口（兼容 GF 包里的 Node 服务） |
| `bili_cookie` | 空 | B站接口 412 时可填浏览器 Cookie |
| `request_timeout` | 30 | HTTP 请求超时（秒） |
| `download_timeout` | 300 | 媒体下载超时（秒） |
| `ua` | Chrome UA | 请求 B站/下载媒体使用的 User-Agent |
| `proxy` | 空 | HTTP 代理 |

---

## 五、B站：官方接口 + DASH 原画

1. 从链接提取 `BV` / `av` 号（支持 `b23.tv` / `bili2233.cn` 短链、`?p=` 分P）；
2. `x/web-interface/view` 取标题、封面、UP主、时长、播放量、分P cid；
3. `x/player/playurl`（`fnval=4048`）取 DASH 视频流 + 音轨；
4. 选择**最高画质 id**（同一画质内 AVC 优先，再选最高码率），下载后：
   - 视频流 + 音频流：`ffmpeg -c copy` 合并，不重新编码；
   - 音频容器不兼容时才单独转 AAC，视频轨仍是原画；
5. 合并后 ≤ `bili_limit_mb`：作为视频文件发送；
6. 合并后超限 / 本机没有 ffmpeg / 合并失败：**直接发 B站原页面链接**（原画质/原帧率）；
7. `a:` 由 ffmpeg 从原画 DASH 音轨导出 MP3，不经过第三方接口。

> B站官方接口在部分网络/机房 IP 下可能返回 412。插件会自动回退到第三方聚合接口；
> 聚合接口拿到的 B站直链通常是纯视频流（没有音轨），此时 `v:` 会按 GF 版逻辑改发原页面链接。
> 也可以在插件配置里填 `bili_cookie` 提高官方接口成功率。

---

## 六、抖音 / 快手 / 小红书等：第三方聚合接口

请求：

```json
{
  "text": "https://v.douyin.com/xxxxxx/"
}
```

解析结果中读取：

- `video_url` / `source_video_url`：无水印视频；
- `image_list`：图集（字符串或 `{url:...}` 对象均可）；
- `audio_url` / `source_audio_url`：音频；
- `title`、`cover_url`、`author.nickname`、`platform`、`size`（可选）。

第三方接口是外部服务，可能限流、改字段或临时不可用。如果字段变化，只需要改
`main.py` 里 `_media_from_third_party` 的字段名。项目参考：
[ZParse](https://github.com/ZixueX/ZParse)、[接口文档](https://video.zacao.top/docs)。

---

## 七、一次性令牌与缓存

每次解析生成一个短令牌，结果保存在：

```text
data/plugin_data/astrbot_plugin_resolve/cache/<令牌>.json
```

令牌流程：

1. 检查缓存是否存在、是否超过 `cache_hours`；
2. `owner_only` 开启时，检查使用者是不是发起人（AstrBot `event.get_sender_id()`）；
3. 检查对应按钮 `video_used` / `audio_used` 是否已经用过；
4. **先写「已用」，再发送**；发送失败自动退回，允许重试；
5. 发送成功后再用同一个按钮，会提示已经用过。

`v:` 和 `a:` 各自独立，互不影响。缓存和媒体文件会在后续解析时按有效期/24 小时自动清理。

---

## 八、从 GF 插件移植的对应关系

GF 插件是 QQ 官方机器人（kakake / mjts 简易文本）的 `.ka` 脚本 + Node 运行时。
本次移植按工作区的三个基础例子来理解 `.ka` 的能力，并逐项改写成 AstrBot 插件：

### 基础例子1（消息、媒体、Markdown、按钮、读写、访问、循环）

| `.ka` 写法 | 在 AstrBot 插件中的实现 |
|---|---|
| `[全局定义]` / `[临时定义]` | `_conf_schema.json` 配置 + Python 局部变量 / `MediaInfo` |
| `[如果] … [反之]` | Python `if / else`、`ParseError` 分支 |
| `[For循环]` + `[中断]` | Python `for` / `while`（选择 DASH 流、重试下载、分P时长换算等） |
| `[文本消息]` / `[发言人]` / `[群号]` | `event.message_str` / `event.get_sender_id()` / `event.get_group_id()` |
| `发消息` / `发Markdown` / `[图片,url:]` / `[视频,url:]` / `[文件,url:]` | `event.send(MessageChain([...]))` + `Comp.Plain/Image/Video/File` |
| `按钮行` / `回调钮` / `键盘` | AstrBot 插件 API 未暴露 QQ Markdown 键盘回调，改为等价的 `v:<令牌>` / `a:<令牌>` 文字命令（GF 版在未开通自定义键盘时也是补发同样的文字提示） |
| `读` / `写` / `读文件` / `写文件` | JSON 缓存文件 `cache/<令牌>.json` + `Path.read_text/write_text` |
| `访问("GET"/"POST", ...)` / `取JSON` | `httpx.AsyncClient` + `response.json()` |
| `[时间:时间戳秒/毫秒]` | `time.time()` / `int(time.time()*1000)` |
| `替换` / `取长度` / `匹配` / `取括号` | `str.replace` / `len` / `re.search` / `match.group()` |
| `解析视频(链接)` / `发送视频(会话ID, 类型)` | `_parse_bilibili()` + `_download_bili_stream()` / `_merge_av()` / `_to_mp3()` |
| `[等待] 发消息(...)` 回执 | `await event.send(...)` + 异常捕获（失败回退「已用」标记） |

### 基础例子2（群身份 / 群管理）

GF 版只在「仅发起人可用」处用到发言人身份；AstrBot 版用 `event.get_sender_id()`
保存并校验 `owner`。群管理、禁言、群信息等能力与视频解析无关，未移植。

### 基础例子3（生命周期事件）

GF 版解析插件没有使用进群/退群/加好友等生命周期事件；AstrBot 版同样只处理消息事件，
因此不注册 `on_astrbot_loaded` 之外的钩子，只保留 `initialize` / `terminate` 做 HTTP 客户端生命周期管理。

### 与 GF 版的差异

1. **按钮 → 文字令牌**：AstrBot 插件 API 没有 QQ 官方 Markdown 键盘/回调按钮接口，
   所以保留 GF 版「未开通自定义键盘」时的降级方案：`v:<令牌>` / `a:<令牌>`。
2. **本地 Node 转码服务**：不再强制依赖 `工具/MP3转码服务.mjs`。
   B站音频直接用本机 ffmpeg 导出 MP3；第三方 m4a 也优先本机 ffmpeg。
   如仍想用原 Node 服务，把 `convert_api` 填成 `http://127.0.0.1:18080/convert` 即可。
3. **大小探测**：GF 版用本地服务 `/probe`；AstrBot 版直接内置 `HEAD` / `Range` 探测，
   不需要额外启动服务。
4. **解析缓存位置**：GF 版在插件数据目录 `解析缓存/`；AstrBot 版按规范放到
   `data/plugin_data/astrbot_plugin_resolve/cache/`。

---

## 九、常见问题

### 1. 提示「B站接口拒绝访问（412）」

B站对部分 IP/网络有风控。可以：

- 稍后重试或换网络；
- 在配置 `bili_cookie` 填浏览器登录 B站后的 Cookie；
- 不填也能用：插件会自动回退第三方聚合接口，只是 B站视频可能只能发原页面链接。

### 2. 视频发送失败 / 超过 64MB

QQ 官方富媒体单文件上限约 64MB。插件不会为了塞进 64MB 而降画质：

- B站：原画合并后超限、没 ffmpeg 或合并失败 → 发原页面链接；
- 第三方：先用 `HEAD` / `Range` 探测大小，超过 `video_limit_mb` → 发视频直链；
- 发送失败时会提示直链，可手动打开。

### 3. 音频是 m4a，不是 mp3

B站音频需要本机 ffmpeg 才能导出 MP3；没有 ffmpeg 时按 m4a 原格式发送。
第三方平台的 `audio_url` 本身是 mp3 时会直接按 mp3 发送。

### 4. 群里发链接没反应

- 确认机器人能收到该消息（QQ 官方群通常需要 @机器人，或把机器人设为群助手）；
- 如果关闭了 `auto_parse_link`，需要写「解析」；
- 确认链接域名在支持列表内。

### 5. 令牌过期

默认 2 小时。B站/抖音直链本身也会过期，重新发送链接解析即可。

### 6. 想改按钮文案 / 权限

- 文案：改 `main.py` 里 `_card_text` 的两行提示；AstrBot 没有按钮文案；
- 权限：改配置 `owner_only`；
- 有效期：改 `cache_hours`。

---

## 十、已知限制

- QQ 被动回复条数有限（约 5 条）；图集较多时后面的图片可能被平台限制。
- B站原画合并需要 ffmpeg，且合并/下载需要一点时间；插件会先发「正在准备…」提示。
- 第三方接口的 `audio_url` 通常是作品配乐/背景音乐，不一定等于视频里重新分离的人声与环境声；
  B站走 DASH 音轨，是完整音频。
- 解析直链有时效；缓存默认 2 小时。
- 插件只在用户主动发送链接后解析并转发媒体地址，请遵守各平台服务条款与著作权规定。

---

## 十一、移植来源与许可

- 原始 GF 插件：工作区 `GF-plugin-video-parser`（`视频解析.ka`、`video-core.mjs`、
  `工具/MP3转码服务.mjs`），作者 DSH；其 Node 运行时基于咔咔珂「简易文本」0.3.1（MIT）。
- 第三方解析接口与字段设计参考 [ZParse](https://github.com/ZixueX/ZParse)、
  [video.zacao.top](https://video.zacao.top/docs)。
- 本插件是对上述功能的 Python 重写/移植，保留原项目的功能思路与行为约定；
  如原项目有许可要求，请同时遵守原项目许可。
