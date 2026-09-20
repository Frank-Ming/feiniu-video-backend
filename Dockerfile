# 飞牛短视频后端镜像
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIDEO_ROOT=/vol1/1000/视频/H \
    SERVER_PORT=6969

WORKDIR /app

# 装系统依赖: yaml 解析需要 libyaml, 视频详细信息需要 ffmpeg/ffprobe
RUN apt-get update && apt-get install -y --no-install-recommends \
        libyaml-dev \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

EXPOSE 6969

# 把 NAS 的视频目录挂到这里
VOLUME ["/vol1/1000/视频/H"]

CMD ["python", "-m", "app.main"]
