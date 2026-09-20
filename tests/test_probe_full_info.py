"""v6.4: probe_full_info 解析 ffprobe JSON / ffmpeg -i stderr。"""

import json
from pathlib import Path

from app import scanner as scanner_mod
from app.scanner import _parse_ffprobe_json, _probe_via_ffmpeg, probe_full_info

# ---------- _parse_ffprobe_json ----------

def test_parse_ffprobe_json_basic():
    raw = json.dumps({
        "format": {
            "duration": "12.345",
            "bit_rate": "1234567",
            "size": "987654321",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "codec_long_name": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10",
                "profile": "High",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "pix_fmt": "yuv420p",
                "bit_rate": "1000000",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "bit_rate": "128000",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
    })
    info = _parse_ffprobe_json(raw)
    assert info is not None
    assert info["duration"] == 12.345
    assert info["bitrate"] == 1234567
    assert info["size"] == 987654321
    assert "mp4" in info["format_name"]
    v = info["video"]
    assert v["codec_name"] == "h264"
    assert v["profile"] == "High"
    assert v["width"] == 1920
    assert v["height"] == 1080
    assert v["avg_frame_rate"] == 30.0
    assert v["pix_fmt"] == "yuv420p"
    assert v["bit_rate"] == 1000000
    a = info["audio"]
    assert a["codec_name"] == "aac"
    assert a["sample_rate"] == 48000
    assert a["channels"] == 2


def test_parse_ffprobe_json_video_only():
    """无音频流的视频也能解析。"""
    raw = json.dumps({
        "format": {"duration": "5.0"},
        "streams": [{
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 3840,
            "height": 2160,
            "avg_frame_rate": "24000/1001",
        }],
    })
    info = _parse_ffprobe_json(raw)
    assert info is not None
    v = info["video"]
    assert v["codec_name"] == "hevc"
    assert v["width"] == 3840
    assert v["height"] == 2160
    assert abs(v["avg_frame_rate"] - 23.976) < 0.01
    assert "audio" not in info


def test_parse_ffprobe_json_invalid_returns_none():
    assert _parse_ffprobe_json("not json") is None
    assert _parse_ffprobe_json("{}") is not None  # 空 dict OK


# ---------- _probe_via_ffmpeg ----------

def test_probe_via_ffmpeg_parses_stderr(monkeypatch):
    fake_stderr = (
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'test.mp4':\n"
        "  Duration: 00:01:23.45, start: 0.000000, bitrate: 2500 kb/s\n"
        "  Stream #0:0(und): Video: h264 (High), "
        "yuv420p, 1920x1080, 2000 kb/s, "
        "29.97 fps, 30 tbr (default)\n"
        "  Stream #0:1(und): Audio: aac, "
        "48000 Hz, stereo, 192 kb/s (default)\n"
    )
    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = fake_stderr
    import subprocess
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: FakeProc())
    info = _probe_via_ffmpeg(Path("dummy"), "ffmpeg")
    assert info is not None
    assert abs(info["duration"] - 83.45) < 0.1
    assert info["bitrate"] == 2500000
    v = info["video"]
    assert v["codec_name"] == "h264"
    assert v["width"] == 1920
    assert v["height"] == 1080
    assert abs(v["avg_frame_rate"] - 29.97) < 0.01
    a = info["audio"]
    assert a["codec_name"] == "aac"
    assert a["sample_rate"] == 48000


# ---------- probe_full_info: 端到端(无 ffprobe)----------

def test_probe_full_info_no_ffprobe(monkeypatch, tmp_path):
    """机器无 ffprobe 时返回 None (端点不会崩)。"""
    # 强制 _get_ffprobe 返回 None
    monkeypatch.setattr(scanner_mod, "_FFPROBE_BIN", None)
    assert probe_full_info(tmp_path / "x.mp4") is None


def test_probe_full_info_with_ffprobe(monkeypatch, tmp_path):
    """有 ffprobe 时正常解析。"""
    raw = json.dumps({
        "format": {"duration": "10.0", "format_name": "mp4"},
        "streams": [{
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1280, "height": 720,
            "avg_frame_rate": "60/1",
        }],
    })
    class FakeProc:
        returncode = 0
        stdout = raw
        stderr = ""
    import subprocess
    monkeypatch.setattr(scanner_mod, "_FFPROBE_BIN", "/usr/bin/ffprobe")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: FakeProc())
    info = probe_full_info(tmp_path / "x.mp4")
    assert info is not None
    assert info["video"]["codec_name"] == "h264"
    assert info["video"]["width"] == 1280
    assert info["video"]["avg_frame_rate"] == 60.0
