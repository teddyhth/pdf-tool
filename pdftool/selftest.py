"""自我檢查：執行檔加上 --selftest [紀錄檔路徑] 參數時執行，供 CI 確認打包結果能正常運作。"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback


def run(log_path: str | None = None) -> int:
    try:
        import tkinter
        tkinter.Tcl()  # 不需要螢幕，即可確認 Tcl/Tk 有被打包進去
        from PIL import Image

        from . import core, gui  # noqa: F401

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.pdf")
            doc = core.fitz.open()
            page = doc.new_page()
            buf = io.BytesIO()
            Image.effect_noise((1200, 900), 60).convert("RGB").save(buf, "PNG")
            page.insert_image(page.rect, stream=buf.getvalue())
            doc.save(src, encryption=core.fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
            doc.close()

            r = core.process_pdf(src, os.path.join(tmp, "out.pdf"), password="pw", unlock=True,
                                 compress=core.CompressOptions())
            assert r.new_size < r.original_size, "壓縮後沒有變小"
            assert not core.get_pdf_info(r.dst)["needs_pass"], "解鎖失敗"
            text = (f"selftest ok: Tk {tkinter.TkVersion}, PyMuPDF {core.fitz.VersionBind}, "
                    f"{r.original_size} -> {r.new_size} bytes")
        code = 0
    except Exception:
        text, code = traceback.format_exc(), 1

    if log_path:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    elif sys.stdout:  # Windows 視窗程式沒有 stdout
        print(text)
    return code
