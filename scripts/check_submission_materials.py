"""检查参赛提交材料是否齐全，并校验正式演示视频时长。

默认只报告视频待补齐状态；传入 --require-video 可将缺失或不合规视频视为失败。
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    "参赛作品说明.md",
    "submission/01_可运行作品说明.md",
    "submission/02_技术文档.md",
    "submission/03_测试用例摘要.md",
    "submission/04_演示视频交付说明.md",
    "submission/05_合规说明.md",
    "submission/提交前核验清单.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOYMENT.md",
    "docs/TEST_CASES.md",
    "docs/COMPLIANCE.md",
    "docs/DEMO_VIDEO_SCRIPT.md",
    "docs/QUANTITATIVE_EVALUATION.md",
    "start.ps1",
    "start.sh",
)
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
MIN_VIDEO_SECONDS = 180
MAX_VIDEO_SECONDS = 300


def mp4_duration_seconds(path: Path) -> float | None:
    """从 MP4 的 mvhd atom 读取时长；无需 ffprobe 等外部二进制。"""
    if path.suffix.lower() not in {".mp4", ".mov"}:
        return None
    data = path.read_bytes()
    marker = b"mvhd"
    atom_type_offset = data.find(marker)
    if atom_type_offset < 4:
        return None
    body = atom_type_offset + 4
    if body >= len(data):
        return None
    version = data[body]
    try:
        if version == 0:
            timescale = struct.unpack(">I", data[body + 12 : body + 16])[0]
            duration = struct.unpack(">I", data[body + 16 : body + 20])[0]
        elif version == 1:
            timescale = struct.unpack(">I", data[body + 20 : body + 24])[0]
            duration = struct.unpack(">Q", data[body + 24 : body + 32])[0]
        else:
            return None
    except struct.error:
        return None
    return duration / timescale if timescale else None


def main() -> int:
    parser = argparse.ArgumentParser(description="校验校园科创导航智能体参赛提交材料")
    parser.add_argument("--video", type=Path, help="待提交的正式演示视频路径")
    parser.add_argument("--require-video", action="store_true", help="将视频缺失或不合规视为失败")
    args = parser.parse_args()

    missing = [name for name in REQUIRED_FILES if not (PROJECT_ROOT / name).is_file()]
    if missing:
        print("FAIL 缺少提交材料：")
        for name in missing:
            print(f"  - {name}")
    else:
        print(f"PASS 基础材料齐全：{len(REQUIRED_FILES)} 项")

    video_ok = False
    if args.video:
        video_path = args.video.expanduser().resolve()
        if not video_path.is_file():
            print(f"FAIL 视频不存在：{video_path}")
        elif video_path.suffix.lower() not in VIDEO_SUFFIXES:
            print(f"FAIL 视频格式不支持：{video_path.suffix}")
        else:
            duration = mp4_duration_seconds(video_path)
            if duration is None:
                print("FAIL 无法读取视频时长；请提供 MP4 或 MOV 文件。")
            elif MIN_VIDEO_SECONDS <= duration <= MAX_VIDEO_SECONDS:
                video_ok = True
                print(f"PASS 视频时长：{duration:.2f} 秒（符合 3—5 分钟要求）")
            else:
                print(
                    f"PENDING 视频时长：{duration:.2f} 秒（正式要求 {MIN_VIDEO_SECONDS}—{MAX_VIDEO_SECONDS} 秒）"
                )
    else:
        print("PENDING 尚未指定正式演示视频；可使用 --video 路径 --require-video 进行最终校验。")

    if missing or (args.require_video and not video_ok):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
