# osu! Ranked 地图下载器增强版

一个基于 sayobot 镜像 + 官方 osu! API v2 的谱面批量下载工具，比原来的 `Ranked地图下载器.exe` 增加更灵活的筛选能力。

## 主要改进

- **双数据源**：可选择 sayobot 镜像（无需 API Key，按日期抓取）或官方 osu! API v2（需要 Client ID / Client Secret，搜索结果更权威）。
- **自动分页抓取**：原工具（以及 sayobot `get_beatmaps` 默认行为）一次最多只能拿 500 条；本工具会按时间自动翻页，不再漏掉后续谱面。单页请求超时或失败时会自动重试 3 次；如果重试后仍失败，会保留已获取的进度，可点击“从断点继续”接着抓，不用从头开始。
- **更丰富的筛选条件**：
  - 日期范围（开始 / 结束）
  - 游戏模式：osu! / 太鼓 / 接水果 / 下落式 / 全部
  - mania 键数快捷筛选：4K / 5K / 6K / 7K / 8K / 9K / 10K，可选“仅纯该键数”排除混键数谱面
  - 星级、BPM、时长、AR、OD、CS、HP 范围
  - 作者 / 曲师 / 标题 / 标签关键词
  - 审核状态：Ranked / Qualified / Loved / Pending / WIP / Graveyard
  - 视频、故事板筛选
  - 自动跳过已下载谱面
- **批量下载**：
  - 可选择下载全部或只下载选中的谱面
  - 多线程并发下载（默认 3，可调 1~10）
  - 下载进度、成功/失败日志
  - 下载到 osu! 的 `Songs` 目录，和原工具行为一致

## 运行

```bash
python RankedDownloader.py
```

Windows 下也可以直接双击 `启动下载器.bat`（如果已安装 Python）。

## CLI 用法（Linux / 无 GUI 环境）

脚本本身也支持命令行模式，Linux 服务器或没有 Tkinter 的环境也能正常使用。

```bash
# 查看帮助
python3 RankedDownloader.py --help

# 使用 sayobot 列出 4K mania Ranked 谱面
python3 RankedDownloader.py \
  --source sayobot \
  --start-date 2025-01-01 \
  --mode mania --keys 4 --approved 1 \
  --list

# 使用 sayobot 下载到指定 Songs 目录
python3 RankedDownloader.py \
  --source sayobot \
  --start-date 2025-01-01 --end-date 2025-12-31 \
  --mode mania --keys 4 --pure-keys \
  --approved 1,3,4 \
  --download --songs /path/to/osu/Songs

# 使用官方源
python3 RankedDownloader.py \
  --source official \
  --client-id 你的ClientID \
  --client-secret 你的ClientSecret \
  --mode mania --keys 4 --approved 1 \
  --download --songs /path/to/osu/Songs
```

常用 CLI 参数：

| 参数 | 说明 |
|---|---|
| `--source` | `sayobot` 或 `official` |
| `--client-id` / `--client-secret` | 官方 OAuth 凭据 |
| `--start-date` / `--end-date` | sayobot 日期范围 |
| `--mode` | `osu` / `taiko` / `catch` / `mania` / `any` |
| `--keys` | mania 键数，如 `4` |
| `--pure-keys` | 只保留整组都是该键数的谱面 |
| `--approved` | 如 `1,3,4` 或 `ranked,loved` |
| `--star-min` / `--star-max` | 星级范围 |
| `--bpm-min` / `--bpm-max` | BPM 范围 |
| `--length-min` / `--length-max` | 时长范围（秒） |
| `--ar-min/max`、`--od-min/max`、`--cs-min/max`、`--hp-min/max` | 难度参数范围 |
| `--creator` / `--artist` / `--title` / `--tags` | 文本筛选 |
| `--video` / `--storyboard` | `any` / `no` / `yes` |
| `--file-type` | `novideo` / `full` / `mini` |
| `--workers` | 并发下载数 |
| `--songs` / `--output` | 保存目录 |
| `--list` / `--download` | 列出 / 下载 |

## 打包为独立 exe

在 Windows 上执行：

```bat
build.bat
```

或手动执行：

```bat
pip install pyinstaller
pyinstaller --onefile --windowed --name "Ranked地图下载器增强版" RankedDownloader.py
```

打包后的 exe 在 `dist\Ranked地图下载器增强版.exe`，可复制到任意位置使用。

## 使用提示

1. 首次使用点击 **自动检测** 或手动选择 osu! 的 `Songs` 文件夹。
2. 在“数据源”里选择：
   - `sayobot`：无需 API Key；填写开始日期（必填），结束日期可留空。
   - `official`：需要填写 osu! 官方 OAuth 应用的 `Client ID` 和 `Client Secret`；开始日期不是必填。
3. 设置筛选条件，点击 **获取并筛选**。
4. 在结果列表中选择需要的谱面（不选则下载全部），点击 **下载全部** 或 **下载选中**。
5. 下载的 `.osz` 文件会直接放入 `Songs` 目录；下次启动 osu! 时通常会自动导入。

## 说明

- sayobot 数据来源：`https://api.sayobot.cn/ppy/get_beatmaps`
- sayobot 下载来源：`http://dl.sayobot.cn/beatmaps/...`
- 官方数据来源：`https://osu.ppy.sh/api/v2/beatmapsets/search`
- 官方下载来源：`https://osu.ppy.sh/beatmapsets/{id}/download`
- 使用官方源需要先在 https://osu.ppy.sh/home/account/edit#oauth 注册 OAuth 应用，并填写 Client ID / Client Secret。
- 仅供个人学习与备份使用，请尊重谱面作者与 osu! 相关规定。
