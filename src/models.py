"""Data models for fixed and keyframed video masks."""
from dataclasses import dataclass, field


@dataclass
class Keyframe:
    """One mask rectangle at a time on the ORIGINAL video timeline."""
    time: float
    x: float
    y: float
    width: float
    height: float

    def geometry(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class Mask:
    name: str
    start_time: float
    end_time: float
    x: float
    y: float
    width: float
    height: float
    # Masks without keyframes use a fixed rectangle.
    keyframes: list[Keyframe] = field(default_factory=list)
    interpolation: str = "linear"  # "linear" or "hold"

    def is_active(self, current_time: float) -> bool:
        return self.start_time <= current_time <= self.end_time

    def geometry_at(self, current_time: float) -> tuple[float, float, float, float]:
        """Evaluate position + size at original-video time, independent of trimming."""
        frames = sorted(self.keyframes, key=lambda f: f.time)
        if not frames:
            return (self.x, self.y, self.width, self.height)
        if current_time <= frames[0].time:
            return frames[0].geometry()
        if current_time >= frames[-1].time:
            return frames[-1].geometry()

        for left, right in zip(frames, frames[1:]):
            if left.time <= current_time < right.time:
                if self.interpolation == "hold":
                    return left.geometry()
                if self.interpolation != "linear":
                    raise ValueError(f"不支援的關鍵影格插值模式：{self.interpolation}")
                fraction = (current_time - left.time) / (right.time - left.time)
                return tuple(
                    a + (b - a) * fraction
                    for a, b in zip(left.geometry(), right.geometry())
                )
        return frames[-1].geometry()

    def set_keyframe(
        self, current_time: float,
        x: float, y: float, width: float, height: float,
    ) -> None:
        """Replace keyframe at the same timestamp rather than appending duplicates."""
        if not (self.start_time <= current_time <= self.end_time):
            raise ValueError("位置點時間必須位於遮罩作用時間範圍內")
        for f in self.keyframes:
            if abs(f.time - current_time) < 1e-6:
                f.x, f.y, f.width, f.height = x, y, width, height
                return
        self.keyframes.append(Keyframe(current_time, x, y, width, height))
        self.keyframes.sort(key=lambda f: f.time)

    def remove_keyframe(self, current_time: float) -> None:
        self.keyframes = [
            f for f in self.keyframes if abs(f.time - current_time) >= 1e-6
        ]

    def get_pixel_rect(
        self, video_width: int, video_height: int,
    ) -> tuple[int, int, int, int]:
        """Convert the fixed normalized rectangle to video pixel coordinates."""
        return (int(self.x * video_width), int(self.y * video_height),
                int(self.width * video_width), int(self.height * video_height))

    def get_pixel_rect_at(
        self, current_time: float, video_width: int, video_height: int,
    ) -> tuple[int, int, int, int]:
        x, y, w, h = self.geometry_at(current_time)
        # Bound every corner to the source image; no mask can extend outside it.
        left = max(0, min(video_width, round(x * video_width)))
        top = max(0, min(video_height, round(y * video_height)))
        right = max(left, min(video_width, round((x + w) * video_width)))
        bottom = max(top, min(video_height, round((y + h) * video_height)))
        return left, top, right - left, bottom - top
