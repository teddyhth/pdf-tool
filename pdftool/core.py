"""PDF 解鎖與壓縮的核心邏輯（不依賴 GUI，可單獨匯入使用）。

壓縮原理：
  1. 找出每張圖片在頁面上實際顯示的尺寸，依「目標 DPI」計算需要的像素數，
     超過的就縮小，再以指定的 JPEG 品質重新編碼（只有變小才替換）。
  2. 儲存時清除未使用物件、合併重複物件、以 Flate 壓縮所有串流。
  3. （選用）字型子集化、轉灰階、移除中繼資料。
"""
from __future__ import annotations

import io
import math
import os
import re
import shutil
from dataclasses import dataclass, replace
from typing import Callable, Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3
except ImportError:  # 舊版只有 fitz 名稱
    import fitz

from PIL import Image

ProgressCb = Optional[Callable[[float, str], None]]


class PDFToolError(Exception):
    """一般處理錯誤。"""


class PasswordRequired(PDFToolError):
    """檔案需要開啟密碼但未提供。"""


class WrongPassword(PDFToolError):
    """提供的密碼不正確。"""


@dataclass
class CompressOptions:
    dpi: int = 150              # 圖片目標解析度
    quality: int = 70           # JPEG 品質 (1-95)
    grayscale: bool = False     # 圖片轉灰階
    subset_fonts: bool = False  # 字型子集化
    remove_metadata: bool = False


PRESETS: dict[str, CompressOptions] = {
    "高品質（輕度壓縮）": CompressOptions(dpi=200, quality=85),
    "平衡（推薦）": CompressOptions(dpi=150, quality=70),
    "小檔案": CompressOptions(dpi=110, quality=55),
    "極限壓縮": CompressOptions(dpi=72, quality=35),
}
DEFAULT_PRESET = "平衡（推薦）"

# 「目標大小」模式依序嘗試的 (DPI, 品質)，由高畫質到低畫質
TARGET_STEPS = [
    (220, 85), (180, 80), (150, 72), (135, 65), (120, 58),
    (105, 50), (96, 45), (85, 40), (72, 35), (60, 28), (50, 22),
]

# 小於此像素數或位元組數的圖片不處理（圖示、線條等，壓了也省不了多少）
MIN_IMAGE_PIXELS = 64 * 64
MIN_IMAGE_BYTES = 4 * 1024
# 縮放比例高於此值就不縮小，只重新編碼
RESIZE_THRESHOLD = 0.9
# 不安全轉換成 RGB/灰階的色彩空間
UNSAFE_COLORSPACES = ("Separation", "DeviceN", "Lab", "Pattern")

_SAVE_KW = dict(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)


@dataclass
class ProcessResult:
    src: str
    dst: Optional[str]
    original_size: int
    new_size: int
    message: str = ""
    skipped: bool = False

    @property
    def ratio(self) -> float:
        if not self.original_size:
            return 0.0
        return 1 - self.new_size / self.original_size


# ----------------------------------------------------------------------------
# 基本工具
# ----------------------------------------------------------------------------

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def open_pdf(path: str, password: str = ""):
    """開啟 PDF；若需要密碼則驗證，失敗時拋出 PasswordRequired / WrongPassword。"""
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise PDFToolError(f"無法開啟檔案：{e}") from e
    if not doc.is_pdf:
        doc.close()
        raise PDFToolError("不是有效的 PDF 檔案")
    if doc.needs_pass:
        if not password:
            doc.close()
            raise PasswordRequired("此 PDF 需要開啟密碼")
        if not doc.authenticate(password):
            doc.close()
            raise WrongPassword("密碼錯誤")
    return doc


def get_pdf_info(path: str) -> dict:
    """取得頁數、是否需要密碼、是否有加密/權限限制等資訊（不需密碼）。"""
    info = {"pages": None, "needs_pass": False, "encrypted": False,
            "size": os.path.getsize(path), "error": None}
    try:
        with fitz.open(path) as doc:
            info["needs_pass"] = bool(doc.needs_pass)
            info["encrypted"] = bool(doc.needs_pass or (doc.metadata or {}).get("encryption"))
            if not doc.needs_pass:
                info["pages"] = doc.page_count
    except Exception as e:
        info["error"] = str(e)
    return info


def check_password(path: str, password: str) -> bool:
    try:
        open_pdf(path, password).close()
        return True
    except PDFToolError:
        return False


def make_output_path(src: str, out_dir: Optional[str], suffix: str) -> str:
    """產生不覆蓋既有檔案的輸出路徑，例如 report_compressed.pdf、report_compressed (2).pdf。"""
    folder = out_dir or os.path.dirname(os.path.abspath(src))
    stem = os.path.splitext(os.path.basename(src))[0]
    candidate = os.path.join(folder, f"{stem}{suffix}.pdf")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem}{suffix} ({n}).pdf")
        n += 1
    return candidate


def _save_kwargs(decrypt: bool) -> dict:
    kw = dict(_SAVE_KW)
    kw["encryption"] = fitz.PDF_ENCRYPT_NONE if decrypt else fitz.PDF_ENCRYPT_KEEP
    return kw


