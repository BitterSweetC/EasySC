from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from urllib.error import HTTPError, URLError

from app.discovery import discover_devices
from app.dlna import DlnaController
from app.http_server import MediaHttpServer
from app.media_source import is_http_url, is_probable_direct_media_url
from app.web_video_extractor import VideoPageExtractionError, VideoPageExtractionUnavailable, resolve_media_source
from app.mirror_server import ScreenMirrorServer
from app.models import DlnaDevice
from app.video_transcoder import FfmpegNotFoundError, prepare_compatible_mp4
from app.screen_capture import (
    CAPTURE_MODE_ACTIVE_WINDOW,
    CAPTURE_MODE_FULL_DESKTOP,
    CAPTURE_MODE_SINGLE_MONITOR,
    MonitorInfo,
    ScreenCaptureConfig,
    get_foreground_window_handle,
    get_window_title,
    list_monitors,
)
from app.windows_projection import (
    ProjectionDiagnostics,
    collect_projection_diagnostics,
    open_connect_panel,
    open_display_settings,
    open_project_settings,
    switch_projection_mode,
)

PAGE_BG = "#F3F5F7"
SURFACE_SOFT = "#F6F8FA"
CARD_BG = "#FFFFFF"
CARD_BORDER = "#D8DFE6"
PRIMARY = "#335C81"
PRIMARY_HOVER = "#274865"
PRIMARY_SOFT = "#ECF1F6"
ACCENT = "#50657A"
ACCENT_SOFT = "#F0F3F6"
WARNING = "#66758A"
WARNING_SOFT = "#F2F4F7"
TEXT_PRIMARY = "#17212B"
TEXT_SECONDARY = "#5E6B78"
TEXT_MUTED = "#8A94A1"

PRESET_LABEL_TO_KEY = {
    "流畅": "Smooth",
    "标准": "Standard",
    "高清": "HD",
}

MODE_META = {
    CAPTURE_MODE_FULL_DESKTOP: {
        "title": "电脑全部投屏",
        "badge": "原生",
        "summary": "调用 Windows 原生无线投放。",
        "detail": "适合整机桌面与系统声音，连接后可直接切换复制或扩展模式。",
        "button": "打开 Win+K 投放",
    },
    CAPTURE_MODE_SINGLE_MONITOR: {
        "title": "投屏一个屏幕",
        "badge": "单屏",
        "summary": "共享指定显示器，适合双屏办公。",
        "detail": "生成镜像接收地址，在电视浏览器打开即可。",
        "button": "开始单屏投屏",
    },
    CAPTURE_MODE_ACTIVE_WINDOW: {
        "title": "投屏当前页面",
        "badge": "窗口",
        "summary": "共享当前前台窗口，适合演示。",
        "detail": "启动后有 3 秒倒计时，请先切到目标页面。",
        "button": "开始当前页面投屏",
    },
}


class ScreenCastingApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("视频投送助手")
        self.geometry("1260x860")
        self.minsize(1120, 760)
        self.configure(bg=PAGE_BG)

        self.devices: list[DlnaDevice] = []
        self.monitor_infos: list[MonitorInfo] = []
        self.choice_cards: dict[str, dict[str, object]] = {}
        self.http_server = MediaHttpServer()
        self.mirror_server = ScreenMirrorServer()
        self.selected_file: Path | None = None
        self.selected_media_url: str | None = None
        self.current_controller: DlnaController | None = None
        self.latest_diagnostics: ProjectionDiagnostics | None = None

        self.status_var = tk.StringVar(value="就绪：请选择本地视频、网络视频直链或视频播放页地址。本地文件会先自动转为电视兼容 MP4，再进行直投或生成兼容播放页。")
        self.header_source_var = tk.StringVar(value="未选择来源")
        self.header_device_var = tk.StringVar(value="未选择电视/盒子")
        self.header_next_step_var = tk.StringVar(value="下一步：选择视频或输入网址。")
        self.os_var = tk.StringVar(value="未检测")
        self.wlan_var = tk.StringVar(value="未检测")
        self.miracast_var = tk.StringVar(value="未检测")
        self.hdcp_var = tk.StringVar(value="未检测")
        self.guidance_var = tk.StringVar(value="点击“检测投放环境”查看当前电脑是否适合直接使用 Win+K 投放。")
        self.box_status_var = tk.StringVar(value="点击“扫描局域网盒子”搜索同一网络中的电视和机顶盒。")
        self.device_count_var = tk.StringVar(value="尚未扫描盒子")
        self.selected_box_var = tk.StringVar(value="未选择盒子")

        self.projection_target_var = tk.StringVar(value=CAPTURE_MODE_FULL_DESKTOP)
        self.mirror_preset_var = tk.StringVar(value="标准")
        self.monitor_label_var = tk.StringVar(value="未检测到屏幕")
        self.mirror_url_var = tk.StringVar(value="当前未启动兼容镜像地址")
        self.capture_note_var = tk.StringVar(value=MODE_META[CAPTURE_MODE_FULL_DESKTOP]["detail"])
        self.selected_window_var = tk.StringVar(value="未锁定页面")
        self.selection_title_var = tk.StringVar(value=MODE_META[CAPTURE_MODE_FULL_DESKTOP]["title"])
        self.selection_badge_var = tk.StringVar(value=MODE_META[CAPTURE_MODE_FULL_DESKTOP]["badge"])
        self.selection_summary_var = tk.StringVar(value=MODE_META[CAPTURE_MODE_FULL_DESKTOP]["summary"])
        self.start_action_text_var = tk.StringVar(value=MODE_META[CAPTURE_MODE_FULL_DESKTOP]["button"])

        self.file_var = tk.StringVar(value="未选择本地视频或网络视频地址")
        self.source_type_var = tk.StringVar(value="未选择来源")
        self.source_tip_var = tk.StringVar(value="先选本地视频，或输入网络视频直链 / 播放页地址。")
        self.speed_var = tk.StringVar(value="1")
        self.volume_var = tk.IntVar(value=50)
        self.volume_text_var = tk.StringVar(value="50%")
        self.media_url_var = tk.StringVar(value="当前未生成兼容视频地址")
        self.player_url_var = tk.StringVar(value="当前未生成兼容播放页地址")

        self.volume_var.trace_add("write", self._update_volume_text)
        self._configure_styles()
        self._build_layout()
        self._refresh_overview_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self.refresh_projection_info)
        self.after(300, self.refresh_monitor_list)
        self.after(900, self.scan_devices)

    def _configure_styles(self) -> None:
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TNotebook", background=PAGE_BG, borderwidth=0)
        style.configure(
            "App.TNotebook.Tab",
            padding=(18, 10),
            font=("Microsoft YaHei UI", 10, "bold"),
            background=SURFACE_SOFT,
            foreground=TEXT_SECONDARY,
        )
        style.map(
            "App.TNotebook.Tab",
            background=[("selected", CARD_BG), ("active", PRIMARY_SOFT)],
            foreground=[("selected", PRIMARY), ("active", TEXT_PRIMARY)],
        )
        style.configure(
            "Primary.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(14, 9),
            background=PRIMARY,
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", PRIMARY_HOVER), ("pressed", PRIMARY_HOVER)],
            foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
        )
        style.configure(
            "HeroPrimary.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(16, 10),
            background=PRIMARY,
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "HeroPrimary.TButton",
            background=[("active", PRIMARY_HOVER), ("pressed", PRIMARY_HOVER)],
            foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Microsoft YaHei UI", 10),
            padding=(13, 9),
            background=CARD_BG,
            foreground=TEXT_PRIMARY,
            bordercolor=CARD_BORDER,
            lightcolor=CARD_BG,
            darkcolor=CARD_BG,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", PRIMARY_SOFT), ("pressed", PRIMARY_SOFT)],
            foreground=[("active", PRIMARY), ("pressed", PRIMARY)],
        )
        style.configure(
            "Soft.TCombobox",
            padding=8,
            fieldbackground=SURFACE_SOFT,
            background=SURFACE_SOFT,
            foreground=TEXT_PRIMARY,
            bordercolor=CARD_BORDER,
            arrowsize=13,
        )
        style.map(
            "Soft.TCombobox",
            fieldbackground=[("readonly", SURFACE_SOFT)],
            selectbackground=[("readonly", SURFACE_SOFT)],
            selectforeground=[("readonly", TEXT_PRIMARY)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=SURFACE_SOFT,
            troughcolor=SURFACE_SOFT,
            bordercolor=SURFACE_SOFT,
            arrowcolor=TEXT_SECONDARY,
        )
        style.configure("TScale", background=SURFACE_SOFT)

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = self._create_card(self, padx=22, pady=20)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 14))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title_block = tk.Frame(header, bg=CARD_BG)
        title_block.grid(row=0, column=0, sticky="ew")
        tk.Label(title_block, text="视频投送助手", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 22, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            title_block,
            text="整合网页视频、本地视频和兼容投屏，优先展示当前状态与下一步操作。",
            bg=CARD_BG,
            fg=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 10),
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        hero_actions = tk.Frame(header, bg=CARD_BG)
        hero_actions.grid(row=0, column=1, sticky="ne", padx=(18, 0))
        ttk.Button(hero_actions, text="选择视频", command=self.choose_file, style="HeroPrimary.TButton").grid(row=0, column=0, padx=(0, 10))
        ttk.Button(hero_actions, text="输入网址", command=self.use_media_url, style="Secondary.TButton").grid(row=0, column=1, padx=(0, 10))
        ttk.Button(hero_actions, text="扫描设备", command=self.scan_devices, style="Secondary.TButton").grid(row=0, column=2)

        overview_row = tk.Frame(header, bg=CARD_BG)
        overview_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        for column in range(3):
            overview_row.columnconfigure(column, weight=1)
        self._create_summary_card(overview_row, 0, "当前来源", self.header_source_var, tone="primary")
        self._create_summary_card(overview_row, 1, "当前设备", self.header_device_var, tone="accent")
        self._create_summary_card(overview_row, 2, "下一步建议", self.header_next_step_var, tone="warning")

        status_strip = tk.Frame(header, bg=SURFACE_SOFT, padx=16, pady=14, highlightbackground=CARD_BORDER, highlightthickness=1)
        status_strip.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        status_strip.columnconfigure(0, weight=1)
        tk.Label(status_strip, text="当前状态", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            status_strip,
            textvariable=self.status_var,
            bg=SURFACE_SOFT,
            fg=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 10),
            wraplength=1120,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        notebook = ttk.Notebook(self, style="App.TNotebook")
        notebook.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 20))

        projection_tab = tk.Frame(notebook, bg=PAGE_BG)
        media_tab = tk.Frame(notebook, bg=PAGE_BG)
        projection_tab.columnconfigure(0, weight=1)
        projection_tab.rowconfigure(2, weight=1)
        media_tab.columnconfigure(0, weight=1)
        media_tab.rowconfigure(1, weight=1)

        notebook.add(media_tab, text="视频投送")
        notebook.add(projection_tab, text="兼容投屏")

        self._build_media_tab(media_tab)
        self._build_projection_tab(projection_tab)

    def _create_section_title(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        tk.Label(parent, text=title, bg=PAGE_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(parent, text=subtitle, bg=PAGE_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=1080, justify="left").pack(anchor="w", pady=(4, 0))

    def _create_card(self, parent: tk.Widget, *, padx: int = 18, pady: int = 18) -> tk.Frame:
        return tk.Frame(parent, bg=CARD_BG, padx=padx, pady=pady, highlightbackground=CARD_BORDER, highlightthickness=1)

    def _create_summary_card(self, parent: tk.Widget, column: int, title: str, variable: tk.StringVar, *, tone: str) -> None:
        tone_map = {
            "primary": PRIMARY,
            "accent": ACCENT,
            "warning": WARNING,
        }
        card = self._create_card(parent, padx=16, pady=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        tk.Frame(card, bg=tone_map[tone], height=3).pack(fill="x")
        tk.Label(card, text=title, bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", pady=(12, 0))
        tk.Label(card, textvariable=variable, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 11, "bold"), wraplength=300, justify="left").pack(anchor="w", pady=(10, 0))

    def _create_listbox(self, parent: tk.Widget, *, height: int = 7) -> tk.Listbox:
        return tk.Listbox(
            parent,
            exportselection=False,
            relief="flat",
            borderwidth=0,
            height=height,
            font=("Microsoft YaHei UI", 10),
            bg=SURFACE_SOFT,
            fg=TEXT_PRIMARY,
            selectbackground=PRIMARY,
            selectforeground="#FFFFFF",
            activestyle="none",
            highlightthickness=0,
        )

    def _update_volume_text(self, *_args: object) -> None:
        self.volume_text_var.set(f"{int(self.volume_var.get())}%")

    def _truncate_text(self, value: str, limit: int = 54) -> str:
        return value if len(value) <= limit else f"{value[:limit - 1]}…"

    def _refresh_overview_state(self) -> None:
        if self.selected_file is not None:
            self.header_source_var.set(f"本地文件 · {self.selected_file.name}")
            self.source_type_var.set("本地视频文件")
            self.source_tip_var.set("开始投送或生成播放页时会自动转为兼容 MP4。")
        elif self.selected_media_url:
            display = self._truncate_text(self.selected_media_url)
            if is_probable_direct_media_url(self.selected_media_url):
                self.header_source_var.set(f"网络直链 · {display}")
                self.source_type_var.set("网络视频直链")
                self.source_tip_var.set("可以直接尝试 DLNA 推送，也可以先生成兼容播放页。")
            else:
                self.header_source_var.set(f"播放页地址 · {display}")
                self.source_type_var.set("网页播放页地址")
                self.source_tip_var.set("开始投送时会先解析真实视频流；如站点限制较强，可优先测试兼容播放页。")
        else:
            self.header_source_var.set("未选择来源")
            self.source_type_var.set("未选择来源")
            self.source_tip_var.set("先选本地视频，或输入网络视频直链 / 播放页地址。")

        if self.device_count_var.get() == "扫描中…":
            self.header_device_var.set("正在扫描电视/盒子…")
            selected_device = None
        else:
            selected_device = self._get_selected_device()
            if selected_device is not None:
                suffix = "可直接投送" if selected_device.supports_media_cast else "建议使用兼容播放页"
                self.header_device_var.set(f"{selected_device.display_name} · {suffix}")
            elif self.devices:
                self.header_device_var.set(f"已发现 {len(self.devices)} 台设备，等待选择")
            else:
                self.header_device_var.set("未选择电视/盒子")

        if self.current_controller is not None:
            next_step = "视频正在投送中，可复制兼容地址，或点击停止。"
        elif self.mirror_url_var.get().startswith("http://"):
            next_step = "兼容镜像已启动，请在电视浏览器中打开接收地址。"
        elif self.player_url_var.get().startswith("http://"):
            next_step = "兼容播放页已就绪，可复制到电视浏览器继续播放。"
        elif self.selected_file is None and not self.selected_media_url:
            next_step = "下一步：选择视频或输入网址。"
        elif selected_device is None:
            next_step = "下一步：扫描并选择电视 / 盒子。"
        elif not selected_device.supports_media_cast:
            next_step = "当前设备不支持 DLNA 直投，建议生成兼容播放页。"
        else:
            next_step = "可以开始 DLNA 推送，或先生成兼容播放页。"
        self.header_next_step_var.set(next_step)

    def _build_projection_tab(self, parent: tk.Frame) -> None:
        cards_section = tk.Frame(parent, bg=PAGE_BG)
        cards_section.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        cards_section.columnconfigure(0, weight=1)
        self._create_section_title(cards_section, "兼容投屏模式", "选择模式后开始投屏。")

        cards_grid = tk.Frame(cards_section, bg=PAGE_BG)
        cards_grid.pack(fill="x", pady=(14, 0))
        for column in range(3):
            cards_grid.columnconfigure(column, weight=1)

        self._create_choice_card(cards_grid, CAPTURE_MODE_FULL_DESKTOP, 0)
        self._create_choice_card(cards_grid, CAPTURE_MODE_SINGLE_MONITOR, 1)
        self._create_choice_card(cards_grid, CAPTURE_MODE_ACTIVE_WINDOW, 2)

        middle = tk.Frame(parent, bg=PAGE_BG)
        middle.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=2)

        control_card = self._create_card(middle, padx=20, pady=20)
        control_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        control_card.columnconfigure(1, weight=1)

        head = tk.Frame(control_card, bg=CARD_BG)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(1, weight=1)
        tk.Label(head, textvariable=self.selection_badge_var, bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4).grid(row=0, column=0, sticky="w")
        tk.Label(head, textvariable=self.selection_title_var, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Label(head, textvariable=self.selection_summary_var, bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=620, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        form_panel = tk.Frame(control_card, bg=SURFACE_SOFT, padx=16, pady=16)
        form_panel.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        form_panel.columnconfigure(1, weight=1)
        tk.Label(form_panel, text="屏幕选择", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.monitor_combo = ttk.Combobox(form_panel, textvariable=self.monitor_label_var, state="readonly", width=40, style="Soft.TCombobox")
        self.monitor_combo.grid(row=0, column=1, sticky="ew")

        tk.Label(form_panel, text="镜像画质", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=1, column=0, sticky="w", pady=(14, 0))
        self.preset_combo = ttk.Combobox(form_panel, textvariable=self.mirror_preset_var, state="readonly", values=list(PRESET_LABEL_TO_KEY.keys()), width=18, style="Soft.TCombobox")
        self.preset_combo.grid(row=1, column=1, sticky="w", pady=(14, 0))

        tk.Label(form_panel, text="当前页面", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=2, column=0, sticky="nw", pady=(14, 0))
        tk.Label(form_panel, textvariable=self.selected_window_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=560, justify="left").grid(row=2, column=1, sticky="w", pady=(14, 0))

        tk.Label(form_panel, text="接收地址", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=3, column=0, sticky="nw", pady=(14, 0))
        tk.Label(form_panel, textvariable=self.mirror_url_var, bg=SURFACE_SOFT, fg=PRIMARY, font=("Microsoft YaHei UI", 10, "bold"), wraplength=560, justify="left").grid(row=3, column=1, sticky="w", pady=(14, 0))

        note_card = tk.Frame(control_card, bg=SURFACE_SOFT, padx=14, pady=12)
        note_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        tk.Label(note_card, text="说明", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(note_card, textvariable=self.capture_note_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=700, justify="left").pack(anchor="w", pady=(6, 0))

        ttk.Button(control_card, textvariable=self.start_action_text_var, command=self.start_selected_projection, style="HeroPrimary.TButton").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))

        action_row = tk.Frame(control_card, bg=CARD_BG)
        action_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for idx in range(3):
            action_row.columnconfigure(idx, weight=1)
        ttk.Button(action_row, text="刷新屏幕列表", command=self.refresh_monitor_list, style="Secondary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(action_row, text="复制接收地址", command=self.copy_mirror_url, style="Secondary.TButton").grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(action_row, text="停止兼容镜像", command=self.stop_mirror, style="Secondary.TButton").grid(row=0, column=2, sticky="ew")

        right_column = tk.Frame(middle, bg=PAGE_BG)
        right_column.grid(row=0, column=1, sticky="nsew")
        right_column.columnconfigure(0, weight=1)
        right_column.rowconfigure(1, weight=1)

        status_card = self._create_card(right_column)
        status_card.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        tk.Label(status_card, text="投放环境概览", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(status_card, text="集中展示 Miracast 相关判断信息。", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=320, justify="left").pack(anchor="w", pady=(6, 14))
        self._create_info_row(status_card, "系统", self.os_var)
        self._create_info_row(status_card, "无线网卡", self.wlan_var)
        self._create_info_row(status_card, "Miracast", self.miracast_var)
        self._create_info_row(status_card, "HDCP", self.hdcp_var)
        self._create_info_row(status_card, "建议", self.guidance_var, wraplength=320)

        box_card = self._create_card(right_column)
        box_card.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        box_card.columnconfigure(0, weight=1)
        box_card.rowconfigure(2, weight=1)
        box_head = tk.Frame(box_card, bg=CARD_BG)
        box_head.grid(row=0, column=0, sticky="ew")
        box_head.columnconfigure(0, weight=1)
        tk.Label(box_head, text="局域网电视 / 盒子", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(box_head, text="扫描设备", command=self.scan_devices, style="Secondary.TButton").grid(row=0, column=1, sticky="e")
        tk.Label(box_card, text="Win+K 不可用时，仍可尝试兼容镜像或媒体投送。", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=320, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))

        list_frame = tk.Frame(box_card, bg=CARD_BG)
        list_frame.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.home_device_list = self._create_listbox(list_frame, height=7)
        self.home_device_list.grid(row=0, column=0, sticky="nsew")
        self.home_device_list.bind("<<ListboxSelect>>", self._on_home_device_select)
        home_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.home_device_list.yview)
        home_scrollbar.grid(row=0, column=1, sticky="ns")
        self.home_device_list.configure(yscrollcommand=home_scrollbar.set)

        quick_card = self._create_card(right_column)
        quick_card.grid(row=2, column=0, sticky="nsew")
        tk.Label(quick_card, text="系统入口", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(quick_card, text="快速打开系统投放相关入口。", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=320, justify="left").pack(anchor="w", pady=(6, 14))
        ttk.Button(quick_card, text="检测投放环境", command=self.refresh_projection_info, style="Secondary.TButton").pack(fill="x")
        ttk.Button(quick_card, text="打开 Win+K 投放面板", command=self.open_connect_panel, style="Primary.TButton").pack(fill="x", pady=(10, 0))
        ttk.Button(quick_card, text="打开显示设置", command=self.open_display_settings, style="Secondary.TButton").pack(fill="x", pady=(10, 0))
        ttk.Button(quick_card, text="打开“投影到此电脑”设置", command=self.open_project_settings, style="Secondary.TButton").pack(fill="x", pady=(10, 0))

        bottom = tk.Frame(parent, bg=PAGE_BG)
        bottom.grid(row=2, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        bottom.rowconfigure(0, weight=1)

        diag_card = self._create_card(bottom)
        diag_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(diag_card, text="诊断详情", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(diag_card, text="保留原始检测摘要，便于排查。", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=640, justify="left").pack(anchor="w", pady=(6, 12))
        self.diag_text = scrolledtext.ScrolledText(diag_card, wrap="word", height=16, relief="flat", borderwidth=0, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Consolas", 10))
        self.diag_text.pack(fill="both", expand=True)
        self.diag_text.insert("1.0", "检测结果会显示在这里。\n\n推荐操作：\n1. 电脑全部投屏：点击 Win+K 连接电视。\n2. 投屏一个屏幕：生成接收地址，在电视浏览器打开。\n3. 投屏当前页面：点击开始后 3 秒内切到目标页面。")
        self.diag_text.configure(state="disabled")

        guide_card = self._create_card(bottom)
        guide_card.grid(row=0, column=1, sticky="nsew")
        tk.Label(guide_card, text="使用建议", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        for step in [
            "先点“检测投放环境”，确认 Miracast 是否可用。",
            "电视支持无线显示时，优先使用“电脑全部投屏”。",
            "只共享某块显示器时，选择“投屏一个屏幕”。",
            "只演示某个应用窗口时，选择“投屏当前页面”。",
            "兼容镜像模式启动后，在电视浏览器中打开接收地址。",
        ]:
            line = tk.Frame(guide_card, bg=CARD_BG)
            line.pack(fill="x", pady=(10, 0))
            tk.Label(line, text="•", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", anchor="n")
            tk.Label(line, text=step, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=300, justify="left").pack(side="left", padx=(8, 0))

        self._on_projection_target_changed()

    def _create_choice_card(self, parent: tk.Widget, mode: str, column: int) -> None:
        meta = MODE_META[mode]
        card = self._create_card(parent, padx=16, pady=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        parent.columnconfigure(column, weight=1)

        badge = tk.Label(card, text=meta["badge"], bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4)
        badge.pack(anchor="w")
        title = tk.Label(card, text=meta["title"], bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold"))
        title.pack(anchor="w", pady=(14, 0))
        desc = tk.Label(card, text=meta["summary"], bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=280, justify="left")
        desc.pack(anchor="w", pady=(10, 0))
        hint = tk.Label(card, text=meta["detail"], bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=280, justify="left")
        hint.pack(anchor="w", pady=(12, 0))
        button = ttk.Button(card, text="选择此模式", command=lambda value=mode: self.select_projection_target(value), style="Secondary.TButton")
        button.pack(fill="x", pady=(16, 0))

        self.choice_cards[mode] = {"frame": card, "button": button, "badge": badge, "title": title, "desc": desc, "hint": hint}
        self._bind_click_recursive(card, mode)

    def _bind_click_recursive(self, widget: tk.Widget, mode: str) -> None:
        widget.bind("<Button-1>", lambda event, value=mode: self.select_projection_target(value))
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                continue
            self._bind_click_recursive(child, mode)

    def _create_info_row(self, parent: tk.Widget, label: str, variable: tk.StringVar, *, wraplength: int = 340) -> None:
        container = tk.Frame(parent, bg=CARD_BG)
        container.pack(fill="x", pady=(10, 0))
        tk.Label(container, text=label, bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold"), width=8, anchor="w").pack(side="left")
        tk.Label(container, textvariable=variable, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=wraplength, justify="left").pack(side="left", fill="x", expand=True)


    def _device_list_widgets(self) -> list[tk.Listbox]:
        widgets: list[tk.Listbox] = []
        for attr in ("home_device_list", "device_list"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widgets.append(widget)
        return widgets

    def _set_device_selection(self, index: int | None) -> None:
        for widget in self._device_list_widgets():
            widget.selection_clear(0, tk.END)

        if index is None or index < 0 or index >= len(self.devices):
            self.selected_box_var.set("未选择盒子")
            if self.devices:
                self.box_status_var.set("请选择一台盒子后再进行媒体投屏。")
            else:
                self.box_status_var.set("未发现支持 DLNA/UPnP 的盒子。你可以改用“生成兼容播放页”，在电视浏览器中打开。")
            self._refresh_overview_state()
            return

        device = self.devices[index]
        for widget in self._device_list_widgets():
            widget.selection_set(index)
            widget.activate(index)
            widget.see(index)
        self.selected_box_var.set(device.display_name)
        if device.supports_media_cast:
            self.box_status_var.set("已识别到可用于 DLNA 媒体投屏的盒子。")
        else:
            self.box_status_var.set("已发现盒子，但未识别到 DLNA 播放控制服务。你可以改用电视浏览器播放页。")
        self._refresh_overview_state()

    def _sync_device_views(self, preferred_index: int | None = None) -> None:
        names = [device.display_name for device in self.devices]
        for widget in self._device_list_widgets():
            widget.delete(0, tk.END)
            for name in names:
                widget.insert(tk.END, name)

        if names:
            self.device_count_var.set(f"已发现 {len(names)} 台盒子/电视")
            if preferred_index is None or preferred_index < 0 or preferred_index >= len(names):
                preferred_index = 0
            self._set_device_selection(preferred_index)
        else:
            self.device_count_var.set("未发现盒子/电视")
            self._set_device_selection(None)

    def _handle_device_select(self, widget: tk.Listbox) -> None:
        selection = widget.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.devices):
            return
        self._set_device_selection(index)

    def _on_home_device_select(self, event=None) -> None:
        if hasattr(self, "home_device_list"):
            self._handle_device_select(self.home_device_list)

    def _on_media_device_select(self, event=None) -> None:
        if hasattr(self, "device_list"):
            self._handle_device_select(self.device_list)

    def _build_media_tab(self, parent: tk.Frame) -> None:
        banner = self._create_card(parent, padx=22, pady=18)
        banner.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        tk.Label(banner, text="视频投送", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(
            banner,
            text="设备选择、来源处理和兼容地址统一放在一个工作区内。",
            bg=CARD_BG,
            fg=TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 10),
            wraplength=1060,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        banner_row = tk.Frame(banner, bg=CARD_BG)
        banner_row.pack(fill="x", pady=(16, 0))
        banner_row.columnconfigure(0, weight=1)
        banner_row.columnconfigure(1, weight=1)

        source_banner = tk.Frame(banner_row, bg=SURFACE_SOFT, padx=14, pady=12)
        source_banner.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(source_banner, text="当前来源", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(source_banner, textvariable=self.source_type_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 11, "bold"), justify="left").pack(anchor="w", pady=(10, 0))
        tk.Label(source_banner, textvariable=self.source_tip_var, bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=500, justify="left").pack(anchor="w", pady=(6, 0))

        action_banner = tk.Frame(banner_row, bg=SURFACE_SOFT, padx=14, pady=12)
        action_banner.grid(row=0, column=1, sticky="nsew")
        tk.Label(action_banner, text="下一步", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(action_banner, textvariable=self.header_next_step_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 11, "bold"), wraplength=500, justify="left").pack(anchor="w", pady=(10, 0))
        tk.Label(action_banner, text="DLNA 不稳定时，可改用兼容播放页。", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=500, justify="left").pack(anchor="w", pady=(6, 0))

        content = tk.Frame(parent, bg=PAGE_BG)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=5)
        content.columnconfigure(1, weight=6)
        content.rowconfigure(0, weight=1)

        device_card = self._create_card(content, padx=20, pady=20)
        device_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        device_card.columnconfigure(0, weight=1)
        device_card.rowconfigure(3, weight=1)

        device_head = tk.Frame(device_card, bg=CARD_BG)
        device_head.grid(row=0, column=0, sticky="ew")
        device_head.columnconfigure(0, weight=1)
        tk.Label(device_head, text="设备列表", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(device_head, text="重新扫描", command=self.scan_devices, style="Secondary.TButton").grid(row=0, column=1, sticky="e")
        tk.Label(device_card, text="自动发现同一局域网内支持 DLNA / UPnP 的设备。", bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=520, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))

        device_meta = tk.Frame(device_card, bg=CARD_BG)
        device_meta.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tk.Label(device_meta, textvariable=self.device_count_var, bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4).pack(side="left")
        tk.Label(device_meta, textvariable=self.selected_box_var, bg=CARD_BG, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9), wraplength=320, justify="right").pack(side="right")

        list_frame = tk.Frame(device_card, bg=CARD_BG)
        list_frame.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.device_list = self._create_listbox(list_frame, height=11)
        self.device_list.grid(row=0, column=0, sticky="nsew")
        self.device_list.bind("<<ListboxSelect>>", self._on_media_device_select)
        device_scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.device_list.yview)
        device_scrollbar.grid(row=0, column=1, sticky="ns")
        self.device_list.configure(yscrollcommand=device_scrollbar.set)

        device_note = tk.Frame(device_card, bg=SURFACE_SOFT, padx=12, pady=12)
        device_note.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        tk.Label(device_note, text="当前状态", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(device_note, textvariable=self.box_status_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=520, justify="left").pack(anchor="w", pady=(6, 0))

        control_card = self._create_card(content, padx=20, pady=20)
        control_card.grid(row=0, column=1, sticky="nsew")
        control_card.columnconfigure(0, weight=1)

        tk.Label(control_card, text="来源与操作", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")

        source_panel = tk.Frame(control_card, bg=SURFACE_SOFT, padx=16, pady=16)
        source_panel.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        source_panel.columnconfigure(0, weight=1)
        source_panel.columnconfigure(1, weight=0)
        tk.Label(source_panel, text="当前来源", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(source_panel, textvariable=self.source_type_var, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4).grid(row=0, column=1, sticky="e")
        tk.Label(source_panel, textvariable=self.file_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10, "bold"), wraplength=540, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        tk.Label(source_panel, textvariable=self.source_tip_var, bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 10), wraplength=540, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        button_row = tk.Frame(control_card, bg=CARD_BG)
        button_row.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)
        ttk.Button(button_row, text="选择本地视频", command=self.choose_file, style="Primary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(button_row, text="输入网络网址", command=self.use_media_url, style="Secondary.TButton").grid(row=0, column=1, sticky="ew")

        options_row = tk.Frame(control_card, bg=CARD_BG)
        options_row.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        options_row.columnconfigure(0, weight=1)
        options_row.columnconfigure(1, weight=1)

        speed_card = tk.Frame(options_row, bg=SURFACE_SOFT, padx=14, pady=12)
        speed_card.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tk.Label(speed_card, text="播放速度", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        ttk.Combobox(speed_card, textvariable=self.speed_var, state="readonly", values=["1", "1.25", "1.5", "2"], width=12, style="Soft.TCombobox").pack(anchor="w", pady=(8, 0))

        volume_card = tk.Frame(options_row, bg=SURFACE_SOFT, padx=14, pady=12)
        volume_card.grid(row=0, column=1, sticky="ew")
        head_row = tk.Frame(volume_card, bg=SURFACE_SOFT)
        head_row.pack(fill="x")
        tk.Label(head_row, text="音量", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        tk.Label(head_row, textvariable=self.volume_text_var, bg=SURFACE_SOFT, fg=TEXT_PRIMARY, font=("Microsoft YaHei UI", 10, "bold")).pack(side="right")
        ttk.Scale(volume_card, from_=0, to=100, variable=self.volume_var, orient="horizontal").pack(fill="x", pady=(8, 0))

        urls_card = tk.Frame(control_card, bg=SURFACE_SOFT, padx=16, pady=16)
        urls_card.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        urls_card.columnconfigure(0, weight=1)
        urls_card.columnconfigure(1, weight=0)
        tk.Label(urls_card, text="兼容视频地址", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Button(urls_card, text="复制视频地址", command=self.copy_media_url, style="Secondary.TButton").grid(row=0, column=1, sticky="e")
        tk.Label(urls_card, textvariable=self.media_url_var, bg=SURFACE_SOFT, fg=PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=520, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(urls_card, text="兼容播放页地址", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).grid(row=2, column=0, sticky="w", pady=(14, 0))
        ttk.Button(urls_card, text="复制播放页", command=self.copy_player_url, style="Secondary.TButton").grid(row=2, column=1, sticky="e", pady=(14, 0))
        tk.Label(urls_card, textvariable=self.player_url_var, bg=SURFACE_SOFT, fg=PRIMARY, font=("Microsoft YaHei UI", 10), wraplength=520, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        action_row = tk.Frame(control_card, bg=CARD_BG)
        action_row.grid(row=5, column=0, sticky="ew", pady=(18, 0))
        action_row.columnconfigure(0, weight=1)
        action_row.columnconfigure(1, weight=1)
        ttk.Button(action_row, text="开始 DLNA 推送", command=self.cast_media, style="HeroPrimary.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(action_row, text="生成兼容播放页", command=self.prepare_video_delivery, style="Secondary.TButton").grid(row=0, column=1, sticky="ew")
        ttk.Button(action_row, text="停止视频投送", command=self.stop_casting, style="Secondary.TButton").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        note = tk.Frame(control_card, bg=SURFACE_SOFT, padx=12, pady=12)
        note.grid(row=6, column=0, sticky="ew", pady=(18, 0))
        tk.Label(note, text="说明", bg=SURFACE_SOFT, fg=TEXT_SECONDARY, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(
            note,
            text="支持输入视频播放页地址。程序会优先解析真实视频流，再按 DLNA 方式直接推送到盒子；如果站点启用了 DRM、登录校验或强加密，解析可能失败。",
            bg=SURFACE_SOFT,
            fg=TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 10),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def select_projection_target(self, target: str) -> None:
        if self.projection_target_var.get() == target:
            return
        self.projection_target_var.set(target)
        self._on_projection_target_changed()

    def _on_projection_target_changed(self) -> None:
        target = self.projection_target_var.get()
        meta = MODE_META[target]
        self.selection_title_var.set(meta["title"])
        self.selection_badge_var.set(meta["badge"])
        self.selection_summary_var.set(meta["summary"])
        self.start_action_text_var.set(meta["button"])
        self.capture_note_var.set(meta["detail"])

        self._refresh_choice_card_styles()

        if target == CAPTURE_MODE_FULL_DESKTOP:
            self.monitor_combo.configure(state="disabled")
            self.preset_combo.configure(state="disabled")
        elif target == CAPTURE_MODE_SINGLE_MONITOR:
            self.monitor_combo.configure(state="readonly")
            self.preset_combo.configure(state="readonly")
        else:
            self.monitor_combo.configure(state="disabled")
            self.preset_combo.configure(state="readonly")

        self._refresh_overview_state()

    def _refresh_choice_card_styles(self) -> None:
        selected = self.projection_target_var.get()
        for mode, refs in self.choice_cards.items():
            frame = refs["frame"]
            button = refs["button"]
            badge = refs["badge"]
            title = refs["title"]
            desc = refs["desc"]
            hint = refs["hint"]
            is_selected = mode == selected
            bg = SURFACE_SOFT if is_selected else CARD_BG
            border = PRIMARY if is_selected else CARD_BORDER
            badge_bg = PRIMARY_SOFT if is_selected else SURFACE_SOFT
            badge_fg = PRIMARY if is_selected else TEXT_SECONDARY
            title_fg = TEXT_PRIMARY
            desc_fg = TEXT_PRIMARY if is_selected else TEXT_SECONDARY
            frame.configure(bg=bg, highlightbackground=border, highlightcolor=border)
            badge.configure(bg=badge_bg, fg=badge_fg)
            title.configure(bg=bg, fg=title_fg)
            desc.configure(bg=bg, fg=desc_fg)
            hint.configure(bg=bg, fg=TEXT_SECONDARY)
            button.configure(style="Primary.TButton" if is_selected else "Secondary.TButton", text="当前模式" if is_selected else "选中此模式")

    def refresh_projection_info(self) -> None:
        self.status_var.set("正在检测 Windows 投放环境…")
        threading.Thread(target=self._projection_worker, daemon=True).start()

    def _projection_worker(self) -> None:
        diagnostics = collect_projection_diagnostics()
        self.after(0, lambda: self._apply_diagnostics(diagnostics))

    def _apply_diagnostics(self, diagnostics: ProjectionDiagnostics) -> None:
        self.latest_diagnostics = diagnostics
        self.os_var.set(diagnostics.os_version or "未知")
        wlan_text = diagnostics.wlan_interface or "未检测到 WLAN 接口"
        if diagnostics.netsh_summary:
            wlan_text = f"{wlan_text}；{diagnostics.netsh_summary}"
        self.wlan_var.set(wlan_text)

        miracast_parts = ["可用" if diagnostics.miracast_available is True else "不可用" if diagnostics.miracast_available is False else "未知"]
        if diagnostics.miracast_summary:
            miracast_parts.append(diagnostics.miracast_summary)
        self.miracast_var.set("；".join(miracast_parts))

        if diagnostics.hdcp_supported is True:
            self.hdcp_var.set("支持")
        elif diagnostics.hdcp_supported is False:
            self.hdcp_var.set("不支持或未启用")
        else:
            self.hdcp_var.set("未知")

        guidance = "\n".join(diagnostics.warnings) if diagnostics.warnings else "未发现明显阻塞，可以直接尝试 Win+K。"
        self.guidance_var.set(guidance)
        self.status_var.set("投放环境检测完成。")

        blocks = ["== 推荐结论 ==", guidance, "", "== netsh wlan show drivers ==", diagnostics.raw_netsh or "无输出", "", "== dxdiag Miracast ==", diagnostics.miracast_summary or "未解析到 Miracast 行"]
        self.diag_text.configure(state="normal")
        self.diag_text.delete("1.0", tk.END)
        self.diag_text.insert("1.0", "\n".join(blocks))
        self.diag_text.configure(state="disabled")

    def refresh_monitor_list(self) -> None:
        self.monitor_infos = list_monitors()
        values = [item.label for item in self.monitor_infos]
        self.monitor_combo.configure(values=values)
        if values:
            self.monitor_combo.current(0)
            self.monitor_label_var.set(values[0])
            self.status_var.set(f"已检测到 {len(values)} 个屏幕。")
        else:
            self.monitor_label_var.set("未检测到屏幕")
            self.status_var.set("未检测到可选屏幕。")

    def start_full_desktop_projection(self) -> None:
        self.select_projection_target(CAPTURE_MODE_FULL_DESKTOP)
        self.start_selected_projection()

    def start_single_monitor_projection(self) -> None:
        self.select_projection_target(CAPTURE_MODE_SINGLE_MONITOR)
        self.start_selected_projection()

    def start_active_window_projection(self) -> None:
        self.select_projection_target(CAPTURE_MODE_ACTIVE_WINDOW)
        self.start_selected_projection()
    def start_selected_projection(self) -> None:
        target = self.projection_target_var.get()
        if target == CAPTURE_MODE_FULL_DESKTOP:
            self.open_connect_panel(show_result_dialog=True)
            return
        if target == CAPTURE_MODE_SINGLE_MONITOR:
            self._start_single_monitor_mirror()
            return
        self._start_active_window_mirror()

    def _selected_preset_key(self) -> str:
        return PRESET_LABEL_TO_KEY.get(self.mirror_preset_var.get(), "Standard")

    def _start_single_monitor_mirror(self) -> None:
        if not self.monitor_infos:
            self.refresh_monitor_list()
        if not self.monitor_infos:
            messagebox.showwarning("没有可用屏幕", "未检测到可以投屏的屏幕。")
            return
        selected_index = max(0, self.monitor_combo.current())
        monitor = self.monitor_infos[selected_index]
        config = ScreenCaptureConfig.from_preset(self._selected_preset_key(), capture_mode=CAPTURE_MODE_SINGLE_MONITOR, monitor_index=monitor.index)
        threading.Thread(target=self._start_mirror_worker, args=(config, f"屏幕 {monitor.index + 1}"), daemon=True).start()

    def _start_active_window_mirror(self) -> None:
        self.status_var.set("3 秒后开始捕获当前页面，请立即切换到目标页面…")
        self.iconify()
        threading.Thread(target=self._active_window_worker, daemon=True).start()

    def _active_window_worker(self) -> None:
        try:
            for remaining in [3, 2, 1]:
                self.after(0, lambda r=remaining: self.status_var.set(f"{r} 秒后开始捕获当前页面，请切换到目标页面…"))
                time.sleep(1)
            window_handle = get_foreground_window_handle()
            window_title = get_window_title(window_handle)
            config = ScreenCaptureConfig.from_preset(self._selected_preset_key(), capture_mode=CAPTURE_MODE_ACTIVE_WINDOW, window_handle=window_handle, window_title=window_title)
            self._start_mirror_worker(config, f"当前页面：{window_title}")
        except Exception as exc:
            self.after(0, lambda error=str(exc): self._apply_active_window_start_failed(error))
        finally:
            self.after(0, self.deiconify)

    def _start_mirror_worker(self, config: ScreenCaptureConfig, target_label: str) -> None:
        self.after(0, lambda: self.status_var.set(f"正在启动兼容镜像：{target_label}…"))
        try:
            url = self.mirror_server.start(config)
        except Exception as exc:
            self.after(0, lambda error=str(exc), label=target_label: self._apply_mirror_start_failed(label, error))
            return
        self.after(0, lambda started_url=url, started_config=config, label=target_label: self._apply_mirror_started(started_config, label, started_url))

    def _apply_mirror_started(self, config: ScreenCaptureConfig, target_label: str, url: str) -> None:
        self.mirror_url_var.set(url)
        self.selected_window_var.set(config.window_title or "未锁定页面")
        self.status_var.set(f"兼容镜像已启动：{target_label}。请在电视浏览器中打开接收地址。")
        self._refresh_overview_state()
        messagebox.showinfo(
            "投屏启动成功",
            f"{target_label} 已启动成功。\n\n接收地址：{url}\n\n请在电视浏览器中打开这个地址完成投屏。",
        )

    def _apply_mirror_start_failed(self, target_label: str, error: str) -> None:
        messagebox.showerror("投屏启动失败", f"{target_label} 启动失败：\n{error}")
        self.status_var.set(f"{target_label} 启动失败。")
        self._refresh_overview_state()

    def _apply_active_window_start_failed(self, error: str) -> None:
        messagebox.showerror("当前页面投屏启动失败", f"无法开始当前页面投屏：\n{error}")
        self.status_var.set("当前页面投屏启动失败。")
        self._refresh_overview_state()

    def stop_mirror(self) -> None:
        self.mirror_server.stop()
        self.mirror_url_var.set("当前未启动兼容镜像地址")
        self.selected_window_var.set("未锁定页面")
        self.status_var.set("已停止兼容镜像。")
        self._refresh_overview_state()

    def copy_mirror_url(self) -> None:
        value = self.mirror_url_var.get()
        if not value.startswith("http://"):
            messagebox.showinfo("无可复制地址", "当前没有正在运行的兼容镜像地址。")
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.status_var.set("已复制兼容镜像接收地址。")

    def _clear_video_delivery_urls(self) -> None:
        self.media_url_var.set("当前未生成兼容视频地址")
        self.player_url_var.set("当前未生成兼容播放页地址")
        self._refresh_overview_state()

    def _sync_video_delivery_urls(self, media_url: str | None, player_url: str | None) -> None:
        self.media_url_var.set(media_url or "当前未生成兼容视频地址")
        self.player_url_var.set(player_url or "当前未生成兼容播放页地址")
        self._refresh_overview_state()

    def _get_selected_media_source(self) -> Path | str | None:
        if self.selected_file is not None:
            return self.selected_file
        if self.selected_media_url:
            return self.selected_media_url
        return None

    def _prepare_compatible_media(self, media_file: Path) -> Path:
        return prepare_compatible_mp4(media_file)

    def _ensure_video_delivery(self, media_file: Path) -> tuple[str, str]:
        resolved = media_file.resolve()
        if self.http_server.media_file == resolved and self.http_server.media_url and self.http_server.player_url:
            return self.http_server.media_url, self.http_server.player_url
        media_url = self.http_server.start(str(resolved))
        player_url = self.http_server.player_url or ""
        return media_url, player_url

    def _ensure_remote_video_delivery(self, media_url: str, display_name: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
        if (
            self.http_server.media_file is None
            and self.http_server.remote_source_url == media_url
            and self.http_server.player_url
            and self.http_server.media_url
        ):
            return self.http_server.media_url, self.http_server.player_url
        served_media_url = self.http_server.start_remote(media_url, display_name, headers=headers)
        player_url = self.http_server.player_url or ""
        return served_media_url, player_url

    def _prepare_media_delivery_source(self, media_source: Path | str) -> tuple[str, str, str]:
        if isinstance(media_source, Path):
            compatible_file = self._prepare_compatible_media(media_source)
            media_url, player_url = self._ensure_video_delivery(compatible_file)
            return media_url, player_url, str(compatible_file)

        resolved = resolve_media_source(media_source)
        media_url, player_url = self._ensure_remote_video_delivery(resolved.media_url, resolved.display_name, headers=resolved.headers)
        return media_url, player_url, resolved.media_url

    def prepare_video_delivery(self) -> None:
        media_source = self._get_selected_media_source()
        if media_source is None:
            messagebox.showwarning("未选择视频", "请先选择一个本地视频文件，或输入网络视频直链 / 视频播放页地址。")
            return
        threading.Thread(target=self._prepare_video_delivery_worker, args=(media_source,), daemon=True).start()

    def _prepare_video_delivery_worker(self, media_source: Path | str) -> None:
        preparing_text = "正在转为兼容 MP4 并生成播放页…" if isinstance(media_source, Path) else "正在解析视频网址并生成直投地址…"
        self.after(0, lambda text=preparing_text: self.status_var.set(text))
        try:
            media_url, player_url, _ = self._prepare_media_delivery_source(media_source)
        except VideoPageExtractionUnavailable as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("缺少网页解析组件", error))
            self.after(0, lambda: self.status_var.set("无法解析播放页：缺少网页视频解析组件。"))
            return
        except (ValueError, VideoPageExtractionError) as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("解析视频播放页失败", error))
            self.after(0, lambda: self.status_var.set("无法从当前网址解析可直投视频。"))
            return
        except FfmpegNotFoundError as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("缺少 ffmpeg", error))
            self.after(0, lambda: self.status_var.set("无法生成兼容播放页：未找到 ffmpeg。"))
            return
        except (OSError, RuntimeError) as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("生成兼容播放页失败", error))
            self.after(0, lambda: self.status_var.set("生成兼容播放页失败。"))
            return
        self.after(0, lambda: self._sync_video_delivery_urls(media_url, player_url))
        self.after(0, lambda: self.status_var.set("已生成兼容视频地址和兼容播放页地址。"))

    def _copy_value(self, value: str, empty_message: str, success_message: str) -> None:
        if not value.startswith("http://"):
            messagebox.showinfo("无可复制地址", empty_message)
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.status_var.set(success_message)

    def copy_media_url(self) -> None:
        self._copy_value(self.media_url_var.get(), "当前没有兼容视频地址，请先生成。", "已复制兼容视频地址。")

    def copy_player_url(self) -> None:
        self._copy_value(self.player_url_var.get(), "当前没有兼容播放页地址，请先生成。", "已复制兼容播放页地址。")

    def open_connect_panel(self, show_result_dialog: bool = False) -> None:
        try:
            open_connect_panel()
            self.status_var.set("已触发 Win+K 投放面板。")
            if show_result_dialog:
                messagebox.showinfo(
                    "已打开 Win+K 投放面板",
                    "系统投放面板已成功打开。\n\n如果列表中出现电视，请继续在 Windows 面板中完成连接。\n如果没有看到电视，通常表示当前电视或盒子不支持 Miracast，或尚未被 Windows 识别。",
                )
        except Exception as exc:
            messagebox.showerror("打开投放面板失败", f"无法打开 Win+K 投放面板：\n{exc}")
            self.status_var.set("无法打开 Win+K 投放面板。")

    def open_display_settings(self) -> None:
        try:
            open_display_settings()
            self.status_var.set("已打开显示设置。")
        except Exception as exc:
            messagebox.showerror("打开显示设置失败", str(exc))
            self.status_var.set("无法打开显示设置。")

    def open_project_settings(self) -> None:
        try:
            open_project_settings()
            self.status_var.set("已打开“投影到此电脑”设置。")
        except Exception as exc:
            messagebox.showerror("打开设置失败", str(exc))
            self.status_var.set("无法打开“投影到此电脑”设置。")

    def switch_mode(self, mode: str) -> None:
        try:
            switch_projection_mode(mode)
            labels = {"clone": "复制屏幕", "extend": "扩展屏幕", "internal": "仅电脑屏幕", "external": "仅第二屏幕"}
            self.status_var.set(f"已请求切换显示模式：{labels.get(mode, mode)}。")
        except Exception as exc:
            messagebox.showerror("切换显示模式失败", str(exc))
            self.status_var.set("切换显示模式失败。")

    def scan_devices(self) -> None:
        self.status_var.set("正在扫描局域网中的盒子和电视…")
        self.box_status_var.set("正在通过 DLNA/UPnP 刷新设备列表…")
        self.device_count_var.set("扫描中…")
        self.selected_box_var.set("正在扫描…")
        for widget in self._device_list_widgets():
            widget.delete(0, tk.END)
        self._refresh_overview_state()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            devices = discover_devices(timeout=5.5)
        except OSError as exc:
            self.after(0, lambda error=str(exc): self._handle_scan_error(error))
            return
        self.after(0, lambda: self._update_devices(devices))

    def _handle_scan_error(self, error: str) -> None:
        self.device_count_var.set("扫描失败")
        self.selected_box_var.set("未选择盒子")
        self.box_status_var.set("请检查电脑网络是否正常，并确认盒子与电脑处于同一局域网。")
        self.status_var.set(f"扫描失败：{error}")
        self._refresh_overview_state()

    def _update_devices(self, devices: list[DlnaDevice]) -> None:
        self.devices = devices
        self._sync_device_views(0 if devices else None)
        if devices:
            self.status_var.set(f"扫描完成：发现 {len(devices)} 台局域网盒子/电视。")
        else:
            self.status_var.set("扫描完成：未发现支持 DLNA 的盒子。你仍可先输入视频播放页地址解析直投源，或改用兼容播放页。")
        self._refresh_overview_state()

    def choose_file(self) -> None:
        file_path = filedialog.askopenfilename(title="选择要投送的视频文件", filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.wmv *.ts *.flv *.m4v *.webm"), ("所有文件", "*.*")])
        if not file_path:
            return
        new_file = Path(file_path)
        if self.selected_file is None or self.selected_file.resolve() != new_file.resolve():
            self._clear_video_delivery_urls()
        self.selected_media_url = None
        self.selected_file = new_file
        self.file_var.set(str(self.selected_file))
        self.status_var.set("已选择本地视频文件。生成兼容播放页或开始 DLNA 推送时会自动转为兼容 MP4(H.264/AAC)。")
        self._refresh_overview_state()

    def use_media_url(self) -> None:
        current_value = self.selected_media_url or ""
        media_url = simpledialog.askstring("输入视频网址", "请输入网络视频直链，或直接输入视频播放页地址。程序会优先尝试解析播放页中的真实视频流，再直接推送到盒子。", initialvalue=current_value, parent=self)
        if media_url is None:
            return
        media_url = media_url.strip()
        if not media_url:
            messagebox.showwarning("网址为空", "请输入 http:// 或 https:// 开头的网络视频地址。")
            return
        if not is_http_url(media_url):
            messagebox.showwarning("网址格式错误", "当前只支持 http:// 或 https:// 开头的网络视频地址。")
            return
        self._clear_video_delivery_urls()
        self.selected_file = None
        self.selected_media_url = media_url
        self.file_var.set(media_url)
        if is_probable_direct_media_url(media_url):
            self.status_var.set("已选择网络视频直链。可以直接生成播放页或尝试 DLNA 直投。")
        else:
            self.status_var.set("已选择视频播放页地址。开始视频投送时会先解析真实视频地址，再直接推送到盒子。")
        self._refresh_overview_state()

    def _get_selected_device(self) -> DlnaDevice | None:
        for widget in self._device_list_widgets():
            selection = widget.curselection()
            if not selection:
                continue
            index = selection[0]
            if index < len(self.devices):
                return self.devices[index]
        return None

    def cast_media(self) -> None:
        device = self._get_selected_device()
        if device is None:
            messagebox.showwarning("未选择设备", "请先扫描并选择一个 DLNA 设备。")
            return
        if not device.supports_media_cast:
            messagebox.showwarning("设备暂不支持 DLNA 推送", "当前盒子已被搜索到，但没有暴露 DLNA 播放控制服务。你可以改用“生成兼容播放页”。")
            return
        media_source = self._get_selected_media_source()
        if media_source is None:
            messagebox.showwarning("未选择视频", "请先选择本地视频文件，或输入网络视频直链 / 视频播放页地址。")
            return
        threading.Thread(target=self._cast_worker, args=(device, media_source), daemon=True).start()

    def _cast_worker(self, device: DlnaDevice, media_source: Path | str) -> None:
        preparing_text = "正在转为兼容 MP4 并发送播放指令…" if isinstance(media_source, Path) else "正在解析视频网址并向设备发送播放指令…"
        self.after(0, lambda text=preparing_text: self.status_var.set(text))
        try:
            media_url, player_url, metadata_source = self._prepare_media_delivery_source(media_source)
        except VideoPageExtractionUnavailable as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("缺少网页解析组件", error))
            self.after(0, lambda: self.status_var.set("无法直投该网址：缺少网页视频解析组件。"))
            return
        except (ValueError, VideoPageExtractionError) as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("解析视频播放页失败", error))
            self.after(0, lambda: self.status_var.set("无法从当前网址解析可直投视频。"))
            return
        except FfmpegNotFoundError as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("缺少 ffmpeg", error))
            self.after(0, lambda: self.status_var.set("无法开始视频投送：未找到 ffmpeg。"))
            return

        self.after(0, lambda: self._sync_video_delivery_urls(media_url, player_url))

        try:
            controller = DlnaController(device)
            controller.set_media(media_url, metadata_source)
            controller.play(self.speed_var.get())
            try:
                controller.set_volume(int(self.volume_var.get()))
            except (ValueError, HTTPError, URLError):
                pass
            self.current_controller = controller
            self.after(0, self._refresh_overview_state)
        except (HTTPError, URLError, OSError, RuntimeError) as exc:
            self.after(0, lambda error=str(exc): messagebox.showerror("视频投送失败", f"{error}\n\n已保留兼容播放页地址，可使用“复制兼容播放页”继续播放。"))
            self.after(0, lambda: self.status_var.set("视频投送失败，但已保留兼容播放页地址，可直接复制。"))
            return
        status_text = f"已开始向 {device.display_name} 进行视频投送。" if isinstance(media_source, str) else f"已开始向 {device.display_name} 进行视频投送，当前使用兼容 MP4。"
        self.after(0, lambda text=status_text: self.status_var.set(text))

    def stop_casting(self) -> None:
        if self.current_controller is None:
            self.http_server.stop()
            self._clear_video_delivery_urls()
            self.status_var.set("已停止本地视频服务。")
            return
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        try:
            self.current_controller.stop()
        except (ValueError, HTTPError, URLError, OSError):
            pass
        self.http_server.stop()
        self.current_controller = None
        self.after(0, self._clear_video_delivery_urls)
        self.after(0, lambda: self.status_var.set("已停止视频投送。"))
        self.after(0, self._refresh_overview_state)

    def _on_close(self) -> None:
        self.http_server.stop()
        self.mirror_server.stop()
        self.destroy()


def run() -> None:
    app = ScreenCastingApp()
    app.mainloop()











