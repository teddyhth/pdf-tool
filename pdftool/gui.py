"""Tkinter 圖形介面（macOS / Windows 通用）。"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import core
from .page_editor import PageEditor

APP_TITLE = "PDF 工具（解鎖 / 壓縮 / 頁面編輯）"
CUSTOM_PRESET = "自訂"


def _enable_windows_dpi_awareness() -> None:
    """避免 Windows 11 高解析度螢幕上文字模糊。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def open_in_file_manager(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.master = master
        self.files: list[dict] = []   # 每項：path, info, iid
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_output_dir: str | None = None

        self.var_unlock = tk.BooleanVar(value=True)
        self.var_compress = tk.BooleanVar(value=True)
        self.var_password = tk.StringVar()
        self.var_preset = tk.StringVar(value=core.DEFAULT_PRESET)
        default = core.PRESETS[core.DEFAULT_PRESET]
        self.var_dpi = tk.IntVar(value=default.dpi)
        self.var_quality = tk.IntVar(value=default.quality)
        self.var_gray = tk.BooleanVar(value=False)
        self.var_subset = tk.BooleanVar(value=False)
        self.var_meta = tk.BooleanVar(value=False)
        self.var_use_target = tk.BooleanVar(value=False)
        self.var_target_mb = tk.DoubleVar(value=5.0)
        self.var_out_mode = tk.StringVar(value="same")
        self.var_out_dir = tk.StringVar()
        self._applying_preset = False

        self._build()
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self._update_states()
        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(bar, text="加入 PDF…", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="移除選取", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self.clear_files).pack(side="left")

        # 檔案清單
        lst = ttk.Frame(self)
        lst.grid(row=1, column=0, sticky="nsew")
        lst.columnconfigure(0, weight=1)
        lst.rowconfigure(0, weight=1)
        cols = ("name", "pages", "size", "lock", "status")
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", height=7, selectmode="extended")
        for col, text, width, anchor in (
            ("name", "檔案", 260, "w"), ("pages", "頁數", 50, "center"),
            ("size", "大小", 90, "e"), ("lock", "加密", 70, "center"),
            ("status", "狀態 / 結果", 300, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col in ("name", "status")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        # 操作
        ops = ttk.LabelFrame(self, text="操作", padding=8)
        ops.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(ops, text="移除密碼與限制（解鎖）", variable=self.var_unlock,
                        command=self._update_states).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(ops, text="壓縮", variable=self.var_compress,
                        command=self._update_states).grid(row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Label(ops, text="開啟密碼：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        pw = ttk.Frame(ops)
        pw.grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Entry(pw, textvariable=self.var_password, show="•", width=24).pack(side="left")
        ttk.Label(pw, text="（只有權限限制的檔案不需密碼；密碼錯誤時會逐一詢問）",
                  foreground="gray").pack(side="left", padx=6)

        # 壓縮設定
        self.comp = ttk.LabelFrame(self, text="壓縮設定", padding=8)
        self.comp.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.comp.columnconfigure(1, weight=1)

        ttk.Label(self.comp, text="預設：").grid(row=0, column=0, sticky="w")
        self.cb_preset = ttk.Combobox(self.comp, textvariable=self.var_preset, state="readonly", width=20,
                                      values=list(core.PRESETS) + [CUSTOM_PRESET])
        self.cb_preset.grid(row=0, column=1, sticky="w")
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        ttk.Label(self.comp, text="圖片解析度 (DPI)：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sc_dpi = ttk.Scale(self.comp, from_=50, to=300, variable=self.var_dpi,
                                command=lambda v: self._on_slider(self.var_dpi, v))
        self.sc_dpi.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.lb_dpi = ttk.Label(self.comp, width=12)
        self.lb_dpi.grid(row=1, column=2, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(self.comp, text="JPEG 品質：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.sc_q = ttk.Scale(self.comp, from_=10, to=95, variable=self.var_quality,
                              command=lambda v: self._on_slider(self.var_quality, v))
        self.sc_q.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        self.lb_q = ttk.Label(self.comp, width=12)
        self.lb_q.grid(row=2, column=2, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(self.comp, text="DPI 越低、品質越低 → 檔案越小。螢幕閱讀 100–150、列印 200–300。",
                  foreground="gray").grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        opts = ttk.Frame(self.comp)
        opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(opts, text="圖片轉灰階", variable=self.var_gray).pack(side="left")
        ttk.Checkbutton(opts, text="字型子集化（內嵌完整字型時有效）",
                        variable=self.var_subset).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="移除中繼資料", variable=self.var_meta).pack(side="left")

        tgt = ttk.Frame(self.comp)
        tgt.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(tgt, text="指定目標大小：", variable=self.var_use_target,
                        command=self._update_states).pack(side="left")
        self.sp_target = ttk.Spinbox(tgt, from_=0.1, to=2000, increment=0.5, width=8,
                                     textvariable=self.var_target_mb)
        self.sp_target.pack(side="left")
        ttk.Label(tgt, text="MB（自動調整 DPI 與品質，會花較久時間）",
                  foreground="gray").pack(side="left", padx=6)

        # 輸出
        out = ttk.LabelFrame(self, text="輸出位置", padding=8)
        out.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        out.columnconfigure(2, weight=1)
        ttk.Radiobutton(out, text="與原檔相同資料夾", value="same", variable=self.var_out_mode,
                        command=self._update_states).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(out, text="指定資料夾：", value="custom", variable=self.var_out_mode,
                        command=self._update_states).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.en_out = ttk.Entry(out, textvariable=self.var_out_dir)
        self.en_out.grid(row=0, column=2, sticky="ew")
        self.bt_out = ttk.Button(out, text="選擇…", command=self.choose_out_dir)
        self.bt_out.grid(row=0, column=3, padx=(6, 0))

        # 執行
        run = ttk.Frame(self)
        run.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        run.columnconfigure(2, weight=1)
        self.bt_start = ttk.Button(run, text="開始處理", command=self.start)
        self.bt_start.grid(row=0, column=0)
        self.bt_open = ttk.Button(run, text="開啟輸出資料夾", command=self.open_output, state="disabled")
        self.bt_open.grid(row=0, column=1, padx=6)
        self.progress = ttk.Progressbar(run, maximum=1.0)
        self.progress.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        self.lb_status = ttk.Label(self, text="請加入 PDF 檔案", foreground="gray")
        self.lb_status.grid(row=6, column=0, sticky="w", pady=(4, 0))

        self._refresh_slider_labels()

    # ------------------------------------------------------------ 設定互動
    def _apply_preset(self) -> None:
        preset = core.PRESETS.get(self.var_preset.get())
        if not preset:
            return
        self._applying_preset = True
        self.var_dpi.set(preset.dpi)
        self.var_quality.set(preset.quality)
        self._applying_preset = False
        self._refresh_slider_labels()

    def _on_slider(self, var: tk.IntVar, value: str) -> None:
        var.set(int(round(float(value))))
        self._refresh_slider_labels()
        if not self._applying_preset:
            self.var_preset.set(CUSTOM_PRESET)

    def _refresh_slider_labels(self) -> None:
        self.lb_dpi.configure(text=f"{self.var_dpi.get()} DPI")
        self.lb_q.configure(text=f"{self.var_quality.get()}")

    def _update_states(self) -> None:
        busy = self.worker is not None and self.worker.is_alive()
        compress = self.var_compress.get()

        def set_state(widget, enabled):
            if isinstance(widget, (ttk.Combobox,)):
                widget.configure(state="readonly" if enabled else "disabled")
            elif isinstance(widget, ttk.Scale):
                widget.state(["!disabled"] if enabled else ["disabled"])
            else:
                try:
                    widget.configure(state="normal" if enabled else "disabled")
                except tk.TclError:
                    pass

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Frame, ttk.LabelFrame)):
                    walk(child)
                elif not isinstance(child, ttk.Label):
                    set_state(child, compress and not busy)

        walk(self.comp)
        if compress and not busy:
            target = self.var_use_target.get()
            set_state(self.sp_target, target)
            # 目標大小模式會自動決定 DPI/品質
            for w in (self.cb_preset, self.sc_dpi, self.sc_q):
                set_state(w, not target)

        custom = self.var_out_mode.get() == "custom" and not busy
        set_state(self.en_out, custom)
        set_state(self.bt_out, custom)
        set_state(self.bt_start, not busy)

    # ------------------------------------------------------------ 檔案清單
    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="選擇 PDF 檔案", filetypes=[("PDF 檔案", "*.pdf *.PDF"), ("所有檔案", "*.*")])
        known = {f["path"] for f in self.files}
        for p in paths:
            p = os.path.abspath(p)
            if p in known:
                continue
            info = core.get_pdf_info(p)
            if info["error"]:
                lock, status = "-", f"無法讀取：{info['error']}"
            else:
                lock = "需密碼" if info["needs_pass"] else ("有限制" if info["encrypted"] else "無")
                status = "等待中"
            iid = self.tree.insert("", "end", values=(
                os.path.basename(p), info["pages"] or "?", core.human_size(info["size"]), lock, status))
            self.files.append({"path": p, "info": info, "iid": iid})
            known.add(p)
        if self.files:
            self.lb_status.configure(text=f"共 {len(self.files)} 個檔案")

    def remove_selected(self) -> None:
        if self._busy():
            return
        selected = set(self.tree.selection())
        for iid in selected:
            self.tree.delete(iid)
        self.files = [f for f in self.files if f["iid"] not in selected]

    def clear_files(self) -> None:
        if self._busy():
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear()

    def choose_out_dir(self) -> None:
        d = filedialog.askdirectory(title="選擇輸出資料夾")
        if d:
            self.var_out_dir.set(d)

    def open_output(self) -> None:
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            open_in_file_manager(self.last_output_dir)

    def _set_status(self, iid: str, text: str) -> None:
        if self.tree.exists(iid):
            self.tree.set(iid, "status", text)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    # ------------------------------------------------------------ 執行
    def _collect_options(self):
        if not self.var_compress.get():
            return None, None
        opts = replace(core.CompressOptions(), dpi=self.var_dpi.get(), quality=self.var_quality.get(),
                       grayscale=self.var_gray.get(), subset_fonts=self.var_subset.get(),
                       remove_metadata=self.var_meta.get())
        target = None
        if self.var_use_target.get():
            try:
                mb = float(self.sp_target.get())
            except ValueError:
                mb = 0
            if mb <= 0:
                raise ValueError("目標大小必須大於 0")
            target = int(mb * 1024 * 1024)
        return opts, target

    def _resolve_passwords(self, items: list[dict]) -> list[tuple[dict, str]]:
        """在主執行緒逐一確認密碼；使用者取消的檔案會被略過。"""
        common = self.var_password.get()
        jobs = []
        for item in items:
            if not item["info"]["needs_pass"]:
                jobs.append((item, ""))
                continue
            pw = common
            prompt = f"「{os.path.basename(item['path'])}」需要開啟密碼："
            while not (pw and core.check_password(item["path"], pw)):
                if pw:
                    prompt = f"密碼錯誤，請重新輸入「{os.path.basename(item['path'])}」的密碼："
                pw = simpledialog.askstring("需要密碼", prompt, show="•", parent=self.master)
                if pw is None:
                    break
            if pw is None:
                self._set_status(item["iid"], "已略過（未提供密碼）")
                continue
            jobs.append((item, pw))
        return jobs

    def start(self) -> None:
        if self._busy():
            return
        unlock, compress = self.var_unlock.get(), self.var_compress.get()
        if not (unlock or compress):
            messagebox.showwarning(APP_TITLE, "請至少勾選「解鎖」或「壓縮」其中一項。")
            return
        items = [f for f in self.files if not f["info"]["error"]]
        if not items:
            messagebox.showinfo(APP_TITLE, "請先加入可讀取的 PDF 檔案。")
            return
        out_dir = None
        if self.var_out_mode.get() == "custom":
            out_dir = self.var_out_dir.get().strip()
            if not out_dir or not os.path.isdir(out_dir):
                messagebox.showwarning(APP_TITLE, "請選擇有效的輸出資料夾。")
                return
        try:
            opts, target = self._collect_options()
        except ValueError as e:
            messagebox.showwarning(APP_TITLE, str(e))
            return

        jobs = self._resolve_passwords(items)
        if not jobs:
            return
        suffix = "_unlocked_compressed" if unlock and compress else (
            "_unlocked" if unlock else "_compressed")

        for item, _ in jobs:
            self._set_status(item["iid"], "等待中")
        self.progress["value"] = 0
        self.worker = threading.Thread(
            target=self._run_jobs, args=(jobs, unlock, opts, target, out_dir, suffix), daemon=True)
        self.worker.start()
        self._update_states()

    def _run_jobs(self, jobs, unlock, opts, target, out_dir, suffix) -> None:
        """背景執行緒：只透過 queue 與介面溝通。"""
        n = len(jobs)
        total_before = total_after = 0
        for idx, (item, pw) in enumerate(jobs):
            iid, src = item["iid"], item["path"]

            def progress(frac, text, idx=idx, iid=iid):
                self.events.put(("progress", iid, (idx + frac) / n, text))

            try:
                dst = core.make_output_path(src, out_dir, suffix)
                result = core.process_pdf(src, dst, password=pw, unlock=unlock, compress=opts,
                                          target_bytes=target, progress=progress)
                if not result.skipped:
                    total_before += result.original_size
                    total_after += result.new_size
                self.events.put(("done", iid, result))
            except Exception as e:
                self.events.put(("error", iid, str(e)))
        self.events.put(("finished", total_before, total_after))

    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    _, iid, overall, text = ev
                    self.progress["value"] = overall
                    self._set_status(iid, text)
                    self.lb_status.configure(text=text)
                elif kind == "done":
                    _, iid, r = ev
                    if r.skipped:
                        self._set_status(iid, f"略過：{r.message}")
                    else:
                        self.last_output_dir = os.path.dirname(r.dst)
                        change = f"{core.human_size(r.original_size)} → {core.human_size(r.new_size)}"
                        if r.ratio > 0:
                            change += f"（-{r.ratio * 100:.1f}%）"
                        self._set_status(iid, f"✓ {change}　{r.message}")
                elif kind == "error":
                    _, iid, msg = ev
                    self._set_status(iid, f"✗ 失敗：{msg}")
                elif kind == "finished":
                    _, before, after = ev
                    self.progress["value"] = 1.0
                    summary = "處理完成"
                    if before:
                        summary += f"：合計 {core.human_size(before)} → {core.human_size(after)}"
                    self.lb_status.configure(text=summary)
                    self.bt_open.configure(state="normal" if self.last_output_dir else "disabled")
                    self.worker = None
                    self._update_states()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


def main() -> None:
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(820, 660)
    root.geometry("1000x720")
    style = ttk.Style(root)
    if sys.platform == "win32" and "vista" in style.theme_names():
        style.theme_use("vista")

    notebook = ttk.Notebook(root)
    notebook.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    tab = ttk.Frame(notebook)
    App(tab)
    notebook.add(tab, text="解鎖 / 壓縮")
    editor = PageEditor(notebook)
    notebook.add(editor, text="頁面編輯（排序 / 合併）")

    def on_close():
        if editor.dirty and not messagebox.askyesno(
                APP_TITLE, "「頁面編輯」有尚未儲存的變更，確定要關閉嗎？", icon="warning"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    # 從終端機或捷徑啟動時，視窗可能被擋在後面
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