def _save(doc, dst: str, **kw) -> None:
    """先寫入暫存檔再改名，避免中途失敗留下壞檔。"""
    tmp = dst + ".part"
    try:
        try:
            doc.save(tmp, use_objstms=1, **kw)
        except TypeError:  # 舊版 PyMuPDF 不支援 use_objstms
            doc.save(tmp, **kw)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _to_bytes(doc, **kw) -> bytes:
    try:
        return doc.tobytes(use_objstms=1, **kw)
    except TypeError:
        return doc.tobytes(**kw)


# ----------------------------------------------------------------------------
# 圖片重新壓縮
# ----------------------------------------------------------------------------

def _collect_images(doc):
    """回傳 {xref: (顯示寬 pt, 顯示高 pt) 或 None} 以及 {xref: 所在頁面最長邊 pt}。"""
    display: dict[int, tuple[float, float]] = {}
    page_max: dict[int, float] = {}
    for page in doc:
        longest = max(page.rect.width, page.rect.height)
        for img in page.get_images(full=True):
            xref = img[0]
            page_max[xref] = max(page_max.get(xref, 0.0), longest)
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            infos = []
        for info in infos:
            xref = info.get("xref", 0)
            if xref <= 0:
                continue
            a, b, c, d, _, _ = info["transform"]
            # 圖片的 x、y 軸在頁面上的實際長度（已考慮旋轉）
            dx, dy = math.hypot(a, b), math.hypot(c, d)
            ox, oy = display.get(xref, (0.0, 0.0))
            display[xref] = (max(ox, dx), max(oy, dy))
    return display, page_max


def _colorspace_is_safe(doc, xref: int) -> bool:
    kind, value = doc.xref_get_key(xref, "ColorSpace")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    # 展開一層間接參照，例如 [/Indexed 12 0 R 255 13 0 R]
    for ref in re.findall(r"(\d+) 0 R", value):
        try:
            value += doc.xref_object(int(ref), compressed=True)[:200]
        except Exception:
            pass
    return not any(name in value for name in UNSAFE_COLORSPACES)


def _recompress_image(doc, xref: int, display, page_max_pt: float, opts: CompressOptions) -> int:
    """重新壓縮單張圖片；回傳節省的位元組數（0 表示未變更）。"""
    if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
        return 0
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return 0
    if doc.xref_get_key(xref, "BitsPerComponent")[1] == "1":
        return 0  # 黑白圖通常已用 CCITT/JBIG2 壓得很小
    filt = doc.xref_get_key(xref, "Filter")[1]
    if "JBIG2Decode" in filt or "CCITTFaxDecode" in filt:
        return 0
    if "JPXDecode" in filt and doc.xref_get_key(xref, "SMaskInData")[0] != "null":
        return 0  # 透明度存在 JPX 資料內，轉 JPEG 會遺失
    if not _colorspace_is_safe(doc, xref):
        return 0

    raw = doc.xref_stream_raw(xref) or b""
    orig_len = len(raw)
    if orig_len < MIN_IMAGE_BYTES:
        return 0

    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace is None or pix.width * pix.height < MIN_IMAGE_PIXELS:
        return 0
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if opts.grayscale and pix.n != 1:
        pix = fitz.Pixmap(fitz.csGRAY, pix)
    elif pix.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)

    w, h = pix.width, pix.height
    if display and display[0] >= 1 and display[1] >= 1:
        scale = max(display[0] / 72 * opts.dpi / w, display[1] / 72 * opts.dpi / h)
    else:  # 找不到顯示位置時，以頁面大小為上限
        scale = (page_max_pt / 72 * opts.dpi) / max(w, h)

    mode = "L" if pix.n == 1 else "RGB"
    img = Image.frombytes(mode, (w, h), pix.samples)
    if scale < RESIZE_THRESHOLD:
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=opts.quality, optimize=True)
    data = buf.getvalue()
    if len(data) >= orig_len:
        return 0

    doc.update_stream(xref, data, compress=0)  # 會移除舊的 Filter/DecodeParms
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    doc.xref_set_key(xref, "Width", str(w))
    doc.xref_set_key(xref, "Height", str(h))
    doc.xref_set_key(xref, "ColorSpace", "/DeviceGray" if mode == "L" else "/DeviceRGB")
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "Decode", "null")
    if doc.xref_get_key(xref, "Mask")[0] == "array":  # 色鍵遮罩依賴原色彩空間
        doc.xref_set_key(xref, "Mask", "null")
    return orig_len - len(data)


def _compress_doc(doc, opts: CompressOptions, progress: ProgressCb = None) -> int:
    """就地壓縮已開啟的文件；回傳被重新壓縮的圖片數。"""
    report = progress or (lambda f, t: None)
    report(0.0, "分析圖片…")
    display, page_max = _collect_images(doc)
    total = len(page_max)
    changed = 0
    for i, (xref, longest) in enumerate(page_max.items(), 1):
        try:
            if _recompress_image(doc, xref, display.get(xref), longest, opts):
                changed += 1
        except Exception:
            pass  # 單張圖片失敗就保留原圖
        report(0.9 * i / total, f"壓縮圖片 {i}/{total}")

    if opts.subset_fonts:
        report(0.92, "字型子集化…")
        try:
            doc.subset_fonts()
        except Exception:
            pass
    if opts.remove_metadata:
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
    report(0.95, "寫入檔案…")
    return changed


