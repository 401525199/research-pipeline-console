# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Qi Wang

from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# In a PyInstaller one-file build, ``__file__`` points into a temporary
# extraction directory. Keep the user-owned configuration beside the exe so
# scan settings and snapshots survive the next launch.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    # In the development layout the executable lives in ``dist`` while the
    # durable state is kept one level above it.  Prefer that canonical state
    # so the source self-test and the desktop shortcut cannot diverge.
    _canonical_state = APP_DIR.parent / "state" if APP_DIR.name.casefold() == "dist" else APP_DIR / "state"
else:
    APP_DIR = Path(__file__).resolve().parent
    _canonical_state = APP_DIR / "state"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATE_DIR = _canonical_state if _canonical_state.exists() else APP_DIR / "state"
CONFIG_PATH = STATE_DIR / "console_config.json"
SNAPSHOT_PATH = STATE_DIR / "scan_snapshot.json"
FILE_NOTES_PATH = STATE_DIR / "file_research_notes.json"
PROJECT_NOTE_OVERRIDES_PATH = STATE_DIR / "project_note_overrides.json"
DATA_PROFILE_HELPER_PATH = RESOURCE_DIR / "data_profile_helper.py"


DEFAULT_ROOTS = [str(Path.home() / "Documents" / "Research")]
IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".idea",
    ".vscode",
    "$recycle.bin",
    "researchpipelineconsole",
}

TEXT_EXTENSIONS = {
    ".py",
    ".pyw",
    ".ipynb",
    ".do",
    ".ado",
    ".tex",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".bat",
    ".cmd",
    ".ps1",
}

# Only inspect Windows drive paths.  A previous version also matched doubled
# backslashes, which made LaTeX commands such as ``\\begin{table}`` and
# ``\\setlength{...}`` look like missing filesystem paths.
ABS_PATH_RE = re.compile(
    r"(?P<path>(?<![A-Za-z0-9])[A-Z]:[\\/][^\"'\r\n<>`]+)"
)

PALETTE = {
    "navy": "#101828",
    "navy_light": "#EEF2F7",
    "surface": "#F5F7FB",
    "surface_alt": "#EEF2F6",
    "card": "#FFFFFF",
    "ink": "#172033",
    "muted": "#667085",
    "line": "#E4E7EC",
    "cyan": "#2563EB",
    "cyan_soft": "#EAF2FF",
    "amber": "#F59E0B",
    "green": "#16A34A",
    "red": "#DC2626",
}

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"


class FlatSelect(tk.Frame):
    """A compact flat dropdown that avoids the dated native combobox chrome."""

    def __init__(
        self,
        master: tk.Misc,
        variable: tk.StringVar,
        values: Iterable[str],
        width: int = 15,
    ) -> None:
        super().__init__(
            master,
            background=PALETTE["card"],
            highlightbackground=PALETTE["line"],
            highlightthickness=1,
        )
        self.variable = variable
        self.values = list(values)
        self.menu = tk.Menu(
            self,
            tearoff=False,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            activebackground=PALETTE["cyan_soft"],
            activeforeground="#1D4ED8",
            font=(UI_FONT, 9),
            borderwidth=0,
        )
        for value in self.values:
            self.menu.add_command(label=value, command=lambda item=value: self.variable.set(item))
        self.button = tk.Button(
            self,
            textvariable=self.variable,
            command=self.show_menu,
            anchor="w",
            width=width,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            activebackground=PALETTE["card"],
            activeforeground=PALETTE["ink"],
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9),
            padx=10,
            pady=7,
        )
        self.button.pack(side="left", fill="x", expand=True)
        arrow = tk.Label(
            self,
            text="⌄",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 10, "bold"),
            padx=8,
        )
        arrow.pack(side="right", fill="y")
        arrow.bind("<Button-1>", lambda _event: self.show_menu())

    def show_menu(self) -> None:
        try:
            self.menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            self.menu.grab_release()


@dataclass
class ProjectDefinition:
    project_id: str
    name: str
    status: str
    kind: str
    relative_path: str
    note: str
    portfolio: str = "Local research"




def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def path_key(path: str | Path) -> str:
    return str(safe_resolve(path)).casefold()


def load_file_research_notes() -> dict[tuple[str, str], dict[str, Any]]:
    """Load app-owned file notes without touching any research source file."""
    if not FILE_NOTES_PATH.exists():
        return {}
    try:
        payload = json.loads(FILE_NOTES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    notes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("notes", []):
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("project_id", "")).strip().casefold()
        relative_path = str(item.get("relative_path", "")).strip().replace("\\", "/").casefold()
        if project_id and relative_path:
            notes[(project_id, relative_path)] = item
    return notes


