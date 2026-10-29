"""頁面重新排列與跨檔合併的核心邏輯（不依賴 GUI）。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from . import core
from .core import ProgressCb, fitz


@dataclass(frozen=True)
class PageRef:
    """指向某個 PDF 檔的某一頁。"""
    path: str
    index: int          # 0 起算
    rotation: int = 0   # 額外的順時針旋轉角度（0/90/180/270）

    def rotated(self, delta: int) -> "PageRef":
        return PageRef(self.path, self.index, (self.rotation + delta) % 360)


def parse_page_ranges(text: str, count: int) -> list[int]:
    """把「1-3, 5, 8-」這類文字轉成 0 起算的頁碼清單（依輸入順序、去除重複）。

    支援：單頁「5」、範圍「1-3」、倒序「5-3」、到最後「8-」、從頭「-3」；
    分隔符可用半形或全形逗號、頓號、空白。
    """
    result: list[int] = []
    seen: set[int] = set()
    for part in re.split(r"[,，、\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*[-~－～]\s*(\d*)|(\d+)", part)
        if not m:
            raise ValueError(f"看不懂的頁碼：{part}")
        if m.group(3):
            start = end = int(m.group(3))
        else:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else count
        if not (1 <= start <= count and 1 <= end <= count):
            raise ValueError(f"頁碼超出範圍（1–{count}）：{part}")
        step = 1 if end >= start else -1
        for p in range(start, end + step, step):
            if p not in seen:
                seen.add(p)
                result.append(p - 1)
    return result


def page_count(path: str, password: str = "") -> int:
    doc = core.open_pdf(path, password)
    try:
        return doc.page_count
    finally:
        doc.close()


def render_thumbnail(doc, index: int, rotation: int, max_w: int, max_h: int) -> bytes:
    """把一頁算繪成不超過 max_w × max_h 的 PPM 圖（Tk 8.5/8.6 都能直接顯示）。"""
    page = doc[index]
    w, h = page.rect.width, page.rect.height
    if rotation % 180:
        w, h = h, w
    zoom = min(max_w / w, max_h / h)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom).prerotate(rotation), alpha=False)
    return pix.tobytes("ppm")


def build_pdf(refs: Iterable[PageRef], dst: str, passwords: Optional[dict[str, str]] = None,
              progress: ProgressCb = None) -> int:
    """依 refs 的順序組出新的 PDF（可來自多個檔案、可重複、可旋轉）；回傳頁數。

    輸出不加密。dst 可以是其中一個來源檔（會先寫到暫存檔，關閉來源後再取代）。
    """
    refs = list(refs)
    if not refs:
        raise core.PDFToolError("沒有任何頁面可以儲存")
    passwords = passwords or {}
    report = progress or (lambda f, t: None)
    sources: dict[str, object] = {}
    out = fitz.open()
    tmp = dst + ".part"
    try:
        try:
            for n, ref in enumerate(refs, 1):
                src = sources.get(ref.path)
                if src is None:
                    src = sources[ref.path] = core.open_pdf(ref.path, passwords.get(ref.path, ""))
                out.insert_pdf(src, from_page=ref.index, to_page=ref.index)
                if ref.rotation:
                    page = out[out.page_count - 1]
                    page.set_rotation((page.rotation + ref.rotation) % 360)
                report(0.9 * n / len(refs), f"組合頁面 {n}/{len(refs)}")

            report(0.95, "寫入檔案…")
            out.save(tmp, garbage=3, deflate=True)
            count = out.page_count
        finally:
            out.close()
            for src in sources.values():
                src.close()  # Windows 上必須先關閉，才能取代同名的來源檔
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    report(1.0, "完成")
    return count
