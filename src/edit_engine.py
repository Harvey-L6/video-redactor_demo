from dataclasses import dataclass, field


@dataclass
class EditPlan:
    keep_start: float = 0.0
    keep_end: float | None = None

    deleted_ranges: list[tuple[float, float]] = field(
        default_factory=list
    )

    def get_keep_ranges(
        self,
        duration: float
    ) -> list[tuple[float, float]]:

        if self.keep_start < 0:
            raise ValueError("影片起點不能小於 0")

        start = self.keep_start

        end = (
            duration
            if self.keep_end is None
            else min(self.keep_end, duration)
        )

        if start >= end:
            raise ValueError("影片保留範圍無效")

        # 整理要刪除的區段
        deleted = []

        for delete_start, delete_end in self.deleted_ranges:

            if delete_start < 0 or delete_start >= delete_end:
                raise ValueError("刪除區段設定無效")

            a = max(start, delete_start)
            b = min(end, delete_end)

            if a < b:
                deleted.append((a, b))

        deleted.sort()

        # 從刪除區段反推出保留區段
        keep_ranges = []
        cursor = start

        for a, b in deleted:

            if a > cursor:
                keep_ranges.append((cursor, a))

            cursor = max(cursor, b)

        if cursor < end:
            keep_ranges.append((cursor, end))

        if not keep_ranges:
            raise ValueError("沒有任何可以保留的影片區段")

        return keep_ranges