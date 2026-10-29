"""頁面編輯分頁：以縮圖拖曳調整頁面順序，並可從另一個 PDF 拖曳頁面合併進來。"""
from __future__ import annotations

import math
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import core, pages
from .core import fitz
from .pages import PageRef

THUMB_W, THUMB_H = 110, 150
CELL_W, CELL_H = THUMB_W + 26, THUMB_H + 48
CANVAS_BG = "#eceff3"
SELECT_BG = "#d6e6fb"
ACCENT = "#2f80ed"
SHIFT_MASK = 0x1
# 多選的切換鍵：Mac 用 Command、其他平台用 Ctrl（Windows 的 0x8 是 NumLock，不能共用）
TOGGLE_MASK = 0x8 if sys.platform == "darwin" else 0x4
MOD = "Command" if sys.platform == "darwin" else "Control"
MAX_UNDO = 50
MAX_OPEN_DOCS = 4


class ThumbnailRenderer:
    """在背景執行緒產生縮圖；PyMuPDF 文件只在該執行緒內使用。"""

    def __init__(self, widget: tk.Misc):
        self.widget = widget
        self.requests: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.images: dict[tuple, tk.PhotoImage] = {}
        self.failed: set[tuple] = set()
        self.pending: set[tuple] = set()
        self.listeners: list = []
        threading.Thread(target=self._work, daemon=True).start()
        widget.after(50, self._poll)

    @staticmethod
    def key(ref: PageRef) -> tuple:
        # 含修改時間：檔案被覆寫後會自動重新產生縮圖
        try:
            mtime = os.stat(ref.path).st_mtime_ns
        except OSError:
            mtime = 0
        return (ref.path, mtime, ref.index, ref.rotation)

    def get(self, ref: PageRef, password: str = ""):
        k = self.key(ref)
        img = self.images.get(k)
        if img is None and k not in self.pending and k not in self.failed:
            self.pending.add(k)
            self.requests.put((k, password))
        return img

    def _work(self) -> None:
        docs: dict[tuple, object] = {}
        while True:
            k, password = self.requests.get()
            path, mtime, index, rotation = k
            try:
                doc = docs.get((path, mtime))
                if doc is None:
                    # 從記憶體開啟，不佔用檔案（Windows 才能覆寫同名檔）
                    with open(path, "rb") as f:
                        doc = fitz.open(stream=f.read(), filetype="pdf")
                    if doc.needs_pass:
                        doc.authenticate(password)
                    docs[(path, mtime)] = doc
                    if len(docs) > MAX_OPEN_DOCS:
                        docs.pop(next(iter(docs))).close()
                data = pages.render_thumbnail(doc, index, rotation, THUMB_W, THUMB_H)
            except Exception:
                data = None
            self.results.put((k, data))

    def _poll(self) -> None:
        changed = False
        try:
            while True:
                k, data = self.results.get_nowait()
                self.pending.discard(k)
                if data:
                    self.images[k] = tk.PhotoImage(data=data)
                else:
                    self.failed.add(k)
                changed = True
        except queue.Empty:
            pass
        if changed:
            for callback in self.listeners:
                callback()
        self.widget.after(50, self._poll)