def load_project_note_overrides() -> dict[str, str]:
    if not PROJECT_NOTE_OVERRIDES_PATH.exists():
        return {}
    try:
        payload = json.loads(PROJECT_NOTE_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    raw_notes = payload.get("notes", {})
    if not isinstance(raw_notes, dict):
        return {}
    return {str(key).casefold(): str(value) for key, value in raw_notes.items()}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Persist app metadata safely while leaving research files untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def unique_existing_paths(paths: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = safe_resolve(raw)
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(str(path))
    return result


class Registry:
    """Read-only scanner and project registry.

    This class deliberately never moves, renames, deletes, or edits anything
    under the monitored roots.
    """

    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.roots = self.load_roots()

    def load_roots(self) -> list[str]:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                roots = data.get("roots", [])
                if isinstance(roots, list) and roots:
                    return unique_existing_paths([str(x) for x in roots])
            except (OSError, json.JSONDecodeError):
                pass
        return unique_existing_paths(DEFAULT_ROOTS)

    def save_roots(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps({"roots": self.roots}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def articles_root(self) -> Path | None:
        for raw_root in self.roots:
            candidate = safe_resolve(raw_root)
            if candidate.is_dir():
                return candidate
        return None

    def locate_project(self, relative_path: str) -> Path | None:
        relative = Path(relative_path)
        if relative.is_absolute():
            return safe_resolve(relative)
        articles = self.articles_root()
        candidates: list[Path] = []
        if articles:
            candidates.append(articles / relative)
        for raw_root in self.roots:
            root = safe_resolve(raw_root)
            candidates.append(root / relative)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def iter_files(
        self,
        root: Path,
        max_files: int = 20000,
        max_depth: int = 12,
    ) -> tuple[list[Path], int, int, bool]:
        files: list[Path] = []
        directories = 0
        truncated = False
        if not root.is_dir():
            return files, directories, 0, False

        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                continue
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d.casefold() not in IGNORED_DIR_NAMES
                    and not d.casefold().startswith(".venv")
                ]
            directories += len(dirnames)
            for filename in filenames:
                file_path = current_path / filename
                files.append(file_path)
                if len(files) >= max_files:
                    truncated = True
                    return files, directories, len(files), truncated
        return files, directories, len(files), truncated

    def scan_text_references(
        self,
        files: Iterable[Path],
        max_text_files: int = 6000,
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        checked = 0
        for file_path in files:
            if checked >= max_text_files:
                break
            if file_path.suffix.casefold() not in TEXT_EXTENSIONS:
                continue
            try:
                if file_path.stat().st_size > 4 * 1024 * 1024:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeError):
                continue
            checked += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                for match in ABS_PATH_RE.finditer(line):
                    # Markdown backticks and sentence punctuation are not part
                    # of a path.  Keeping them out here also prevents a valid
                    # directory from becoming a false MISSING result.
                    candidate = match.group("path").strip()
                    candidate = candidate.rstrip("`'\" ,;:)]}>\t")
                    candidate = candidate.replace("\\\\", "\\")
                    if len(candidate) < 8:
                        continue
                    candidate_path = safe_resolve(candidate)
                    exists = candidate_path.exists()
                    alerts.append(
                        {
                            "status": "OK" if exists else "MISSING",
                            "category": "固定路径",
                            "file": str(file_path),
                            "line": line_number,
                            "reference": candidate,
                            "message": "路径存在" if exists else "按当前完整路径未找到；请核查是否为旧引用",
                        }
                    )
                    if len(alerts) >= 1500:
                        return alerts
        return alerts

    def project_rows(self) -> list[dict[str, Any]]:
        """Discover immediate project folders under the roots selected by the user."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        ignored_names = IGNORED_DIR_NAMES | {"build", "dist", "state", "tmp", "temp", "cache", "caches"}

        for raw_root in self.roots:
            root = safe_resolve(raw_root)
            if not root.is_dir():
                continue
            try:
                children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue

            for child in children:
                if not child.is_dir() or child.is_symlink():
                    continue
                name_key = child.name.casefold()
                if name_key in ignored_names or name_key.startswith((".codex", ".chart-data-", "ppt_build_")):
                    continue
                project_id = path_key(child)
                if project_id in seen:
                    continue
                seen.add(project_id)
                project = ProjectDefinition(
                    project_id=project_id,
                    name=child.name,
                    status="Discovered",
                    kind="Folder",
                    relative_path=str(child),
                    note="",
                    portfolio=root.name or "Local",
                )
                rows.append({**asdict(project), "path": str(child), "exists": True})
        return rows

    def scan(self, progress: Any | None = None) -> dict[str, Any]:
        started = now_text()
        root_rows: list[dict[str, Any]] = []
        all_files: list[Path] = []
        for index, raw_root in enumerate(self.roots, start=1):
            root = safe_resolve(raw_root)
            if progress:
                progress(f"正在扫描 {root}")
            files, directories, file_count, truncated = self.iter_files(root)
            all_files.extend(files)
            root_rows.append(
                {
                    "root": str(root),
                    "exists": root.is_dir(),
                    "directories": directories,
                    "files": file_count,
                    "truncated": truncated,
                }
            )
            if progress:
                progress(f"已完成 {index}/{len(self.roots)} 个目录")

        alerts = self.scan_text_references(all_files)
        snapshot = {
            "scanned_at": started,
            "roots": root_rows,
            "projects": self.project_rows(),
            "alerts": alerts,
            "limits": {
                "max_files_per_root": 20000,
                "max_depth": 12,
                "max_text_files": 6000,
            },
        }
        SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return snapshot

    @staticmethod
    def load_snapshot() -> dict[str, Any] | None:
        if not SNAPSHOT_PATH.exists():
            return None
        try:
            return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


class ProjectCanvasWindow(tk.Toplevel):
    """A read-only, zoomable project file canvas.

    The canvas loads only the children of folders that the user opens. This
    keeps large research projects usable without changing their real layout.
    """

    MAX_CHILDREN = 180
    DATA_SUFFIXES = {".csv", ".tsv", ".dta", ".sav", ".parquet", ".feather", ".pkl", ".xlsx", ".xls", ".xlsm"}
    LATEX_BUILD_SUFFIXES = (
        ".aux",
        ".bbl",
        ".bcf",
        ".blg",
        ".dvi",
        ".fdb_latexmk",
        ".fls",
        ".lof",
        ".log",
        ".lot",
        ".nav",
        ".run.xml",
        ".snm",
        ".synctex.gz",
        ".toc",
        ".vrb",
        ".xdv",
    )

    def __init__(self, app: "ResearchPipelineConsole", project: dict[str, Any], on_close: Any) -> None:
        super().__init__(app)
        self.app = app
        self.project = project
        self.on_close_callback = on_close
        self.project_root = safe_resolve(project["path"])
        self.root_key = path_key(self.project_root)
        self.title(f"项目画布 · {project['name']}")
        self.geometry("1540x920")
        self.minsize(1100, 700)
        self.configure(bg=PALETTE["surface"])
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.nodes: dict[str, dict[str, Any]] = {}
        self.loaded_all: set[str] = set()
        self.focus_stack: list[str] = [self.root_key]
        self.focus_key = self.root_key
        self.selected_key: str | None = self.root_key
        self.item_to_key: dict[int, str] = {}
        self.view_cx = 0.0
        self.view_cy = 0.0
        self.view_scale = 0.62
        self.animating = False
        self.drag_start: tuple[int, int, float, float] | None = None
        self.scene_keys: list[str] = []
        self.scene_positions: dict[str, tuple[float, float]] = {}
        self.display_positions: dict[str, tuple[float, float]] = {}
        self.scene_transition: dict[str, Any] | None = None
        self.breadcrumb_var = tk.StringVar()
        self.detail_title_var = tk.StringVar(value="项目根目录")
        self.detail_status_var = tk.StringVar(value="")
        self.index_status_var = tk.StringVar(value="")
        self.file_note_status_var = tk.StringVar(value="选择文件后显示")
        self.file_note_title_var = tk.StringVar(value="单文件研究说明")
        self.file_note_tags_var = tk.StringVar(value="")
        self.data_profile_status_var = tk.StringVar(value="请选择数据文件")
        self.data_profile_rows_var = tk.StringVar(value="—")
        self.data_profile_columns_var = tk.StringVar(value="—")
        self.data_profile_missing_var = tk.StringVar(value="—")
        self.data_profile_duplicates_var = tk.StringVar(value="—")
        self.data_profile_detail_var = tk.StringVar(value="选择 CSV、Excel 或 Stata 数据后生成只读画像。")
        self.file_research_notes = load_file_research_notes()
        self.generated_data_notes: dict[str, dict[str, Any]] = {}
        self.data_profile_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self.data_profile_queue: queue.Queue[tuple[int, tuple[str, int, int], Path, dict[str, Any]]] = queue.Queue()
        self.current_data_profile: dict[str, Any] | None = None
        self.current_data_profile_path: Path | None = None
        self.data_profile_request_id = 0
        self._analysis_python_path: str | None = None
        self.detail_tab = "note"
        self.detail_text: tk.Text
        self.file_note_text: tk.Text
        self.load_all_button: tk.Button
        self.child_index: ttk.Treeview
        self.index_item_to_key: dict[str, str] = {}
        self.index_item_to_path: dict[str, Path] = {}
        self.index_item_to_parent: dict[str, str] = {}
        self.show_build_artifacts = False
        self._displayed_note_key: str | None = None
        self.file_note_editing = False
        self._editing_note_key: str | None = None
        self._file_note_edit_original = ""
        self.artifact_toggle_button: tk.Button
        self.file_note_edit_button: tk.Button
        self.file_note_cancel_button: tk.Button
        self.data_profile_tree: ttk.Treeview
        self.data_profile_sample_button: tk.Button
        self.data_profile_refresh_button: tk.Button
        self.note_card: tk.Frame
        self.data_profile_card: tk.Frame
        self.detail_shell: tk.Frame
        self.detail_title_label: tk.Label
        self.file_note_title_label: tk.Label
        self.file_note_tags_label: tk.Label
        self.data_profile_detail_label: tk.Label

        self._build_ui()
        self.after(120, self._poll_data_profile_queue)
        self._create_root_node()
        self._ensure_children(self.root_key)
        self._set_initial_scene()
        self._update_breadcrumb()
        self.render()
        self._update_detail(refresh_index=True)
        self.after(35, self._fade_in, 0)
        self.after(180, self.fit_view)

    def _button(self, parent: tk.Widget, text: str, command: Any, primary: bool = False) -> tk.Button:
        bg = PALETTE["cyan"] if primary else PALETTE["surface_alt"]
        fg = "#FFFFFF" if primary else PALETTE["ink"]
        active = "#4285F4" if primary else "#E8EAED"
        return tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            font=(UI_FONT, 9, "bold" if primary else "normal"),
            padx=11,
            pady=6,
            cursor="hand2",
        )

    def _build_ui(self) -> None:
        toolbar = tk.Frame(self, bg=PALETTE["card"], height=64, highlightbackground=PALETTE["line"], highlightthickness=1)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(
            toolbar,
            text="PROJECT CANVAS",
            bg=PALETTE["card"],
            fg=PALETTE["cyan"],
            font=(UI_FONT, 9, "bold"),
        ).pack(side="left", padx=(20, 12))
        tk.Label(
            toolbar,
            text=self.project["name"],
            bg=PALETTE["card"],
            fg=PALETTE["ink"],
            font=(UI_FONT, 12, "bold"),
        ).pack(side="left")
        self._button(toolbar, "↩  返回上层", self.go_back).pack(side="right", padx=(6, 16), pady=12)
        self._button(toolbar, "⌂  项目根", self.go_root).pack(side="right", padx=6, pady=12)
        self._button(toolbar, "⊙  适应画布", self.fit_view).pack(side="right", padx=6, pady=12)
        self._button(toolbar, "↗  打开文件夹", self.open_physical_folder, primary=True).pack(side="right", padx=6, pady=12)

        crumb_bar = tk.Frame(self, bg=PALETTE["surface_alt"], height=38)
        crumb_bar.pack(fill="x")
        crumb_bar.pack_propagate(False)
        tk.Label(
            crumb_bar,
            textvariable=self.breadcrumb_var,
            bg=PALETTE["surface_alt"],
            fg=PALETTE["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(side="left", fill="both", expand=True, padx=(20, 8))
        self._button(crumb_bar, "返回主页", self.return_home).pack(side="right", padx=(6, 16), pady=4)
        self.artifact_toggle_button = self._button(crumb_bar, "○  显示构建产物", self.toggle_build_artifacts)
        self.artifact_toggle_button.pack(side="right", padx=6, pady=4)

        body = tk.Frame(self, bg=PALETTE["surface"])
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=470)
        self.canvas = tk.Canvas(body, bg=PALETTE["surface"], highlightthickness=0, cursor="arrow")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.render())
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<ButtonPress-2>", self.start_pan)
        self.canvas.bind("<B2-Motion>", self.pan)
        self.canvas.bind("<ButtonRelease-2>", self.end_pan)
        self.canvas.bind("<Button-1>", self.select_from_canvas)
        self.canvas.bind("<Double-Button-1>", self.open_from_canvas)
        self.canvas.bind("<KeyPress-plus>", lambda _event: self.adjust_zoom(1.18))
        self.canvas.bind("<KeyPress-minus>", lambda _event: self.adjust_zoom(0.85))
        self.canvas.bind("<Home>", lambda _event: self.go_root())
        self.canvas.focus_set()

        detail_shell = tk.Frame(body, bg=PALETTE["card"], width=500, highlightbackground=PALETTE["line"], highlightthickness=1)
        self.detail_shell = detail_shell
        detail_shell.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=(0, 10))
        # Children inside this shell use pack(), so pack propagation—not grid
        # propagation—must be disabled for the responsive width to take effect.
        detail_shell.pack_propagate(False)
        detail_scroll = ttk.Scrollbar(detail_shell, orient="vertical", style="Modern.Vertical.TScrollbar")
        detail_scroll.pack(side="right", fill="y")
        self.detail_canvas = tk.Canvas(
            detail_shell,
            bg=PALETTE["card"],
            highlightthickness=0,
            yscrollcommand=detail_scroll.set,
        )
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        detail_scroll.configure(command=self.detail_canvas.yview)
        detail = tk.Frame(self.detail_canvas, bg=PALETTE["card"])
        self.detail_window = self.detail_canvas.create_window((0, 0), window=detail, anchor="nw")
        detail.bind(
            "<Configure>",
            lambda _event: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all")),
        )
        self.detail_canvas.bind(
            "<Configure>",
            self._on_detail_canvas_configure,
        )
        body.bind("<Configure>", self._resize_detail_shell, add="+")
        self.detail_canvas.bind(
            "<MouseWheel>",
            self._scroll_detail_panel,
        )
        detail_shell.bind("<Enter>", lambda _event: self.bind_all("<MouseWheel>", self._scroll_detail_panel))
        detail_shell.bind("<Leave>", lambda _event: self.unbind_all("<MouseWheel>"))
        tk.Label(detail, text="当前节点", bg=PALETTE["card"], fg=PALETTE["muted"], font=(UI_FONT, 9, "bold"), anchor="w").pack(fill="x", padx=18, pady=(20, 4))
        self.detail_title_label = tk.Label(detail, textvariable=self.detail_title_var, bg=PALETTE["card"], fg=PALETTE["ink"], font=(UI_FONT, 14, "bold"), anchor="w", wraplength=430, justify="left")
        self.detail_title_label.pack(fill="x", padx=18)
        tk.Label(detail, textvariable=self.detail_status_var, bg=PALETTE["card"], fg=PALETTE["cyan"], font=(UI_FONT, 9), anchor="w").pack(fill="x", padx=18, pady=(4, 12))
        self.detail_text = tk.Text(detail, height=5, wrap="word", relief="flat", bd=0, bg=PALETTE["card"], fg=PALETTE["ink"], font=(UI_FONT, 9), padx=18, pady=4)
        self.detail_text.pack(fill="x", padx=0)
        self.detail_text.configure(state="disabled")

        detail_tabs = tk.Frame(detail, bg=PALETTE["card"])
        detail_tabs.pack(fill="x", padx=18, pady=(4, 0))
        self.note_tab_button = tk.Button(
            detail_tabs,
            text="研究说明",
            command=lambda: self.show_detail_tab("note"),
            relief="flat",
            borderwidth=0,
            background="#E8F0FE",
            foreground="#1D4ED8",
            activebackground="#D2E3FC",
            font=(UI_FONT, 9, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.note_tab_button.pack(side="left")
        self.profile_tab_button = tk.Button(
            detail_tabs,
            text="数据画像",
            command=lambda: self.show_detail_tab("profile"),
            relief="flat",
            borderwidth=0,
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            activebackground="#F1F5F9",
            font=(UI_FONT, 9),
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.profile_tab_button.pack(side="left", padx=(3, 0))

        detail_content = tk.Frame(detail, bg=PALETTE["card"], height=430)
        detail_content.pack(fill="x", padx=18, pady=(0, 9))
        detail_content.pack_propagate(False)

        self.note_card = tk.Frame(
            detail_content,
            bg="#F8FAFC",
            highlightbackground="#D9E2F0",
            highlightthickness=1,
        )
        note_card = self.note_card
        note_card.pack(fill="both", expand=True)
        note_header = tk.Frame(note_card, bg="#F8FAFC")
        note_header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            note_header,
            text="单文件研究说明",
            bg="#F8FAFC",
            fg=PALETTE["ink"],
            font=(UI_FONT, 9, "bold"),
        ).pack(side="left")
        note_actions = tk.Frame(note_header, bg="#F8FAFC")
        note_actions.pack(side="right")
        tk.Label(
            note_actions,
            textvariable=self.file_note_status_var,
            bg="#E8F0FE",
            fg="#1D4ED8",
            font=(UI_FONT, 8, "bold"),
            padx=7,
            pady=3,
        ).pack(side="left", padx=(0, 6))
        self.file_note_cancel_button = tk.Button(
            note_actions,
            text="取消",
            command=self.cancel_file_note_edit,
            relief="flat",
            borderwidth=0,
            background="#EEF2F6",
            foreground=PALETTE["muted"],
            activebackground="#E2E8F0",
            cursor="hand2",
            font=(UI_FONT, 8),
            padx=7,
            pady=3,
        )
        self.file_note_edit_button = tk.Button(
            note_actions,
            text="编辑",
            command=self.toggle_file_note_edit,
            relief="flat",
            borderwidth=0,
            background="#E8F0FE",
            foreground="#1D4ED8",
            activebackground="#D2E3FC",
            cursor="hand2",
            font=(UI_FONT, 8, "bold"),
            padx=8,
            pady=3,
            state="disabled",
            disabledforeground="#98A2B3",
        )
        self.file_note_edit_button.pack(side="left")
        self.file_note_title_label = tk.Label(
            note_card,
            textvariable=self.file_note_title_var,
            bg="#F8FAFC",
            fg=PALETTE["ink"],
            font=(UI_FONT, 10, "bold"),
            anchor="w",
            justify="left",
            wraplength=418,
        )
        self.file_note_title_label.pack(fill="x", padx=12, pady=(2, 5))
        note_body = tk.Frame(note_card, bg="#F8FAFC")
        note_body.pack(fill="x", padx=(12, 7))
        self.file_note_text = tk.Text(
            note_body,
            height=9,
            wrap="word",
            relief="flat",
            borderwidth=0,
            background="#F8FAFC",
            foreground=PALETTE["ink"],
            font=(UI_FONT, 8),
            padx=0,
            pady=0,
        )
        self.file_note_text.pack(side="left", fill="x", expand=True)
        note_scroll = ttk.Scrollbar(
            note_body,
            orient="vertical",
            command=self.file_note_text.yview,
            style="Modern.Vertical.TScrollbar",
        )
        note_scroll.pack(side="right", fill="y")
        self.file_note_text.configure(yscrollcommand=note_scroll.set, state="disabled")
        self.file_note_text.tag_configure("section", foreground="#1D4ED8", font=(UI_FONT, 8, "bold"), spacing1=3)
        self.file_note_text.tag_configure("body", foreground=PALETTE["ink"], font=(UI_FONT, 8), spacing3=4)
        self.file_note_tags_label = tk.Label(
            note_card,
            textvariable=self.file_note_tags_var,
            bg="#F8FAFC",
            fg=PALETTE["muted"],
            font=(UI_FONT, 8),
            anchor="w",
            justify="left",
            wraplength=418,
        )
        self.file_note_tags_label.pack(fill="x", padx=12, pady=(5, 10))

        self.data_profile_card = tk.Frame(
            detail_content,
            bg="#F8FAFC",
            highlightbackground="#D9E2F0",
            highlightthickness=1,
        )
        profile_header = tk.Frame(self.data_profile_card, bg="#F8FAFC")
        profile_header.pack(fill="x", padx=12, pady=(10, 7))
        tk.Label(
            profile_header,
            text="数据结构与质量",
            bg="#F8FAFC",
            fg=PALETTE["ink"],
            font=(UI_FONT, 9, "bold"),
        ).pack(side="left")
        tk.Label(
            profile_header,
            textvariable=self.data_profile_status_var,
            bg="#E8F0FE",
            fg="#1D4ED8",
            font=(UI_FONT, 8, "bold"),
            padx=7,
            pady=3,
        ).pack(side="right")

        metrics = tk.Frame(self.data_profile_card, bg="#F8FAFC")
        metrics.pack(fill="x", padx=9, pady=(0, 6))
        for column in range(4):
            metrics.grid_columnconfigure(column, weight=1, uniform="profile_metric")
        metric_specs = (
            ("记录数", self.data_profile_rows_var),
            ("字段数", self.data_profile_columns_var),
            ("缺失率", self.data_profile_missing_var),
            ("重复键", self.data_profile_duplicates_var),
        )
        for column, (label, variable) in enumerate(metric_specs):
            card = tk.Frame(metrics, bg="#FFFFFF", highlightbackground="#E2E8F0", highlightthickness=1)
            card.grid(row=0, column=column, sticky="nsew", padx=3)
            tk.Label(card, text=label, bg="#FFFFFF", fg=PALETTE["muted"], font=(UI_FONT, 7)).pack(anchor="w", padx=7, pady=(5, 0))
            tk.Label(card, textvariable=variable, bg="#FFFFFF", fg=PALETTE["ink"], font=(UI_FONT, 11, "bold")).pack(anchor="w", padx=7, pady=(0, 5))

        self.data_profile_detail_label = tk.Label(
            self.data_profile_card,
            textvariable=self.data_profile_detail_var,
            bg="#F8FAFC",
            fg=PALETTE["muted"],
            font=(UI_FONT, 8),
            anchor="w",
            justify="left",
            wraplength=418,
        )
        self.data_profile_detail_label.pack(fill="x", padx=12, pady=(0, 6))

        profile_tree_frame = tk.Frame(self.data_profile_card, bg="#F8FAFC")
        profile_tree_frame.pack(fill="both", expand=True, padx=(12, 7), pady=(0, 6))
        self.data_profile_tree = ttk.Treeview(
            profile_tree_frame,
            columns=("field", "dtype", "missing", "unique"),
            show="headings",
            style="Index.Treeview",
            height=5,
            selectmode="browse",
        )
        for key, heading, width, anchor in (
            ("field", "字段", 150, "w"),
            ("dtype", "类型", 100, "w"),
            ("missing", "缺失", 64, "e"),
            ("unique", "唯一值", 68, "e"),
        ):
            self.data_profile_tree.heading(key, text=heading)
            self.data_profile_tree.column(key, width=width, minwidth=36, anchor=anchor, stretch=False)
        self.data_profile_tree.pack(side="left", fill="both", expand=True)
        profile_scroll = ttk.Scrollbar(
            profile_tree_frame,
            orient="vertical",
            command=self.data_profile_tree.yview,
            style="Modern.Vertical.TScrollbar",
        )
        profile_scroll.pack(side="right", fill="y")
        self.data_profile_tree.configure(yscrollcommand=profile_scroll.set)
        profile_tree_frame.bind(
            "<Configure>",
            lambda event: self._resize_profile_columns(event.width, profile_scroll),
            add="+",
        )

        profile_actions = tk.Frame(self.data_profile_card, bg="#F8FAFC")
        profile_actions.pack(fill="x", padx=12, pady=(0, 9))
        self.data_profile_sample_button = self._button(profile_actions, "查看样本", self.show_data_sample)
        self.data_profile_sample_button.configure(font=(UI_FONT, 8), padx=9, pady=4, state="disabled")
        self.data_profile_sample_button.pack(side="left")
        self.data_profile_refresh_button = self._button(profile_actions, "重新分析", self.refresh_data_profile)
        self.data_profile_refresh_button.configure(font=(UI_FONT, 8), padx=9, pady=4, state="disabled")
        self.data_profile_refresh_button.pack(side="left", padx=(6, 0))

        tk.Label(detail, text="当前文件夹索引", bg=PALETTE["card"], fg=PALETTE["ink"], font=(UI_FONT, 10, "bold"), anchor="w").pack(fill="x", padx=18, pady=(2, 1))
        tk.Label(detail, textvariable=self.index_status_var, bg=PALETTE["card"], fg=PALETTE["muted"], font=(UI_FONT, 8), anchor="w").pack(fill="x", padx=18, pady=(0, 5))
        index_frame = tk.Frame(detail, bg=PALETTE["card"])
        index_frame.pack(fill="x", padx=18, pady=(0, 7))
        self.child_index = ttk.Treeview(index_frame, columns=("name", "kind"), show="headings", style="Index.Treeview", selectmode="browse", height=12)
        self.child_index.heading("name", text="名称")
        self.child_index.heading("kind", text="类型")
        self.child_index.column("name", width=210, anchor="w")
        self.child_index.column("kind", width=105, anchor="w")
        self.child_index.pack(side="left", fill="both", expand=True)
        index_scroll = ttk.Scrollbar(index_frame, orient="vertical", command=self.child_index.yview, style="Modern.Vertical.TScrollbar")
        index_scroll.pack(side="right", fill="y")
        self.child_index.configure(yscrollcommand=index_scroll.set)
        self.child_index.bind("<<TreeviewSelect>>", self.select_from_index)
        self.child_index.bind("<Double-1>", self.open_from_index)
        self.load_all_button = self._button(detail, "加载当前视图全部内容", self.load_all_current)
        self.load_all_button.pack(fill="x", padx=18, pady=(5, 8))
        tk.Label(detail, text="滚轮缩放 · 中键拖动 · 双击进入/打开", bg=PALETTE["card"], fg=PALETTE["muted"], font=(UI_FONT, 8), anchor="w").pack(fill="x", padx=18, pady=(0, 18))

    def _resize_detail_shell(self, event: tk.Event) -> None:
        """Keep the inspector readable while giving the canvas most of the window."""
        target_width = max(470, min(620, int(event.width * 0.32)))
        if abs(self.detail_shell.winfo_width() - target_width) > 2:
            self.detail_shell.configure(width=target_width)

    def _on_detail_canvas_configure(self, event: tk.Event) -> None:
        """Fit the scrollable detail body and its wrapped copy to the visible width."""
        self.detail_canvas.itemconfigure(self.detail_window, width=event.width)
        wraplength = max(260, event.width - 44)
        for label_name in (
            "detail_title_label",
            "file_note_title_label",
            "file_note_tags_label",
            "data_profile_detail_label",
        ):
            label = getattr(self, label_name, None)
            if label is not None:
                label.configure(wraplength=wraplength)

    def _resize_profile_columns(self, frame_width: int, scrollbar: ttk.Scrollbar) -> None:
        """Distribute all four profile columns inside the visible table width."""
        scrollbar_width = scrollbar.winfo_width()
        if scrollbar_width <= 1:
            scrollbar_width = scrollbar.winfo_reqwidth()
        available = max(300, frame_width - scrollbar_width - 3)
        field_width = int(available * 0.39)
        dtype_width = int(available * 0.27)
        missing_width = int(available * 0.16)
        unique_width = available - field_width - dtype_width - missing_width
        for key, width in (
            ("field", field_width),
            ("dtype", dtype_width),
            ("missing", missing_width),
            ("unique", unique_width),
        ):
            self.data_profile_tree.column(key, width=width, stretch=False)

    def show_detail_tab(self, tab: str) -> None:
        if tab == self.detail_tab:
            return
        if tab == "profile" and self._file_note_edit_guard():
            return
        self.note_card.pack_forget()
        self.data_profile_card.pack_forget()
        if tab == "profile":
            self.data_profile_card.pack(fill="both", expand=True)
            self.note_tab_button.configure(background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
            self.profile_tab_button.configure(background="#E8F0FE", foreground="#1D4ED8", font=(UI_FONT, 9, "bold"))
            self.detail_tab = "profile"
            node = self.nodes.get(self.selected_key or self.root_key)
            if node:
                self._update_data_profile(node)
        else:
            self.note_card.pack(fill="both", expand=True)
            self.note_tab_button.configure(background="#E8F0FE", foreground="#1D4ED8", font=(UI_FONT, 9, "bold"))
            self.profile_tab_button.configure(background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
            self.detail_tab = "note"

    def _scroll_detail_panel(self, event: tk.Event) -> str:
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self.detail_canvas.yview_scroll(delta, "units")
        return "break"

    def _clear_data_profile_tree(self) -> None:
        for item in self.data_profile_tree.get_children():
            self.data_profile_tree.delete(item)

    def _reset_data_profile_view(self, status: str, detail: str) -> None:
        self.data_profile_status_var.set(status)
        self.data_profile_rows_var.set("—")
        self.data_profile_columns_var.set("—")
        self.data_profile_missing_var.set("—")
        self.data_profile_duplicates_var.set("—")
        self.data_profile_detail_var.set(detail)
        self._clear_data_profile_tree()
        self.current_data_profile = None
        self.data_profile_sample_button.configure(state="disabled")

    @staticmethod
    def _data_profile_cache_key(path: Path) -> tuple[str, int, int]:
        try:
            stat = path.stat()
            return path_key(path), int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return path_key(path), 0, 0

    def _find_data_profile_python(self) -> str | None:
        if self._analysis_python_path and Path(self._analysis_python_path).exists():
            return self._analysis_python_path
        candidates: list[Path] = []
        if not getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.extend(sorted((Path(local_app_data) / "Programs" / "Python").glob("Python*/python.exe"), reverse=True))
        for executable_name in ("python", "python3"):
            located = shutil.which(executable_name)
            if located:
                candidates.append(Path(located))
        seen: set[str] = set()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            try:
                probe = subprocess.run(
                    [str(candidate), "-c", "import pandas, openpyxl"],
                    capture_output=True,
                    timeout=8,
                    creationflags=creation_flags,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0:
                self._analysis_python_path = str(candidate)
                return self._analysis_python_path
        return None

    def _basic_data_profile(self, path: Path, message: str) -> dict[str, Any]:
        fields: list[str] = []
        source_detail = message
        if path.suffix.casefold() in {".csv", ".tsv"}:
            fields, source_detail = self._delimited_data_profile(path)
        elif path.suffix.casefold() in {".xlsx", ".xls", ".xlsm"}:
            fields, source_detail = self._xlsx_data_profile(path)
        return {
            "status": "limited",
            "message": message,
            "source_detail": source_detail,
            "rows_total": None,
            "rows_scanned": 0,
            "sampled": True,
            "columns_total": len(fields) if fields else None,
            "overall_missing_rate": None,
            "candidate_key": [],
            "duplicate_count": None,
            "duplicate_scope": "未执行深度检查",
            "year_range": "未执行深度检查",
            "date_range": "未执行深度检查",
            "sheets": [],
            "fields": [
                {"name": field, "dtype": "未读取", "missing_rate": None, "unique_count": None, "label": ""}
                for field in fields
            ],
            "sample_columns": [],
            "sample_rows": [],
        }

    def _run_data_profile(self, path: Path) -> dict[str, Any]:
        if not DATA_PROFILE_HELPER_PATH.exists():
            return self._basic_data_profile(path, "数据画像组件未找到，已回退到轻量表头预览。")
        python_path = self._find_data_profile_python()
        if not python_path:
            return self._basic_data_profile(path, "未找到带 pandas/openpyxl 的 Python，已回退到轻量表头预览。")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        profile_env = dict(os.environ)
        profile_env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                [python_path, "-I", str(DATA_PROFILE_HELPER_PATH), str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=240,
                creationflags=creation_flags,
                env=profile_env,
            )
        except subprocess.TimeoutExpired:
            return self._basic_data_profile(path, "深度画像超过 4 分钟，已回退到轻量预览。")
        except OSError as exc:
            return self._basic_data_profile(path, f"深度画像无法启动：{exc}")
        output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not output_lines:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "没有返回分析结果"
            return self._basic_data_profile(path, f"深度画像失败：{detail}")
        try:
            result = json.loads(output_lines[-1])
        except json.JSONDecodeError:
            return self._basic_data_profile(path, "深度画像返回了无法识别的结果，已回退到轻量预览。")
        if not isinstance(result, dict):
            return self._basic_data_profile(path, "深度画像结果格式不正确，已回退到轻量预览。")
        return result

    def _update_data_profile(self, node: dict[str, Any], force: bool = False) -> None:
        path: Path = node["path"]
        if node["is_dir"] or path.suffix.casefold() not in self.DATA_SUFFIXES:
            self.current_data_profile_path = None
            self.data_profile_request_id += 1
            self._reset_data_profile_view("仅数据文件", "请选择 CSV、TSV、Excel、Stata、SPSS 或列式数据文件。")
            self.data_profile_refresh_button.configure(state="disabled")
            return
        self.current_data_profile_path = path
        self.data_profile_refresh_button.configure(state="normal")
        cache_key = self._data_profile_cache_key(path)
        if force:
            self.data_profile_cache.pop(cache_key, None)
        cached = self.data_profile_cache.get(cache_key)
        if cached is not None:
            self._apply_data_profile(path, cached)
            return
        self.data_profile_request_id += 1
        request_id = self.data_profile_request_id
        self._reset_data_profile_view("分析中", "正在只读检查字段、缺失率、候选主键与时间覆盖……")
        self.data_profile_refresh_button.configure(state="disabled")

        def worker() -> None:
            try:
                result = self._run_data_profile(path)
            except Exception as exc:
                result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
            self.data_profile_queue.put((request_id, cache_key, path, result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_data_profile_queue(self) -> None:
        try:
            while True:
                request_id, cache_key, path, result = self.data_profile_queue.get_nowait()
                self.data_profile_cache[cache_key] = result
                if request_id == self.data_profile_request_id and self.current_data_profile_path and path_key(path) == path_key(self.current_data_profile_path):
                    self._apply_data_profile(path, result)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(120, self._poll_data_profile_queue)
        except tk.TclError:
            pass

    def _apply_data_profile(self, path: Path, profile: dict[str, Any]) -> None:
        self.current_data_profile = profile
        self.current_data_profile_path = path
        status = str(profile.get("status", "error"))
        if status == "error":
            self._reset_data_profile_view("分析失败", str(profile.get("message", "无法读取该数据文件。")))
            self.data_profile_refresh_button.configure(state="normal")
            return

        total_rows = profile.get("rows_total")
        scanned_rows = int(profile.get("rows_scanned") or 0)
        sampled = bool(profile.get("sampled"))
        if isinstance(total_rows, int):
            rows_text = f"{total_rows:,}"
        elif scanned_rows:
            rows_text = f"≥{scanned_rows:,}"
        else:
            rows_text = "—"
        columns_total = profile.get("columns_total")
        missing_rate = profile.get("overall_missing_rate")
        duplicate_count = profile.get("duplicate_count")
        self.data_profile_rows_var.set(rows_text)
        self.data_profile_columns_var.set(f"{int(columns_total):,}" if isinstance(columns_total, (int, float)) else "—")
        self.data_profile_missing_var.set(f"{float(missing_rate):.1%}{'*' if sampled else ''}" if isinstance(missing_rate, (int, float)) else "—")
        self.data_profile_duplicates_var.set(f"{int(duplicate_count):,}{'*' if sampled else ''}" if isinstance(duplicate_count, (int, float)) else "—")
        self.data_profile_status_var.set("已分析 · 抽样" if sampled and status == "ok" else ("已分析" if status == "ok" else "轻量画像"))

        candidate = profile.get("candidate_key") or []
        key_text = " + ".join(str(item) for item in candidate) if candidate else "未识别"
        source_detail = str(profile.get("source_detail") or "")
        profile_message = str(profile.get("message") or "")
        if status == "limited" and profile_message and profile_message not in source_detail:
            source_detail = f"{profile_message}\n{source_detail}".strip()
        elif not source_detail:
            source_detail = profile_message
        year_range = str(profile.get("year_range") or "未识别")
        date_range = str(profile.get("date_range") or "未识别")
        duplicate_scope = str(profile.get("duplicate_scope") or "")
        sample_note = f"\n* 缺失率、唯一值与重复检查基于前 {scanned_rows:,} 行" if sampled and scanned_rows else ""
        self.data_profile_detail_var.set(
            f"{source_detail}\n年份：{year_range} · 日期：{date_range}\n候选主键：{key_text} · 重复检查：{duplicate_scope}{sample_note}"
        )

        self._clear_data_profile_tree()
        for field in profile.get("fields") or []:
            rate = field.get("missing_rate")
            unique = field.get("unique_count")
            missing_text = f"{float(rate):.1%}" if isinstance(rate, (int, float)) else "—"
            unique_text = f"{int(unique):,}" if isinstance(unique, (int, float)) else "—"
            self.data_profile_tree.insert(
                "",
                "end",
                values=(
                    str(field.get("name", "")),
                    str(field.get("dtype", "")),
                    missing_text,
                    unique_text,
                ),
            )
        has_sample = bool(profile.get("sample_columns") and profile.get("sample_rows"))
        self.data_profile_sample_button.configure(state="normal" if has_sample else "disabled")
        self.data_profile_refresh_button.configure(state="normal")

    def refresh_data_profile(self) -> None:
        node = self.nodes.get(self.selected_key or "")
        if not node or node["is_dir"] or node["path"].suffix.casefold() not in self.DATA_SUFFIXES:
            return
        self._update_data_profile(node, force=True)

    def show_data_sample(self) -> None:
        profile = self.current_data_profile or {}
        columns = [str(column) for column in profile.get("sample_columns") or []]
        rows = profile.get("sample_rows") or []
        if not columns or not rows:
            return
        window = tk.Toplevel(self)
        window.title(f"数据样本 · {self.current_data_profile_path.name if self.current_data_profile_path else ''}")
        window.geometry("1080x560")
        window.minsize(760, 420)
        window.configure(bg=PALETTE["card"])
        tk.Label(
            window,
            text="只读样本预览",
            bg=PALETTE["card"],
            fg=PALETTE["ink"],
            font=(UI_FONT, 14, "bold"),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 2))
        tk.Label(
            window,
            text="最多显示 12 行和 12 个关键字段；空白表示缺失值。",
            bg=PALETTE["card"],
            fg=PALETTE["muted"],
            font=(UI_FONT, 9),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 12))
        frame = tk.Frame(window, bg=PALETTE["card"])
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        column_ids = [f"c{index}" for index in range(len(columns))]
        tree = ttk.Treeview(frame, columns=column_ids, show="headings", style="Index.Treeview")
        for column_id, heading in zip(column_ids, columns):
            tree.heading(column_id, text=heading)
            tree.column(column_id, width=max(110, min(220, len(heading) * 10)), anchor="w", stretch=False)
        for row in rows:
            tree.insert("", "end", values=[str(value) for value in row])
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview, style="Modern.Vertical.TScrollbar")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview, style="Modern.Horizontal.TScrollbar")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

    def _fade_in(self, step: int) -> None:
        try:
            self.attributes("-alpha", min(1.0, (step + 1) / 8))
        except tk.TclError:
            return
        if step < 7 and self.winfo_exists():
            self.after(25, self._fade_in, step + 1)

    def _create_root_node(self) -> None:
        self.nodes[self.root_key] = {
            "key": self.root_key,
            "path": self.project_root,
            "parent": None,
            "x": 0.0,
            "y": 0.0,
            "depth": 0,
            "is_dir": True,
            "children_total": None,
            "all_children_total": None,
            "hidden_build_count": 0,
            "shown_children": 0,
            "truncated": False,
        }

    def _all_entries(self, path: Path) -> list[Path]:
        try:
            entries = [
                item
                for item in path.iterdir()
                if item.name.casefold() not in IGNORED_DIR_NAMES
            ]
        except (OSError, PermissionError):
            return []
        return sorted(entries, key=lambda item: (not item.is_dir(), item.name.casefold()))

    def _is_build_artifact(self, path: Path) -> bool:
        if path.is_dir():
            return False
        name = path.name.casefold()
        if name.endswith(self.LATEX_BUILD_SUFFIXES):
            return True
        # ``.out`` is ambiguous in research projects. Treat it as temporary
        # only when a same-named TeX source exists beside it.
        if name.endswith(".out") and path.with_suffix(".tex").exists():
            return True
        return False

    def _is_office_auxiliary_file(self, path: Path) -> bool:
        """Identify lock/recovery files that are not independent research material."""
        if path.is_dir():
            return False
        name = path.name.casefold()
        return name.startswith("~$") or name.startswith("~wrl") or path.suffix.casefold() == ".tmp"

    def _entries(self, path: Path) -> list[Path]:
        entries = self._all_entries(path)
        if self.show_build_artifacts:
            return entries
        return [entry for entry in entries if not self._is_build_artifact(entry)]

    def _file_meta(self, path: Path, is_dir: bool | None = None) -> dict[str, str]:
        if is_dir is None:
            is_dir = path.is_dir()
        if is_dir:
            return {"badge": "DIR", "label": "文件夹", "group": "文件夹", "fill": "#E8F0FE", "accent": "#1A73E8"}
        if self._is_build_artifact(path):
            return {"badge": "TMP", "label": "LaTeX 构建产物", "group": "构建产物", "fill": "#F2F4F7", "accent": "#98A2B3"}
        if self._is_office_auxiliary_file(path):
            return {"badge": "AUX", "label": "Office 临时 / 锁定文件", "group": "辅助文件", "fill": "#F2F4F7", "accent": "#98A2B3"}

        suffix = path.suffix.casefold()
        if suffix in {".py", ".pyw"}:
            return {"badge": "PY", "label": "Python 脚本", "group": "代码", "fill": "#F3E8FF", "accent": "#7C3AED"}
        if suffix == ".ipynb":
            return {"badge": "NB", "label": "Jupyter Notebook", "group": "代码", "fill": "#FFF1E8", "accent": "#EA580C"}
        if suffix in {".do", ".ado"}:
            return {"badge": "DO", "label": "Stata 脚本", "group": "代码", "fill": "#EDE9FE", "accent": "#6D5BD0"}
        if suffix in {".r", ".rmd", ".qmd"}:
            return {"badge": "R", "label": "R / Quarto 脚本", "group": "代码", "fill": "#E8F0FE", "accent": "#3367D6"}
        if suffix in {".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sql", ".ps1", ".bat", ".cmd"}:
            return {"badge": "CODE", "label": "代码文件", "group": "代码", "fill": "#EEF2FF", "accent": "#6366F1"}
        if suffix == ".tex":
            return {"badge": "TEX", "label": "LaTeX 源文件", "group": "论文", "fill": "#FFF4DA", "accent": "#D97706"}
        if suffix == ".pdf":
            return {"badge": "PDF", "label": "PDF 文档", "group": "文档", "fill": "#FEECEC", "accent": "#D93025"}
        if suffix in {".doc", ".docx"}:
            return {"badge": "DOC", "label": "Word 文档", "group": "文档", "fill": "#E8F0FE", "accent": "#185ABD"}
        if suffix in {".ppt", ".pptx"}:
            return {"badge": "PPT", "label": "演示文稿", "group": "文档", "fill": "#FFF0E6", "accent": "#C43E1C"}
        if suffix in {".xlsx", ".xls", ".xlsm"}:
            return {"badge": "XLS", "label": "Excel 数据表", "group": "数据", "fill": "#E6F4EA", "accent": "#188038"}
        if suffix in {".csv", ".tsv", ".dta", ".sav", ".parquet", ".feather", ".pkl"}:
            labels = {".csv": "CSV 数据", ".tsv": "TSV 数据", ".dta": "Stata 数据", ".sav": "SPSS 数据", ".parquet": "Parquet 数据", ".feather": "Feather 数据", ".pkl": "序列化数据"}
            return {"badge": "DATA", "label": labels.get(suffix, "数据文件"), "group": "数据", "fill": "#E6F4EA", "accent": "#188038"}
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".tif", ".tiff"}:
            return {"badge": "IMG", "label": "图片 / 图表", "group": "图像", "fill": "#FCE8F3", "accent": "#C2185B"}
        if suffix == ".bib":
            return {"badge": "BIB", "label": "参考文献库", "group": "论文", "fill": "#FFF4DA", "accent": "#B06000"}
        if suffix in {".sty", ".cls", ".bst"}:
            labels = {".sty": "LaTeX 样式", ".cls": "LaTeX 模板", ".bst": "参考文献样式"}
            return {"badge": "STYLE", "label": labels[suffix], "group": "论文", "fill": "#FFF8E7", "accent": "#B7791F"}
        if suffix in {".md", ".txt", ".rtf"}:
            return {"badge": "NOTE", "label": "笔记 / 文本", "group": "文档", "fill": "#F2F4F7", "accent": "#667085"}
        if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
            return {"badge": "CFG", "label": "配置 / 元数据", "group": "配置", "fill": "#ECFDF3", "accent": "#0F766E"}
        if suffix in {".zip", ".7z", ".rar", ".tar", ".gz"}:
            return {"badge": "ZIP", "label": "压缩包", "group": "归档", "fill": "#F4F3FF", "accent": "#6941C6"}
        badge = suffix[1:5].upper() if suffix else "FILE"
        return {"badge": badge or "FILE", "label": f"{suffix.upper()} 文件" if suffix else "无扩展名文件", "group": "其他", "fill": "#F2F4F7", "accent": "#7A8997"}

    def _ensure_children(self, parent_key: str, load_all: bool = False) -> None:
        parent = self.nodes.get(parent_key)
        if not parent or not parent["is_dir"]:
            return
        if parent_key in self.loaded_all and not load_all:
            return
        all_entries = self._all_entries(parent["path"])
        entries = all_entries if self.show_build_artifacts else [entry for entry in all_entries if not self._is_build_artifact(entry)]
        total = len(entries)
        parent["children_total"] = total
        parent["all_children_total"] = len(all_entries)
        parent["hidden_build_count"] = max(0, len(all_entries) - total)
        limit = total if load_all else min(total, self.MAX_CHILDREN)
        radius = max(280.0, min(680.0, 92.0 * math.sqrt(max(total, 1))))
        for index, entry in enumerate(entries[:limit]):
            key = path_key(entry)
            if key in self.nodes:
                continue
            angle = -math.pi / 2 + (2 * math.pi * index / max(total, 1))
            self.nodes[key] = {
                "key": key,
                "path": entry,
                "parent": parent_key,
                "x": parent["x"] + radius * math.cos(angle),
                "y": parent["y"] + radius * math.sin(angle),
                "depth": parent["depth"] + 1,
                "is_dir": entry.is_dir(),
                "children_total": None,
                "all_children_total": None,
                "hidden_build_count": 0,
                "shown_children": 0,
                "truncated": False,
            }
        parent["shown_children"] = sum(1 for node in self.nodes.values() if node.get("parent") == parent_key)
        parent["truncated"] = total > limit
        if load_all or total <= self.MAX_CHILDREN:
            self.loaded_all.add(parent_key)

    def _node_children(self, key: str) -> list[dict[str, Any]]:
        return [node for node in self.nodes.values() if node.get("parent") == key]

    def _node_color(self, node: dict[str, Any]) -> tuple[str, str]:
        meta = self._file_meta(node["path"], node["is_dir"])
        return meta["fill"], meta["accent"]

    def _rebuild_filtered_scene(self) -> None:
        focus_path = self.nodes.get(self.focus_key, {}).get("path", self.project_root)
        self.nodes.clear()
        self.loaded_all.clear()
        self._create_root_node()

        current_key = self.root_key
        focus_stack = [self.root_key]
        try:
            relative_parts = focus_path.relative_to(self.project_root).parts
        except (ValueError, AttributeError):
            relative_parts = ()
        current_path = self.project_root
        for part in relative_parts:
            self._ensure_children(current_key, load_all=True)
            current_path = current_path / part
            next_key = path_key(current_path)
            if next_key not in self.nodes:
                break
            current_key = next_key
            focus_stack.append(current_key)

        self.focus_stack = focus_stack
        self.focus_key = current_key
        self.selected_key = current_key
        self._ensure_children(current_key)
        self._transition_to_scene(current_key, immediate=True)

    def toggle_build_artifacts(self) -> None:
        if self._file_note_edit_guard():
            return
        self.show_build_artifacts = not self.show_build_artifacts
        self._rebuild_filtered_scene()

    def _set_initial_scene(self) -> None:
        self.scene_keys = [self.root_key] + [node["key"] for node in self._node_children(self.root_key)]
        self.scene_positions = self._layout_for_focus(self.root_key)
        self.display_positions = dict(self.scene_positions)
        self.view_cx, self.view_cy, self.view_scale = self._scene_view(self.scene_positions, self.scene_keys)

    def _layout_for_focus(self, focus_key: str) -> dict[str, tuple[float, float]]:
        children = self._node_children(focus_key)
        positions: dict[str, tuple[float, float]] = {}
        focus_x = -360.0 if children else 0.0
        positions[focus_key] = (focus_x, 0.0)
        count = len(children)
        if not count:
            return positions
        if count <= 10:
            columns = 1
        elif count <= 24:
            columns = 2
        elif count <= 60:
            columns = 4
        else:
            columns = 6
        rows = math.ceil(count / columns)
        col_gap = 215.0
        row_gap = 92.0
        x_start = 0.0
        for index, child in enumerate(children):
            column = index % columns
            row = index // columns
            x = x_start + column * col_gap
            y = (row - (rows - 1) / 2) * row_gap
            positions[child["key"]] = (x, y)
        return positions

    def _scene_view(self, positions: dict[str, tuple[float, float]], keys: list[str]) -> tuple[float, float, float]:
        if not keys:
            return 0.0, 0.0, 0.62
        xs = [positions[key][0] for key in keys]
        ys = [positions[key][1] for key in keys]
        min_x, max_x = min(xs) - 120, max(xs) + 120
        min_y, max_y = min(ys) - 70, max(ys) + 70
        width = max(max_x - min_x, 600.0)
        height = max(max_y - min_y, 430.0)
        canvas_width = max(self.canvas.winfo_width(), 1000)
        canvas_height = max(self.canvas.winfo_height(), 650)
        scale = min((canvas_width - 80) / width, (canvas_height - 80) / height)
        scale = max(0.36, min(1.18, scale))
        return (min_x + max_x) / 2, (min_y + max_y) / 2, scale

    @staticmethod
    def _blend_color(base: str, target: str, amount: float) -> str:
        amount = max(0.0, min(1.0, amount))
        base = base.lstrip("#")
        target = target.lstrip("#")
        values = [
            round(int(base[index : index + 2], 16) + (int(target[index : index + 2], 16) - int(base[index : index + 2], 16)) * amount)
            for index in (0, 2, 4)
        ]
        return "#" + "".join(f"{value:02X}" for value in values)

    def _transition_to_scene(self, focus_key: str, immediate: bool = False) -> None:
        self._ensure_children(focus_key)
        target_keys = [focus_key] + [node["key"] for node in self._node_children(focus_key)]
        target_positions = self._layout_for_focus(focus_key)
        target_cx, target_cy, target_scale = self._scene_view(target_positions, target_keys)
        if immediate:
            self.focus_key = focus_key
            self.scene_keys = target_keys
            self.scene_positions = target_positions
            self.display_positions = dict(target_positions)
            self.selected_key = focus_key
            self.view_cx, self.view_cy, self.view_scale = target_cx, target_cy, target_scale
            self._update_breadcrumb()
            self.render()
            self._update_detail(refresh_index=True)
            return

        old_keys = list(self.scene_keys)
        old_positions = dict(self.display_positions or self.scene_positions)
        anchor = old_positions.get(focus_key, old_positions.get(self.focus_key, (0.0, 0.0)))
        union = list(dict.fromkeys(old_keys + target_keys))
        starts = {key: old_positions.get(key, anchor) for key in union}
        ends = {key: target_positions.get(key, starts[key]) for key in union}
        self.focus_key = focus_key
        self.scene_positions = target_positions
        self.selected_key = focus_key
        self._update_breadcrumb()
        self._update_detail(refresh_index=True)
        self.scene_transition = {
            "old_keys": old_keys,
            "target_keys": target_keys,
            "starts": starts,
            "ends": ends,
            "start_cx": self.view_cx,
            "start_cy": self.view_cy,
            "start_scale": self.view_scale,
            "target_cx": target_cx,
            "target_cy": target_cy,
            "target_scale": target_scale,
            "started": time.perf_counter(),
            "duration": 620,
        }
        self.animating = True
        self._animate_scene()

    def _animate_scene(self) -> None:
        transition = self.scene_transition
        if not transition or not self.winfo_exists():
            return
        progress = min(1.0, (time.perf_counter() - transition["started"]) * 1000 / transition["duration"])
        eased = 1 - (1 - progress) ** 3
        self.view_cx = transition["start_cx"] + (transition["target_cx"] - transition["start_cx"]) * eased
        self.view_cy = transition["start_cy"] + (transition["target_cy"] - transition["start_cy"]) * eased
        self.view_scale = transition["start_scale"] + (transition["target_scale"] - transition["start_scale"]) * eased
        self.display_positions = {
            key: (
                transition["starts"][key][0] + (transition["ends"][key][0] - transition["starts"][key][0]) * eased,
                transition["starts"][key][1] + (transition["ends"][key][1] - transition["starts"][key][1]) * eased,
            )
            for key in transition["starts"]
        }
        self.render()
        if progress < 1.0:
            self.after(16, self._animate_scene)
        else:
            self.scene_keys = list(transition["target_keys"])
            self.display_positions = dict(self.scene_positions)
            self.scene_transition = None
            self.animating = False
            self.render()

    @staticmethod
    def _short_label(name: str, limit: int = 24) -> str:
        return name if len(name) <= limit else name[: limit - 1] + "…"

    def _world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        return (width / 2 + (x - self.view_cx) * self.view_scale, height / 2 + (y - self.view_cy) * self.view_scale)

    def _screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        return (self.view_cx + (x - width / 2) / self.view_scale, self.view_cy + (y - height / 2) / self.view_scale)

    def render(self) -> None:
        if not self.winfo_exists():
            return
        self.canvas.delete("all")
        self.item_to_key.clear()
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        transition = self.scene_transition
        if transition:
            draw_keys = list(dict.fromkeys(transition["old_keys"] + transition["target_keys"]))
            progress = min(1.0, (time.perf_counter() - transition["started"]) * 1000 / transition["duration"])
            target_keys = set(transition["target_keys"])
            old_keys = set(transition["old_keys"])
        else:
            draw_keys = list(self.scene_keys)
            progress = 1.0
            target_keys = set(draw_keys)
            old_keys = set(draw_keys)

        visible: dict[str, dict[str, Any]] = {}
        opacities: dict[str, float] = {}
        margin = 180
        for key in draw_keys:
            node = self.nodes.get(key)
            position = self.display_positions.get(key)
            if not node or not position:
                continue
            if transition:
                if key in target_keys and key in old_keys:
                    opacity = 1.0
                elif key in target_keys:
                    opacity = progress
                else:
                    opacity = 1.0 - progress
            else:
                opacity = 1.0
            sx, sy = self._world_to_screen(position[0], position[1])
            if opacity > 0.05 and -margin <= sx <= width + margin and -margin <= sy <= height + margin:
                visible[key] = node
                opacities[key] = opacity

        for key, node in visible.items():
            parent_key = node.get("parent")
            if parent_key not in visible:
                continue
            parent_position = self.display_positions.get(parent_key)
            node_position = self.display_positions.get(key)
            if not parent_position or not node_position:
                continue
            x1, y1 = self._world_to_screen(parent_position[0], parent_position[1])
            x2, y2 = self._world_to_screen(node_position[0], node_position[1])
            edge_opacity = min(opacities.get(parent_key, 1.0), opacities.get(key, 1.0))
            self.canvas.create_line(x1, y1, x2, y2, fill=self._blend_color(PALETTE["surface"], "#A9BAC6", edge_opacity), width=max(1, int(2 * self.view_scale)), arrow=tk.LAST)

        for key, node in visible.items():
            position = self.display_positions[key]
            sx, sy = self._world_to_screen(position[0], position[1])
            opacity = opacities[key]
            meta = self._file_meta(node["path"], node["is_dir"])
            node_width = (212 if node["is_dir"] else 190) * self.view_scale
            node_height = (82 if node["is_dir"] else 74) * self.view_scale
            accent = meta["accent"]
            if node_width < 12 or node_height < 8:
                radius = max(4, int(7 * self.view_scale))
                item = self.canvas.create_oval(sx - radius, sy - radius, sx + radius, sy + radius, fill=self._blend_color(PALETTE["surface"], accent, opacity), outline="")
                self.item_to_key[item] = key
                continue
            selected = key == self.selected_key
            fill = PALETTE["cyan"] if selected else PALETTE["card"]
            outline = PALETTE["cyan"] if selected else PALETTE["line"]
            item = self.canvas.create_rectangle(
                sx - node_width / 2,
                sy - node_height / 2,
                sx + node_width / 2,
                sy + node_height / 2,
                fill=self._blend_color(PALETTE["surface"], fill, opacity),
                outline=self._blend_color(PALETTE["surface"], outline, opacity),
                width=3 if key == self.selected_key else 2,
            )
            self.item_to_key[item] = key
            stripe_width = max(3, min(7, int(6 * self.view_scale)))
            stripe = self.canvas.create_rectangle(
                sx - node_width / 2,
                sy - node_height / 2,
                sx - node_width / 2 + stripe_width,
                sy + node_height / 2,
                fill=self._blend_color(PALETTE["surface"], PALETTE["cyan"] if selected else accent, opacity),
                outline="",
            )
            self.item_to_key[stripe] = key

            total = node.get("children_total")
            if node["is_dir"]:
                if total is not None:
                    subtitle = f"文件夹 · {total:,} 项"
                else:
                    subtitle = "文件夹 · 双击展开"
            else:
                subtitle = meta["label"]

            badge_width = max(25, min(42, 40 * self.view_scale))
            badge_height = max(18, min(25, 22 * self.view_scale))
            card_left = sx - node_width / 2
            badge_x = card_left + stripe_width + max(8, 10 * self.view_scale) + badge_width / 2
            badge_fill = "#FFFFFF" if selected else accent
            badge_text_color = PALETTE["cyan"] if selected else "#FFFFFF"
            badge_item = self.canvas.create_rectangle(
                badge_x - badge_width / 2,
                sy - badge_height / 2,
                badge_x + badge_width / 2,
                sy + badge_height / 2,
                fill=self._blend_color(PALETTE["surface"], badge_fill, opacity),
                outline="",
            )
            self.item_to_key[badge_item] = key

            text_x = badge_x + badge_width / 2 + max(7, 9 * self.view_scale)
            text_width = max(52, sx + node_width / 2 - text_x - 7)
            title_size = max(8, min(12, int(9.5 * max(self.view_scale, 0.9))))
            type_size = max(7, min(10, int(8 * max(self.view_scale, 0.9))))
            if opacity > 0.38:
                badge_label = self.canvas.create_text(
                    badge_x,
                    sy,
                    text=meta["badge"],
                    fill=self._blend_color(PALETTE["surface"], badge_text_color, opacity),
                    font=(UI_FONT, max(6, min(8, int(7 * max(self.view_scale, 0.95)))), "bold"),
                )
                self.item_to_key[badge_label] = key
                title_item = self.canvas.create_text(
                    text_x,
                    sy - max(7, 9 * self.view_scale),
                    anchor="w",
                    text=self._short_label(node["path"].name, 22 if node["is_dir"] else 19),
                    fill=self._blend_color(PALETTE["surface"], "#FFFFFF" if selected else PALETTE["ink"], opacity),
                    font=(UI_FONT, title_size, "bold"),
                    width=text_width,
                )
                self.item_to_key[title_item] = key
                type_item = self.canvas.create_text(
                    text_x,
                    sy + max(9, 12 * self.view_scale),
                    anchor="w",
                    text=subtitle,
                    fill=self._blend_color(PALETTE["surface"], "#DCE7FF" if selected else PALETTE["muted"], opacity),
                    font=(UI_FONT, type_size),
                    width=text_width,
                )
                self.item_to_key[type_item] = key

        focus_node = self.nodes.get(self.focus_key)
        focus_label = focus_node["path"].name if focus_node else self.project_root.name
        hidden_count = focus_node.get("hidden_build_count", 0) if focus_node else 0
        mode_text = "完整视图" if self.show_build_artifacts else "语义视图"
        hidden_text = f" · 已折叠 {hidden_count} 个构建产物" if hidden_count and not self.show_build_artifacts else ""
        self.canvas.create_text(
            22,
            18,
            anchor="nw",
            text=f"{focus_label} · {mode_text}{hidden_text} · 双击进入或打开",
            fill=PALETTE["muted"],
            font=(UI_FONT, 9),
        )

        group_counts: dict[str, tuple[int, str]] = {}
        for child in self._node_children(self.focus_key):
            child_meta = self._file_meta(child["path"], child["is_dir"])
            group = child_meta["group"]
            previous = group_counts.get(group, (0, child_meta["accent"]))
            group_counts[group] = (previous[0] + 1, child_meta["accent"])
        legend_x = 22
        for group in ("文件夹", "论文", "代码", "数据", "文档", "图像", "配置", "辅助文件", "构建产物", "其他"):
            if group not in group_counts:
                continue
            count, color = group_counts[group]
            self.canvas.create_oval(legend_x, 48, legend_x + 8, 56, fill=color, outline="")
            legend_label = f"{group} {count}"
            self.canvas.create_text(legend_x + 13, 52, anchor="w", text=legend_label, fill=PALETTE["muted"], font=(UI_FONT, 8))
            legend_x += 42 + len(legend_label) * 8
        self._update_detail()

    def _update_breadcrumb(self) -> None:
        focus = self.nodes.get(self.focus_key)
        if not focus:
            return
        pieces = [self.project_root.name]
        path = focus["path"]
        try:
            relative = path.relative_to(self.project_root)
            pieces.extend(relative.parts)
        except ValueError:
            pass
        self.breadcrumb_var.set("  /  ".join(pieces))

    def _research_note_for(self, path: Path) -> dict[str, Any] | None:
        try:
            relative_path = path.relative_to(self.project_root).as_posix().casefold()
        except ValueError:
            return None
        project_id = str(self.project.get("project_id", "")).casefold()
        return self.file_research_notes.get((project_id, relative_path))

    @staticmethod
    def _short_field(value: Any, limit: int = 34) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    def _delimited_data_profile(self, path: Path) -> tuple[list[str], str]:
        """Read a small, read-only structural profile from CSV/TSV data."""
        raw = b""
        try:
            with path.open("rb") as handle:
                raw = handle.read(131072)
        except OSError:
            return [], "无法读取表头"

        text = ""
        encoding_used = ""
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1252"):
            try:
                text = raw.decode(encoding)
                encoding_used = encoding
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw.decode("utf-8", errors="replace")
            encoding_used = "utf-8（容错）"

        default_delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
        delimiter = default_delimiter
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",\t;|").delimiter
        except csv.Error:
            pass
        try:
            first_row = next(csv.reader(text.splitlines(), delimiter=delimiter), [])
        except (csv.Error, StopIteration):
            first_row = []
        fields = [self._short_field(item) for item in first_row if str(item).strip()]

        row_text = "行数未展开"
        try:
            if path.stat().st_size <= 4 * 1024 * 1024:
                with path.open("r", encoding=encoding_used.split("（", 1)[0], errors="replace", newline="") as handle:
                    rows = sum(1 for _ in csv.reader(handle, delimiter=delimiter))
                row_text = f"约 {max(0, rows - 1):,} 条数据记录"
            else:
                row_text = "大文件，界面仅抽取表头"
        except (OSError, UnicodeError, csv.Error):
            pass
        delimiter_name = {",": "逗号", "\t": "制表符", ";": "分号", "|": "竖线"}.get(delimiter, repr(delimiter))
        return fields, f"{row_text} · {delimiter_name}分隔 · {encoding_used}"

    def _xlsx_data_profile(self, path: Path) -> tuple[list[str], str]:
        """Inspect workbook sheets and the first visible row without modifying it."""
        if path.suffix.casefold() == ".xls":
            return [], "旧式 XLS 二进制工作簿；为保持轻量只读，未展开字段"
        try:
            with zipfile.ZipFile(path) as archive:
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                sheets = [
                    self._short_field(node.attrib.get("name", ""), 28)
                    for node in workbook.iter()
                    if node.tag.endswith("sheet") and node.attrib.get("name")
                ]

                shared: list[str] = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    info = archive.getinfo("xl/sharedStrings.xml")
                    if info.file_size <= 4 * 1024 * 1024:
                        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                        for item in shared_root:
                            shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("t")))

                worksheet_names = sorted(
                    name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
                )
                first_row_values: list[str] = []
                if worksheet_names:
                    with archive.open(worksheet_names[0]) as worksheet:
                        for _, node in ET.iterparse(worksheet, events=("end",)):
                            if not node.tag.endswith("row"):
                                continue
                            for cell in node:
                                if not cell.tag.endswith("c"):
                                    continue
                                cell_type = cell.attrib.get("t", "")
                                value_node = next((child for child in cell if child.tag.endswith("v")), None)
                                inline_text = "".join(child.text or "" for child in cell.iter() if child.tag.endswith("t"))
                                value = inline_text
                                if value_node is not None and value_node.text is not None:
                                    value = value_node.text
                                    if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                                        value = shared[int(value)]
                                if value:
                                    first_row_values.append(self._short_field(value))
                            break
                sheet_text = "、".join(sheets[:6]) if sheets else "未识别"
                if len(sheets) > 6:
                    sheet_text += f" 等 {len(sheets)} 个"
                return first_row_values, f"工作表：{sheet_text}"
        except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
            return [], "工作簿结构未能读取"

    def _data_role(self, path: Path, fields: list[str]) -> tuple[str, str, str, str]:
        try:
            relative = path.relative_to(self.project_root).as_posix()
        except ValueError:
            relative = path.name
        path_marker = relative.casefold()
        marker = f"{path_marker} {' '.join(fields)}".casefold()

        role_rules = (
            (("variable_dictionary", "data_dictionary", "codebook"), "字段字典", "记录字段含义、类型与来源，便于复核后续处理步骤。"),
            (("missingness", "missing_field", "unmatched", "duplicate_key"), "数据质量诊断", "用于定位缺失、未匹配或重复记录；使用前应核对诊断口径。"),
            (("diagnostic", "audit", "verification", "coverage", "summary"), "检查与摘要", "用于查看数据覆盖、处理质量或版本一致性。"),
            (("analysis_ready", "model_input"), "分析输入数据", "这是后续分析的输入；使用前应核对主键、筛选条件和版本。"),
            (("merged", "combined", "integrated"), "合并数据", "包含多个来源合并后的记录；请核对连接键与来源。"),
            (("manifest", "registry", "source_list"), "来源清单", "用于记录数据来源、批次或文件映射。"),
            (("mapping", "variable_map", "lookup", "classification"), "映射与分类表", "用于统一标识符、字段名称或分类口径。"),
            (("feature_importance", "features", "indicators"), "特征与指标", "包含计算后的特征或指标；请核对生成方法。"),
            (("prediction", "predictions", "probability", "scores"), "模型预测结果", "包含预测标签、概率或得分；请核对模型版本。"),
            (("training", "train_test", "annotation", "labels"), "训练与标注数据", "用于训练、验证或人工复核；请保留标签版本。"),
            (("table", "results", "output"), "结果表与输出", "包含计算或分析结果；如需复现，请回溯输入和生成脚本。"),
        )
        role = "项目数据文件"
        use = "用于当前项目的数据整理或分析；使用前请核对来源、字段含义和生成流程。"
        for needles, candidate_role, candidate_use in role_rules:
            if any(needle in marker for needle in needles):
                role, use = candidate_role, candidate_use
                break

        stage = "项目数据"
        if any(token in path_marker for token in ("raw", "source", "input", "original")):
            stage = "源数据 / 输入层"
        if any(token in path_marker for token in ("merged", "integration", "cleaned", "processed")):
            stage = "清洗 / 合并层"
        if any(token in path_marker for token in ("output", "results", "tables", "diagnostic", "audit")):
            stage = "输出 / 诊断层"
        if any(token in path_marker for token in ("training", "label", "snippet")):
            stage = "训练 / 标注层"
        if any(token in path_marker for token in ("analysis_ready", "model_input")):
            stage = "分析输入层"

        normalized_fields = {field.casefold() for field in fields}
        joined_fields = " ".join(normalized_fields)
        has_year = any(token in joined_fields for token in ("year", "fyear", "fiscal_year"))
        has_entity = any(field == "id" or field.endswith("_id") or field in {"code", "entity", "subject", "record"} for field in normalized_fields)
        has_date = any(token in joined_fields for token in ("date", "trading_day", "time"))
        if has_entity and has_year:
            grain = "实体—年度"
        elif has_entity and has_date:
            grain = "实体—日期"
        elif has_date:
            grain = "日期 / 时间序列"
        elif has_entity:
            grain = "实体 / 记录"
        else:
            grain = "需结合字段进一步确认"
        return role, stage, f"数据粒度：{grain}", use

    def _generated_data_note(self, path: Path) -> dict[str, Any] | None:
        suffix = path.suffix.casefold()
        if suffix not in self.DATA_SUFFIXES:
            return None
        key = path_key(path)
        cached = self.generated_data_notes.get(key)
        if cached:
            return cached

        fields: list[str] = []
        profile = "未展开列结构"
        if suffix in {".csv", ".tsv"}:
            fields, profile = self._delimited_data_profile(path)
        elif suffix in {".xlsx", ".xls", ".xlsm"}:
            fields, profile = self._xlsx_data_profile(path)
        else:
            labels = {
                ".dta": "Stata 数据文件",
                ".sav": "SPSS 数据文件",
                ".parquet": "Parquet 列式数据",
                ".feather": "Feather 列式数据",
                ".pkl": "Python 序列化数据",
            }
            profile = labels.get(suffix, "二进制数据文件") + "；界面保持轻量，只读取路径与文件元数据"

        role, stage, grain, research_use = self._data_role(path, fields)
        try:
            relative = path.relative_to(self.project_root).as_posix()
        except ValueError:
            relative = path.name
        try:
            size_bytes = path.stat().st_size
            if size_bytes >= 1024 * 1024:
                size_text = f"{size_bytes / (1024 * 1024):.1f} MB"
            elif size_bytes >= 1024:
                size_text = f"{size_bytes / 1024:.1f} KB"
            else:
                size_text = f"{size_bytes} bytes"
        except OSError:
            size_text = "大小未知"
        field_text = "、".join(fields[:12]) if fields else "未在轻量预览中取得字段名"
        if len(fields) > 12:
            field_text += f" 等 {len(fields)} 列"
        title_stem = self._short_field(path.stem, 72)
        note = {
            "status": "自动数据档案 · 可编辑",
            "title": f"{role} · {title_stem}",
            "research_question": f"文件性质：{role}；所在阶段：{stage}。相对位置为 {relative}。",
            "method": f"{path.suffix.upper().lstrip('.')} 格式 · {size_text} · {profile}。字段预览：{field_text}。",
            "finding": f"{grain}。该说明基于真实文件结构、表头和目录语义自动生成，只描述数据资产，不把文件名或数值误写成实证结论。",
            "research_use": research_use + " 如口径与实际用途不一致，可点击“编辑”保存人工说明，人工版本会优先显示。",
            "tags": list(dict.fromkeys([role, stage, grain.removeprefix("数据粒度："), path.suffix.upper().lstrip("."), "自动识别"])),
        }
        self.generated_data_notes[key] = note
        return note

    def _file_note_edit_guard(self) -> bool:
        if not self.file_note_editing:
            return False
        editing_item = next(
            (item_id for item_id, key in self.index_item_to_key.items() if key == self._editing_note_key),
            None,
        )
        if editing_item and self.child_index.exists(editing_item):
            self.child_index.selection_set(editing_item)
            self.child_index.see(editing_item)
        self.file_note_status_var.set("请先保存或取消")
        self.bell()
        return True

    def toggle_file_note_edit(self) -> None:
        if self.file_note_editing:
            self.save_file_note_edit()
            return
        node = self.nodes.get(self.selected_key or "")
        if not node or node["is_dir"] or self._is_build_artifact(node["path"]) or self._is_office_auxiliary_file(node["path"]):
            return
        self.file_note_editing = True
        self._editing_note_key = node["key"]
        self._file_note_edit_original = self.file_note_text.get("1.0", "end-1c")
        self.file_note_text.configure(state="normal", background="#FFFFFF", insertbackground=PALETTE["ink"])
        self.file_note_edit_button.configure(text="保存", background=PALETTE["cyan"], foreground="#FFFFFF", activebackground="#1D4ED8")
        self.file_note_cancel_button.pack(side="left", padx=(0, 5), before=self.file_note_edit_button)
        self.file_note_status_var.set("编辑中")
        self.file_note_text.focus_set()
        self.file_note_text.mark_set("insert", "end-1c")

    def _finish_file_note_edit_ui(self) -> None:
        self.file_note_editing = False
        self._editing_note_key = None
        self._file_note_edit_original = ""
        self.file_note_text.configure(state="disabled", background="#F8FAFC")
        self.file_note_edit_button.configure(text="编辑", background="#E8F0FE", foreground="#1D4ED8", activebackground="#D2E3FC")
        self.file_note_cancel_button.pack_forget()
        self._displayed_note_key = None
        node = self.nodes.get(self.selected_key or "")
        if node:
            self._update_file_note(node)

    def save_file_note_edit(self) -> None:
        key = self._editing_note_key
        node = self.nodes.get(key or "")
        if not node or node["is_dir"]:
            self.cancel_file_note_edit()
            return
        path: Path = node["path"]
        try:
            relative_path = path.relative_to(self.project_root).as_posix()
        except ValueError:
            messagebox.showerror("保存失败", "无法确定该文件相对于项目根目录的位置。", parent=self)
            return
        project_id = str(self.project.get("project_id", "")).casefold()
        registry_key = (project_id, relative_path.casefold())
        existing = dict(self.file_research_notes.get(registry_key, {}))
        existing.update(
            {
                "project_id": self.project.get("project_id", ""),
                "relative_path": relative_path,
                "status": "用户修改",
                "title": existing.get("title") or path.stem,
                "manual_text": self.file_note_text.get("1.0", "end-1c").strip(),
                "updated_at": now_text(),
            }
        )
        existing.setdefault("tags", [])
        updated_registry = dict(self.file_research_notes)
        updated_registry[registry_key] = existing
        payload = {
            "schema_version": 1,
            "updated_at": now_text(),
            "scope_note": "控制台文件研究说明；用户修改仅写入本文件，不改动研究源文件。",
            "notes": sorted(
                updated_registry.values(),
                key=lambda item: (str(item.get("project_id", "")).casefold(), str(item.get("relative_path", "")).casefold()),
            ),
        }
        try:
            write_json_atomic(FILE_NOTES_PATH, payload)
        except OSError as exc:
            messagebox.showerror("保存失败", f"研究说明未能保存：\n{exc}", parent=self)
            return
        self.file_research_notes = updated_registry
        self.app.status_var.set("单文件研究说明已保存；研究源文件未修改")
        self._finish_file_note_edit_ui()

    def cancel_file_note_edit(self) -> None:
        if not self.file_note_editing:
            return
        self.file_note_text.configure(state="normal")
        self.file_note_text.delete("1.0", "end")
        self.file_note_text.insert("1.0", self._file_note_edit_original)
        self.app.status_var.set("已取消文件说明修改；研究源文件未修改")
        self._finish_file_note_edit_ui()

    def _update_file_note(self, node: dict[str, Any]) -> None:
        if self.file_note_editing:
            return
        note_key = node["key"]
        if note_key == self._displayed_note_key:
            return
        self._displayed_note_key = note_key
        path: Path = node["path"]
        note = None if node["is_dir"] else self._research_note_for(path)
        if note is None and not node["is_dir"]:
            note = self._generated_data_note(path)

        self.file_note_text.configure(state="normal")
        self.file_note_text.delete("1.0", "end")
        if node["is_dir"]:
            self.file_note_status_var.set("请选择文件")
            self.file_note_title_var.set("这里显示单个文件的研究用途")
            self.file_note_text.insert("end", "单击画布卡片或右侧索引中的文件，即可查看研究问题、方法、核心发现以及它与当前研究管线的关系。", "body")
            self.file_note_tags_var.set("文件夹本身不生成研究说明")
        elif self._is_build_artifact(path):
            self.file_note_status_var.set("系统辅助文件")
            self.file_note_title_var.set("该文件无需单独整理研究说明")
            self.file_note_text.insert("end", "这是 LaTeX 编译过程自动生成的辅助文件，可由源文件重新生成，不属于论文正文、数据或研究证据。", "body")
            self.file_note_tags_var.set("建议保持折叠")
        elif self._is_office_auxiliary_file(path):
            self.file_note_status_var.set("系统辅助文件")
            self.file_note_title_var.set("Office 临时 / 锁定文件")
            self.file_note_text.insert("end", "这是 Word 或 Office 在编辑、自动恢复过程中生成的临时文件，不是独立研究材料。正式内容应以同目录下对应的 DOCX 文档为准。", "body")
            self.file_note_tags_var.set("无需填写研究说明 · 可在关闭 Office 后清理")
        elif note:
            self.file_note_status_var.set(str(note.get("status", "已整理 · 示例")))
            self.file_note_title_var.set(str(note.get("title", path.stem)))
            if "manual_text" in note:
                manual_text = str(note.get("manual_text", "")).strip()
                if manual_text:
                    section_headings = {"研究问题", "数据 / 方法", "核心发现", "对当前项目的用途"}
                    for line in manual_text.splitlines(keepends=True):
                        tag = "section" if line.strip() in section_headings else "body"
                        self.file_note_text.insert("end", line, tag)
                else:
                    self.file_note_text.insert("end", "（空白说明）", "body")
            else:
                sections = (
                    ("研究问题", note.get("research_question")),
                    ("数据 / 方法", note.get("method")),
                    ("核心发现", note.get("finding")),
                    ("对当前项目的用途", note.get("research_use")),
                )
                for heading, value in sections:
                    if not value:
                        continue
                    self.file_note_text.insert("end", f"{heading}\n", "section")
                    self.file_note_text.insert("end", f"{value}\n", "body")
            tags = note.get("tags") or []
            self.file_note_tags_var.set("关键词  " + " · ".join(str(tag) for tag in tags) if tags else "")
        else:
            self.file_note_status_var.set("待整理")
            self.file_note_title_var.set("尚未填写该文件的研究说明")
            self.file_note_text.insert("end", "尚无文件说明。请在确认文件内容后填写；程序不会根据文件名推断研究结论。", "body")
            self.file_note_tags_var.set("待后续核验内容")
        self.file_note_text.configure(state="disabled")
        self.file_note_text.yview_moveto(0.0)
        can_edit = not node["is_dir"] and not self._is_build_artifact(path) and not self._is_office_auxiliary_file(path)
        self.file_note_edit_button.configure(state="normal" if can_edit else "disabled")

    def _update_detail(self, refresh_index: bool = False) -> None:
        node = self.nodes.get(self.selected_key or self.root_key)
        if not node:
            return
        path: Path = node["path"]
        meta = self._file_meta(path, node["is_dir"])
        self.detail_title_var.set(path.name or str(path))
        if node["is_dir"]:
            hidden_count = node.get("hidden_build_count", 0)
            if hidden_count and not self.show_build_artifacts:
                self.detail_status_var.set(f"DIR · 文件夹 · 已折叠 {hidden_count} 个构建产物")
            else:
                self.detail_status_var.set("DIR · 文件夹 · 双击进入")
        else:
            self.detail_status_var.set(f"{meta['badge']} · {meta['label']} · 双击打开")
        try:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size = f"{stat.st_size:,} bytes"
        except OSError:
            modified = "无法读取"
            size = "无法读取"
        if node["is_dir"]:
            total = node.get("children_total")
            all_total = node.get("all_children_total")
            hidden_count = node.get("hidden_build_count", 0)
            shown = node.get("shown_children", 0)
            count_line = f"已展开：{shown}/{total} 项" if total is not None else "尚未读取子项"
            filter_line = ""
            if hidden_count and not self.show_build_artifacts:
                filter_line = f"\n当前视图：显示 {total:,} / 全部 {all_total:,} 项\n已折叠：{hidden_count:,} 个 LaTeX 构建产物"
            text = f"类型：文件夹\n{count_line}{filter_line}\n修改时间：{modified}\n\n路径：\n{path}"
            self.load_all_button.configure(state="normal" if node.get("truncated") else "disabled")
        else:
            suffix = path.suffix.upper() if path.suffix else "无扩展名"
            text = f"类型：{meta['label']}\n文件类别：{meta['group']}\n扩展名：{suffix}\n大小：{size}\n修改时间：{modified}\n\n路径：\n{path}"
            self.load_all_button.configure(state="disabled")
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")
        self._update_file_note(node)
        if self.detail_tab == "profile":
            self._update_data_profile(node)
        focus_node = self.nodes.get(self.focus_key)
        focus_hidden = focus_node.get("hidden_build_count", 0) if focus_node else 0
        if self.show_build_artifacts:
            self.artifact_toggle_button.configure(text="✓  隐藏构建产物")
        elif focus_hidden:
            self.artifact_toggle_button.configure(text=f"○  显示构建产物 ({focus_hidden})")
        else:
            self.artifact_toggle_button.configure(text="○  显示构建产物")
        if refresh_index:
            self._refresh_child_index(node)

    def _refresh_child_index(self, selected_node: dict[str, Any]) -> None:
        index_node = selected_node if selected_node["is_dir"] else self.nodes.get(selected_node.get("parent"))
        for item in self.child_index.get_children():
            self.child_index.delete(item)
        self.index_item_to_key.clear()
        self.index_item_to_path.clear()
        self.index_item_to_parent.clear()
        if not index_node or not index_node["is_dir"]:
            self.index_status_var.set("没有可展示的上级文件夹")
            return
        all_entries = self._all_entries(index_node["path"])
        entries = all_entries if self.show_build_artifacts else [entry for entry in all_entries if not self._is_build_artifact(entry)]
        hidden_count = max(0, len(all_entries) - len(entries))
        limit = min(len(entries), 5000)
        for entry in entries[:limit]:
            key = path_key(entry)
            meta = self._file_meta(entry, entry.is_dir())
            item_id = self.child_index.insert(
                "",
                "end",
                values=(f"{meta['badge']}   {entry.name}", meta["label"]),
            )
            self.index_item_to_key[item_id] = key
            self.index_item_to_path[item_id] = entry
            self.index_item_to_parent[item_id] = index_node["key"]
        if len(entries) > limit:
            self.index_status_var.set(f"共 {len(entries):,} 项；当前显示前 {limit:,} 项")
        elif hidden_count:
            self.index_status_var.set(f"显示 {len(entries):,} / 全部 {len(all_entries):,} 项 · 已折叠 {hidden_count:,} 个构建产物")
        else:
            self.index_status_var.set(f"共 {len(entries):,} 项 · 名称与类型可滚动浏览")

    def _materialize_index_item(self, item_id: str) -> tuple[str | None, Path | None]:
        path = self.index_item_to_path.get(item_id)
        parent_key = self.index_item_to_parent.get(item_id)
        if not path or not parent_key:
            return None, path
        key = self.index_item_to_key.get(item_id)
        if key not in self.nodes:
            self._ensure_children(parent_key, load_all=True)
        return key, path

    def select_from_index(self, _event: tk.Event | None = None) -> None:
        if self._file_note_edit_guard():
            return
        selection = self.child_index.selection()
        if not selection:
            return
        key, _path = self._materialize_index_item(selection[0])
        if key and key in self.nodes:
            self.selected_key = key
            self.render()

    def open_from_index(self, _event: tk.Event | None = None) -> None:
        if self._file_note_edit_guard():
            return
        selection = self.child_index.selection()
        if not selection:
            return
        key, path = self._materialize_index_item(selection[0])
        if not path:
            return
        if path.is_dir() and key:
            if self.focus_key != key:
                self.focus_stack.append(key)
            self._transition_to_scene(key)
        elif path.is_file():
            self.app.open_path(path)

    def _nearest_key(self, event: tk.Event) -> str | None:
        closest = self.canvas.find_closest(event.x, event.y)
        if not closest:
            return None
        return self.item_to_key.get(closest[0])

    def select_from_canvas(self, event: tk.Event) -> None:
        if self._file_note_edit_guard():
            return
        key = self._nearest_key(event)
        if key:
            self.selected_key = key
            self.render()
            self._update_detail(refresh_index=True)

    def open_from_canvas(self, event: tk.Event) -> None:
        if self._file_note_edit_guard():
            return
        key = self._nearest_key(event)
        if not key:
            return
        node = self.nodes.get(key)
        if not node:
            return
        self.selected_key = key
        if node["is_dir"]:
            if self.focus_key != key:
                self.focus_stack.append(key)
            self._transition_to_scene(key)
        else:
            self.app.open_path(node["path"])

    def start_pan(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y, self.view_cx, self.view_cy)

    def pan(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        x0, y0, cx, cy = self.drag_start
        self.view_cx = cx - (event.x - x0) / self.view_scale
        self.view_cy = cy - (event.y - y0) / self.view_scale
        self.render()

    def end_pan(self, _event: tk.Event) -> None:
        self.drag_start = None

    def on_mousewheel(self, event: tk.Event) -> None:
        factor = 1.16 if event.delta > 0 else 0.86
        self.zoom_at(event.x, event.y, factor)

    def adjust_zoom(self, factor: float) -> None:
        self.zoom_at(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, factor)

    def zoom_at(self, x: float, y: float, factor: float) -> None:
        old_scale = self.view_scale
        world_x, world_y = self._screen_to_world(x, y)
        self.view_scale = max(0.32, min(3.0, old_scale * factor))
        self.view_cx = world_x - (x - self.canvas.winfo_width() / 2) / self.view_scale
        self.view_cy = world_y - (y - self.canvas.winfo_height() / 2) / self.view_scale
        self.render()

    def _animate_to(self, target_cx: float, target_cy: float, target_scale: float, duration: int = 420) -> None:
        if self.animating:
            return
        self.animating = True
        start_cx, start_cy, start_scale = self.view_cx, self.view_cy, self.view_scale
        start_time = time.perf_counter()

        def step() -> None:
            if not self.winfo_exists():
                return
            progress = min(1.0, (time.perf_counter() - start_time) * 1000 / duration)
            eased = 1 - (1 - progress) ** 3
            self.view_cx = start_cx + (target_cx - start_cx) * eased
            self.view_cy = start_cy + (target_cy - start_cy) * eased
            self.view_scale = start_scale + (target_scale - start_scale) * eased
            self.render()
            if progress < 1.0:
                self.after(16, step)
            else:
                self.animating = False

        step()

    def go_back(self) -> None:
        if self._file_note_edit_guard():
            return
        if len(self.focus_stack) <= 1:
            return
        self.focus_stack.pop()
        self._transition_to_scene(self.focus_stack[-1])

    def go_root(self) -> None:
        if self._file_note_edit_guard():
            return
        self.focus_stack = [self.root_key]
        self._transition_to_scene(self.root_key)

    def fit_view(self) -> None:
        if self.scene_transition:
            keys = list(self.scene_transition["target_keys"])
            positions = {key: self.scene_transition["ends"][key] for key in keys}
        else:
            keys = list(self.scene_keys)
            positions = self.scene_positions
        if not positions or not keys:
            return
        target_cx, target_cy, target_scale = self._scene_view(positions, keys)
        self._animate_to(target_cx, target_cy, target_scale)

    def load_all_current(self) -> None:
        node = self.nodes.get(self.focus_key)
        if node and node["is_dir"]:
            self._ensure_children(node["key"], load_all=True)
            self._transition_to_scene(node["key"])

    def open_physical_folder(self) -> None:
        self.app.open_path(self.project_root)

    def return_home(self) -> None:
        if self._file_note_edit_guard():
            return
        self.close()
        self.app.deiconify()
        self.app.lift()
        self.app.focus_force()

    def close(self) -> None:
        if self.file_note_editing:
            discard = messagebox.askyesno("说明尚未保存", "当前文件说明仍在编辑。是否放弃修改并关闭？", parent=self)
            if not discard:
                return
        try:
            self.on_close_callback(self.project["project_id"])
        finally:
            self.destroy()


class ResearchPipelineConsole(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("研究管线控制台 · Research Portfolio")
        self.geometry("1500x940")
        self.minsize(1180, 720)
        self.configure(background=PALETTE["surface"])
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        icon_path = APP_DIR / "ResearchPipelineConsole.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.registry = Registry()
        self.snapshot: dict[str, Any] | None = Registry.load_snapshot()
        self.scan_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.scan_thread: threading.Thread | None = None
        self.selected_project_path: str | None = None
        self.selected_alert_file: str | None = None
        self.status_var = tk.StringVar(value="研究文件只读；项目说明与文件说明可单独编辑")
        self.project_note_overrides = load_project_note_overrides()
        self.overview_note_editing = False
        self._overview_edit_project_id: str | None = None
        self._overview_edit_original = ""
        self.root_list: tk.Listbox
        self.overview_tree: ttk.Treeview
        self.projects_tree: ttk.Treeview
        self.health_tree: ttk.Treeview
        self.log_text: tk.Text
        self.pipeline_canvas: tk.Canvas
        self.kpi_vars: dict[str, tk.StringVar] = {}
        self.project_search_var = tk.StringVar(value="")
        self.portfolio_filter_var = tk.StringVar(value="全部研究组")
        self.status_filter_var = tk.StringVar(value="全部状态")
        self.filtered_count_var = tk.StringVar(value="")
        self.nav_buttons: dict[int, tk.Button] = {}
        self.node_paths: dict[int, str] = {}
        self.project_windows: dict[str, ProjectCanvasWindow] = {}
        self.build_styles()
        self.build_ui()
        self.project_search_var.trace_add("write", self._project_filters_changed)
        self.portfolio_filter_var.trace_add("write", self._project_filters_changed)
        self.status_filter_var.trace_add("write", self._project_filters_changed)
        self.refresh_all()
        self.after(100, self.poll_scan_queue)

    def close_app(self) -> None:
        has_unsaved_file_note = any(
            window.winfo_exists() and window.file_note_editing
            for window in self.project_windows.values()
        )
        if self.overview_note_editing or has_unsaved_file_note:
            discard = messagebox.askyesno(
                "说明尚未保存",
                "仍有研究说明处于编辑状态。是否放弃这些未保存修改并退出？",
                parent=self,
            )
            if not discard:
                return
        self.destroy()

    def build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=PALETTE["surface"])
        style.configure("Header.TFrame", background=PALETTE["card"])
        style.configure("HeaderIcon.TLabel", background=PALETTE["card"])
        style.configure("HeaderTitle.TLabel", background=PALETTE["card"], foreground=PALETTE["ink"], font=(UI_FONT, 18, "bold"))
        style.configure("HeaderSub.TLabel", background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 10))
        style.configure("Sidebar.TFrame", background=PALETTE["surface"])
        style.configure("SidebarTitle.TLabel", background=PALETTE["surface"], foreground=PALETTE["ink"], font=(UI_FONT, 11, "bold"))
        style.configure("SidebarText.TLabel", background=PALETTE["surface"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
        style.configure("ReadOnly.TLabel", background="#E6F4EA", foreground="#137333", padding=(8, 4), font=(UI_FONT, 9, "bold"))
        style.configure("Main.TFrame", background=PALETTE["surface"])
        style.configure("Status.TFrame", background=PALETTE["surface_alt"])
        style.configure("Status.TLabel", background=PALETTE["surface_alt"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
        style.configure("KPI.TFrame", background=PALETTE["card"], relief="flat", borderwidth=0)
        style.configure("KPIName.TLabel", background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
        style.configure("KPIValue.TLabel", background=PALETTE["card"], foreground=PALETTE["ink"], font=(UI_FONT, 20, "bold"))
        style.configure("Section.TLabel", background=PALETTE["surface"], font=(UI_FONT, 13, "bold"), foreground=PALETTE["ink"])
        style.configure("Small.TLabel", background=PALETTE["surface"], foreground=PALETTE["muted"], font=(UI_FONT, 9))
        style.configure(
            "Treeview",
            background=PALETTE["card"],
            fieldbackground=PALETTE["card"],
            foreground=PALETTE["ink"],
            rowheight=34,
            font=(UI_FONT, 9),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", PALETTE["cyan_soft"])],
            foreground=[("selected", "#174EA6")],
        )
        style.configure(
            "Treeview.Heading",
            background="#F7F9FC",
            foreground="#475467",
            font=(UI_FONT, 9, "bold"),
            relief="flat",
            padding=(8, 9),
        )
        style.map("Treeview.Heading", background=[("active", "#EEF4FF")])
        style.configure("Index.Treeview", background=PALETTE["card"], fieldbackground=PALETTE["card"], foreground=PALETTE["ink"], rowheight=28, font=(UI_FONT, 9), borderwidth=0)
        style.map("Index.Treeview", background=[("selected", PALETTE["cyan_soft"])], foreground=[("selected", PALETTE["ink"])])
        style.configure("Index.Treeview.Heading", background=PALETTE["surface_alt"], foreground=PALETTE["muted"], font=(UI_FONT, 8, "bold"), relief="flat")
        style.configure("Hidden.TNotebook", background=PALETTE["surface"], borderwidth=0, tabmargins=0)
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("TButton", background=PALETTE["card"], foreground=PALETTE["ink"], padding=(10, 6), font=(UI_FONT, 9))
        style.map("TButton", background=[("active", PALETTE["cyan_soft"])])
        style.configure("Primary.TButton", background=PALETTE["cyan"], foreground="#FFFFFF", padding=(12, 7), font=(UI_FONT, 9, "bold"))
        style.map("Primary.TButton", background=[("active", "#1D4ED8"), ("pressed", "#1E40AF")])
        style.configure("Sidebar.TButton", background=PALETTE["surface"], foreground=PALETTE["ink"], padding=(9, 6), font=(UI_FONT, 9))
        style.map("Sidebar.TButton", background=[("active", PALETTE["cyan_soft"]), ("pressed", "#D2E3FC")])
        style.configure(
            "Filter.TCombobox",
            fieldbackground=PALETTE["card"],
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            arrowcolor=PALETTE["muted"],
            padding=(8, 7),
        )
        style.layout(
            "Modern.Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
        )
        style.layout(
            "Modern.Horizontal.TScrollbar",
            [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
        )
        for scrollbar_style in ("Modern.Vertical.TScrollbar", "Modern.Horizontal.TScrollbar"):
            style.configure(
                scrollbar_style,
                troughcolor="#F1F4F8",
                background="#C9D2E0",
                bordercolor="#F1F4F8",
                darkcolor="#C9D2E0",
                lightcolor="#C9D2E0",
                relief="flat",
                borderwidth=0,
                width=9,
            )
            style.map(scrollbar_style, background=[("active", "#94A3B8"), ("pressed", "#64748B")])

    def build_ui(self) -> None:
        header = tk.Frame(
            self,
            background=PALETTE["card"],
            highlightbackground=PALETTE["line"],
            highlightthickness=1,
        )
        header.pack(fill="x")
        header_inner = tk.Frame(header, background=PALETTE["card"])
        header_inner.pack(fill="both", expand=True, padx=26, pady=(15, 14))
        icon_path = APP_DIR / "ResearchPipelineConsole-icon.png"
        if icon_path.exists():
            try:
                raw_icon = tk.PhotoImage(file=str(icon_path))
                shrink = max(1, raw_icon.width() // 40)
                self.header_icon = raw_icon.subsample(shrink, shrink)
                tk.Label(header_inner, image=self.header_icon, background=PALETTE["card"], borderwidth=0).pack(side="left", padx=(0, 13))
            except tk.TclError:
                self.header_icon = None
        title_frame = tk.Frame(header_inner, background=PALETTE["card"])
        title_frame.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_frame,
            text="研究管线控制台",
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_frame,
            text="Research Portfolio  ·  文件、项目、路径与研究说明的一站式视图",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(3, 0))
        tk.Label(
            header_inner,
            text="●  研究文件只读",
            background="#ECFDF3",
            foreground="#15803D",
            font=(UI_FONT, 9, "bold"),
            padx=11,
            pady=7,
        ).pack(side="right", padx=(12, 0))
        tk.Button(
            header_inner,
            text="↻  扫描目录",
            command=self.start_scan,
            background=PALETTE["cyan"],
            foreground="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9, "bold"),
            padx=17,
            pady=9,
        ).pack(side="right")

        body = tk.Frame(self, background=PALETTE["surface"])
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(
            body,
            background=PALETTE["card"],
            width=272,
            highlightbackground=PALETTE["line"],
            highlightthickness=1,
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(
            sidebar,
            text="工作区",
            background=PALETTE["card"],
            foreground="#98A2B3",
            font=(UI_FONT, 8, "bold"),
        ).pack(anchor="w", padx=18, pady=(18, 8))
        nav_items = [
            ("⌂", "研究总览"),
            ("⌁", "项目结构图"),
            ("✓", "路径健康"),
            ("▦", "项目登记"),
            ("↺", "扫描记录"),
        ]
        for index, (icon, label) in enumerate(nav_items):
            button = tk.Button(
                sidebar,
                text=f"{icon}   {label}",
                command=lambda page=index: self.switch_tab(page),
                anchor="w",
                background=PALETTE["card"],
                foreground=PALETTE["muted"],
                activebackground=PALETTE["cyan_soft"],
                activeforeground="#1D4ED8",
                relief="flat",
                borderwidth=0,
                cursor="hand2",
                font=(UI_FONT, 10),
                padx=16,
                pady=10,
            )
            button.pack(fill="x", padx=10, pady=1)
            self.nav_buttons[index] = button

        tk.Frame(sidebar, background=PALETTE["line"], height=1).pack(fill="x", padx=18, pady=16)
        tk.Label(
            sidebar,
            text="监控目录",
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 10, "bold"),
        ).pack(anchor="w", padx=18)
        tk.Label(
            sidebar,
            text="只读取文件名、目录结构和代码中的路径引用。",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 8),
            wraplength=220,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(4, 10))
        list_shell = tk.Frame(sidebar, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        list_shell.pack(fill="both", expand=True, padx=14)
        self.root_list = tk.Listbox(
            list_shell,
            height=10,
            selectmode="extended",
            relief="flat",
            borderwidth=0,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            selectbackground=PALETTE["cyan_soft"],
            selectforeground="#174EA6",
            highlightthickness=0,
            activestyle="none",
            font=(UI_FONT, 9),
        )
        self.root_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        root_scroll = ttk.Scrollbar(list_shell, orient="vertical", command=self.root_list.yview, style="Modern.Vertical.TScrollbar")
        root_scroll.pack(side="right", fill="y")
        self.root_list.configure(yscrollcommand=root_scroll.set)
        for root in self.registry.roots:
            self.root_list.insert("end", root)

        root_actions = tk.Frame(sidebar, background=PALETTE["card"])
        root_actions.pack(fill="x", padx=14, pady=(10, 4))
        for text, command in (("＋ 添加", self.add_root), ("－ 移除", self.remove_roots)):
            tk.Button(
                root_actions,
                text=text,
                command=command,
                background="#F8FAFC",
                foreground=PALETTE["ink"],
                activebackground=PALETTE["cyan_soft"],
                relief="flat",
                borderwidth=0,
                cursor="hand2",
                font=(UI_FONT, 9),
                pady=7,
            ).pack(side="left", fill="x", expand=True, padx=(0, 5) if text.startswith("＋") else 0)
        tk.Button(
            sidebar,
            text="保存目录配置",
            command=self.save_roots,
            background=PALETTE["card"],
            foreground=PALETTE["cyan"],
            activebackground=PALETTE["cyan_soft"],
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9, "bold"),
            pady=7,
        ).pack(fill="x", padx=14)
        tk.Button(
            sidebar,
            text="打开控制台目录  ↗",
            command=lambda: self.open_path(APP_DIR),
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            activebackground="#F8FAFC",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9),
            pady=9,
        ).pack(fill="x", padx=14, pady=(2, 12))

        main = tk.Frame(body, background=PALETTE["surface"])
        main.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        self.notebook = ttk.Notebook(main, style="Hidden.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.build_overview_tab()
        self.build_pipeline_tab()
        self.build_health_tab()
        self.build_projects_tab()
        self.build_log_tab()
        self.switch_tab(0)

        status_bar = tk.Frame(self, background=PALETTE["card"], height=34, highlightbackground=PALETTE["line"], highlightthickness=1)
        status_bar.pack(fill="x")
        status_bar.pack_propagate(False)
        tk.Label(status_bar, text="●", background=PALETTE["card"], foreground=PALETTE["green"], font=(UI_FONT, 8)).pack(side="left", padx=(16, 6))
        tk.Label(status_bar, textvariable=self.status_var, background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 8)).pack(side="left")
        self.progress_var = tk.StringVar(value="")
        tk.Label(status_bar, textvariable=self.progress_var, background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 8)).pack(side="right", padx=16)

    def switch_tab(self, index: int) -> None:
        if hasattr(self, "notebook"):
            self.notebook.select(index)
        for page, button in self.nav_buttons.items():
            selected = page == index
            button.configure(
                background=PALETTE["cyan_soft"] if selected else PALETTE["card"],
                foreground="#1D4ED8" if selected else PALETTE["muted"],
                font=(UI_FONT, 10, "bold" if selected else "normal"),
            )

    def _project_filters_changed(self, *_args: Any) -> None:
        if hasattr(self, "overview_tree"):
            self.refresh_overview()
        if hasattr(self, "projects_tree"):
            self.refresh_projects()

    def filtered_project_rows(self, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        rows = rows if rows is not None else self.registry.project_rows()
        query = self.project_search_var.get().strip().casefold()
        portfolio = self.portfolio_filter_var.get().strip()
        status = self.status_filter_var.get().strip()
        result: list[dict[str, Any]] = []
        for row in rows:
            haystack = " ".join(
                str(row.get(key, ""))
                for key in ("name", "project_id", "portfolio", "status", "kind", "path", "note")
            ).casefold()
            if query and query not in haystack:
                continue
            if portfolio and portfolio != "全部研究组" and row.get("portfolio") != portfolio:
                continue
            if status and status != "全部状态" and row.get("status") != status:
                continue
            result.append(row)
        return result

    @staticmethod
    def display_status(status: str) -> str:
        labels = {
            "ACTIVE": "进行中",
            "DRAFT": "草稿",
            "OPEN": "待核查",
            "SCREENING": "筛选中",
            "FROZEN": "已冻结",
            "SHELVED": "已搁置",
            "SUPPORT": "支撑",
            "ARCHIVED": "已归档",
            "TOOL": "工具",
        }
        return labels.get(status, status)

    def _project_note_for(self, row: dict[str, Any]) -> str:
        project_id = str(row.get("project_id", "")).casefold()
        if project_id in self.project_note_overrides:
            return self.project_note_overrides[project_id]
        return str(row.get("note", ""))

    def toggle_overview_note_edit(self) -> None:
        if self.overview_note_editing:
            self.save_overview_note_edit()
            return
        selection = self.overview_tree.selection()
        if not selection:
            return
        self.overview_note_editing = True
        self._overview_edit_project_id = selection[0]
        self._overview_edit_original = self.overview_detail_text.get("1.0", "end-1c")
        self.overview_detail_text.configure(state="normal", background="#FFFFFF", insertbackground=PALETTE["ink"])
        self.overview_note_edit_button.configure(text="保存", background=PALETTE["cyan"], foreground="#FFFFFF", activebackground="#1D4ED8")
        self.overview_note_cancel_button.pack(side="right", padx=(0, 5), before=self.overview_note_edit_button)
        self.overview_detail_text.focus_set()
        self.overview_detail_text.mark_set("insert", "end-1c")
        self.status_var.set("正在编辑项目研究说明；研究文件仍保持只读")

    def _finish_overview_note_edit_ui(self) -> None:
        self.overview_note_editing = False
        self._overview_edit_project_id = None
        self._overview_edit_original = ""
        self.overview_detail_text.configure(state="disabled", background="#F8FAFC")
        self.overview_note_edit_button.configure(text="编辑", background="#E8F0FE", foreground="#1D4ED8", activebackground="#D2E3FC")
        self.overview_note_cancel_button.pack_forget()
        self.update_overview_detail()

    def save_overview_note_edit(self) -> None:
        project_id = self._overview_edit_project_id
        if not project_id:
            self.cancel_overview_note_edit()
            return
        updated_overrides = dict(self.project_note_overrides)
        updated_overrides[project_id.casefold()] = self.overview_detail_text.get("1.0", "end-1c").strip()
        payload = {
            "schema_version": 1,
            "updated_at": now_text(),
            "notes": dict(sorted(updated_overrides.items())),
        }
        try:
            write_json_atomic(PROJECT_NOTE_OVERRIDES_PATH, payload)
        except OSError as exc:
            messagebox.showerror("保存失败", f"项目研究说明未能保存：\n{exc}", parent=self)
            return
        self.project_note_overrides = updated_overrides
        self.status_var.set("项目研究说明已保存；研究源文件未修改")
        self.refresh_projects()
        self._finish_overview_note_edit_ui()

    def cancel_overview_note_edit(self) -> None:
        if not self.overview_note_editing:
            return
        self.overview_detail_text.configure(state="normal")
        self.overview_detail_text.delete("1.0", "end")
        self.overview_detail_text.insert("1.0", self._overview_edit_original)
        self.status_var.set("已取消项目说明修改；研究源文件未修改")
        self._finish_overview_note_edit_ui()

    def update_overview_detail(self, _event: Any | None = None) -> None:
        if not hasattr(self, "overview_tree") or not hasattr(self, "overview_detail_text"):
            return
        selection = self.overview_tree.selection()
        if not selection:
            return
        project_id = selection[0]
        if self.overview_note_editing:
            if self._overview_edit_project_id and project_id != self._overview_edit_project_id:
                self.overview_tree.selection_set(self._overview_edit_project_id)
                self.overview_tree.see(self._overview_edit_project_id)
                self.status_var.set("请先保存或取消当前项目说明")
                self.bell()
            return
        row = next((item for item in self.registry.project_rows() if item["project_id"] == project_id), None)
        if not row:
            return
        self.overview_detail_name_var.set(row.get("name", ""))
        self.overview_detail_meta_var.set(
            f"{self.display_status(row.get('status', ''))}  ·  {row.get('portfolio', '')}"
        )
        self.overview_detail_path = row.get("path", "")
        self.overview_detail_path_var.set(self.overview_detail_path or "路径待核查")
        self.overview_detail_text.configure(state="normal")
        self.overview_detail_text.delete("1.0", "end")
        self.overview_detail_text.insert("1.0", self._project_note_for(row) or "暂无说明")
        self.overview_detail_text.configure(state="disabled")
        self.overview_note_edit_button.configure(state="normal")

    def open_overview_folder(self) -> None:
        if self.overview_detail_path:
            self.open_path(self.overview_detail_path)

    def build_overview_tab(self) -> None:
        tab = ttk.Frame(self.notebook, style="Main.TFrame", padding=(2, 2))
        self.notebook.add(tab, text="总览")
        title_row = tk.Frame(tab, background=PALETTE["surface"])
        title_row.pack(fill="x", pady=(0, 16))
        title_copy = tk.Frame(title_row, background=PALETTE["surface"])
        title_copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_copy,
            text="研究组合总览",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_copy,
            text="浏览全部研究资产，双击项目进入无限画布。研究文件保持只读，说明卡片可编辑。",
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 0))

        kpi_frame = tk.Frame(tab, background=PALETTE["surface"])
        kpi_frame.pack(fill="x", pady=(0, 18))
        kpi_specs = [
            ("projects", "研究单元", "▦", PALETTE["cyan"], "已登记并去除工具"),
            ("active", "开放管线", "↗", "#7C3AED", "进行中、草稿与待核查"),
            ("files", "扫描文件", "◫", "#0F766E", "两个根目录的只读索引"),
            ("alerts", "路径引用", "✓", "#D97706", "固定路径健康记录"),
        ]
        for index, (key, label, icon, accent, caption) in enumerate(kpi_specs):
            card = tk.Frame(
                kpi_frame,
                background=PALETTE["card"],
                highlightbackground=PALETTE["line"],
                highlightthickness=1,
                padx=15,
                pady=12,
            )
            card.pack(side="left", fill="x", expand=True, padx=(0, 12) if index < len(kpi_specs) - 1 else 0)
            top = tk.Frame(card, background=PALETTE["card"])
            top.pack(fill="x")
            tk.Label(
                top,
                text=icon,
                background=accent,
                foreground="#FFFFFF",
                font=("Segoe UI Symbol", 11, "bold"),
                width=2,
                height=1,
            ).pack(side="left")
            tk.Label(
                top,
                text=label,
                background=PALETTE["card"],
                foreground=PALETTE["muted"],
                font=(UI_FONT, 9, "bold"),
            ).pack(side="left", padx=(9, 0))
            var = tk.StringVar(value="—")
            self.kpi_vars[key] = var
            tk.Label(
                card,
                textvariable=var,
                background=PALETTE["card"],
                foreground=PALETTE["ink"],
                font=(UI_FONT, 21, "bold"),
            ).pack(anchor="w", pady=(10, 0))
            tk.Label(
                card,
                text=caption,
                background=PALETTE["card"],
                foreground="#98A2B3",
                font=(UI_FONT, 8),
            ).pack(anchor="w", pady=(2, 0))

        section_bar = tk.Frame(tab, background=PALETTE["surface"])
        section_bar.pack(fill="x", pady=(0, 9))
        section_copy = tk.Frame(section_bar, background=PALETTE["surface"])
        section_copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            section_copy,
            text="研究项目",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 12, "bold"),
        ).pack(side="left")
        tk.Label(
            section_copy,
            textvariable=self.filtered_count_var,
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(side="left", padx=(8, 0))

        filters = tk.Frame(section_bar, background=PALETTE["surface"])
        filters.pack(side="right")
        search_shell = tk.Frame(filters, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        search_shell.pack(side="left", padx=(0, 8))
        tk.Label(
            search_shell,
            text="⌕",
            background=PALETTE["card"],
            foreground="#98A2B3",
            font=("Segoe UI Symbol", 12),
        ).pack(side="left", padx=(9, 4))
        tk.Entry(
            search_shell,
            textvariable=self.project_search_var,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            relief="flat",
            borderwidth=0,
            width=22,
            font=(UI_FONT, 9),
        ).pack(side="left", padx=(0, 8), pady=8)

        rows = self.registry.project_rows()
        portfolios = ["全部研究组"] + sorted({str(row.get("portfolio", "")) for row in rows if row.get("portfolio")})
        statuses = ["全部状态"] + sorted({str(row.get("status", "")) for row in rows if row.get("status")})
        FlatSelect(
            filters,
            variable=self.portfolio_filter_var,
            values=portfolios,
            width=17,
        ).pack(side="left", padx=(0, 8))
        FlatSelect(
            filters,
            variable=self.status_filter_var,
            values=statuses,
            width=12,
        ).pack(side="left")

        content = tk.Frame(tab, background=PALETTE["surface"])
        content.pack(fill="both", expand=True, pady=(0, 10))
        tree_frame = tk.Frame(
            content,
            background=PALETTE["card"],
            highlightbackground=PALETTE["line"],
            highlightthickness=1,
        )
        tree_frame.pack(side="left", fill="both", expand=True)
        columns = ("name", "portfolio", "status", "kind", "exists")
        self.overview_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "name": "项目",
            "portfolio": "研究组",
            "status": "状态",
            "kind": "类型",
            "exists": "路径",
        }
        widths = {"name": 300, "portfolio": 165, "status": 88, "kind": 185, "exists": 70}
        for col in columns:
            self.overview_tree.heading(col, text=headings[col])
            self.overview_tree.column(col, width=widths[col], minwidth=60, anchor="w", stretch=(col == "name"))
        self.overview_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self.overview_tree.yview,
            style="Modern.Vertical.TScrollbar",
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.overview_tree.configure(yscrollcommand=scrollbar.set)
        self.overview_tree.tag_configure("even", background=PALETTE["card"])
        self.overview_tree.tag_configure("odd", background="#FAFBFD")
        self.overview_tree.bind("<Double-1>", self.open_selected_project)
        self.overview_tree.bind("<<TreeviewSelect>>", self.update_overview_detail)

        detail = tk.Frame(
            content,
            background=PALETTE["card"],
            width=330,
            highlightbackground=PALETTE["line"],
            highlightthickness=1,
        )
        detail.pack(side="right", fill="y", padx=(14, 0))
        detail.pack_propagate(False)
        self.overview_detail_name_var = tk.StringVar(value="请选择一个项目")
        self.overview_detail_meta_var = tk.StringVar(value="")
        self.overview_detail_path_var = tk.StringVar(value="")
        self.overview_detail_path = ""
        tk.Label(
            detail,
            text="项目详情",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9, "bold"),
        ).pack(anchor="w", padx=18, pady=(18, 8))
        tk.Label(
            detail,
            textvariable=self.overview_detail_name_var,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 13, "bold"),
            wraplength=290,
            justify="left",
        ).pack(anchor="w", padx=18)
        tk.Label(
            detail,
            textvariable=self.overview_detail_meta_var,
            background=PALETTE["cyan_soft"],
            foreground="#1D4ED8",
            font=(UI_FONT, 8, "bold"),
            padx=8,
            pady=4,
        ).pack(anchor="w", padx=18, pady=(10, 16))
        tk.Frame(detail, background=PALETTE["line"], height=1).pack(fill="x", padx=18)
        tk.Label(detail, text="实际位置", background=PALETTE["card"], foreground=PALETTE["muted"], font=(UI_FONT, 8, "bold")).pack(anchor="w", padx=18, pady=(16, 5))
        tk.Label(
            detail,
            textvariable=self.overview_detail_path_var,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 8),
            wraplength=292,
            justify="left",
        ).pack(anchor="w", padx=18)
        overview_note_header = tk.Frame(detail, background=PALETTE["card"])
        overview_note_header.pack(fill="x", padx=18, pady=(16, 5))
        tk.Label(
            overview_note_header,
            text="研究说明",
            background=PALETTE["card"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 8, "bold"),
        ).pack(side="left")
        self.overview_note_cancel_button = tk.Button(
            overview_note_header,
            text="取消",
            command=self.cancel_overview_note_edit,
            background="#F1F5F9",
            foreground=PALETTE["muted"],
            activebackground="#E2E8F0",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 8),
            padx=8,
            pady=3,
        )
        self.overview_note_edit_button = tk.Button(
            overview_note_header,
            text="编辑",
            command=self.toggle_overview_note_edit,
            background="#E8F0FE",
            foreground="#1D4ED8",
            activebackground="#D2E3FC",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 8, "bold"),
            padx=9,
            pady=3,
            state="disabled",
            disabledforeground="#98A2B3",
        )
        self.overview_note_edit_button.pack(side="right")
        self.overview_detail_text = tk.Text(
            detail,
            height=8,
            wrap="word",
            relief="flat",
            borderwidth=0,
            background="#F8FAFC",
            foreground=PALETTE["ink"],
            font=(UI_FONT, 8),
            padx=10,
            pady=8,
        )
        self.overview_detail_text.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.overview_detail_text.configure(state="disabled")
        detail_actions = tk.Frame(detail, background=PALETTE["card"])
        detail_actions.pack(fill="x", padx=18, pady=18)
        tk.Button(
            detail_actions,
            text="进入画布",
            command=self.open_selected_project,
            background=PALETTE["cyan"],
            foreground="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9, "bold"),
            pady=8,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(
            detail_actions,
            text="打开目录",
            command=self.open_overview_folder,
            background="#F1F5F9",
            foreground=PALETTE["ink"],
            activebackground=PALETTE["cyan_soft"],
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9),
            pady=8,
        ).pack(side="left", fill="x", expand=True)

    def build_pipeline_tab(self) -> None:
        tab = ttk.Frame(self.notebook, style="Main.TFrame", padding=(2, 2))
        self.notebook.add(tab, text="项目结构图")
        top = tk.Frame(tab, background=PALETTE["surface"])
        top.pack(fill="x", pady=(0, 16))
        tk.Label(
            top,
            text="本地项目结构图",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            top,
            text="根据已配置的根目录和直接子项目动态生成；双击卡片打开实际目录。",
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 0))
        canvas_frame = tk.Frame(tab, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        canvas_frame.pack(fill="both", expand=True)
        self.pipeline_canvas = tk.Canvas(canvas_frame, background="#FAFBFD", highlightthickness=0)
        hbar = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.pipeline_canvas.xview, style="Modern.Horizontal.TScrollbar")
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.pipeline_canvas.yview, style="Modern.Vertical.TScrollbar")
        self.pipeline_canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.pipeline_canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.pipeline_canvas.bind("<Double-Button-1>", self.open_pipeline_node)

    def build_health_tab(self) -> None:
        tab = ttk.Frame(self.notebook, style="Main.TFrame", padding=(2, 2))
        self.notebook.add(tab, text="路径健康")
        top = tk.Frame(tab, background=PALETTE["surface"])
        top.pack(fill="x", pady=(0, 16))
        title_copy = tk.Frame(top, background=PALETTE["surface"])
        title_copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_copy,
            text="路径健康",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_copy,
            text="检查代码和记录中的固定路径。MISSING 仅表示当前完整路径未找到，不会自动改脚本。",
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 0))
        tk.Button(
            top,
            text="↻  重新扫描",
            command=self.start_scan,
            background=PALETTE["cyan"],
            foreground="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9, "bold"),
            padx=16,
            pady=9,
        ).pack(side="right", anchor="n")
        frame = tk.Frame(tab, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        frame.pack(fill="both", expand=True)
        columns = ("status", "category", "file", "line", "reference", "message")
        self.health_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "status": "状态",
            "category": "类型",
            "file": "来源文件",
            "line": "行",
            "reference": "引用路径",
            "message": "说明",
        }
        widths = {"status": 85, "category": 80, "file": 380, "line": 55, "reference": 520, "message": 130}
        for col in columns:
            self.health_tree.heading(col, text=headings[col])
            self.health_tree.column(col, width=widths[col], anchor="w")
        self.health_tree.tag_configure("missing", foreground="#b42318")
        self.health_tree.tag_configure("ok", foreground="#216e39")
        self.health_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.health_tree.yview, style="Modern.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.health_tree.xview, style="Modern.Horizontal.TScrollbar")
        hscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.health_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=hscroll.set)
        self.health_tree.bind("<Double-1>", self.open_selected_alert)

    def build_projects_tab(self) -> None:
        tab = ttk.Frame(self.notebook, style="Main.TFrame", padding=(2, 2))
        self.notebook.add(tab, text="项目登记")
        top = tk.Frame(tab, background=PALETTE["surface"])
        top.pack(fill="x", pady=(0, 16))
        title_copy = tk.Frame(top, background=PALETTE["surface"])
        title_copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_copy,
            text="项目登记",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_copy,
            text="查看研究组、状态和真实路径；当前筛选与总览保持同步。",
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 0))
        tk.Button(
            top,
            text="打开项目画布  ↗",
            command=self.open_selected_project,
            background=PALETTE["cyan"],
            foreground="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            cursor="hand2",
            font=(UI_FONT, 9, "bold"),
            padx=16,
            pady=9,
        ).pack(side="right", anchor="n")

        filter_row = tk.Frame(tab, background=PALETTE["surface"])
        filter_row.pack(fill="x", pady=(0, 9))
        tk.Label(
            filter_row,
            text="完整登记表",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 12, "bold"),
        ).pack(side="left")
        search_shell = tk.Frame(filter_row, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        search_shell.pack(side="right")
        tk.Label(search_shell, text="⌕", background=PALETTE["card"], foreground="#98A2B3", font=("Segoe UI Symbol", 12)).pack(side="left", padx=(9, 4))
        tk.Entry(
            search_shell,
            textvariable=self.project_search_var,
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            relief="flat",
            borderwidth=0,
            width=28,
            font=(UI_FONT, 9),
        ).pack(side="left", padx=(0, 8), pady=8)

        frame = tk.Frame(tab, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        frame.pack(fill="both", expand=True)
        columns = ("id", "name", "portfolio", "status", "kind", "exists", "path", "note")
        self.projects_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "id": "ID",
            "name": "项目",
            "portfolio": "研究组",
            "status": "状态",
            "kind": "类型",
            "exists": "存在",
            "path": "实际路径",
            "note": "说明",
        }
        widths = {"id": 190, "name": 230, "portfolio": 150, "status": 95, "kind": 135, "exists": 60, "path": 400, "note": 520}
        for col in columns:
            self.projects_tree.heading(col, text=headings[col])
            self.projects_tree.column(col, width=widths[col], anchor="w")
        self.projects_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.projects_tree.yview, style="Modern.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.projects_tree.xview, style="Modern.Horizontal.TScrollbar")
        hscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.projects_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=hscroll.set)
        self.projects_tree.tag_configure("even", background=PALETTE["card"])
        self.projects_tree.tag_configure("odd", background="#FAFBFD")
        self.projects_tree.bind("<Double-1>", self.open_selected_project)

    def build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook, style="Main.TFrame", padding=(2, 2))
        self.notebook.add(tab, text="扫描记录")
        tk.Label(
            tab,
            text="扫描记录",
            background=PALETTE["surface"],
            foreground=PALETTE["ink"],
            font=(UI_FONT, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tab,
            text="查看最近一次只读扫描的范围、文件数和结果摘要。",
            background=PALETTE["surface"],
            foreground=PALETTE["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 16))
        text_shell = tk.Frame(tab, background=PALETTE["card"], highlightbackground=PALETTE["line"], highlightthickness=1)
        text_shell.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            text_shell,
            height=20,
            wrap="word",
            relief="flat",
            background=PALETTE["card"],
            foreground=PALETTE["ink"],
            insertbackground=PALETTE["ink"],
            font=(MONO_FONT, 9),
            padx=18,
            pady=16,
        )
        self.log_text.pack(fill="both", expand=True)

    def refresh_all(self) -> None:
        self.refresh_projects()
        self.refresh_overview()
        self.refresh_health()
        self.draw_pipeline()
        self.refresh_log()

    def refresh_projects(self) -> None:
        rows = self.filtered_project_rows(self.registry.project_rows())
        if hasattr(self, "projects_tree"):
            for item in self.projects_tree.get_children():
                self.projects_tree.delete(item)
            for index, row in enumerate(rows):
                self.projects_tree.insert(
                    "",
                    "end",
                    iid=row["project_id"],
                    values=(
                        row["project_id"],
                        row["name"],
                        row.get("portfolio", ""),
                        self.display_status(row["status"]),
                        row["kind"],
                        "是" if row.get("exists") else "否",
                        row.get("path", ""),
                        self._project_note_for(row),
                    ),
                    tags=("even" if index % 2 == 0 else "odd",),
                )

    def refresh_overview(self) -> None:
        all_rows = self.registry.project_rows()
        rows = self.filtered_project_rows(all_rows)
        if hasattr(self, "overview_tree"):
            previous = self.overview_tree.selection()
            previous_id = previous[0] if previous else ""
            for item in self.overview_tree.get_children():
                self.overview_tree.delete(item)
            for index, row in enumerate(rows):
                self.overview_tree.insert(
                    "",
                    "end",
                    iid=row["project_id"],
                    values=(
                        row["name"],
                        row.get("portfolio", ""),
                        self.display_status(row["status"]),
                        row["kind"],
                        "已连接" if row.get("exists") else "待核查",
                    ),
                    tags=("even" if index % 2 == 0 else "odd",),
                )
            self.filtered_count_var.set(f"{len(rows)} / {len(all_rows)}")
            visible_ids = {row["project_id"] for row in rows}
            target_id = previous_id if previous_id in visible_ids else (rows[0]["project_id"] if rows else "")
            if target_id:
                self.overview_tree.selection_set(target_id)
                self.overview_tree.focus(target_id)
                self.update_overview_detail()
            elif hasattr(self, "overview_detail_name_var"):
                self.overview_detail_name_var.set("没有符合筛选条件的项目")
                self.overview_detail_meta_var.set("")
                self.overview_detail_path_var.set("")
                self.overview_detail_path = ""
                self.overview_detail_text.configure(state="normal")
                self.overview_detail_text.delete("1.0", "end")
                self.overview_detail_text.configure(state="disabled")
                self.overview_note_edit_button.configure(state="disabled")

        roots = (self.snapshot or {}).get("roots", [])
        files = sum(int(row.get("files", 0)) for row in roots)
        alerts = len((self.snapshot or {}).get("alerts", []))
        research_rows = [row for row in all_rows if row.get("status") != "TOOL"]
        active_statuses = {"ACTIVE", "DRAFT", "OPEN", "SCREENING"}
        active = sum(1 for row in research_rows if row.get("status") in active_statuses)
        self.kpi_vars["projects"].set(str(len(research_rows)))
        self.kpi_vars["active"].set(str(active))
        self.kpi_vars["files"].set(f"{files:,}" if files else "—")
        self.kpi_vars["alerts"].set(str(alerts) if alerts else "—")

    def refresh_health(self) -> None:
        if not hasattr(self, "health_tree"):
            return
        for item in self.health_tree.get_children():
            self.health_tree.delete(item)
        alerts = (self.snapshot or {}).get("alerts", [])
        for index, alert in enumerate(alerts):
            tag = "missing" if alert.get("status") == "MISSING" else "ok"
            self.health_tree.insert(
                "",
                "end",
                iid=f"alert-{index}",
                values=(
                    alert.get("status", ""),
                    alert.get("category", ""),
                    alert.get("file", ""),
                    alert.get("line", ""),
                    alert.get("reference", ""),
                    alert.get("message", ""),
                ),
                tags=(tag,),
            )

    def refresh_log(self) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.delete("1.0", "end")
        if not self.snapshot:
            self.log_text.insert("end", "尚未扫描。点击右上角‘扫描现有目录’开始。\n")
            return
        self.log_text.insert("end", f"扫描时间：{self.snapshot.get('scanned_at', '—')}\n")
        self.log_text.insert("end", "模式：只读；没有执行移动、重命名或修改。\n\n")
        for row in self.snapshot.get("roots", []):
            suffix = "（达到扫描上限，结果可能不完整）" if row.get("truncated") else ""
            self.log_text.insert(
                "end",
                f"{row.get('root')}\n  目录：{row.get('directories', 0):,}  文件：{row.get('files', 0):,}{suffix}\n",
            )
        self.log_text.insert("end", f"\n路径引用记录：{len(self.snapshot.get('alerts', [])):,}\n")

    def draw_pipeline(self) -> None:
        if not hasattr(self, "pipeline_canvas"):
            return
        canvas = self.pipeline_canvas
        canvas.delete("all")
        self.node_paths.clear()
        roots = [safe_resolve(root) for root in self.registry.roots]
        projects_by_root: dict[str, list[dict[str, Any]]] = {}
        for row in self.registry.project_rows():
            root_key = path_key(Path(row["path"]).parent)
            projects_by_root.setdefault(root_key, []).append(row)

        canvas.create_text(65, 42, anchor="w", text="根目录", fill=PALETTE["muted"], font=(UI_FONT, 10, "bold"))
        canvas.create_text(420, 42, anchor="w", text="直接子项目", fill=PALETTE["muted"], font=(UI_FONT, 10, "bold"))
        if not roots:
            canvas.create_text(65, 100, anchor="w", text="请先在侧边栏添加一个本地根目录。", fill=PALETTE["muted"], font=(UI_FONT, 11))

        top = 75
        for root in roots:
            projects = projects_by_root.get(path_key(root), [])
            block_height = max(90, len(projects) * 76)
            root_center = top + block_height / 2
            root_exists = root.is_dir()
            root_fill = PALETTE["cyan_soft"] if root_exists else "#F2F4F7"
            root_outline = PALETTE["cyan"] if root_exists else PALETTE["muted"]
            root_card = canvas.create_rectangle(65, root_center - 32, 315, root_center + 32, fill=root_fill, outline=root_outline, width=2)
            root_label = canvas.create_text(190, root_center - 10, text=root.name or str(root), width=220, fill=PALETTE["ink"], font=(UI_FONT, 11, "bold"))
            root_count = canvas.create_text(190, root_center + 14, text=f"{len(projects)} 个项目" if root_exists else "目录不存在", fill=PALETTE["muted"], font=(UI_FONT, 8))
            for item in (root_card, root_label, root_count):
                self.node_paths[item] = str(root) if root_exists else ""

            if not projects:
                canvas.create_text(420, root_center, anchor="w", text="没有直接子项目", fill=PALETTE["muted"], font=(UI_FONT, 10))
            for index, row in enumerate(projects):
                project_center = top + 38 + index * 76
                canvas.create_line(315, root_center, 420, project_center, fill="#A9BBC8", width=2)
                project_card = canvas.create_rectangle(420, project_center - 27, 825, project_center + 27, fill=PALETTE["card"], outline=PALETTE["line"], width=2)
                project_label = canvas.create_text(438, project_center - 8, anchor="w", text=row["name"], width=365, fill=PALETTE["ink"], font=(UI_FONT, 10, "bold"))
                project_detail = canvas.create_text(438, project_center + 12, anchor="w", text="双击打开项目目录", fill=PALETTE["muted"], font=(UI_FONT, 8))
                for item in (project_card, project_label, project_detail):
                    self.node_paths[item] = str(row["path"])
            top += block_height + 25
        canvas.configure(scrollregion=(0, 0, 1000, max(520, top + 30)))

    def start_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            return
        self.registry.roots = self.current_roots_from_list()
        self.registry.save_roots()
        self.status_var.set("正在只读扫描，请稍候……")
        self.progress_var.set("")
        self.scan_thread = threading.Thread(target=self.scan_worker, daemon=True)
        self.scan_thread.start()

    def scan_worker(self) -> None:
        try:
            snapshot = self.registry.scan(progress=lambda message: self.scan_queue.put(("progress", message)))
            self.scan_queue.put(("done", snapshot))
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.scan_queue.put(("error", repr(exc)))

    def poll_scan_queue(self) -> None:
        try:
            while True:
                kind, payload = self.scan_queue.get_nowait()
                if kind == "progress":
                    self.progress_var.set(str(payload))
                elif kind == "done":
                    self.snapshot = payload
                    self.status_var.set("扫描完成；只读模式，没有修改研究文件")
                    self.progress_var.set("")
                    self.refresh_all()
                elif kind == "error":
                    self.status_var.set("扫描失败；未修改研究文件")
                    self.progress_var.set("")
                    messagebox.showerror("扫描失败", str(payload))
        except queue.Empty:
            pass
        self.after(100, self.poll_scan_queue)

    def current_roots_from_list(self) -> list[str]:
        return [str(self.root_list.get(index)) for index in range(self.root_list.size())]

    def add_root(self) -> None:
        selected = filedialog.askdirectory(title="选择需要观察的研究根目录")
        if selected and path_key(selected) not in {path_key(x) for x in self.current_roots_from_list()}:
            self.root_list.insert("end", str(safe_resolve(selected)))

    def remove_roots(self) -> None:
        selected = list(self.root_list.curselection())
        for index in reversed(selected):
            self.root_list.delete(index)

    def save_roots(self) -> None:
        roots = self.current_roots_from_list()
        if not roots:
            messagebox.showwarning("没有目录", "请至少保留一个监控目录。")
            return
        self.registry.roots = unique_existing_paths(roots)
        self.registry.save_roots()
        self.status_var.set("目录配置已保存；没有修改研究文件")

    def open_path(self, path: str | Path) -> None:
        candidate = safe_resolve(path)
        if not candidate.exists():
            messagebox.showwarning("路径不存在", str(candidate))
            return
        try:
            os.startfile(str(candidate))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开路径", str(exc))

    def open_selected_project(self, _event: Any | None = None) -> None:
        tree = self.overview_tree if self.notebook.index(self.notebook.select()) == 0 else self.projects_tree
        selection = tree.selection()
        if not selection:
            return
        project_id = selection[0]
        rows = self.registry.project_rows()
        row = next((item for item in rows if item["project_id"] == project_id), None)
        if not row or not row.get("path"):
            return
        if not Path(row["path"]).is_dir():
            self.open_path(row["path"])
            return
        existing = self.project_windows.get(project_id)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        self.project_windows[project_id] = ProjectCanvasWindow(self, row, self.project_window_closed)

    def project_window_closed(self, project_id: str) -> None:
        self.project_windows.pop(project_id, None)

    def open_selected_alert(self, _event: Any | None = None) -> None:
        selection = self.health_tree.selection()
        if not selection:
            return
        item = self.health_tree.item(selection[0])
        values = item.get("values", [])
        if values:
            self.open_path(values[2])

    def open_pipeline_node(self, event: tk.Event) -> None:
        item = self.pipeline_canvas.find_withtag("current")
        if not item:
            return
        path = self.node_paths.get(item[0])
        if path:
            self.open_path(path)


def run_self_test() -> int:
    """Exercise scanning against an isolated temporary fixture only."""
    global SNAPSHOT_PATH
    original_snapshot_path = SNAPSHOT_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="research-pipeline-console-test-") as temp_dir:
            root = Path(temp_dir)
            sample_project = root / "sample_project"
            sample_project.mkdir()
            (sample_project / "README.txt").write_text(
                "Synthetic self-test fixture." + os.linesep,
                encoding="utf-8",
            )

            SNAPSHOT_PATH = root / "scan_snapshot.json"
            registry = Registry.__new__(Registry)
            registry.roots = [str(root)]
            snapshot = registry.scan()

            if (
                len(snapshot.get("roots", [])) != 1
                or snapshot["roots"][0].get("files") != 1
                or len(snapshot.get("projects", [])) != 1
                or snapshot["projects"][0].get("name") != "sample_project"
            ):
                print("Self-test failed: isolated scan results did not match the fixture.", file=sys.stderr)
                return 1
            print("Self-test passed using an isolated temporary fixture.")
            return 0
    except Exception as exc:
        print(f"Self-test failed: {type(exc).__name__}.", file=sys.stderr)
        return 1
    finally:
        SNAPSHOT_PATH = original_snapshot_path


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(run_self_test())
    app = ResearchPipelineConsole()
    app.mainloop()