# ----------------------------------------------------------------------------
# 對外功能
# ----------------------------------------------------------------------------

def unlock_pdf(src: str, dst: str, password: str = "", progress: ProgressCb = None) -> ProcessResult:
    """移除開啟密碼與權限限制（列印、複製、編輯等）。"""
    report = progress or (lambda f, t: None)
    orig = os.path.getsize(src)
    report(0.1, "開啟檔案…")
    doc = open_pdf(src, password)
    try:
        if not (doc.metadata or {}).get("encryption"):
            return ProcessResult(src, None, orig, orig, "檔案沒有加密，不需解鎖", skipped=True)
        report(0.5, "移除加密…")
        _save(doc, dst, **_save_kwargs(decrypt=True))
    finally:
        doc.close()
    report(1.0, "完成")
    return ProcessResult(src, dst, orig, os.path.getsize(dst), "已移除密碼與限制")


def compress_pdf(src: str, dst: str, opts: CompressOptions, password: str = "",
                 decrypt: bool = False, progress: ProgressCb = None) -> ProcessResult:
    report = progress or (lambda f, t: None)
    orig = os.path.getsize(src)
    doc = open_pdf(src, password)
    try:
        was_encrypted = bool((doc.metadata or {}).get("encryption"))
        changed = _compress_doc(doc, opts, progress)
        _save(doc, dst, **_save_kwargs(decrypt))
    finally:
        doc.close()

    new = os.path.getsize(dst)
    if new >= orig and not decrypt:
        shutil.copyfile(src, dst)
        report(1.0, "完成")
        return ProcessResult(src, dst, orig, orig, "原檔已相當精簡，無法再縮小（已保留原內容）")
    report(1.0, "完成")
    msg = f"重新壓縮 {changed} 張圖片（{opts.dpi} DPI、品質 {opts.quality}）"
    if decrypt and was_encrypted:
        msg += "，並移除加密"
    return ProcessResult(src, dst, orig, new, msg)


def compress_to_target(src: str, dst: str, target_bytes: int, base: CompressOptions,
                       password: str = "", decrypt: bool = False,
                       progress: ProgressCb = None) -> ProcessResult:
    """自動尋找能小於目標大小、且畫質最好的 DPI/品質組合（二分搜尋）。"""
    report = progress or (lambda f, t: None)
    orig = os.path.getsize(src)
    if orig <= target_bytes and not decrypt:
        shutil.copyfile(src, dst)
        report(1.0, "完成")
        return ProcessResult(src, dst, orig, orig, "原檔已小於目標大小，未做壓縮")

    cache: dict[int, bytes] = {}
    attempts = 0
    max_attempts = math.ceil(math.log2(len(TARGET_STEPS))) + 1

    def attempt(i: int) -> bytes:
        nonlocal attempts
        if i not in cache:
            dpi, q = TARGET_STEPS[i]
            base_f = min(attempts / max_attempts, 0.95)
            span = 0.95 / max_attempts

            def sub(f, t, dpi=dpi, q=q):
                report(base_f + span * f, f"嘗試 {dpi} DPI / 品質 {q}：{t}")

            doc = open_pdf(src, password)
            try:
                _compress_doc(doc, replace(base, dpi=dpi, quality=q), sub)
                cache[i] = _to_bytes(doc, **_save_kwargs(decrypt))
            finally:
                doc.close()
            attempts += 1
        return cache[i]

    lo, hi, best = 0, len(TARGET_STEPS) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(attempt(mid)) <= target_bytes:
            best, hi = mid, mid - 1
        else:
            lo = mid + 1

    reached = best is not None
    if not reached:
        best = min(cache, key=lambda k: len(cache[k]))
    data = cache[best]
    dpi, q = TARGET_STEPS[best]

    if len(data) >= orig and not decrypt:
        shutil.copyfile(src, dst)
        report(1.0, "完成")
        return ProcessResult(src, dst, orig, orig, "無法再縮小（已保留原內容）")

    tmp = dst + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    report(1.0, "完成")
    msg = f"使用 {dpi} DPI、品質 {q}"
    if not reached:
        msg = f"無法達到目標大小，已採用最小結果（{dpi} DPI、品質 {q}）"
    return ProcessResult(src, dst, orig, len(data), msg)


def process_pdf(src: str, dst: str, *, password: str = "", unlock: bool = False,
                compress: Optional[CompressOptions] = None,
                target_bytes: Optional[int] = None,
                progress: ProgressCb = None) -> ProcessResult:
    """統一入口：依選項解鎖及/或壓縮。"""
    if compress is None:
        if not unlock:
            raise PDFToolError("請至少選擇一項操作")
        return unlock_pdf(src, dst, password, progress)
    if target_bytes:
        return compress_to_target(src, dst, target_bytes, compress, password, unlock, progress)
    return compress_pdf(src, dst, compress, password, unlock, progress)
