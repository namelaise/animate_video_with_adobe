# gui.py — Mr Martin Dashboard (Flet / Material Design 3)
# python gui.py

import flet as ft
import threading
import subprocess
import sys
import json
import re
import shutil
import os
import tempfile
import unicodedata
from pathlib import Path
from datetime import date, datetime


def strip_accents(s: str) -> str:
    """Supprime les accents d'une chaîne (é→e, è→e, etc.)."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "download"
PENDING_DIR = BASE_DIR / "pending_posts"
DAILY_LOG = BASE_DIR / "scraper_daily.json"
LOGS_DIR = BASE_DIR / "logs"
HISTORY_FILE           = BASE_DIR / "upload_history.json"
MATCHING_MODE_FILE     = BASE_DIR / "matching_mode.json"
MATCHING_REQUEST_FILE  = BASE_DIR / "matching_request.json"
MATCHING_RESPONSE_FILE = BASE_DIR / "matching_response.json"
GUI_CONFIG_FILE        = BASE_DIR / "gui_config.json"
SCRAPER_CONFIG_FILE    = BASE_DIR / "scraper_config.json"
ACCOUNTS_FILE          = BASE_DIR / "config" / "tiktok_accounts.json"
PIPELINE_STATE_FILE    = BASE_DIR / "pipeline_state.json"
VENV_PYTHON = BASE_DIR / "venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
DEFAULT_DAILY_LIMIT = 5

# Couleurs
SURFACE = "#1c1b1f"
ON_SURFACE = "#e6e1e5"
OUTLINE = "#938f99"
PRIMARY = "#d0bcff"
ON_PRIMARY = "#381e72"
SECONDARY = "#ccc2dc"
ERROR = "#f2b8b5"
ERROR_C = "#f44336"
SUCCESS = "#4caf50"
WARN_C = "#ff9800"

PIPELINE_STEPS = [
    ("En attente de video", "video choisie|copie .* download"),
    ("Nettoyage", "nettoyage"),
    ("Extraction audio", "extraction audio"),
    ("Transcription", "transcription|diarisation"),
    ("Speakers", "verification|reecriture"),
    ("Alignement", "realignement|alignement"),
    ("Decoupe audio", "decoupe|decoupage"),
    ("Manifest audio", "manifest"),
    ("Background", "background|split|image de fond"),
    ("Animation", "animation|adobe|generation video"),
    ("Assemblage", "assemblage"),
    ("Upload TikTok", "upload tiktok|generation description"),
]


def main(page: ft.Page):
    page.title = "Mr Martin"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(
        color_scheme_seed="#8b5cf6",
        font_family="Segoe UI",
    )
    page.padding = 0
    page.window.width = 1400
    page.window.height = 900
    page.window.min_width = 1000
    page.window.min_height = 700

    # Icon — .ico en priorité pour la barre des tâches Windows
    ico = BASE_DIR / "app_icon.ico"
    if not ico.exists():
        ico = BASE_DIR / "app_icon.png"
    if ico.exists():
        page.window.icon = str(ico)

    # ── State ─────────────────────────────────────────────
    scheduler_proc = {"ref": None}
    log_offset      = {"ref": 0}   # position dans pipeline.log (affichage)
    pipeline_offset = {"ref": 0}   # même fichier, pour process_pipeline_line
    log_txt_offset  = {"ref": 0}   # position dans log.txt (événements scheduler)
    selected_pending = {"ref": None}
    upload_running = {"ref": False}
    log_source = {"ref": "pipeline"}   # "pipeline" | "action"
    matching_mode = {"ref": "auto"}    # "auto" | "manual"
    matching_popup_open = {"ref": False}

    # ── Snackbar ──────────────────────────────────────────
    snack_ref = {"bar": None}

    def toast(msg, kind="info"):
        colors = {"success": SUCCESS, "error": ERROR_C, "warning": WARN_C, "info": "#8b5cf6"}
        icons_map = {"success": ft.Icons.CHECK_CIRCLE, "error": ft.Icons.ERROR,
                 "warning": ft.Icons.WARNING, "info": ft.Icons.INFO}
        sb = ft.SnackBar(
            content=ft.Row([
                ft.Icon(icons_map.get(kind, ft.Icons.INFO), color=colors.get(kind), size=20),
                ft.Text(msg, color="white", size=13),
            ], spacing=10),
            bgcolor="#2d2d2d",
            duration=4000,
        )
        page.overlay.append(sb)
        sb.open = True
        page.update()

    # ── Metric cards ──────────────────────────────────────
    def metric_card(icon, title, value_ref, subtitle_ref=None):
        val = ft.Text(value_ref, size=28, weight=ft.FontWeight.BOLD)
        sub = ft.Text(subtitle_ref or "", size=11, color=OUTLINE)
        card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(icon, size=18, color=OUTLINE),
                    ft.Text(title, size=12, color=OUTLINE),
                ], spacing=8),
                val,
                sub,
            ], spacing=4),
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border_radius=12,
            padding=ft.Padding.all(20),
            expand=True,
        )
        return card, val, sub

    sched_card, sched_val, sched_sub = metric_card(ft.Icons.PLAY_CIRCLE, "Scheduler", "OFF", "Arrete")
    quota_card, quota_val, quota_sub = metric_card(ft.Icons.DOWNLOAD, "Quota", "0/5", "")
    dl_card, dl_val, dl_sub = metric_card(ft.Icons.VIDEO_FILE, "En attente", "0", "download/")
    pend_card, pend_val, pend_sub = metric_card(ft.Icons.PENDING, "Echecs", "0", "pending_posts/")

    metrics_row = ft.Row([sched_card, quota_card, dl_card, pend_card], spacing=12)

    # ── Pipeline steps ────────────────────────────────────
    pipeline_badge = ft.Container(
        content=ft.Text("En attente", size=11, weight=ft.FontWeight.BOLD, color=OUTLINE),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=12, vertical=4),
    )
    pipeline_progress = ft.ProgressBar(value=0, height=4, color="#8b5cf6",
                                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST)

    step_dots = []
    step_labels = []
    step_times = []
    step_rows = []

    for i, (name, _) in enumerate(PIPELINE_STEPS):
        dot = ft.Container(width=8, height=8, border_radius=4,
                           bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST)
        label = ft.Text(name, size=12, color=OUTLINE)
        time_lbl = ft.Text("", size=11, color=OUTLINE)
        row = ft.Row([dot, label, ft.Container(expand=True), time_lbl],
                     spacing=10, height=26, key=f"step_{i}")
        step_dots.append(dot)
        step_labels.append(label)
        step_times.append(time_lbl)
        step_rows.append(row)

    pipeline_col = ft.Column([
        ft.Row([ft.Text("Pipeline", size=16, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True), pipeline_badge]),
        pipeline_progress,
        *step_rows,
    ], spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)

    pipeline_card = ft.Container(
        content=pipeline_col,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        padding=20,
        width=320,
        expand=True,
    )

    def set_pipeline_step(idx, status, elapsed=""):
        if idx < 0 or idx >= len(PIPELINE_STEPS):
            return
        if status == "running":
            step_dots[idx].bgcolor = "#8b5cf6"
            step_labels[idx].color = "white"
            step_labels[idx].weight = ft.FontWeight.BOLD
            pipeline_progress.value = (idx + 0.5) / len(PIPELINE_STEPS)
            pipeline_badge.content.value = PIPELINE_STEPS[idx][0]
            pipeline_badge.content.color = "white"
            pipeline_badge.bgcolor = "#8b5cf6"
            try:
                pipeline_col.scroll_to(key=f"step_{idx}", duration=200)
            except Exception:
                pass
        elif status == "done":
            step_dots[idx].bgcolor = SUCCESS
            step_labels[idx].color = SUCCESS
            step_labels[idx].weight = None
            if elapsed:
                step_times[idx].value = elapsed
                step_times[idx].color = SUCCESS
            pipeline_progress.value = (idx + 1) / len(PIPELINE_STEPS)
            # Dernière étape → scroll retour en haut
            if idx == len(PIPELINE_STEPS) - 1:
                try:
                    pipeline_col.scroll_to(offset=0, duration=500)
                except Exception:
                    pass
            else:
                try:
                    pipeline_col.scroll_to(key=f"step_{idx}", duration=200)
                except Exception:
                    pass
        elif status == "error":
            step_dots[idx].bgcolor = ERROR_C
            step_labels[idx].color = ERROR_C
            step_labels[idx].weight = ft.FontWeight.BOLD
            pipeline_badge.content.value = "Erreur"
            pipeline_badge.content.color = "white"
            pipeline_badge.bgcolor = ERROR_C

    def reset_pipeline():
        for i in range(len(PIPELINE_STEPS)):
            step_dots[i].bgcolor = ft.Colors.SURFACE_CONTAINER_HIGHEST
            step_labels[i].color = OUTLINE
            step_labels[i].weight = None
            step_times[i].value = ""
        pipeline_progress.value = 0
        pipeline_progress.color = "#8b5cf6"
        try:
            pipeline_col.scroll_to(offset=0, duration=300)
        except Exception:
            pass
        # Set waiting state
        has_video = DOWNLOAD_DIR.exists() and any(
            f.suffix == ".mp4" for f in DOWNLOAD_DIR.iterdir() if f.is_file()
        ) if DOWNLOAD_DIR.exists() else False
        if has_video:
            step_dots[0].bgcolor = SUCCESS
            step_labels[0].color = SUCCESS
            pipeline_badge.content.value = "Video disponible"
            pipeline_badge.content.color = "white"
            pipeline_badge.bgcolor = SUCCESS
        else:
            step_dots[0].bgcolor = WARN_C
            step_labels[0].color = WARN_C
            pipeline_badge.content.value = "En attente de video"
            pipeline_badge.content.color = OUTLINE
            pipeline_badge.bgcolor = ft.Colors.SURFACE_CONTAINER_HIGHEST

    pipeline_current = {"ref": -1}

    def process_pipeline_line(line):
        ll = strip_accents(line.lower())
        if "video choisie" in ll or ("copie" in ll and "download" in ll):
            reset_pipeline()
            on_new_generation()
            set_pipeline_step(0, "done")
        # Étapes ignorées lors d'une reprise → marquer comme faites
        if "ignoree" in ll or "ignorees" in ll:
            for i, (name, pattern) in enumerate(PIPELINE_STEPS):
                if i == 0:
                    continue
                if re.search(pattern, ll):
                    set_pipeline_step(i, "done")
            return
        for i, (name, pattern) in enumerate(PIPELINE_STEPS):
            if i == 0:
                continue
            if "debut etape" in ll and re.search(pattern, ll):
                pipeline_current["ref"] = i
                set_pipeline_step(i, "running")
                return
            if "fin etape" in ll and re.search(pattern, ll):
                tm = re.search(r'([\d.]+)s\)', line)
                el = f"{float(tm.group(1)):.0f}s" if tm else ""
                set_pipeline_step(i, "done", elapsed=el)
                return
        if "pipeline complet" in ll:
            pipeline_progress.value = 1.0
            pipeline_progress.color = SUCCESS
            pipeline_badge.content.value = "Termine"
            pipeline_badge.content.color = "white"
            pipeline_badge.bgcolor = SUCCESS
        elif "fin du cycle" in ll:
            reset_pipeline()
        elif "erreur non ger" in ll or ("echec" in ll and "etape" in ll):
            if pipeline_current["ref"] >= 0:
                set_pipeline_step(pipeline_current["ref"], "error")
        elif "traitement initial" in ll:
            reset_pipeline()
            set_pipeline_step(1, "running")
            pipeline_current["ref"] = 1

    # ── Logs panel ────────────────────────────────────────
    MAX_LOG_ENTRIES = 400

    _LOG_LINE_RE = re.compile(
        r'^\[(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}))\]\s+(INFO|WARNING|ERROR|DEBUG)\s+\[(\w+)\]\s+(.*)$'
    )

    def build_log_entry(line: str):
        line = line.rstrip()
        if not line:
            return None
        m = _LOG_LINE_RE.match(line)
        if not m:
            # Ligne brute sans format structuré (subprocess, etc.)
            return ft.Container(
                content=ft.Text(line[:400], size=11, color="#4a4a4a",
                                font_family="Cascadia Code"),
                padding=ft.Padding.symmetric(horizontal=10, vertical=1),
            )
        ts       = m.group(2)
        level    = m.group(3)
        module   = m.group(4)
        msg      = m.group(5)

        if level == "ERROR":
            level_color, row_bg, msg_color = "#f44336", "#1c0a0a", "#ffcdd2"
        elif level == "WARNING":
            level_color, row_bg, msg_color = "#ff9800", "#1c1200", "#ffe0b2"
        elif level == "DEBUG":
            level_color, row_bg, msg_color = "#8b5cf6", None, "#c5b4f0"
        else:
            level_color, row_bg, msg_color = "#4caf50", None, "#e0e0e0"

        mod_colors = {"pipeline": "#8b5cf6", "scheduler": "#2196f3", "scraper": "#00bcd4"}
        mod_color = mod_colors.get(module, "#607d8b")

        return ft.Container(
            content=ft.Row([
                ft.Text(ts, size=10, color="#4a4a4a", font_family="Cascadia Code", width=65),
                ft.Container(
                    content=ft.Text(level[:4], size=9, weight=ft.FontWeight.BOLD, color=level_color),
                    border=ft.Border.all(1, level_color),
                    border_radius=3,
                    padding=ft.Padding.symmetric(horizontal=4, vertical=1),
                    width=38,
                ),
                ft.Container(
                    content=ft.Text(module[:9], size=9, color=mod_color),
                    border=ft.Border.all(1, mod_color),
                    border_radius=3,
                    padding=ft.Padding.symmetric(horizontal=4, vertical=1),
                    width=74,
                ),
                ft.Text(msg, size=11, color=msg_color, expand=True),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=row_bg,
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=3,
        )


    log_list = ft.ListView(
        expand=True,
        spacing=1,
        auto_scroll=True,
        padding=ft.Padding.symmetric(horizontal=4, vertical=4),
    )

    log_placeholder = ft.Container(
        content=ft.Column([
            ft.Icon(ft.Icons.RECEIPT_LONG_OUTLINED, size=36, color=OUTLINE),
            ft.Text(
                "En attente de la prochaine génération…",
                size=12, color=OUTLINE, text_align=ft.TextAlign.CENTER,
            ),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
        alignment=ft.Alignment.CENTER,
        expand=True,
        visible=True,
    )

    log_area = ft.Stack([log_list, log_placeholder], expand=True)

    def _set_placeholder(visible: bool):
        log_placeholder.visible = visible

    def _append_log_lines(new_lines):
        """Ajoute de nouvelles lignes à log_list."""
        added = False
        for ln in new_lines:
            widget = build_log_entry(ln)
            if widget:
                log_list.controls.append(widget)
                added = True
        if len(log_list.controls) > MAX_LOG_ENTRIES:
            log_list.controls = log_list.controls[-MAX_LOG_ENTRIES:]
        if added:
            _set_placeholder(False)
            try:
                log_list.scroll_to(offset=float("inf"), duration=100)
            except Exception:
                pass

    def _start_action_log(action_name: str):
        """Bascule le panel de log vers une action manuelle."""
        log_source["ref"] = "action"
        log_list.controls.clear()
        log_list.controls.append(ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.TERMINAL, size=14, color="#8b5cf6"),
                ft.Text(f"▶  {action_name}", size=12,
                        weight=ft.FontWeight.BOLD, color="#8b5cf6"),
            ], spacing=8),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            bgcolor="#1a1040", border_radius=4,
        ))
        _set_placeholder(False)
        page.update()

    def _end_action_log(success: bool, msg: str):
        """Ajoute la ligne de fin, repasse en mode pipeline sans rejouer les anciens logs."""
        color = SUCCESS if success else ERROR_C
        icon = ft.Icons.CHECK_CIRCLE if success else ft.Icons.ERROR
        log_list.controls.append(ft.Container(
            content=ft.Row([
                ft.Icon(icon, size=14, color=color),
                ft.Text(msg, size=12, color=color, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=4,
        ))
        # Caler l'offset à la fin de pipeline.log pour ne pas rejouer les anciens logs
        pp = LOGS_DIR / "pipeline.log"
        try:
            if pp.exists():
                sz = pp.stat().st_size
                log_offset["ref"] = sz
                pipeline_offset["ref"] = sz
        except Exception:
            pass
        log_source["ref"] = "pipeline"
        page.update()

    def _stream_proc_to_log(proc):
        """Lit stdout d'un Popen ligne par ligne et pousse chaque ligne dans le panel."""
        for raw in proc.stdout:
            line = raw.rstrip()
            if line:
                def _push(l=line):
                    _append_log_lines([l])
                    page.update()
                page.run_thread(_push)
        proc.wait()

    logs_panel = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("Logs", size=16, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.Text("pipeline en cours", size=11, color=OUTLINE),
            ], spacing=8),
            log_area,
        ], spacing=8, expand=True),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        padding=ft.Padding.only(left=16, right=16, top=12, bottom=16),
        expand=True,
    )

    # ── Upload progress bar ───────────────────────────────
    upload_bar_text = ft.Text("", size=12, color="white")
    upload_bar_pct = ft.Text("0%", size=12, weight=ft.FontWeight.BOLD, color="#8b5cf6")
    upload_bar_progress = ft.ProgressBar(value=0, height=6, color="#8b5cf6",
                                          bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST)
    upload_bar = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.CLOUD_UPLOAD, color="#8b5cf6", size=20),
                upload_bar_text,
                ft.Container(expand=True),
                upload_bar_pct,
            ], spacing=10),
            upload_bar_progress,
        ], spacing=8),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        padding=ft.Padding.all(16),
        border=ft.Border.all(1, "#8b5cf6"),
        visible=False,
    )

    # ── Pending posts cards ───────────────────────────────
    pending_row = ft.Row([], spacing=10, scroll=ft.ScrollMode.AUTO)
    pending_section = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("Videos en echec", size=16, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.FilledTonalButton("Poster la selection", icon=ft.Icons.SEND,
                                      on_click=lambda _: post_selected()),
            ]),
            pending_row,
        ], spacing=10),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=12,
        padding=16,
        visible=False,
    )

    def _generate_thumbnail(video_path: Path, tmp: Path, card_name: str):
        """Génère une miniature via ffmpeg en arrière-plan, puis refresh les cards."""
        try:
            subprocess.run(
                ["ffmpeg", "-ss", "1", "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", "-y", str(tmp)],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception:
            pass
        if tmp.exists():
            def _refresh():
                pending_cache["names"] = []
                refresh_pending()
                page.update()
            page.run_thread(_refresh)

    def build_pending_card(d):
        video_path = d / "video_final.mp4"
        has_video = video_path.exists()

        reason = "?"
        ts = ""
        meta_file = d / "meta.json"
        if meta_file.exists():
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                reason = m.get("reason", "?")
                ts = m.get("timestamp", "")[:16]
            except Exception:
                pass

        reason_map = {
            "spam_risk_too_many_pending_share": ("Spam risk", WARN_C),
            "token_failed":  ("Token expiré", ERROR_C),
            "spam_risk":     ("Spam risk",    WARN_C),
            "other_failed":  ("API refusé",   ERROR_C),  # sandbox ou erreur API
        }
        reason_text, reason_color = reason_map.get(reason, (reason[:16], OUTLINE))

        # Thumbnail
        tmp = Path(tempfile.gettempdir()) / f"mrm_{d.name}.jpg"
        if has_video and tmp.exists():
            thumb_widget = ft.Stack([
                ft.Image(src=str(tmp), width=140, height=78,
                         fit=ft.BoxFit.COVER, border_radius=8),
                # Overlay play
                ft.Container(
                    content=ft.Icon(ft.Icons.PLAY_CIRCLE_FILL, size=28, color="white",
                                    opacity=0.85),
                    alignment=ft.Alignment.CENTER,
                    width=140, height=78,
                ),
            ], width=140, height=78)
        else:
            thumb_widget = ft.Container(
                content=ft.Icon(
                    ft.Icons.HOURGLASS_EMPTY if has_video else ft.Icons.VIDEO_FILE,
                    size=28, color=OUTLINE,
                ),
                width=140, height=78,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border_radius=8,
                alignment=ft.Alignment.CENTER,
            )
            # Lancer la génération en arrière-plan si pas encore fait
            if has_video and not tmp.exists():
                threading.Thread(
                    target=_generate_thumbnail,
                    args=(video_path, tmp, d.name),
                    daemon=True,
                ).start()

        # Taille
        size_str = ""
        if has_video:
            size_mb = video_path.stat().st_size / (1024 * 1024)
            size_str = f"{size_mb:.0f} MB"

        is_selected = selected_pending["ref"] == d.name

        def on_select(e, name=d.name):
            selected_pending["ref"] = name
            pending_cache["names"] = []
            refresh_pending()
            page.update()

        def on_play(e, vp=video_path):
            if vp.exists():
                try:
                    os.startfile(str(vp))
                except Exception as ex:
                    toast(f"Impossible d'ouvrir: {ex}", "error")

        card = ft.Container(
            content=ft.Column([
                # Thumbnail cliquable pour lecture
                ft.GestureDetector(
                    content=thumb_widget,
                    on_tap=on_play if has_video else None,
                    mouse_cursor=ft.MouseCursor.CLICK if has_video else None,
                ),
                ft.Text(d.name.replace("Video_", "# "), size=12,
                        weight=ft.FontWeight.BOLD, color="white"),
                ft.Row([
                    ft.Container(
                        content=ft.Text(reason_text, size=10, color=reason_color),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=4,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    ),
                    ft.Text(size_str, size=10, color=OUTLINE) if size_str else ft.Container(),
                ], spacing=6),
                # Bouton play explicite
                ft.Row([
                    ft.TextButton(
                        "▶ Lire",
                        on_click=on_play,
                        style=ft.ButtonStyle(
                            color="#8b5cf6",
                            padding=ft.Padding.symmetric(horizontal=0, vertical=0),
                        ),
                    ) if has_video else ft.Container(),
                ]),
            ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.START),
            width=160,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_selected else ft.Colors.SURFACE_CONTAINER,
            border_radius=10,
            padding=10,
            border=ft.Border.all(2, "#8b5cf6") if is_selected else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            on_click=on_select,
            ink=True,
        )
        return card

    pending_cache = {"names": []}

    def refresh_pending():
        pc = 0
        current_names = []
        if PENDING_DIR.exists():
            dirs = sorted([d for d in PENDING_DIR.iterdir() if d.is_dir()],
                         key=lambda d: d.stat().st_mtime, reverse=True)
            pc = len(dirs)
            current_names = [d.name for d in dirs]
        # Ne reconstruire les cards que si la liste a change
        if current_names != pending_cache["names"]:
            pending_cache["names"] = current_names
            pending_row.controls.clear()
            if PENDING_DIR.exists():
                for d in sorted([d for d in PENDING_DIR.iterdir() if d.is_dir()],
                               key=lambda d: d.stat().st_mtime, reverse=True):
                    pending_row.controls.append(build_pending_card(d))
        pend_val.value = str(pc)
        pend_val.color = WARN_C if pc else OUTLINE
        pending_section.visible = pc > 0

    # ── Actions ───────────────────────────────────────────
    def is_running():
        return scheduler_proc["ref"] is not None and scheduler_proc["ref"].poll() is None

    def start_scheduler(e=None):
        if is_running():
            return
        scheduler_proc["ref"] = subprocess.Popen(
            [PYTHON, "-X", "utf8", "-u", "auto_scheduler.py"], cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        toast("Scheduler demarre", "success")
        refresh_all()

    def stop_scheduler(e=None):
        if is_running():
            scheduler_proc["ref"].terminate()
            try:
                scheduler_proc["ref"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                scheduler_proc["ref"].kill()
        scheduler_proc["ref"] = None
        toast("Scheduler arrete", "warning")
        refresh_all()

    def run_scraper(e=None):
        def do():
            page.run_thread(lambda: _start_action_log("Scraping TikTok"))
            proc = subprocess.Popen(
                [PYTHON, "scraper_tiktok.py"], cwd=str(BASE_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            _stream_proc_to_log(proc)
            success = proc.returncode == 0
            msg  = "Scraping terminé" if success else "Erreur scraping"
            kind = "success" if success else "error"
            page.run_thread(lambda: (_end_action_log(success, msg), toast(msg, kind), refresh_all()))
        threading.Thread(target=do, daemon=True).start()

    def reset_quota(e=None):
        DAILY_LOG.write_text(json.dumps({"date": date.today().isoformat(), "count": 0}, indent=2),
                             encoding="utf-8")
        toast("Quota reinitialise", "success")
        refresh_all()

    # refresh_token est maintenant refresh_token_active (défini dans le bloc accounts)

    def post_video(vdir):
        vp = vdir / "video_final.mp4"
        if not vp.exists():
            toast(f"Pas de video dans {vdir.name}", "error")
            return

        upload_running["ref"] = True
        upload_bar.visible = True
        upload_bar_text.value = f"Upload {vdir.name}..."
        upload_bar_pct.value = "0%"
        upload_bar_progress.value = 0
        upload_bar_progress.color = "#8b5cf6"
        page.update()

        def do():
            # Récupérer le token du compte actif depuis le gestionnaire
            _accounts_data, _active_id = _load_accounts_data()
            _active_tok = ""
            if _active_id:
                for _a in _accounts_data:
                    if _a.get("id") == _active_id:
                        _active_tok = (_a.get("access_token") or "").strip()
                        break
            _post_cmd = [PYTHON, "-u", "post_tiktok_inbox.py", "--video", str(vp), "--poll"]
            if _active_tok:
                _post_cmd += ["--token", _active_tok]
            proc = subprocess.Popen(
                _post_cmd,
                cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            last_error = ""
            poll_count = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                ll = line.lower()

                if any(w in ll for w in ["[err]", "spam_risk", "scope_not",
                                          "unaudited", "token_invalid"]):
                    m_json = re.search(r'"message"\s*:\s*"([^"]+)"', line)
                    last_error = m_json.group(1) if m_json else re.sub(r'^\[ERR\]\s*', '', line, flags=re.IGNORECASE)

                m_chunk = re.search(r'chunk (\d+)/(\d+)', line, re.IGNORECASE)
                if m_chunk:
                    c, t = int(m_chunk.group(1)), int(m_chunk.group(2))
                    pct = c / t * 0.6
                    def u1(p=pct, c=c, t=t):
                        upload_bar_progress.value = p
                        upload_bar_pct.value = f"{int(p*100)}%"
                        upload_bar_text.value = f"Chunk {c}/{t}"
                        page.update()
                    page.run_thread(u1)

                if "[ok] upload termin" in ll:
                    def u2():
                        upload_bar_progress.value = 0.6
                        upload_bar_text.value = "Verification TikTok..."
                        upload_bar_pct.value = "60%"
                        page.update()
                    page.run_thread(u2)

                if "tentative" in ll:
                    poll_count += 1
                    pct = 0.6 + min(poll_count * 0.02, 0.35)
                    def u3(p=pct, n=poll_count):
                        upload_bar_progress.value = p
                        upload_bar_pct.value = f"{int(p*100)}%"
                        upload_bar_text.value = f"Verification ({n})..."
                        page.update()
                    page.run_thread(u3)

                if "send_to_user_inbox" in ll or ("success" in ll and "status" in ll):
                    def u4():
                        upload_bar_progress.value = 1.0
                        upload_bar_progress.color = SUCCESS
                        upload_bar_pct.value = "100%"
                        upload_bar_text.value = "Publication reussie !"
                        page.update()
                    page.run_thread(u4)

            proc.wait()
            success = proc.returncode == 0

            def finish():
                upload_running["ref"] = False
                if success:
                    try:
                        shutil.rmtree(vdir)
                        toast(f"{vdir.name} postee et supprimee", "success")
                    except Exception as ex:
                        toast(f"Postee mais erreur suppression: {ex}", "warning")
                    upload_bar_progress.value = 1.0
                    upload_bar_progress.color = SUCCESS
                    upload_bar_pct.value = "100%"
                    upload_bar_text.value = "Publie avec succes !"
                else:
                    reason = last_error[:80] if last_error else "Erreur inconnue"
                    toast(f"Echec: {reason}", "error")
                    upload_bar_progress.color = ERROR_C
                    upload_bar_text.value = reason[:60]
                    upload_bar_pct.value = ""
                refresh_all()
                page.update()
                import time
                time.sleep(8)
                upload_bar.visible = False
                page.update()

            page.run_thread(finish)

        threading.Thread(target=do, daemon=True).start()

    def post_latest(e=None):
        d = find_latest_pending()
        if not d:
            toast("Aucune video pending", "warning")
            return
        post_video(d)

    def post_selected(e=None):
        name = selected_pending["ref"]
        if not name:
            toast("Selectionne une video d'abord", "warning")
            return
        d = PENDING_DIR / name
        if d.exists():
            post_video(d)

    def find_latest_pending():
        if not PENDING_DIR.exists():
            return None
        for d in sorted([x for x in PENDING_DIR.iterdir() if x.is_dir()],
                       key=lambda x: x.stat().st_mtime, reverse=True):
            if (d / "video_final.mp4").exists():
                return d
        return None

    def _open_dialog(dlg):
        page.overlay.append(dlg)
        dlg.open = True
        page.update()

    def _close_dialog(dlg):
        dlg.open = False
        page.update()

    def open_download_folder(e=None):
        if DOWNLOAD_DIR.exists():
            os.startfile(str(DOWNLOAD_DIR))
        else:
            toast("Dossier download/ inexistant", "warning")

    def clear_download(e=None):
        if not DOWNLOAD_DIR.exists():
            toast("Dossier download/ vide", "warning")
            return
        files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix == ".mp4"]
        if not files:
            toast("Aucune vidéo en attente", "warning")
            return

        def confirm(ev):
            _close_dialog(dlg)
            deleted = 0
            for f in files:
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
            toast(f"{deleted} vidéo(s) supprimée(s)", "success")
            refresh_all()

        def cancel(ev):
            _close_dialog(dlg)

        dlg = ft.AlertDialog(
            title=ft.Text("Vider download/"),
            content=ft.Text(f"Supprimer {len(files)} vidéo(s) en attente de traitement ?"),
            actions=[
                ft.TextButton("Annuler", on_click=cancel),
                ft.FilledButton("Supprimer", on_click=confirm,
                                style=ft.ButtonStyle(bgcolor=ERROR_C, color="white")),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    def reset_pipeline_state(e=None):
        def confirm(ev):
            _close_dialog(dlg)
            try:
                PIPELINE_STATE_FILE.unlink(missing_ok=True)
                pipeline_current["ref"] = -1
                on_new_generation()
                reset_pipeline()
                toast("Pipeline réinitialisé", "success")
                refresh_all()
            except Exception as ex:
                toast(f"Erreur reset pipeline : {ex}", "error")

        def cancel(ev):
            _close_dialog(dlg)

        dlg = ft.AlertDialog(
            title=ft.Text("Réinitialiser le pipeline ?"),
            content=ft.Text(
                "Cela supprime pipeline_state.json. Au prochain lancement, les étapes ne seront plus considérées comme déjà faites."
            ),
            actions=[
                ft.TextButton("Annuler", on_click=cancel),
                ft.FilledButton("Réinitialiser", on_click=confirm,
                                style=ft.ButtonStyle(bgcolor=WARN_C, color="white")),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    def update_ytdlp(e=None):
        def do():
            page.run_thread(lambda: _start_action_log("Mise à jour yt-dlp"))
            output_lines = []
            proc = subprocess.Popen(
                [PYTHON, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            for raw in proc.stdout:
                line = raw.rstrip()
                output_lines.append(line)
                if line:
                    def _push(l=line):
                        _append_log_lines([l])
                        page.update()
                    page.run_thread(_push)
            proc.wait()
            combined = "\n".join(output_lines).lower()
            already = "already up-to-date" in combined or "already satisfied" in combined
            vm = re.search(r'Successfully installed yt-dlp-([\d.]+)', "\n".join(output_lines))
            ver = vm.group(1) if vm else ""
            success = proc.returncode == 0
            if success and already:
                msg, kind = "yt-dlp déjà à jour", "info"
            elif success:
                msg, kind = f"yt-dlp mis à jour{f' ({ver})' if ver else ''}", "success"
            else:
                msg, kind = "Mise à jour échouée", "error"
            page.run_thread(lambda: (_end_action_log(success, msg), toast(msg, kind)))
        threading.Thread(target=do, daemon=True).start()

    # ── Scraper config ────────────────────────────────────────
    def _load_scraper_config() -> dict:
        default = {
            "accounts": [],
            "daily_limit": 5,
            "min_duration": 10,
            "max_duration": 600,
        }
        try:
            if SCRAPER_CONFIG_FILE.exists():
                data = json.loads(SCRAPER_CONFIG_FILE.read_text(encoding="utf-8"))
                default.update(data)
        except Exception:
            pass
        return default

    def _save_scraper_config(cfg: dict):
        import tempfile
        dir_ = SCRAPER_CONFIG_FILE.parent
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SCRAPER_CONFIG_FILE)
        except Exception as ex:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            toast(f"Erreur sauvegarde config: {ex}", "error")

    def show_scraper_config(e=None):
        cfg = _load_scraper_config()
        accounts_list = list(cfg.get("accounts", []))

        accounts_field = ft.ListView(spacing=4, height=180)
        limit_val = {"ref": int(cfg.get("daily_limit", 5))}
        min_dur_val = {"ref": int(cfg.get("min_duration", 10))}
        max_dur_val = {"ref": int(cfg.get("max_duration", 600))}

        limit_lbl = ft.Text(str(limit_val["ref"]), size=14, weight=ft.FontWeight.BOLD,
                            color="white", width=28, text_align=ft.TextAlign.CENTER)
        min_dur_lbl = ft.Text(str(min_dur_val["ref"]), size=13, color="white", width=36,
                              text_align=ft.TextAlign.CENTER)
        max_dur_lbl = ft.Text(str(max_dur_val["ref"]), size=13, color="white", width=36,
                              text_align=ft.TextAlign.CENTER)

        def rebuild_list():
            accounts_field.controls.clear()
            for i, url in enumerate(accounts_list):
                idx = i

                def make_remove(j=idx):
                    def do(e):
                        accounts_list.pop(j)
                        rebuild_list()
                        page.update()
                    return do

                accounts_field.controls.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Text(url, size=11, expand=True, color=ON_SURFACE,
                                    overflow=ft.TextOverflow.ELLIPSIS, no_wrap=True),
                            ft.IconButton(ft.Icons.CLOSE, icon_size=14, icon_color=OUTLINE,
                                          on_click=make_remove(i),
                                          style=ft.ButtonStyle(padding=ft.Padding.all(0))),
                        ], spacing=6),
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=6,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                    )
                )

        rebuild_list()

        new_url_field = ft.TextField(
            hint_text="https://www.tiktok.com/@compte",
            text_size=12,
            height=40,
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_color=ft.Colors.OUTLINE_VARIANT,
            focused_border_color="#8b5cf6",
        )

        def add_account(e):
            url = (new_url_field.value or "").strip()
            if url and url not in accounts_list:
                accounts_list.append(url)
                new_url_field.value = ""
                rebuild_list()
                page.update()

        def make_stepper_row(label, val_holder, lbl_widget, step, min_v, max_v):
            def dec(e):
                val_holder["ref"] = max(min_v, val_holder["ref"] - step)
                lbl_widget.value = str(val_holder["ref"])
                page.update()
            def inc(e):
                val_holder["ref"] = min(max_v, val_holder["ref"] + step)
                lbl_widget.value = str(val_holder["ref"])
                page.update()
            return ft.Row([
                ft.Text(label, size=12, color=OUTLINE, expand=True),
                ft.IconButton(ft.Icons.REMOVE, icon_size=16, on_click=dec,
                              style=ft.ButtonStyle(padding=ft.Padding.all(0))),
                ft.Container(content=lbl_widget,
                             bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                             border_radius=6,
                             padding=ft.Padding.symmetric(horizontal=6, vertical=2)),
                ft.IconButton(ft.Icons.ADD, icon_size=16, on_click=inc,
                              style=ft.ButtonStyle(padding=ft.Padding.all(0))),
            ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        def save_and_close(e):
            new_cfg = {
                "accounts": accounts_list,
                "daily_limit": limit_val["ref"],
                "min_duration": min_dur_val["ref"],
                "max_duration": max_dur_val["ref"],
            }
            _save_scraper_config(new_cfg)
            _close_dialog(dlg)
            toast("Configuration scraping sauvegardée", "success")

        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.MANAGE_SEARCH, color="#8b5cf6", size=20),
                ft.Text("Configuration du scraping", size=14, weight=ft.FontWeight.BOLD),
            ], spacing=10),
            content=ft.Container(
                content=ft.Column([
                    ft.Text("Comptes TikTok à scraper", size=12,
                            weight=ft.FontWeight.BOLD, color=ON_SURFACE),
                    accounts_field,
                    ft.Row([
                        new_url_field,
                        ft.FilledTonalButton("Ajouter", icon=ft.Icons.ADD,
                                             on_click=add_account),
                    ], spacing=8),
                    ft.Divider(height=12, color=ft.Colors.OUTLINE_VARIANT),
                    make_stepper_row("Vidéos / jour", limit_val, limit_lbl, 1, 1, 20),
                    make_stepper_row("Durée min (s)", min_dur_val, min_dur_lbl, 5, 5, 120),
                    make_stepper_row("Durée max (s)", max_dur_val, max_dur_lbl, 30, 30, 3600),
                ], spacing=10, tight=True),
                width=520,
                padding=ft.Padding.only(top=4, bottom=8),
            ),
            actions=[
                ft.TextButton("Annuler", on_click=lambda e: _close_dialog(dlg)),
                ft.FilledButton("Enregistrer",
                                on_click=save_and_close,
                                style=ft.ButtonStyle(bgcolor="#8b5cf6", color="white")),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    def show_stats(e=None):
        toast("Chargement des stats...", "info")
        def do():
            r = subprocess.run(
                [PYTHON, "fetch_stats.py", "--report"],
                cwd=str(BASE_DIR), capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            output = (r.stdout or r.stderr or "Aucune donnée disponible.").strip()

            def show():
                dlg = ft.AlertDialog(
                    title=ft.Text("Stats TikTok", size=16, weight=ft.FontWeight.BOLD),
                    content=ft.Container(
                        content=ft.ListView(
                            controls=[
                                ft.Text(output, size=11, font_family="Cascadia Code",
                                        selectable=True, color=ON_SURFACE),
                            ],
                            expand=True,
                        ),
                        width=640, height=420,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                        border_radius=8,
                        padding=12,
                    ),
                    actions=[ft.TextButton("Fermer", on_click=lambda ev: _close_dialog(dlg))],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                _open_dialog(dlg)
            page.run_thread(show)
        threading.Thread(target=do, daemon=True).start()

    def show_upload_history(e=None):
        if not HISTORY_FILE.exists():
            toast("Aucun historique d'upload", "warning")
            return
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            toast("Erreur lecture historique", "error")
            return

        entries = list(reversed(history))[:20]
        rows = []
        for entry in entries:
            ok = entry.get("upload_ok", False)
            ts = entry.get("timestamp", "")[:16]
            tmpl = (entry.get("prompt_template") or "—")[:22]
            reason = (entry.get("reason") or ("OK" if ok else "?"))[:22]
            color = SUCCESS if ok else ERROR_C
            rows.append(ft.Container(
                content=ft.Row([
                    ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR,
                            color=color, size=14),
                    ft.Text(ts, size=11, color=OUTLINE, width=105,
                            font_family="Cascadia Code"),
                    ft.Text(tmpl, size=11, expand=True, color=ON_SURFACE),
                    ft.Text(reason, size=11, color=color),
                ], spacing=10),
                padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                border_radius=6,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
            ))

        if not rows:
            rows = [ft.Text("Aucun upload enregistré.", color=OUTLINE, size=12)]

        dlg = ft.AlertDialog(
            title=ft.Text("Historique uploads", size=16, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.ListView(controls=rows, spacing=4, expand=True),
                width=560, height=380,
            ),
            actions=[ft.TextButton("Fermer", on_click=lambda ev: _close_dialog(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    # ── Refresh ───────────────────────────────────────────
    def refresh_all(e=None):
        # Scheduler
        on = is_running()
        sched_val.value = "ON" if on else "OFF"
        sched_val.color = SUCCESS if on else ERROR_C
        sched_sub.value = "Pipeline actif" if on else "Arrete"
        # Bouton Démarrer : vert quand dispo, grisé quand actif
        start_btn.style = ft.ButtonStyle(
            bgcolor="#4a4a4a" if on else SUCCESS,
            color="#888888"   if on else "white",
        )
        start_btn.disabled = on
        # Bouton Arrêter : rouge quand actif, grisé quand dispo
        stop_btn.style = ft.ButtonStyle(
            bgcolor=ERROR_C   if on else "#4a4a4a",
            color="white"     if on else "#888888",
        )
        stop_btn.disabled = not on

        # Quota
        dc = 0
        try:
            if DAILY_LOG.exists():
                d = json.loads(DAILY_LOG.read_text(encoding="utf-8"))
                if d.get("date") == date.today().isoformat():
                    dc = d.get("count", 0)
        except Exception:
            pass
        full = dc >= DEFAULT_DAILY_LIMIT
        quota_val.value = f"{dc}/{DEFAULT_DAILY_LIMIT}"
        quota_val.color = ERROR_C if full else SUCCESS
        quota_sub.value = "Quota atteint" if full else "Aujourd'hui"

        # Downloads
        dlc = 0
        if DOWNLOAD_DIR.exists():
            dlc = len([f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix == ".mp4"])
        dl_val.value = str(dlc)
        dl_val.color = SUCCESS if dlc else OUTLINE

        # Pending
        refresh_pending()

        # Pipeline waiting state
        if pipeline_current["ref"] == -1:
            reset_pipeline()

        page.update()

    # ── Log tailing ───────────────────────────────────────
    def on_new_generation():
        """Vide l'affichage IN-APP. pipeline.log est mode='w' → repart à 0."""
        log_list.controls.clear()
        _set_placeholder(True)
        # Pointer à la fin du fichier existant pour ne pas re-afficher les anciens logs.
        # Quand main_v3.py démarre, il ouvre pipeline.log en mode='w' (troncature) →
        # tail_logs() détecte sz < offset et remet offset à 0 automatiquement.
        pp = LOGS_DIR / "pipeline.log"
        try:
            cur = pp.stat().st_size if pp.exists() else 0
        except Exception:
            cur = 0
        log_offset["ref"] = cur
        pipeline_offset["ref"] = cur
        # NE PAS remettre log_txt_offset à 0 ici : la détection de troncature
        # (sz < log_txt_offset) dans tail_logs() suffit pour le reset.
        # Le remettre à 0 forcerait une relecture infinie de log.txt (auto_scheduler
        # demarre → on_new_generation → offset=0 → relecture → boucle).

    def process_scheduler_event(line: str):
        """Détecte les événements scheduler dans log.txt (pas affiché)."""
        ll = strip_accents(line.lower())
        # Nouvelle vidéo en traitement → fresh start
        if "video choisie" in ll or ("copie" in ll and "download" in ll) or "reprise du pipeline" in ll:
            on_new_generation()
            reset_pipeline()
            set_pipeline_step(0, "done")
        # Fin de cycle (entre deux vidéos) → retour au placeholder
        elif "fin du cycle" in ll:
            on_new_generation()
            reset_pipeline()
        # Scheduler démarré → reset
        elif "auto_scheduler demarre" in ll:
            on_new_generation()
            reset_pipeline()

    def tail_logs():
        """
        Lit pipeline.log pour l'affichage et l'avancement des étapes.
        Lit log.txt pour les événements scheduler (placeholder, reset pipeline).
        Ignoré quand une action manuelle stream dans le panel (log_source == "action").
        """
        changed = False

        # ── 0. Matching request — toujours vérifié, même en mode action ──
        try:
            if MATCHING_REQUEST_FILE.exists() and not matching_popup_open["ref"]:
                req = json.loads(MATCHING_REQUEST_FILE.read_text(encoding="utf-8"))
                page.run_thread(lambda r=req: show_matching_popup(r))
        except Exception:
            pass

        # Si une action manuelle est en cours, ne pas écraser ses logs
        if log_source["ref"] == "action":
            return False

        # ── 1. Affichage + étapes : pipeline.log ──────────────
        pp = LOGS_DIR / "pipeline.log"
        try:
            if pp.exists():
                sz = pp.stat().st_size
                if sz < log_offset["ref"]:       # fichier tronqué → nouveau run
                    log_offset["ref"] = 0
                    pipeline_offset["ref"] = 0
                    log_list.controls.clear()
                    _set_placeholder(True)
                if sz > log_offset["ref"]:
                    with open(pp, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(log_offset["ref"])
                        data = f.read()
                    log_offset["ref"] = sz
                    pipeline_offset["ref"] = sz
                    lines = [l for l in data.splitlines() if l.strip()]
                    if lines:
                        for line in lines:
                            process_pipeline_line(line)
                        _append_log_lines(lines)
                        changed = True
        except Exception:
            pass

        # ── 2. Événements scheduler : log.txt (pas affiché) ───
        lt = LOGS_DIR / "log.txt"
        try:
            if lt.exists():
                sz = lt.stat().st_size
                if sz < log_txt_offset["ref"]:   # scheduler redémarré
                    log_txt_offset["ref"] = 0
                if sz > log_txt_offset["ref"]:
                    with open(lt, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(log_txt_offset["ref"])
                        data = f.read()
                    log_txt_offset["ref"] = sz
                    for line in data.splitlines():
                        if line.strip():
                            process_scheduler_event(line)
                    changed = True
        except Exception:
            pass

        return changed

    # ── TikTok Accounts panel ─────────────────────────────
    accounts_col = ft.Column([], spacing=6)

    def _resolve_local_path(path_value: str) -> Path:
        p = Path(path_value or "")
        return p if p.is_absolute() else BASE_DIR / p

    def _account_name(acc: dict) -> str:
        name = (acc.get("display_name") or "").strip()
        username = (acc.get("username") or "").strip()
        if name and not name.lower().startswith("(compte"):
            return name
        if username and not username.lower().startswith("(compte"):
            return username if username.startswith("@") else f"@{username}"
        return acc.get("id", "Compte TikTok")

    def _account_username(acc: dict) -> str:
        username = (acc.get("username") or "").strip()
        if username and not username.lower().startswith("(compte"):
            return username if username.startswith("@") else f"@{username}"
        return "Profil à actualiser"

    def _load_accounts_data() -> tuple[list[dict], str | None]:
        """Lit config/tiktok_accounts.json directement (sans importer le module)."""
        try:
            if ACCOUNTS_FILE.exists():
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                accounts_list = list(data.get("accounts", {}).values())
                active_id = data.get("active_account_id")
                return accounts_list, active_id
        except Exception:
            pass
        return [], None

    def _set_active_account_file(account_id: str) -> None:
        """Met à jour l'active_account_id directement dans le fichier JSON."""
        try:
            if ACCOUNTS_FILE.exists():
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                data["active_account_id"] = account_id
                ACCOUNTS_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass

    def build_account_card(acc: dict, is_active: bool) -> ft.Container:
        acc_id       = acc.get("id", "?")
        display_name = _account_name(acc)
        username     = _account_username(acc)
        avatar_local = acc.get("avatar_local", "")
        uploads      = acc.get("upload_count", 0)
        profile_error = (acc.get("profile_error") or "").strip()
        avatar_path = _resolve_local_path(avatar_local)

        # Avatar
        if avatar_local and avatar_path.exists():
            avatar_widget = ft.Container(
                content=ft.Image(
                    src=str(avatar_path),
                    width=44, height=44,
                    fit=ft.BoxFit.COVER,
                    border_radius=22,
                ),
                width=44, height=44,
                border_radius=22,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                border=ft.Border.all(2, "#d0bcff") if is_active else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            )
        else:
            avatar_widget = ft.Container(
                content=ft.Icon(ft.Icons.ACCOUNT_CIRCLE, size=22,
                                color="#d0bcff" if is_active else OUTLINE),
                width=44, height=44,
                border_radius=22,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border=ft.Border.all(2, "#d0bcff") if is_active else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                alignment=ft.Alignment.CENTER,
            )

        def on_switch(e, aid=acc_id):
            _set_active_account_file(aid)
            refresh_accounts()
            toast(f"Compte actif : {display_name}", "success")

        def on_remove(e, aid=acc_id):
            def confirm(ev):
                _close_dialog(dlg)
                try:
                    if ACCOUNTS_FILE.exists():
                        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
                        data["accounts"].pop(aid, None)
                        if data.get("active_account_id") == aid:
                            remaining = list(data["accounts"].keys())
                            data["active_account_id"] = remaining[0] if remaining else None
                        ACCOUNTS_FILE.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    # Supprimer l'avatar local
                    avatar_path = BASE_DIR / "config" / "avatars" / f"{aid}.jpg"
                    if avatar_path.exists():
                        avatar_path.unlink()
                except Exception as ex:
                    toast(f"Erreur suppression : {ex}", "error")
                refresh_accounts()
                toast(f"Compte {display_name} supprimé", "warning")

            def cancel(ev):
                _close_dialog(dlg)

            dlg = ft.AlertDialog(
                title=ft.Text("Supprimer ce compte ?"),
                content=ft.Text(f"Supprimer {display_name} de la liste des comptes ?"),
                actions=[
                    ft.TextButton("Annuler", on_click=cancel),
                    ft.FilledButton("Supprimer", on_click=confirm,
                                    style=ft.ButtonStyle(bgcolor=ERROR_C, color="white")),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            _open_dialog(dlg)

        active_badge = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.CHECK_CIRCLE, size=10, color="#d0bcff"),
                ft.Text("actif", size=9, color="#d0bcff"),
            ], spacing=2),
            bgcolor="#2d2148",
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=4, vertical=1),
            visible=is_active,
        )
        profile_incomplete = bool(profile_error) or username == "Profil à actualiser"
        status_line = (
            "Profil incomplet"
            if profile_incomplete
            else f"{uploads} upload{'s' if uploads != 1 else ''}"
        )
        status_color = WARN_C if profile_incomplete else OUTLINE

        # Token expiry status
        import time as _time
        _now = int(_time.time())
        _expires_at = acc.get("expires_at")
        _ref_expires_at = acc.get("refresh_expires_at")
        if _expires_at:
            _secs_left = int(_expires_at) - _now
            if _secs_left <= 0:
                token_line = "Token expiré !"
                token_color = ERROR_C
            elif _secs_left < 3600:
                token_line = f"Token expire dans {_secs_left // 60}min"
                token_color = WARN_C
            elif _secs_left < 86400:
                token_line = f"Token expire dans {_secs_left // 3600}h"
                token_color = WARN_C
            else:
                days = _secs_left // 86400
                token_line = f"Token valide ({days}j)"
                token_color = SUCCESS
        else:
            token_line = ""
            token_color = OUTLINE

        card = ft.Container(
            content=ft.Row([
                # Avatar
                ft.GestureDetector(
                    content=avatar_widget,
                    on_tap=on_switch,
                    mouse_cursor=ft.MouseCursor.CLICK,
                ),
                # Infos
                ft.Column([
                    ft.Row([
                        ft.Text(
                            display_name,
                            size=12,
                            weight=ft.FontWeight.BOLD if is_active else None,
                            color="white" if is_active else ON_SURFACE,
                            expand=True,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        active_badge,
                    ], spacing=4),
                    ft.Text(
                        username,
                        size=10, color=OUTLINE,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        status_line,
                        size=10, color=status_color,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        token_line,
                        size=10, color=token_color,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        visible=bool(token_line),
                    ),
                ], spacing=0, expand=True),
                # Bouton supprimer
                ft.IconButton(
                    ft.Icons.CLOSE,
                    icon_size=14,
                    icon_color=OUTLINE,
                    on_click=on_remove,
                    style=ft.ButtonStyle(padding=ft.Padding.all(0)),
                    tooltip="Supprimer ce compte",
                ),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH if is_active else ft.Colors.SURFACE_CONTAINER,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.Border.all(1, "#d0bcff") if is_active else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            on_click=on_switch,
            ink=True,
            tooltip=profile_error or None,
        )
        return card

    def connect_new_account(e=None):
        """Lance l'auth TikTok pour ajouter un nouveau compte."""
        _no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        def do():
            page.run_thread(lambda: _start_action_log("Connexion nouveau compte TikTok"))
            proc = subprocess.Popen(
                [PYTHON, "auth_tiktok_token_manager.py"], cwd=str(BASE_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=_no_win,
            )
            _stream_proc_to_log(proc)
            ok = proc.returncode == 0
            msg  = "Compte connecté avec succès !" if ok else "Connexion échouée"
            kind = "success" if ok else "error"
            page.run_thread(lambda: (
                _end_action_log(ok, msg),
                toast(msg, kind),
                refresh_accounts(),
                refresh_all(),
            ))
        threading.Thread(target=do, daemon=True).start()

    def refresh_profiles(e=None):
        """Récupère nom + avatar de tous les comptes TikTok."""
        _no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        def do():
            page.run_thread(lambda: _start_action_log("Actualisation profils TikTok"))
            proc = subprocess.Popen(
                [
                    PYTHON,
                    "-c",
                    "from tiktok_account_manager import refresh_all_profiles; import json; print(json.dumps(refresh_all_profiles(), ensure_ascii=False, indent=2))",
                ],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_no_win,
            )
            _stream_proc_to_log(proc)
            ok = proc.returncode == 0
            page.run_thread(lambda: (
                _end_action_log(ok, "Profils TikTok actualisés" if ok else "Actualisation profils échouée"),
                toast("Profils TikTok actualisés" if ok else "Actualisation profils échouée", "success" if ok else "error"),
                refresh_accounts(),
            ))
        threading.Thread(target=do, daemon=True).start()

    def refresh_token_active(e=None):
        """Refresh le token du compte actif."""
        accounts_list, active_id = _load_accounts_data()
        account_arg = active_id or "acc_1"
        _no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        def do():
            page.run_thread(lambda: _start_action_log(f"Refresh token ({account_arg})"))
            proc = subprocess.Popen(
                [PYTHON, "auth_tiktok_refresh.py", "--account", account_arg],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=_no_win,
            )
            _stream_proc_to_log(proc)
            if proc.returncode == 0:
                page.run_thread(lambda: (
                    _end_action_log(True, "Token rafraîchi"),
                    toast("Token rafraichi", "success"),
                    refresh_accounts(),
                ))
            else:
                page.run_thread(lambda: _append_log_lines(
                    ["[INFO] Refresh échoué — ouverture navigateur pour re-auth..."]))
                proc2 = subprocess.Popen(
                    [PYTHON, "auth_tiktok_token_manager.py"], cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, creationflags=_no_win,
                )
                _stream_proc_to_log(proc2)
                ok2 = proc2.returncode == 0
                page.run_thread(lambda: (
                    _end_action_log(ok2, "Re-auth réussie" if ok2 else "Échec re-auth"),
                    toast("Re-auth réussie" if ok2 else "Échec re-auth",
                          "success" if ok2 else "error"),
                    refresh_accounts(),
                ))
        threading.Thread(target=do, daemon=True).start()

    def refresh_accounts():
        """Reconstruit la liste des comptes dans la sidebar."""
        accounts_list, active_id = _load_accounts_data()
        accounts_col.controls.clear()

        if not accounts_list:
            accounts_col.controls.append(
                ft.Text("Aucun compte connecté", size=11, color=OUTLINE,
                        italic=True)
            )
        else:
            for acc in accounts_list:
                is_active = acc.get("id") == active_id
                accounts_col.controls.append(build_account_card(acc, is_active))

        try:
            page.update()
        except Exception:
            pass

    # ── Matching mode toggle ──────────────────────────────
    def _write_matching_mode(mode: str):
        try:
            MATCHING_MODE_FILE.write_text(
                json.dumps({"mode": mode}), encoding="utf-8"
            )
        except Exception:
            pass

    matching_mode_btn = ft.FilledTonalButton(
        "Mode auto",
        icon=ft.Icons.SMART_TOY,
        style=ft.ButtonStyle(
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            color=OUTLINE,
        ),
    )

    def toggle_matching_mode(e=None):
        new_mode = "manual" if matching_mode["ref"] == "auto" else "auto"
        matching_mode["ref"] = new_mode
        _write_matching_mode(new_mode)
        _refresh_matching_btn()

    matching_mode_btn.on_click = toggle_matching_mode

    def _refresh_matching_btn():
        if matching_mode["ref"] == "manual":
            matching_mode_btn.text = "Matching manuel"
            matching_mode_btn.icon = ft.Icons.PERSON_SEARCH
            matching_mode_btn.style = ft.ButtonStyle(bgcolor="#1a1040", color="#8b5cf6")
        else:
            matching_mode_btn.text = "Mode auto"
            matching_mode_btn.icon = ft.Icons.SMART_TOY
            matching_mode_btn.style = ft.ButtonStyle(
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, color=OUTLINE)
        page.update()

    # ── Popup matching de personnage ──────────────────────
    PUPPET_NAMES = {p: p.replace(" VQA.puppet-button", "")
                    for gender_list in [
                        ["Fergus VQA.puppet-button", "Cecil VQA.puppet-button",
                         "David VQA.puppet-button", "Elliot VQA.puppet-button",
                         "Jonty VQA.puppet-button", "Jonty with prosthetic VQA.puppet-button",
                         "Atlas VQA.puppet-button", "Edith VQA.puppet-button",
                         "Agnes VQA.puppet-button", "Chivy VQA.puppet-button",
                         "Gaby VQA.puppet-button", "Zibby VQA.puppet-button",
                         "Yara VQA.puppet-button", "Yara with prosthetic VQA.puppet-button"]
                    ] for p in gender_list}

    def show_matching_popup(req: dict):
        if matching_popup_open["ref"]:
            return
        matching_popup_open["ref"] = True
        try:
            label   = req.get("label", "?")
            genre   = req.get("genre", "homme")
            puppets = req.get("available_puppets", [])
            if not puppets:
                matching_popup_open["ref"] = False
                return

            selected = {"puppet": None}
            countdown = {"value": 60, "active": True}
            timer_text = ft.Text("60s", size=13, color=WARN_C, weight=ft.FontWeight.BOLD)
            icon_gender = ft.Icons.PERSON if genre == "homme" else ft.Icons.PERSON_2

            puppet_btns: list[tuple[str, ft.Container]] = []

            def select_puppet(puppet_id):
                selected["puppet"] = puppet_id
                for pid, cont in puppet_btns:
                    cont.border  = ft.Border.all(2, "#8b5cf6") if pid == puppet_id else ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
                    cont.bgcolor = ft.Colors.SURFACE_CONTAINER_HIGH if pid == puppet_id else ft.Colors.SURFACE_CONTAINER
                page.update()

            def confirm_and_close(puppet=None):
                if not countdown["active"]:
                    return
                countdown["active"] = False
                chosen = puppet or selected["puppet"]
                if not chosen:
                    import random as _r
                    chosen = _r.choice(puppets)
                try:
                    MATCHING_RESPONSE_FILE.write_text(
                        json.dumps({"puppet": chosen}), encoding="utf-8"
                    )
                except Exception:
                    pass
                matching_popup_open["ref"] = False
                dlg.open = False
                page.update()

            def make_puppet_btn(puppet_id):
                short = PUPPET_NAMES.get(puppet_id, puppet_id.replace(" VQA.puppet-button", ""))
                cont = ft.Container(
                    content=ft.Column([
                        ft.Icon(icon_gender, size=30, color="#8b5cf6"),
                        ft.Text(short, size=10, text_align=ft.TextAlign.CENTER,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                    width=88, height=82,
                    border_radius=8,
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    padding=ft.Padding.all(6),
                    on_click=lambda e, p=puppet_id: select_puppet(p),
                    ink=True,
                )
                puppet_btns.append((puppet_id, cont))
                return cont

            grid = ft.Row(
                [make_puppet_btn(p) for p in puppets],
                wrap=True, spacing=8, run_spacing=8,
            )

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Row([
                    ft.Icon(ft.Icons.PERSON_SEARCH, color="#8b5cf6", size=22),
                    ft.Text(f"Personnage pour «{label}»", size=14,
                            weight=ft.FontWeight.BOLD, expand=True),
                    timer_text,
                ], spacing=10),
                content=ft.Container(
                    content=ft.Column([
                        ft.Text(f"Genre détecté : {genre}  •  Mr Martin est toujours Sticky",
                                size=11, color=OUTLINE),
                        ft.Divider(height=6),
                        grid,
                    ], spacing=10),
                    width=540,
                    padding=4,
                ),
                actions=[
                    ft.TextButton("Auto (aléatoire)",
                                  on_click=lambda e: confirm_and_close()),
                    ft.FilledButton(
                        "Valider la sélection",
                        on_click=lambda e: confirm_and_close(),
                        style=ft.ButtonStyle(bgcolor="#8b5cf6", color="white"),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            _open_dialog(dlg)

            def _countdown_tick():
                import time as _t
                while countdown["value"] > 0 and countdown["active"]:
                    _t.sleep(1)
                    countdown["value"] -= 1
                    v = countdown["value"]
                    c = ERROR_C if v <= 10 else (WARN_C if v <= 30 else OUTLINE)
                    def _upd(val=v, col=c):
                        timer_text.value = f"{val}s"
                        timer_text.color = col
                        page.update()
                    page.run_thread(_upd)
                if countdown["active"]:
                    page.run_thread(lambda: confirm_and_close())

            threading.Thread(target=_countdown_tick, daemon=True).start()
        except Exception:
            matching_popup_open["ref"] = False
            raise

    # ── Sidebar ───────────────────────────────────────────
    start_btn = ft.FilledButton(
        "Demarrer", icon=ft.Icons.PLAY_ARROW,
        on_click=start_scheduler,
        style=ft.ButtonStyle(bgcolor=SUCCESS, color="white"),
    )
    stop_btn = ft.FilledButton(
        "Arreter", icon=ft.Icons.STOP,
        on_click=stop_scheduler,
        style=ft.ButtonStyle(bgcolor="#4a4a4a", color="#888888"),
        disabled=True,
    )

    # ── Stepper concurrence Adobe ──────────────────────────
    _MIN_CONC, _MAX_CONC = 1, 12

    def _read_concurrency() -> int:
        try:
            if GUI_CONFIG_FILE.exists():
                return int(json.loads(GUI_CONFIG_FILE.read_text(encoding="utf-8")).get("adobe_concurrency", 8))
        except Exception:
            pass
        return 4

    def _write_concurrency(val: int):
        cfg = {}
        try:
            if GUI_CONFIG_FILE.exists():
                cfg = json.loads(GUI_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        cfg["adobe_concurrency"] = val
        GUI_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    _conc_val = {"ref": _read_concurrency()}
    _conc_lbl = ft.Text(str(_conc_val["ref"]), size=14, weight=ft.FontWeight.BOLD,
                        color="white", width=24, text_align=ft.TextAlign.CENTER)

    def _conc_change(delta):
        v = max(_MIN_CONC, min(_MAX_CONC, _conc_val["ref"] + delta))
        _conc_val["ref"] = v
        _conc_lbl.value = str(v)
        _write_concurrency(v)
        page.update()

    _conc_row = ft.Row([
        ft.Container(
            content=ft.Text("Onglets Adobe", size=11, color=OUTLINE),
            expand=True,
        ),
        ft.IconButton(ft.Icons.REMOVE, icon_size=16,
                      on_click=lambda _: _conc_change(-1),
                      style=ft.ButtonStyle(padding=0)),
        ft.Container(
            content=_conc_lbl,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
        ),
        ft.IconButton(ft.Icons.ADD, icon_size=16,
                      on_click=lambda _: _conc_change(+1),
                      style=ft.ButtonStyle(padding=0)),
    ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    sidebar = ft.Container(
        content=ft.Column([
            # Brand
            ft.Container(
                content=ft.Column([
                    ft.Text("Mr Martin", size=22, weight=ft.FontWeight.BOLD),
                    ft.Text("Video Automation", size=12, color=OUTLINE),
                ], spacing=2),
                padding=ft.Padding.only(bottom=12),
            ),
            ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),

            # ── Comptes TikTok ────────────────────────────
            ft.Text("COMPTES TIKTOK", size=10, weight=ft.FontWeight.BOLD, color=OUTLINE),
            accounts_col,
            ft.FilledTonalButton(
                "Connecter un compte",
                icon=ft.Icons.ADD,
                on_click=connect_new_account,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                    color="#8b5cf6",
                ),
                width=240,
            ),
            ft.TextButton(
                "Actualiser noms et photos",
                icon=ft.Icons.SYNC,
                on_click=refresh_profiles,
                style=ft.ButtonStyle(color=OUTLINE),
                width=240,
            ),
            ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),

            # ── Scheduler ────────────────────────────────
            ft.Text("AUTOMATISATION", size=10, weight=ft.FontWeight.BOLD, color=OUTLINE),
            ft.Row([start_btn, stop_btn], spacing=8),
            matching_mode_btn,
            _conc_row,
            ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),

            # ── Actions ───────────────────────────────────
            ft.Text("ACTIONS", size=10, weight=ft.FontWeight.BOLD, color=OUTLINE),

            ft.ListTile(
                leading=ft.Icon(ft.Icons.SEARCH, size=20),
                title=ft.Text("Scraper des vidéos", size=13),
                subtitle=ft.Text("Télécharge depuis TikTok", size=11),
                on_click=run_scraper, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.SETTINGS, size=20, color="#8b5cf6"),
                title=ft.Text("Config scraping", size=13, color="#8b5cf6"),
                subtitle=ft.Text("Comptes, quota, durées", size=11),
                on_click=show_scraper_config, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.UPLOAD, size=20, color=SUCCESS),
                title=ft.Text("Poster la dernière vidéo", size=13, color=SUCCESS),
                subtitle=ft.Text("Envoie sur TikTok", size=11),
                on_click=post_latest, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.KEY, size=20),
                title=ft.Text("Rafraîchir le token", size=13),
                subtitle=ft.Text("Renouvelle l'accès TikTok", size=11),
                on_click=refresh_token_active, dense=True,
            ),
            ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),

            # ── Outils ────────────────────────────────────
            ft.Text("OUTILS", size=10, weight=ft.FontWeight.BOLD, color=OUTLINE),

            ft.ListTile(
                leading=ft.Icon(ft.Icons.BAR_CHART, size=20),
                title=ft.Text("Stats TikTok", size=13),
                subtitle=ft.Text("Performances par prompt", size=11),
                on_click=show_stats, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.HISTORY, size=20),
                title=ft.Text("Historique uploads", size=13),
                subtitle=ft.Text("20 derniers envois", size=11),
                on_click=show_upload_history, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.FOLDER_OPEN, size=20),
                title=ft.Text("Ouvrir download/", size=13),
                subtitle=ft.Text("Vidéos en attente", size=11),
                on_click=open_download_folder, dense=True,
            ),
            ft.Divider(height=1, color=ft.Colors.OUTLINE_VARIANT),

            # ── Maintenance ────────────────────────────────
            ft.Text("MAINTENANCE", size=10, weight=ft.FontWeight.BOLD, color=OUTLINE),

            ft.ListTile(
                leading=ft.Icon(ft.Icons.RESTART_ALT, size=20),
                title=ft.Text("Reset quota du jour", size=13),
                subtitle=ft.Text("Remet le compteur à zéro", size=11),
                on_click=reset_quota, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.SYSTEM_UPDATE_ALT, size=20),
                title=ft.Text("Mettre à jour yt-dlp", size=13),
                subtitle=ft.Text("Évite les blocages TikTok", size=11),
                on_click=update_ytdlp, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.RESTART_ALT, size=20, color=WARN_C),
                title=ft.Text("Réinitialiser pipeline", size=13, color=WARN_C),
                subtitle=ft.Text("Repart de l'état zéro", size=11),
                on_click=reset_pipeline_state, dense=True,
            ),
            ft.ListTile(
                leading=ft.Icon(ft.Icons.DELETE_SWEEP, size=20, color=WARN_C),
                title=ft.Text("Vider download/", size=13, color=WARN_C),
                subtitle=ft.Text("Supprime la queue", size=11),
                on_click=clear_download, dense=True,
            ),

            # Clock
            ft.Container(
                content=ft.Text(datetime.now().strftime("%H:%M"), size=11, color=OUTLINE),
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.only(top=8),
            ),
        ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True),
        width=290,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        padding=ft.Padding.all(20),
    )

    # ── Main layout ───────────────────────────────────────
    main_content = ft.Column([
        metrics_row,
        upload_bar,
        pending_section,
        ft.Row([
            pipeline_card,
            logs_panel,
        ], spacing=12, expand=True),
    ], spacing=12, expand=True)

    main_area = ft.Container(
        content=main_content,
        expand=True,
        padding=ft.Padding.all(24),
    )

    page.add(ft.Row([sidebar, main_area], spacing=0, expand=True))

    # ── Init (léger, le reste est différé) ─────────────
    reset_pipeline()
    page.update()

    # Différer le refresh lourd + auto-start pour que la fenêtre s'affiche vite
    def _deferred_init():
        import time as _t
        _t.sleep(0.3)
        # Restaurer le mode matching depuis le fichier (persiste entre sessions)
        try:
            if MATCHING_MODE_FILE.exists():
                saved = json.loads(MATCHING_MODE_FILE.read_text(encoding="utf-8"))
                matching_mode["ref"] = saved.get("mode", "auto")
                _refresh_matching_btn()
        except Exception:
            pass
        refresh_accounts()
        refresh_all()
        on_new_generation()
        start_scheduler()
        page.update()
    threading.Thread(target=_deferred_init, daemon=True).start()

    # Periodic refresh
    import time
    def refresh_loop():
        last_full_refresh   = 0
        last_accounts_refresh = 0
        while True:
            time.sleep(2)
            try:
                changed = tail_logs()
                needs_update = changed
                now = time.time()
                # Refresh stats toutes les 15s
                if now - last_full_refresh >= 15:
                    refresh_all()
                    last_full_refresh = now
                    needs_update = True
                # Refresh comptes toutes les 30s (avatar download async, etc.)
                if now - last_accounts_refresh >= 30:
                    refresh_accounts()
                    last_accounts_refresh = now
                    needs_update = True
                if needs_update:
                    page.update()
            except Exception:
                pass

    threading.Thread(target=refresh_loop, daemon=True).start()

    def on_window_event(e):
        data = str(getattr(e, 'data', ''))
        etype = str(getattr(e, 'type', ''))
        if "close" in data.lower() or "close" in etype.lower():
            stop_scheduler()
            page.window.prevent_close = False
            page.update()
            page.run_task(page.window.close)

    page.window.prevent_close = True
    page.window.on_event = on_window_event


if __name__ == "__main__":
    # Taskbar icon on Windows
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("mrmartin.dashboard")
    except Exception:
        pass

    # ── Instance unique (named mutex Windows) ─────────────────────────────────
    _instance_mutex = None
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            _ERROR_ALREADY_EXISTS = 183
            _instance_mutex = _ct.windll.kernel32.CreateMutexW(
                None, False, "Global\\MrMartinDashboard"
            )
            if _ct.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
                _ct.windll.user32.MessageBoxW(
                    0,
                    "Mr Martin est déjà ouvert.\n\nFerme la fenêtre existante avant d'en ouvrir une nouvelle.",
                    "Mr Martin — Déjà en cours",
                    0x30,  # MB_ICONWARNING | MB_OK
                )
                sys.exit(0)
        except Exception:
            pass  # Si ctypes échoue, on laisse quand même l'app démarrer

    ft.run(main)