class ThumbPanel(ttk.Frame):
    """可捲動的縮圖格狀清單，負責顯示、選取與計算拖放位置。"""

    def __init__(self, master, renderer: ThumbnailRenderer, empty_hint: str, show_source: bool):
        super().__init__(master)
        self.renderer = renderer
        self.empty_hint = empty_hint
        self.show_source = show_source
        self.items: list[PageRef] = []
        self.passwords: dict[str, str] = {}
        self.selected: set[int] = set()
        self.anchor: int | None = None
        self.cols = 1
        self._placeholders: dict[int, int] = {}

        self.canvas = tk.Canvas(self, background=CANVAS_BG, highlightthickness=0, yscrollincrement=20,
                                width=3 * CELL_W, height=2 * CELL_H)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))  # Linux
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        renderer.listeners.append(self._fill_images)

    # ---------------------------------------------------------------- 繪製
    def _cell_origin(self, i: int) -> tuple[int, int]:
        return (i % self.cols) * CELL_W, (i // self.cols) * CELL_H

    def _label(self, i: int, ref: PageRef) -> str:
        text = f"第 {i + 1} 頁"
        if ref.rotation:
            text += f"  ↻{ref.rotation}°"
        if self.show_source:
            name = os.path.splitext(os.path.basename(ref.path))[0]
            if len(name) > 12:
                name = name[:11] + "…"
            text += f"\n{name} p.{ref.index + 1}"
        return text

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), CELL_W)
        self.cols = max(1, width // CELL_W)
        self._placeholders = {}
        for i, ref in enumerate(self.items):
            x, y = self._cell_origin(i)
            if i in self.selected:
                c.create_rectangle(x + 3, y + 3, x + CELL_W - 3, y + CELL_H - 3,
                                   fill=SELECT_BG, outline=ACCENT, width=2)
            cx, cy = x + CELL_W / 2, y + 8 + THUMB_H / 2
            img = self.renderer.get(ref, self.passwords.get(ref.path, ""))
            if img is not None:
                c.create_image(cx, cy, image=img)
            else:
                self._placeholders[i] = c.create_rectangle(
                    cx - THUMB_W / 2 + 10, cy - THUMB_H / 2 + 4, cx + THUMB_W / 2 - 10, cy + THUMB_H / 2 - 4,
                    fill="white", outline="#c8ccd2")
            c.create_text(cx, y + THUMB_H + 14, text=self._label(i, ref), anchor="n",
                          justify="center", fill="#333333", font="TkSmallCaptionFont")
        rows = math.ceil(len(self.items) / self.cols)
        c.configure(scrollregion=(0, 0, width, max(rows * CELL_H + 8, c.winfo_height())))
        if not self.items:
            c.create_text(width / 2, max(c.winfo_height(), 200) / 2, text=self.empty_hint,
                          fill="#8a9099", justify="center")

    def _fill_images(self) -> None:
        """縮圖產生後，把對應的白色佔位框換成圖片。"""
        for i, rect_id in list(self._placeholders.items()):
            if i >= len(self.items):
                continue
            img = self.renderer.images.get(self.renderer.key(self.items[i]))
            if img is not None:
                x, y = self._cell_origin(i)
                self.canvas.delete(rect_id)
                self.canvas.create_image(x + CELL_W / 2, y + 8 + THUMB_H / 2, image=img)
                del self._placeholders[i]

    def _on_wheel(self, e) -> None:
        step = -e.delta if sys.platform == "darwin" else -e.delta // 120 * 3
        self.canvas.yview_scroll(int(step), "units")

    # ---------------------------------------------------------- 位置計算
    def to_canvas(self, x_root: int, y_root: int) -> tuple[float, float]:
        return (self.canvas.canvasx(x_root - self.canvas.winfo_rootx()),
                self.canvas.canvasy(y_root - self.canvas.winfo_rooty()))

    def index_at(self, x: float, y: float) -> int | None:
        col, row = int(x // CELL_W), int(y // CELL_H)
        if x < 0 or y < 0 or col >= self.cols:
            return None
        i = row * self.cols + col
        return i if i < len(self.items) else None

    def insert_index_at(self, x: float, y: float) -> int:
        row = max(0, int(y // CELL_H))
        col = min(self.cols, max(0, round(x / CELL_W)))
        return min(len(self.items), row * self.cols + col)

    def show_marker(self, idx: int | None) -> None:
        self.canvas.delete("marker")
        if idx is None:
            return
        if idx >= len(self.items) and self.items:
            x, y = self._cell_origin(len(self.items) - 1)
            x += CELL_W
        else:
            x, y = self._cell_origin(idx)
        self.canvas.create_line(x, y + 6, x, y + CELL_H - 6, fill=ACCENT, width=4, tags="marker")

    def autoscroll(self, y_root: int) -> None:
        top = self.canvas.winfo_rooty()
        bottom = top + self.canvas.winfo_height()
        if y_root < top + 30:
            self.canvas.yview_scroll(-1, "units")
        elif y_root > bottom - 30:
            self.canvas.yview_scroll(1, "units")

    # ---------------------------------------------------------------- 選取
    def click_select(self, i: int, state: int) -> None:
        if state & SHIFT_MASK and self.anchor is not None:
            lo, hi = sorted((self.anchor, i))
            self.selected = set(range(lo, hi + 1))
        elif state & TOGGLE_MASK:
            self.selected ^= {i}
            self.anchor = i
        else:
            self.selected = {i}
            self.anchor = i
        self.redraw()

    def select(self, indices) -> None:
        self.selected = set(indices)
        self.anchor = min(self.selected) if self.selected else None
        self.redraw()
        if self.selected:  # 捲動到第一個選取的頁面
            _, y = self._cell_origin(min(self.selected))
            region = float(self.canvas.cget("scrollregion").split()[3] or 1)
            self.canvas.yview_moveto(max(0.0, y - 10) / region)

    def set_items(self, items: list[PageRef]) -> None:
        self.items = items
        self.selected = set()
        self.anchor = None
        self.canvas.yview_moveto(0)
        self.redraw()


class PageEditor(ttk.Frame):
    """左：來源 PDF（挑選頁面）；右：編輯中的 PDF（排序、刪除、旋轉、另存）。"""

    def __init__(self, master):
        super().__init__(master, padding=12)
        self.renderer = ThumbnailRenderer(self)
        self.drag: dict | None = None
        self.undo_stack: list[tuple[list[PageRef], set[int]]] = []
        self.dirty = False
        self.base_path: str | None = None
        self._saving = False
        self._range_order: list[int] = []
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text="把左邊的頁面拖到右邊即可合併；在右邊拖曳縮圖可調整順序。"
                             "按住 Shift 或 " + ("⌘" if sys.platform == "darwin" else "Ctrl") + " 可多選。",
                  foreground="gray").grid(row=0, column=0, sticky="w", pady=(0, 6))

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew")

        # 左：來源
        left = ttk.LabelFrame(paned, text="來源 PDF（從這裡挑頁面）", padding=6)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="開啟…", command=self.open_source).pack(side="left")
        self.lb_src = ttk.Label(bar, text="尚未開啟", foreground="gray")
        self.lb_src.pack(side="left", padx=6)
        bar2 = ttk.Frame(left)
        bar2.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        ttk.Label(bar2, text="頁碼：").pack(side="left")
        self.var_range = tk.StringVar()
        en = ttk.Entry(bar2, textvariable=self.var_range, width=12)
        en.pack(side="left")
        en.bind("<Return>", lambda e: self.select_range())
        ttk.Button(bar2, text="選取", command=self.select_range).pack(side="left", padx=(4, 0))
        ttk.Button(bar2, text="加入右側 →", command=self.add_selected_to_target).pack(side="left", padx=4)
        self.src = ThumbPanel(left, self.renderer, "按「開啟…」選擇要從中挑選頁面的 PDF\n（例如 A.pdf）",
                              show_source=False)
        self.src.grid(row=2, column=0, sticky="nsew")
        paned.add(left, weight=1)

        # 右：編輯中
        right = ttk.LabelFrame(paned, text="編輯中的 PDF（排序、刪除、旋轉）", padding=6)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        bar = ttk.Frame(right)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="開啟…", command=self.open_target).pack(side="left")
        ttk.Button(bar, text="清空", command=self.clear_target).pack(side="left", padx=(4, 0))
        self.lb_dst = ttk.Label(bar, text="空白（可直接從左邊拖頁面進來）", foreground="gray")
        self.lb_dst.pack(side="left", padx=6)
        bar2 = ttk.Frame(right)
        bar2.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        ttk.Button(bar2, text="↺ 左轉", command=lambda: self.rotate_selected(-90)).pack(side="left")
        ttk.Button(bar2, text="↻ 右轉", command=lambda: self.rotate_selected(90)).pack(side="left", padx=4)
        ttk.Button(bar2, text="刪除", command=self.delete_selected).pack(side="left")
        ttk.Button(bar2, text="復原", command=self.undo).pack(side="left", padx=4)
        self.bt_save = ttk.Button(bar2, text="另存新檔…", command=self.save)
        self.bt_save.pack(side="right")
        self.dst = ThumbPanel(right, self.renderer, "開啟要編輯的 PDF（例如 B.pdf），\n"
                                                    "或直接把左邊的頁面拖到這裡", show_source=True)
        self.dst.grid(row=2, column=0, sticky="nsew")
        paned.add(right, weight=1)

        self.lb_status = ttk.Label(self, text="", foreground="gray")
        self.lb_status.grid(row=2, column=0, sticky="w", pady=(6, 0))

        for panel in (self.src, self.dst):
            c = panel.canvas
            c.bind("<ButtonPress-1>", lambda e, p=panel: self._on_press(p, e))
            c.bind("<B1-Motion>", self._on_motion)
            c.bind("<ButtonRelease-1>", self._on_release)
            c.bind(f"<{MOD}-a>", lambda e, p=panel: p.select(range(len(p.items))))
            c.bind(f"<{MOD}-z>", lambda e: self.undo())
        self.src.canvas.bind("<Double-Button-1>", self._on_src_double)
        self.dst.canvas.bind("<Delete>", lambda e: self.delete_selected())
        self.dst.canvas.bind("<BackSpace>", lambda e: self.delete_selected())

    def _status(self, text: str) -> None:
        self.lb_status.configure(text=text)

    def _update_target_label(self) -> None:
        n = len(self.dst.items)
        if self.base_path:
            name = os.path.basename(self.base_path)
        else:
            name = "新文件" if n else "空白（可直接從左邊拖頁面進來）"
        mark = "（未儲存）" if self.dirty else ""
        self.lb_dst.configure(text=f"{name}　{n} 頁{mark}" if n or self.base_path else name)

    # -------------------------------------------------------------- 開檔
    def _ask_open(self, title: str):
        path = filedialog.askopenfilename(title=title, filetypes=[("PDF 檔案", "*.pdf *.PDF")])
        if not path:
            return None
        path = os.path.abspath(path)
        info = core.get_pdf_info(path)
        if info["error"]:
            messagebox.showerror("無法開啟", info["error"])
            return None
        password = ""
        if info["needs_pass"]:
            prompt = f"「{os.path.basename(path)}」需要開啟密碼："
            while True:
                password = simpledialog.askstring("需要密碼", prompt, show="•", parent=self)
                if password is None:
                    return None
                if core.check_password(path, password):
                    break
                prompt = "密碼錯誤，請重新輸入："
        return path, password, pages.page_count(path, password)

    def open_source(self) -> None:
        opened = self._ask_open("選擇來源 PDF（要從中挑選頁面）")
        if not opened:
            return
        path, password, count = opened
        self.src.passwords[path] = password
        self.src.set_items([PageRef(path, i) for i in range(count)])
        self.lb_src.configure(text=f"{os.path.basename(path)}　{count} 頁", foreground="")
        self._status("在左邊點選頁面後拖到右邊，或雙擊頁面加到最後。")

    def _confirm_discard(self) -> bool:
        return not self.dirty or messagebox.askyesno(
            "尚未儲存", "右邊的編輯尚未儲存，確定要放棄嗎？", icon="warning", parent=self)

    def open_target(self) -> None:
        if not self._confirm_discard():
            return
        opened = self._ask_open("選擇要編輯的 PDF")
        if not opened:
            return
        path, password, count = opened
        self._load_target(path, password, count)

    def _load_target(self, path: str, password: str, count: int) -> None:
        self.dst.passwords[path] = password
        self.dst.set_items([PageRef(path, i) for i in range(count)])
        self.base_path = path
        self.dirty = False
        self.undo_stack.clear()
        self._update_target_label()

    def clear_target(self) -> None:
        if not self._confirm_discard():
            return
        self.dst.set_items([])
        self.base_path = None
        self.dirty = False
        self.undo_stack.clear()
        self._update_target_label()

    # -------------------------------------------------------------- 編輯
    def _push_undo(self) -> None:
        self.undo_stack.append((list(self.dst.items), set(self.dst.selected)))
        del self.undo_stack[:-MAX_UNDO]

    def _changed(self, message: str) -> None:
        self.dirty = True
        self.dst.redraw()
        self._update_target_label()
        self._status(message)

    def undo(self) -> None:
        if not self.undo_stack:
            self._status("沒有可以復原的動作")
            return
        self.dst.items, self.dst.selected = self.undo_stack.pop()
        self._changed("已復原")

    def insert_pages(self, refs: list[PageRef], idx: int) -> None:
        if not refs:
            return
        self._push_undo()
        for ref in refs:
            if ref.path in self.src.passwords:
                self.dst.passwords.setdefault(ref.path, self.src.passwords[ref.path])
        self.dst.items[idx:idx] = refs
        self.dst.selected = set(range(idx, idx + len(refs)))
        self.dst.anchor = idx
        self._changed(f"已加入 {len(refs)} 頁到第 {idx + 1} 頁的位置")

    def move_pages(self, indices: list[int], idx: int) -> None:
        chosen = set(indices)
        moving = [self.dst.items[i] for i in sorted(chosen)]
        rest = [ref for i, ref in enumerate(self.dst.items) if i not in chosen]
        new_idx = idx - sum(1 for i in chosen if i < idx)
        if rest[:new_idx] + moving + rest[new_idx:] == self.dst.items:
            return
        self._push_undo()
        self.dst.items = rest[:new_idx] + moving + rest[new_idx:]
        self.dst.selected = set(range(new_idx, new_idx + len(moving)))
        self.dst.anchor = new_idx
        self._changed(f"已移動 {len(moving)} 頁")

    def delete_selected(self) -> None:
        if not self.dst.selected:
            self._status("請先在右邊選取要刪除的頁面")
            return
        self._push_undo()
        n = len(self.dst.selected)
        self.dst.items = [ref for i, ref in enumerate(self.dst.items) if i not in self.dst.selected]
        self.dst.selected = set()
        self._changed(f"已刪除 {n} 頁（可按「復原」）")

    def rotate_selected(self, delta: int) -> None:
        if not self.dst.selected:
            self._status("請先在右邊選取要旋轉的頁面")
            return
        self._push_undo()
        for i in self.dst.selected:
            self.dst.items[i] = self.dst.items[i].rotated(delta)
        self._changed(f"已旋轉 {len(self.dst.selected)} 頁")

    def select_range(self) -> None:
        if not self.src.items:
            self._status("請先在左邊開啟來源 PDF")
            return
        try:
            indices = pages.parse_page_ranges(self.var_range.get(), len(self.src.items))
        except ValueError as e:
            messagebox.showwarning("頁碼格式", f"{e}\n\n範例：1-3, 5, 8-", parent=self)
            return
        self.src.select(indices)
        self._range_order = indices  # 「加入右側」時依輸入的順序
        self._status(f"已選取 {len(indices)} 頁")

    def add_selected_to_target(self) -> None:
        if not self.src.selected:
            self._status("請先在左邊選取頁面（點選或輸入頁碼）")
            return
        order = self._range_order
        if set(order) != self.src.selected:
            order = sorted(self.src.selected)
        # 右邊有選取時插在選取頁之後，否則加到最後
        idx = max(self.dst.selected) + 1 if self.dst.selected else len(self.dst.items)
        self.insert_pages([self.src.items[i] for i in order], idx)

    # ------------------------------------------------------------ 拖放
    def _on_press(self, panel: ThumbPanel, e) -> None:
        panel.canvas.focus_set()
        x, y = panel.canvas.canvasx(e.x), panel.canvas.canvasy(e.y)
        i = panel.index_at(x, y)
        self.drag = {"panel": panel, "x": e.x_root, "y": e.y_root, "index": i,
                     "active": False, "collapse": None}
        if i is None:
            if not e.state & (SHIFT_MASK | TOGGLE_MASK):
                panel.select(())
            return
        if i in panel.selected and not e.state & (SHIFT_MASK | TOGGLE_MASK):
            self.drag["collapse"] = i  # 沒拖曳就放開時，只保留這一頁
        else:
            panel.click_select(i, e.state)

    def _drop_target(self, e) -> ThumbPanel | None:
        # 直接比對座標範圍；winfo_containing 在部分 macOS 環境會回傳 None
        c = self.dst.canvas
        x, y = e.x_root - c.winfo_rootx(), e.y_root - c.winfo_rooty()
        return self.dst if 0 <= x < c.winfo_width() and 0 <= y < c.winfo_height() else None

    def _on_motion(self, e) -> None:
        d = self.drag
        if not d or d["index"] is None or not d["panel"].selected:
            return
        if not d["active"]:
            if abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) < 6:
                return
            d["active"] = True
            for panel in (self.src, self.dst):
                panel.canvas.configure(cursor="hand2")
            self._status(f"拖曳 {len(d['panel'].selected)} 頁…放到右邊想要的位置")
        target = self._drop_target(e)
        if target is None:
            self.dst.show_marker(None)
            if d["panel"] is self.dst:
                self.dst.autoscroll(e.y_root)
            return
        target.autoscroll(e.y_root)
        target.show_marker(target.insert_index_at(*target.to_canvas(e.x_root, e.y_root)))

    def _on_release(self, e) -> None:
        d, self.drag = self.drag, None
        for panel in (self.src, self.dst):
            panel.canvas.configure(cursor="")
        self.dst.show_marker(None)
        if not d:
            return
        panel = d["panel"]
        if not d["active"]:
            if d["collapse"] is not None:
                panel.select({d["collapse"]})
            return
        target = self._drop_target(e)
        if target is None:
            self._status("要放到右邊「編輯中的 PDF」才會加入")
            return
        idx = target.insert_index_at(*target.to_canvas(e.x_root, e.y_root))
        if panel is self.src:
            self.insert_pages([self.src.items[i] for i in sorted(self.src.selected)], idx)
        else:
            self.move_pages(sorted(self.dst.selected), idx)

    def _on_src_double(self, e) -> None:
        i = self.src.index_at(self.src.canvas.canvasx(e.x), self.src.canvas.canvasy(e.y))
        if i is not None:
            self.insert_pages([self.src.items[i]], len(self.dst.items))

    # ---------------------------------------------------------------- 儲存
    def save(self) -> None:
        if self._saving:
            return
        if not self.dst.items:
            messagebox.showinfo("沒有頁面", "右邊還沒有任何頁面。", parent=self)
            return
        ref_path = self.base_path or self.dst.items[0].path
        stem = os.path.splitext(os.path.basename(ref_path))[0]
        dst = filedialog.asksaveasfilename(
            title="另存新檔", defaultextension=".pdf", filetypes=[("PDF 檔案", "*.pdf")],
            initialdir=os.path.dirname(ref_path), initialfile=f"{stem}_edited.pdf")
        if not dst:
            return
        dst = os.path.abspath(dst)
        refs = list(self.dst.items)
        passwords = {**self.src.passwords, **self.dst.passwords}
        result: dict = {}

        def work():
            try:
                result["count"] = pages.build_pdf(refs, dst, passwords)
            except Exception as ex:
                result["error"] = str(ex)

        self._saving = True
        self.bt_save.configure(state="disabled")
        self._status("儲存中…")
        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def wait():
            if thread.is_alive():
                self.after(100, wait)
                return
            self._saving = False
            self.bt_save.configure(state="normal")
            if "error" in result:
                messagebox.showerror("儲存失敗", result["error"], parent=self)
                self._status("儲存失敗")
                return
            # 改為編輯剛存好的檔案（旋轉等變更已寫入）
            self._load_target(dst, "", result["count"])
            if self.src.items and self.src.items[0].path == dst:
                self.src.set_items([PageRef(dst, i) for i in range(result["count"])])
                self.src.passwords[dst] = ""
            self._status(f"✓ 已儲存 {result['count']} 頁：{dst}")

        self.after(100, wait)
