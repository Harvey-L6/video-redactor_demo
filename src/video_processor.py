"""Video rendering engine: trim source sections and apply masks frame-by-frame."""
from pathlib import Path

import cv2

from models import Mask
from edit_engine import EditPlan


def process_video(
    input_path: Path,
    output_path: Path,
    masks: list[Mask],
    edit_plan: EditPlan | None = None,
) -> None:
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"無法開啟影片：{input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0 or frame_count <= 0 or width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError("無法取得有效影片資訊")
    duration = frame_count / fps
    if edit_plan is None:
        edit_plan = EditPlan()
    try:
        keep_ranges = edit_plan.get_keep_ranges(duration)
    except Exception:
        cap.release()
        raise
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("無法建立輸出影片")

    print(f"原始影片：{duration:.2f} 秒，{width}×{height}，{fps:.2f} FPS")
    print("保留區段：", keep_ranges)
    frame_index = 0
    output_frames = 0
    range_index = 0
    try:
        while True:
            success, frame = cap.read()
            if not success:
                break
            source_time = frame_index / fps
            frame_index += 1
            while (range_index < len(keep_ranges)
                   and source_time >= keep_ranges[range_index][1]):
                range_index += 1
            if range_index >= len(keep_ranges):
                break
            if source_time < keep_ranges[range_index][0]:
                continue

            for mask in masks:
                if not mask.is_active(source_time):
                    continue
                x, y, w, h = mask.get_pixel_rect_at(source_time, width, height)
                if w <= 0 or h <= 0:
                    continue
                # Use a half-open pixel rectangle: frame[y:y+h, x:x+w].
                # It is always fully opaque; video re-encoding bakes in the mask.
                frame[y:y + h, x:x + w] = 0
            writer.write(frame)
            output_frames += 1
    finally:
        cap.release()
        writer.release()
    if output_frames == 0:
        raise RuntimeError("輸出影片沒有保留任何影格")
    print(f"輸出完成：{output_path}（約 {output_frames / fps:.2f} 秒，無音訊）")
