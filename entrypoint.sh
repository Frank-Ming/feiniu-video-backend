#!/bin/sh
# 飞牛短视频后端诊断启动脚本
# - 先打印所有诊断信息，再启动应用
# - 用 exec 保证日志能实时输出到 Docker stdout

set -e

echo "=========================================="
echo "[diagnostic] 启动诊断"
echo "=========================================="
echo "[diagnostic] 时间: $(date)"
echo "[diagnostic] Python: $(python --version 2>&1)"
echo "[diagnostic] 工作目录: $(pwd)"
echo "[diagnostic] VIDEO_ROOT=$VIDEO_ROOT"
echo "[diagnostic] SERVER_PORT=$SERVER_PORT"
echo ""
echo "[diagnostic] /app 目录内容:"
ls -la /app
echo ""
echo "[diagnostic] /app/app 目录内容:"
ls -la /app/app
echo ""
echo "[diagnostic] VIDEO_ROOT 存在性检查:"
if [ -d "$VIDEO_ROOT" ]; then
    echo "  ✓ $VIDEO_ROOT 是目录，内容如下（前 5 项）："
    ls -la "$VIDEO_ROOT" 2>&1 | head -10
else
    echo "  ✗ $VIDEO_ROOT 不存在或不是目录！"
fi
echo ""
echo "[diagnostic] 6969 端口占用检查:"
netstat -tnlp 2>/dev/null | grep ":$SERVER_PORT " || ss -tnlp 2>/dev/null | grep ":$SERVER_PORT " || echo "  (netstat/ss 都不可用，跳过)"
echo ""
echo "[diagnostic] python 模块导入测试:"
python -c "import fastapi, uvicorn, imageio_ffmpeg; print('  fastapi=', fastapi.__version__); print('  uvicorn=', uvicorn.__version__); print('  imageio_ffmpeg ok')"
echo ""
echo "=========================================="
echo "[diagnostic] 启动 uvicorn:"
echo "=========================================="
exec python -m app.main
