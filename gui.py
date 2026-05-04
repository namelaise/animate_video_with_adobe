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
GUI_CONFIG_FILE        = BASE_DIR / "config" / "gui_config.json"
SCRAPER_CONFIG_FILE    = BASE_DIR / "config" / "scraper_config.json"
ACCOUNTS_FILE          = BASE_DIR / "config" / "tiktok_accounts.json"
PIPELINE_STATE_FILE    = BASE_DIR / "pipeline_state.json"
VENV_PYTHON = BASE_DIR / "venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
DEFAULT_DAILY_LIMIT = 5

# Palette premium dark
BG          = "#09090f"
SURFACE     = "#0f0f17"
SURFACE_HIGH= "#16161f"
SURFACE_TOP = "#1d1d28"
BORDER      = "rgba(255,255,255,0.07)"
ACCENT      = "#7c3aed"
ACCENT_DIM  = "rgba(124,58,237,0.12)"
ON_SURFACE  = "#e2e8f0"
OUTLINE     = "#475569"
PRIMARY     = "#a78bfa"
ON_PRIMARY  = "#1e1b4b"
SECONDARY   = "#c4b5fd"
ERROR       = "#fca5a5"
ERROR_C     = "#ef4444"
ERROR_DIM   = "rgba(239,68,68,0.08)"
SUCCESS     = "#22c55e"
SUCCESS_DIM = "rgba(34,197,94,0.08)"
WARN_C      = "#f59e0b"
WARN_DIM    = "rgba(245,158,11,0.08)"
MUTED       = "#64748b"

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
        color_scheme_seed="#7c3aed",
        font_family="Segoe UI",
    )
    page.padding = 0
    page.bgcolor = BG
    page.window.width = 1400
    page.window.height = 900
    page.window.min_width = 1000
    page.window.min_height = 700

    # Icon — .ico en priorité pour la barre des tâches Windows
    ico = BASE_DIR / "assets" / "app_icon.ico"
    if not ico.exists():
        ico = BASE_DIR / "assets" / "app_icon.png"
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
        colors = {"success": SUCCESS, "error": ERROR_C, "warning": WARN_C, "info": ACCENT}
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
        val = ft.Text(value_ref, size=32, weight=ft.FontWeight.BOLD, color=ON_SURFACE)
        sub = ft.Text(subtitle_ref or "", size=11, color=OUTLINE)
        card = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Container(
                        content=ft.Icon(icon, size=16, color=PRIMARY),
                        width=30, height=30,
                        border_radius=8,
                        bgcolor=ACCENT_DIM,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text(title, size=11, color=OUTLINE, weight=ft.FontWeight.W_500),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Container(height=8),
                val,
                sub,
            ], spacing=2, tight=True),
            bgcolor=SURFACE_HIGH,
            border_radius=14,
            padding=ft.Padding.all(20),
            border=ft.border.all(1, BORDER),
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
        content=ft.Text("En attente", size=10, weight=ft.FontWeight.W_600, color=OUTLINE),
        bgcolor=SURFACE_TOP,
        border_radius=20,
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        border=ft.border.all(1, BORDER),
    )
    pipeline_progress = ft.ProgressBar(value=0, height=4, color=ACCENT,
                                        bgcolor=SURFACE_TOP)

    step_dots = []
    step_labels = []
    step_times = []
    step_rows = []

    for i, (name, _) in enumerate(PIPELINE_STEPS):
        dot = ft.Container(width=10, height=10, border_radius=5,
                           bgcolor=SURFACE_TOP,
                           border=ft.border.all(2, BORDER))
        label = ft.Text(name, size=12, color=OUTLINE)
        time_lbl = ft.Text("", size=10, color=OUTLINE, font_family="Cascadia Code")
        row = ft.Row([dot, label, ft.Container(expand=True), time_lbl],
                     spacing=10, height=28, key=f"step_{i}")
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
        bgcolor=SURFACE_HIGH,
        border_radius=14,
        padding=ft.Padding.all(20),
        border=ft.border.all(1, BORDER),
        width=300,
        expand=True,
    )

    def set_pipeline_step(idx, status, elapsed=""):
        if idx < 0 or idx >= len(PIPELINE_STEPS):
            return
        if status == "running":
            step_dots[idx].bgcolor = ACCENT
            step_dots[idx].border = ft.border.all(2, PRIMARY)
            step_labels[idx].color = PRIMARY
            step_labels[idx].weight = ft.FontWeight.W_600
            pipeline_progress.value = (idx + 0.5) / len(PIPELINE_STEPS)
            pipeline_badge.content.value = PIPELINE_STEPS[idx][0]
            pipeline_badge.content.color = PRIMARY
            pipeline_badge.bgcolor = ACCENT_DIM
            pipeline_badge.border = ft.border.all(1, ACCENT)
            try:
                pipeline_col.scroll_to(key=f"step_{idx}", duration=200)
            except Exception:
                pass
        elif status == "done":
            step_dots[idx].bgcolor = SUCCESS
            step_dots[idx].border = ft.border.all(2, SUCCESS)
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
            step_dots[idx].border = ft.border.all(2, ERROR_C)
            step_labels[idx].color = ERROR_C
            step_labels[idx].weight = ft.FontWeight.BOLD
            pipeline_badge.content.value = "Erreur"
            pipeline_badge.content.color = ERROR_C
            pipeline_badge.bgcolor = ERROR_DIM
            pipeline_badge.border = ft.border.all(1, ERROR_C)

    def reset_pipeline():
        for i in range(len(PIPELINE_STEPS)):
            step_dots[i].bgcolor = SURFACE_TOP
            step_dots[i].border = ft.border.all(2, BORDER)
            step_labels[i].color = OUTLINE
            step_labels[i].weight = None
            step_times[i].value = ""
        pipeline_progress.value = 0
        pipeline_progress.color = ACCENT
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
            step_dots[0].border = ft.border.all(2, SUCCESS)
            step_labels[0].color = SUCCESS
            pipeline_badge.content.value = "Video disponible"
            pipeline_badge.content.color = SUCCESS
            pipeline_badge.bgcolor = SUCCESS_DIM
            pipeline_badge.border = ft.border.all(1, SUCCESS)
        else:
            step_dots[0].bgcolor = SURFACE_TOP
            step_dots[0].border = ft.border.all(2, BORDER)
            step_labels[0].color = OUTLINE
            pipeline_badge.content.value = "En attente de video"
            pipeline_badge.content.color = OUTLINE
            pipeline_badge.bgcolor = SURFACE_TOP
            pipeline_badge.border = ft.border.all(1, BORDER)

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
            pipeline_badge.content.color = SUCCESS
            pipeline_badge.bgcolor = SUCCESS_DIM
            pipeline_badge.border = ft.border.all(1, SUCCESS)
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
            return ft.Container(
                content=ft.Text(line[:400], size=10, color="#2d3748",
                                font_family="Cascadia Code"),
                padding=ft.Padding.symmetric(horizontal=12, vertical=1),
            )
        ts      = m.group(2)
        level   = m.group(3)
        module  = m.group(4)
        msg     = m.group(5)

        level_color = {"ERROR": ERROR_C, "WARNING": WARN_C, "DEBUG": PRIMARY, "INFO": "#22c55e"}[level]
        left_color  = {"ERROR": ERROR_C, "WARNING": WARN_C, "DEBUG": ACCENT, "INFO": SURFACE_TOP}[level]
        msg_color   = {"ERROR": "#fca5a5", "WARNING": "#fde68a", "DEBUG": "#c4b5fd", "INFO": ON_SURFACE}[level]
        mod_colors  = {"pipeline": PRIMARY, "scheduler": "#38bdf8", "scraper": "#34d399"}
        mod_color   = mod_colors.get(module, OUTLINE)

        return ft.Container(
            content=ft.Row([
                ft.Container(width=3, bgcolor=level_color, border_radius=ft.border_radius.only(top_left=2, bottom_left=2)),
                ft.Text(ts, size=10, color=MUTED, font_family="Cascadia Code", width=62, no_wrap=True),
                ft.Container(
                    content=ft.Text(module[:8], size=9, color=mod_color, weight=ft.FontWeight.W_600),
                    width=66,
                ),
                ft.Text(msg, size=11, color=msg_color, expand=True, overflow=ft.TextOverflow.ELLIPSIS, no_wrap=True),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor={"ERROR": ERROR_DIM, "WARNING": WARN_DIM}.get(level),
            border_radius=4,
            padding=ft.Padding.only(top=3, bottom=3, right=10),
            height=26,
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
        return added

    def _start_action_log(action_name: str):
        """Bascule le panel de log vers une action manuelle."""
        log_source["ref"] = "action"
        log_list.controls.clear()
        log_list.controls.append(ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.TERMINAL, size=14, color=ACCENT),
                ft.Text(f"▶  {action_name}", size=12,
                        weight=ft.FontWeight.BOLD, color=PRIMARY),
            ], spacing=8),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            bgcolor=ACCENT_DIM, border_radius=4,
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
                ft.Container(
                    content=ft.Icon(ft.Icons.TERMINAL, size=14, color=OUTLINE),
                    width=26, height=26, border_radius=6,
                    bgcolor="rgba(255,255,255,0.04)",
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Text("Logs", size=14, weight=ft.FontWeight.W_600, color=ON_SURFACE),
                ft.Container(expand=True),
                ft.Text("pipeline en cours", size=10, color=OUTLINE),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(height=8),
            log_area,
        ], spacing=0, expand=True),
        bgcolor=SURFACE_HIGH,
        border_radius=14,
        padding=ft.Padding.all(16),
        border=ft.border.all(1, BORDER),
        expand=True,
    )

    # ── Upload progress bar ───────────────────────────────
    upload_bar_text = ft.Text("", size=12, color=ON_SURFACE)
    upload_bar_pct = ft.Text("0%", size=12, weight=ft.FontWeight.BOLD, color=PRIMARY)
    upload_bar_progress = ft.ProgressBar(value=0, height=6, color=ACCENT,
                                          bgcolor=SURFACE_TOP)
    upload_bar = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Container(
                    content=ft.Icon(ft.Icons.CLOUD_UPLOAD, color=PRIMARY, size=16),
                    width=28, height=28, border_radius=7,
                    bgcolor=ACCENT_DIM, alignment=ft.Alignment.CENTER,
                ),
                upload_bar_text,
                ft.Container(expand=True),
                upload_bar_pct,
            ], spacing=10),
            ft.Container(height=8),
            upload_bar_progress,
        ], spacing=0),
        bgcolor=SURFACE_HIGH,
        border_radius=12,
        padding=ft.Padding.all(16),
        border=ft.border.all(1, "rgba(124,58,237,0.35)"),
        visible=False,
    )

    # ── Pending posts cards ───────────────────────────────
    pending_row = ft.Row([], spacing=10, scroll=ft.ScrollMode.AUTO)
    pending_section = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.ERROR_OUTLINE, size=16, color=WARN_C),
                ft.Text("Vidéos en échec", size=14, weight=ft.FontWeight.W_600, color=ON_SURFACE),
                ft.Container(expand=True),
                ft.FilledTonalButton("Poster la sélection", icon=ft.Icons.SEND,
                                      on_click=lambda _: post_selected(),
                                      style=ft.ButtonStyle(bgcolor=ACCENT_DIM, color=PRIMARY)),
            ]),
            pending_row,
        ], spacing=10),
        bgcolor=SURFACE_HIGH,
        border_radius=14,
        padding=16,
        border=ft.border.all(1, "rgba(245,158,11,0.2)"),
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
                bgcolor=SURFACE_TOP,
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
                        bgcolor=SURFACE_TOP,
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
                            color=PRIMARY,
                            padding=ft.Padding.symmetric(horizontal=0, vertical=0),
                        ),
                    ) if has_video else ft.Container(),
                ]),
            ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.START),
            width=160,
            bgcolor=SURFACE_TOP if is_selected else SURFACE_HIGH,
            border_radius=12,
            padding=12,
            border=ft.border.all(2, PRIMARY) if is_selected else ft.border.all(1, BORDER),
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
        upload_bar_progress.color = ACCENT
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
            _post_cmd = [PYTHON, "-u", str(BASE_DIR / "pipeline" / "post_tiktok_inbox.py"), "--video", str(vp), "--poll"]
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
                        bgcolor=SURFACE_TOP,
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
            bgcolor=SURFACE_TOP,
            border_color=BORDER,
            focused_border_color=ACCENT,
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
                             bgcolor=SURFACE_TOP,
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
                ft.Icon(ft.Icons.MANAGE_SEARCH, color=ACCENT, size=20),
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
                    ft.Divider(height=12, color=BORDER),
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
                                style=ft.ButtonStyle(bgcolor=ACCENT, color="white")),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _open_dialog(dlg)

    def show_stats(e=None):
        toast("Chargement des stats...", "info")
        def do():
            r = subprocess.run(
                [PYTHON, str(BASE_DIR / "tiktok" / "fetch_stats.py"), "--report"],
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
                        bgcolor=SURFACE_TOP,
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
                bgcolor=SURFACE_HIGH,
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
            bgcolor="rgba(255,255,255,0.05)" if on else SUCCESS,
            color=OUTLINE if on else "white",
            shape=ft.RoundedRectangleBorder(radius=8),
            elevation=0,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        )
        start_btn.disabled = on
        # Bouton Arrêter : rouge quand actif, grisé quand dispo
        stop_btn.style = ft.ButtonStyle(
            bgcolor=ERROR_C if on else "rgba(255,255,255,0.05)",
            color="white" if on else OUTLINE,
            shape=ft.RoundedRectangleBorder(radius=8),
            elevation=0,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
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
                border=ft.Border.all(2, PRIMARY) if is_active else ft.Border.all(1, BORDER),
            )
        else:
            avatar_widget = ft.Container(
                content=ft.Icon(ft.Icons.ACCOUNT_CIRCLE, size=22,
                                color=PRIMARY if is_active else OUTLINE),
                width=44, height=44,
                border_radius=22,
                bgcolor=SURFACE_TOP,
                border=ft.Border.all(2, PRIMARY) if is_active else ft.Border.all(1, BORDER),
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
                ft.Icon(ft.Icons.CHECK_CIRCLE, size=10, color=PRIMARY),
                ft.Text("actif", size=9, color=PRIMARY),
            ], spacing=2),
            bgcolor=ACCENT_DIM,
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
            bgcolor=SURFACE_TOP if is_active else SURFACE_HIGH,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.Border.all(1, PRIMARY) if is_active else ft.Border.all(1, BORDER),
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
                [PYTHON, str(BASE_DIR / "tiktok" / "auth_tiktok_token_manager.py")], cwd=str(BASE_DIR),
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
                [PYTHON, str(BASE_DIR / "tiktok" / "auth_tiktok_refresh.py"), "--account", account_arg],
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
                    [PYTHON, str(BASE_DIR / "tiktok" / "auth_tiktok_token_manager.py")], cwd=str(BASE_DIR),
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
            bgcolor=SURFACE_TOP,
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
            matching_mode_btn.style = ft.ButtonStyle(bgcolor=ACCENT_DIM, color=PRIMARY)
        else:
            matching_mode_btn.text = "Mode auto"
            matching_mode_btn.icon = ft.Icons.SMART_TOY
            matching_mode_btn.style = ft.ButtonStyle(
                bgcolor=SURFACE_TOP, color=OUTLINE)
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
                    cont.border  = ft.Border.all(2, ACCENT) if pid == puppet_id else ft.Border.all(1, BORDER)
                    cont.bgcolor = SURFACE_TOP if pid == puppet_id else SURFACE_HIGH
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
                        ft.Icon(icon_gender, size=30, color=ACCENT),
                        ft.Text(short, size=10, text_align=ft.TextAlign.CENTER,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                    width=88, height=82,
                    border_radius=8,
                    bgcolor=SURFACE_HIGH,
                    border=ft.Border.all(1, BORDER),
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
                    ft.Icon(ft.Icons.PERSON_SEARCH, color=ACCENT, size=22),
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
                        style=ft.ButtonStyle(bgcolor=ACCENT, color="white"),
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
    start_btn = ft.ElevatedButton(
        "Démarrer", icon=ft.Icons.PLAY_ARROW,
        on_click=start_scheduler,
        style=ft.ButtonStyle(
            bgcolor=SUCCESS, color="white",
            shape=ft.RoundedRectangleBorder(radius=8),
            elevation=0,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        ),
        expand=True,
    )
    stop_btn = ft.ElevatedButton(
        "Arrêter", icon=ft.Icons.STOP,
        on_click=stop_scheduler,
        style=ft.ButtonStyle(
            bgcolor="rgba(255,255,255,0.05)", color=OUTLINE,
            shape=ft.RoundedRectangleBorder(radius=8),
            elevation=0,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        ),
        disabled=True,
        expand=True,
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
            bgcolor=SURFACE_TOP,
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=6, vertical=2),
        ),
        ft.IconButton(ft.Icons.ADD, icon_size=16,
                      on_click=lambda _: _conc_change(+1),
                      style=ft.ButtonStyle(padding=0)),
    ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _nav_btn(icon, label, subtitle, on_click_fn, color=None, danger=False):
        ic = ERROR_C if danger else (color or OUTLINE)
        lc = ERROR_C if danger else (color or ON_SURFACE)
        return ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Icon(icon, size=15, color=ic),
                    width=30, height=30, border_radius=8,
                    bgcolor="rgba(239,68,68,0.10)" if danger else (ACCENT_DIM if color else "rgba(255,255,255,0.04)"),
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Column([
                    ft.Text(label, size=12, color=lc, weight=ft.FontWeight.W_500),
                    ft.Text(subtitle, size=10, color=OUTLINE),
                ], spacing=1, tight=True, expand=True),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=8,
            on_click=on_click_fn,
            ink=True,
            ink_color="rgba(124,58,237,0.06)",
        )

    sidebar = ft.Container(
        content=ft.Column([
            # ── Brand ──────────────────────────────────────
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Text("M", size=16, weight=ft.FontWeight.BOLD, color="white"),
                        width=36, height=36, border_radius=10,
                        bgcolor=ACCENT,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column([
                        ft.Text("Mr Martin", size=14, weight=ft.FontWeight.BOLD, color=ON_SURFACE),
                        ft.Text("Video Automation", size=10, color=OUTLINE),
                    ], spacing=1, tight=True),
                ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding.only(bottom=16),
            ),
            ft.Divider(height=1, color=BORDER),

            # ── Comptes TikTok ──────────────────────────────
            ft.Container(
                content=ft.Text("COMPTES", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=12, bottom=6, left=2),
            ),
            accounts_col,
            ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.ADD, size=14, color=PRIMARY),
                            ft.Text("Connecter", size=12, color=PRIMARY),
                        ], spacing=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        border_radius=8,
                        bgcolor=ACCENT_DIM,
                        on_click=connect_new_account,
                        ink=True,
                        expand=True,
                    ),
                    ft.Container(width=8),
                    ft.Container(
                        content=ft.Icon(ft.Icons.SYNC, size=15, color=OUTLINE),
                        width=36, height=36, border_radius=8,
                        bgcolor="rgba(255,255,255,0.04)",
                        alignment=ft.Alignment.CENTER,
                        on_click=refresh_profiles,
                        ink=True,
                        tooltip="Actualiser noms et photos",
                    ),
                ], spacing=0),
            ),
            ft.Divider(height=1, color=BORDER),

            # ── Automatisation ──────────────────────────────
            ft.Container(
                content=ft.Text("AUTOMATISATION", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=12, bottom=8, left=2),
            ),
            ft.Row([start_btn, stop_btn], spacing=8),
            ft.Container(height=4),
            matching_mode_btn,
            ft.Container(height=4),
            _conc_row,
            ft.Divider(height=1, color=BORDER),

            # ── Actions ─────────────────────────────────────
            ft.Container(
                content=ft.Text("ACTIONS", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=12, bottom=4, left=2),
            ),
            _nav_btn(ft.Icons.SEARCH, "Scraper des vidéos", "Télécharge depuis TikTok", run_scraper),
            _nav_btn(ft.Icons.TUNE, "Config scraping", "Comptes, quota, durées", show_scraper_config, color=PRIMARY),
            _nav_btn(ft.Icons.SEND, "Poster la dernière vidéo", "Envoie sur TikTok", post_latest, color=SUCCESS),
            _nav_btn(ft.Icons.VPN_KEY, "Rafraîchir le token", "Renouvelle l'accès TikTok", refresh_token_active),
            ft.Divider(height=1, color=BORDER),

            # ── Outils ──────────────────────────────────────
            ft.Container(
                content=ft.Text("OUTILS", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=12, bottom=4, left=2),
            ),
            _nav_btn(ft.Icons.BAR_CHART, "Stats TikTok", "Performances par prompt", show_stats),
            _nav_btn(ft.Icons.HISTORY, "Historique uploads", "20 derniers envois", show_upload_history),
            _nav_btn(ft.Icons.FOLDER_OPEN, "Ouvrir download/", "Vidéos en attente", open_download_folder),
            ft.Divider(height=1, color=BORDER),

            # ── Maintenance ──────────────────────────────────
            ft.Container(
                content=ft.Text("MAINTENANCE", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=12, bottom=4, left=2),
            ),
            _nav_btn(ft.Icons.RESTART_ALT, "Reset quota", "Remet le compteur à zéro", reset_quota),
            _nav_btn(ft.Icons.SYSTEM_UPDATE_ALT, "Mettre à jour yt-dlp", "Évite les blocages TikTok", update_ytdlp),
            _nav_btn(ft.Icons.REPLAY, "Réinitialiser pipeline", "Repart de l'état zéro", reset_pipeline_state, danger=True),
            _nav_btn(ft.Icons.DELETE_SWEEP, "Vider download/", "Supprime la queue", clear_download, danger=True),

            # ── Horloge ─────────────────────────────────────
            ft.Container(
                content=ft.Text(datetime.now().strftime("%H:%M"), size=11, color=MUTED),
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.only(top=12),
            ),
        ], spacing=2, scroll=ft.ScrollMode.AUTO, expand=True),
        width=270,
        bgcolor=SURFACE,
        border=ft.border.only(right=ft.border.BorderSide(1, BORDER)),
        padding=ft.Padding.all(16),
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
        padding=ft.Padding.all(20),
        bgcolor=BG,
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
                if changed:
                    try:
                        log_list.scroll_to(offset=float("inf"), duration=0)
                        page.update()
                    except Exception:
                        pass
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
