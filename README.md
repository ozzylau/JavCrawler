# JavCrawler

基于 [javdb](https://javdb.com) 数据的 JAV 影片刮削工具：为视频文件**就地**生成标准 `NFO` 元数据与海报，供 **Infuse / Kodi / Emby / Jellyfin** 等媒体库识别。

> ⚠️ 仅供个人学习研究，请遵守所在地区法律法规。视频文件本身不会被移动、重命名或改动。

## ✨ 特性

- **就地刮削** — 只在视频文件旁边生成元数据文件，不移动、不改动视频
- **多目录** — 支持添加任意多个刮削目录，网页内可浏览选择
- **媒体库兼容** — 生成的 NFO / poster / fanart 命名符合 Emby、Jellyfin、Kodi、Infuse 规范
- **状态统计** — 已识别 / 已刮削 / 未刮削 / 刮削失败一目了然，点击卡片可查看影片明细
- **定时调度** — 每天固定时间 或 每 4 / 8 小时自动检查补刮，失败自动重试
- **历史日志** — 按日期归档查看，一键清理 7 天前的旧日志
- **Cookie / 代理可选** — 正常情况无需配置，被反爬拦截时才需要

## 📁 工作原理

对配置的每个刮削目录递归扫描视频文件，从文件名提取番号（如 `SSIS-699`、`FC2-PPV-1234567`），然后生成：

```
/媒体目录/
├── MIGD-751.mp4              ← 视频文件（保持不动）
├── MIGD-751.nfo              ← 自动生成：元数据（标题/演员/类型/评分…），与视频完全同名
├── MIGD-751-poster.jpg       ← 自动生成：竖版海报
├── MIGD-751-fanart.jpg       ← 自动生成：横版背景图（javdb 原始完整封面）
└── poster.jpg                ← 单视频文件夹时自动生成（媒体库文件夹海报）
```

**海报策略**：竖版海报优先取详情页预览图里的竖图；若预览图里没有竖图，则取横版封面的右半边（javdb 横图是 DVD 盒正反面展开图，右半为正面）。

**状态判定**：

| 状态 | 判定依据 |
|------|----------|
| 已刮削 | 视频旁边已存在**与视频完全同名**的 `.nfo`（如 `MIGD-751.mp4` ↔ `MIGD-751.nfo`） |
| 未刮削 | 有番号视频文件但还没有 `.nfo` |
| 刮削失败 | 刮过但失败（javdb 未找到、网络被拦截等），下次运行自动重试 |

## 🚀 快速开始（Docker）

镜像已发布到 Docker Hub：[`ozzylau/javcrawler`](https://hub.docker.com/r/ozzylau/javcrawler)，无需克隆源码，任意带 Docker 功能的设备（服务器 / NAS / 路由器）都能直接拉取部署。

### 方式一：Linux 服务器 / 任意终端一条命令部署（推荐）

先准备目录和空配置文件（容器启动时会自动补全默认值）：

```bash
mkdir -p javcrawler/data && cd javcrawler
echo '{}' > config.json
```

> ⚠️ 必须先创建 `config.json` 文件再启动容器——如果挂载一个不存在的文件路径，Docker 会把它创建成**目录**，导致容器启动失败。

启动容器：

```bash
docker run -d --name javcrawler --network host \
  -v /mnt/data:/mnt/data \
  -v $(pwd)/config.json:/app/config.json \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  ozzylau/javcrawler:latest
```

- `/mnt/data:/mnt/data` — 把存放视频的磁盘挂进容器（左边改成你自己的实际路径，左右写一样的路径最省心）
- 打开后台：`http://设备IP:8100`

### 方式二：NAS 部署（群晖 / 威联通 / fnOS 等）

**群晖 DSM 7（Container Manager）：**

1. **File Station** 先建目录：`docker/javcrawler/`，在其中新建一个空文件 `config.json`（内容 `{}`）和一个 `data` 文件夹
2. 打开 **Container Manager → 注册表**，搜索 `ozzylau/javcrawler`，下载 `latest`
3. **映像 → 启动**，创建容器时配置：
   - 端口：本地端口 `8100` → 容器端口 `8100`
   - 存储空间（添加 3 个文件夹映射）：
     | 宿主机（NAS） | 容器 |
     |---|---|
     | `docker/javcrawler/config.json` | `/app/config.json` |
     | `docker/javcrawler/data` | `/app/data` |
     | 你的视频目录，如 `/volume1/media/movies` | `/volume1/media/movies`（两边写一样） |
   - （可选）环境变量不用配，进网页再设代理
4. 启动后访问 `http://NAS的IP:8100`

**威联通 / 其他 NAS（Container Station）：** 同样思路——建目录、拉镜像、映射端口 `8100` 和上面 3 个存储路径即可。支持 YAML 的 NAS 也可以直接用下面的 compose：

```yaml
services:
  javcrawler:
    image: ozzylau/javcrawler:latest
    container_name: javcrawler
    network_mode: host          # 不支持 host 模式就删掉这行，改为 ports: ["8100:8100"]
    restart: unless-stopped
    volumes:
      - ./config.json:/app/config.json
      - ./data:/app/data
      - /your/media/path:/your/media/path   # 左右两边写一样的路径
```

> 💡 建议用 `network_mode: host`（或把两边端口都设为 8100），这样网页里「浏览选择目录」看到的路径和挂载路径一致，最直观。

### 方式三：路由器部署（OpenWrt / 梅林固件等带 Docker 的）

路由器方案适合**媒体库就跑在路由器旁边**的场景（视频一般挂在 USB 硬盘上）：

1. 确认路由器 Docker 可用且 USB 硬盘已挂载（如 `/mnt/sda1`）
2. SSH 进路由器，先建配置（务必先建 `config.json` 文件）：

```bash
mkdir -p /mnt/sda1/javcrawler/data
echo '{}' > /mnt/sda1/javcrawler/config.json
docker run -d --name javcrawler --network host \
  -v /mnt/sda1/javcrawler/config.json:/app/config.json \
  -v /mnt/sda1/javcrawler/data:/app/data \
  -v /mnt/sda1/media:/mnt/sda1/media \
  --restart unless-stopped \
  ozzylau/javcrawler:latest
```

3. 访问 `http://路由器IP:8100`，刮削目录填容器内路径（如 `/mnt/sda1/media`）

> ⚠️ 路由器提示：刮削只做「下载网页 + 存几个小文件」，占用不大，但**路由器必须能访问 javdb**（被墙时需在网页里配置代理，指向局域网内任意可用代理）；CPU 过弱的软路由拉取镜像和建容器会慢一些，属正常现象。

### 方式四：从源码构建

```bash
git clone https://github.com/<你的用户名>/JavCrawler.git
cd JavCrawler
docker compose up -d --build
```

默认 `docker-compose.yml` 把宿主机 `/mnt/data` 挂载到容器同路径（8100 端口）。媒体文件在别的目录就修改 `volumes` 一行：

```yaml
volumes:
  - /volume1/media:/volume1/media     # 左边=宿主机路径，右边=容器内路径
```

**要点：挂载后的「容器内路径」就是网页里填写的刮削目录**——建议左右两边写一样的路径，网页里「浏览选择目录」所见即所得。

## ⚙️ 配置（网页操作）

1. 「📁 刮削目录」— 添加一个或多个目录（支持浏览选择），保存
2. 「⚙️ 数据源设置」
   - **HTTP 代理**（默认不填）：分字段填写 IP/域名、端口、用户名、密码，例如 `192.168.100.2` + `7890`；服务器能直连 javdb 就全部留空
   - **javdb Cookie**（默认不填）：只有日志出现 `403/503 被反爬拦截` 时才需要（浏览器登录 javdb → F12 → 网络请求头复制整段 Cookie）
3. 「⏰ 定时自动刮削」— 每天固定时间，或每 8 小时（0:00/8:00/16:00）、每 4 小时（0:00/4:00/8:00/12:00/16:00/20:00）

所有配置保存在挂载的 `config.json` 中，保存后立即生效，无需重启容器。

## 🖥️ 媒体库识别

- **Infuse**：直接扫描即可识别
- **Emby / Jellyfin**：库设置 → 元数据下载器 → 启用 **NFO 元数据**
- **Kodi**：设置 → 媒体 → 启用「从 NFO 文件读取信息」

## ❓ 常见问题

| 问题 | 处理 |
|------|------|
| 日志提示目录不存在 | 网页里填的是「容器内路径」，检查 docker-compose 挂载 |
| 全部失败，报 403/503 | javdb 反爬，配置 Cookie，或配置 HTTP 代理换节点 |
| 个别番号失败 | javdb 未收录该番号，下次运行会自动重试 |
| 想重新刮削某个影片 | 删除它旁边的 `.nfo` 文件，下次运行会重新生成 |
| 磁盘空间不足 | 历史日志页面点「删除 7 天前日志」清理 |

## 📄 License

MIT
