# 飞牛短视频 - 后端

基于 FastAPI 的轻量视频服务，扫描 NAS 上的视频目录并提供支持 HTTP Range 的流媒体代理，配套 Flutter 客户端使用。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 当前配置（扫描根目录等） |
| GET | `/api/dirs?refresh=1` | 一级子目录列表（用于前端筛选） |
| GET | `/api/videos?refresh=1` | 视频列表，按修改时间倒序 |
| GET | `/api/videos/{id}` | 单条视频详情 |
| GET | `/api/stream/{id}` | 视频流（支持 HTTP Range） |

### `/api/videos` 筛选参数

| 参数 | 说明 |
| --- | --- |
| `dir` / `dirs` | 按一级子目录过滤，可重复 `dir=A&dir=B`，也支持逗号 `dirs=A,B`；传 `(根目录)` 表示仅根目录视频 |
| `min_seconds` | 最小时长（秒），仅返回 `duration >=` 该值 |
| `max_seconds` | 最大时长（秒），仅返回 `duration <=` 该值 |
| `refresh=1` | 强制重新扫描 |
| `limit` / `offset` | 分页 |

例：
```
GET /api/videos?dirs=电影,电视剧&max_seconds=600
```

## 本地运行

```bash
pip install -r requirements.txt
# 视频根目录可通过环境变量覆盖
VIDEO_ROOT=/vol1/1000/视频/H python -m app.main
```

服务默认监听 `0.0.0.0:6969`。

## Docker 部署（飞牛 NAS 推荐）

```bash
cd backend
docker compose up -d --build
```

`docker-compose.yml` 已经把 `/vol1/1000/视频/H` 挂载进容器，并对外暴露 `6969` 端口。手机 App 里填入 `http://NAS_IP:6969` 即可。

> 飞牛 OS 一般允许直接挂载宿主机的同路径。如果你的视频目录路径不一样，修改 `docker-compose.yml` 与 `VIDEO_ROOT` 环境变量即可。
