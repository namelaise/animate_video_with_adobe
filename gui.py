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


# ── Singleton : empêche le démarrage de plusieurs GUI en parallèle ──
def _ensure_single_instance() -> None:
    """Verrou socket : si un autre gui.py tourne déjà, ferme proprement.
    Utilise un socket TCP local plutôt qu'un fichier — auto-libéré au crash."""
    import socket
    _SINGLETON_PORT = 53951  # choisi pour ne pas collisionner avec l'OAuth (53682)
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        _sock.bind(("127.0.0.1", _SINGLETON_PORT))
        _sock.listen(1)
    except OSError:
        print("[INFO] Une autre instance du GUI Mr Martin est déjà ouverte. Sortie.")
        sys.exit(0)
    # Garde la référence pour que le socket vive aussi longtemps que le processus
    globals()["_SINGLETON_SOCK"] = _sock


_ensure_single_instance()

DOWNLOAD_DIR = BASE_DIR / "download"
PENDING_DIR = BASE_DIR / "pending_posts"
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DAILY_LOG              = STATE_DIR / "scraper_daily.json"
LOGS_DIR               = BASE_DIR / "logs"
HISTORY_FILE           = STATE_DIR / "upload_history.json"
MATCHING_MODE_FILE     = STATE_DIR / "matching_mode.json"
MATCHING_REQUEST_FILE  = STATE_DIR / "matching_request.json"
MATCHING_RESPONSE_FILE = STATE_DIR / "matching_response.json"
GUI_CONFIG_FILE        = BASE_DIR / "config" / "gui_config.json"
SCRAPER_CONFIG_FILE    = BASE_DIR / "config" / "scraper_config.json"
ACCOUNTS_FILE          = BASE_DIR / "config" / "tiktok_accounts.json"
PIPELINE_STATE_FILE    = STATE_DIR / "pipeline_state.json"
SCHEDULED_FILE         = STATE_DIR / "scheduled_posts.json"
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

    def _on_page_error(e):
        msg = str(getattr(e, "data", e))[:120]
        try:
            sb = ft.SnackBar(
                content=ft.Row([
                    ft.Icon(ft.Icons.BUG_REPORT, color=ERROR_C, size=18),
                    ft.Text(f"Erreur UI : {msg}", color="white", size=12, expand=True),
                ], spacing=10),
                bgcolor="#2d1a1a",
                duration=6000,
            )
            page.overlay.append(sb)
            sb.open = True
            page.update()
        except Exception:
            pass

    page.on_error = _on_page_error

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

    def _open_post_composer(vdir):
        """
        Dialog conforme aux exigences UX TikTok pour Direct Post (audit) :
        - Affiche nickname du créateur
        - Preview vidéo + durée
        - Champ titre éditable
        - Dropdown Privacy (peuplé depuis creator_info, sans valeur par défaut)
        - Checkboxes Comment/Duet/Stitch (off par défaut, grisées si désactivées côté créateur)
        - Toggle Commercial Content + Brand/Branded Content avec règles
        - Déclaration légale dynamique
        - Bouton Publier conditionnel (consentement explicite)
        """
        vp = vdir / "video_final.mp4"
        if not vp.exists():
            toast(f"Pas de video dans {vdir.name}", "error")
            return

        _pipeline_dir = str(BASE_DIR / "pipeline")
        if _pipeline_dir not in sys.path:
            sys.path.insert(0, _pipeline_dir)

        try:
            from tiktok_publish_helpers import (
                query_creator_info, get_video_duration, get_video_dimensions,
                extract_thumbnail, CreatorInfoError,
            )
        except Exception as ex:
            toast(f"Helpers indisponibles: {ex}", "error")
            return

        # Token actif
        _accounts_data, _active_id = _load_accounts_data()
        _active_tok = ""
        _active_label = "?"
        if _active_id:
            for _a in _accounts_data:
                if _a.get("id") == _active_id:
                    _active_tok = (_a.get("access_token") or "").strip()
                    _active_label = _a.get("display_name") or _a.get("username") or _a.get("id")
                    break
        if not _active_tok:
            toast("Aucun compte TikTok actif. Connecte-toi d'abord.", "error")
            return

        # ── Logger dédié dialog ──
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        composer_log_path = LOGS_DIR / "upload_tiktok.log"

        def _clog(level: str, msg: str):
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                with composer_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{ts}] {level:<8} [upload] composer: {msg}\n")
            except Exception:
                pass

        _clog("INFO", f"=== Ouverture composer pour {vdir.name} ===")
        _clog("INFO", f"Token len={len(_active_tok)} actif={_active_id}")

        # ── État partagé du dialog ─────────────────────────────────────────
        state = {
            "creator_info": None,
            "duration": None,
            "thumb_path": None,
            "dlg": None,
        }

        # ── Widgets : version "Chargement" affichée tout de suite ──────────
        loading_text = ft.Text("Chargement des informations du créateur...",
                               size=13, color=ON_SURFACE)
        loading_spinner = ft.ProgressRing(width=24, height=24, color=ACCENT)
        loading_content = ft.Column(
            [ft.Row([loading_spinner, loading_text], spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            tight=True, height=80, alignment=ft.MainAxisAlignment.CENTER,
        )

        def _close():
            if state["dlg"]:
                state["dlg"].open = False
                page.update()

        dlg = ft.AlertDialog(
            modal=False,
            title=ft.Text("Publier sur TikTok", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=loading_content, width=560),
            actions=[ft.TextButton("Annuler", on_click=lambda e: _close())],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        state["dlg"] = dlg
        _open_dialog(dlg)

        def _fail_load(msg: str):
            _clog("ERROR", f"_fail_load: {msg[:300]}")
            def upd():
                try:
                    dlg.content = ft.Container(content=ft.Column([
                        ft.Icon(ft.Icons.ERROR_OUTLINE, color=ERROR_C, size=32),
                        ft.Text("Impossible de préparer le post", size=14, weight=ft.FontWeight.BOLD),
                        ft.Container(
                            content=ft.Text(msg, size=11, color=OUTLINE, selectable=True),
                            padding=8,
                            bgcolor=SURFACE_HIGH,
                            border_radius=6,
                        ),
                    ], spacing=8, scroll=ft.ScrollMode.AUTO), width=560, height=300)
                    try:
                        dlg.update()
                    except Exception:
                        pass
                    page.update()
                except Exception as ex:
                    _clog("ERROR", f"_fail_load.upd erreur: {ex}")
            page.run_thread(upd)

        def _build_form():
            """Construit le formulaire complet une fois creator_info chargé."""
            ci = state["creator_info"] or {}
            duration = state["duration"]
            nickname = ci.get("creator_nickname") or ci.get("creator_username") or _active_label
            avatar_url = ci.get("creator_avatar_url") or ""

            privacy_options = ci.get("privacy_level_options") or []
            if not privacy_options:
                privacy_options = ["SELF_ONLY"]  # fallback minimal
            max_dur = ci.get("max_video_post_duration_sec") or 0

            # Caption pré-remplie depuis caption.txt si dispo
            preset_caption = ""
            caption_path = vdir / "caption.txt"
            if caption_path.exists():
                try:
                    preset_caption = caption_path.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

            # ── Widgets formulaire ──
            avatar_widget = (
                ft.CircleAvatar(foreground_image_src=avatar_url, radius=18)
                if avatar_url else
                ft.CircleAvatar(content=ft.Text(nickname[:1].upper()), radius=18, bgcolor=ACCENT_DIM)
            )
            header = ft.Row([
                avatar_widget,
                ft.Column([
                    ft.Text(f"Publication sur le profil de", size=11, color=OUTLINE),
                    ft.Text(f"@{nickname}", size=14, weight=ft.FontWeight.BOLD, color=ON_SURFACE),
                ], spacing=2, tight=True),
            ], spacing=10)

            # Preview vidéo
            thumb_src = state["thumb_path"]
            preview_img = (
                ft.Image(src=str(thumb_src), width=120, height=160,
                         fit="cover", border_radius=8)
                if thumb_src and Path(thumb_src).exists()
                else ft.Container(content=ft.Icon(ft.Icons.VIDEO_FILE, size=40, color=OUTLINE),
                                  width=120, height=160, border_radius=8,
                                  bgcolor=SURFACE_HIGH, alignment=ft.Alignment.CENTER)
            )
            dur_str = f"{duration:.1f}s" if duration else "?"
            max_str = f" / max {max_dur}s" if max_dur else ""
            preview_block = ft.Row([
                preview_img,
                ft.Column([
                    ft.Text(vdir.name, size=12, weight=ft.FontWeight.W_600, color=ON_SURFACE),
                    ft.Text(f"Durée : {dur_str}{max_str}", size=11, color=OUTLINE),
                    ft.Text(f"Fichier : {vp.stat().st_size // (1024*1024)} MB", size=11, color=OUTLINE),
                ], spacing=4, tight=True),
            ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START)

            # Title (éditable)
            title_field = ft.TextField(
                label="Titre / Description (max 2200 caractères)",
                value=preset_caption,
                multiline=True,
                min_lines=2,
                max_lines=4,
                max_length=2200,
            )

            # Privacy dropdown — SANS valeur par défaut (exigence TikTok)
            privacy_labels = {
                "PUBLIC_TO_EVERYONE": "Tout le monde",
                "MUTUAL_FOLLOW_FRIENDS": "Amis (abonnés mutuels)",
                "FOLLOWER_OF_CREATOR": "Abonnés",
                "SELF_ONLY": "Seulement moi (privé)",
            }
            privacy_dropdown = ft.Dropdown(
                label="Confidentialité *",
                hint_text="Sélectionne qui peut voir cette vidéo",
                options=[ft.dropdown.Option(key=p, text=privacy_labels.get(p, p))
                         for p in privacy_options],
                value=None,  # AUCUN défaut
            )

            # Interactions — OFF par défaut, grisées si désactivées côté créateur
            comment_disabled = bool(ci.get("comment_disabled"))
            duet_disabled    = bool(ci.get("duet_disabled"))
            stitch_disabled  = bool(ci.get("stitch_disabled"))

            cb_comment = ft.Checkbox(label="Autoriser les commentaires", value=False,
                                     disabled=comment_disabled)
            cb_duet    = ft.Checkbox(label="Autoriser les Duets", value=False,
                                     disabled=duet_disabled)
            cb_stitch  = ft.Checkbox(label="Autoriser les Stitch", value=False,
                                     disabled=stitch_disabled)
            interactions_block = ft.Column([
                ft.Text("Interactions autorisées", size=12, weight=ft.FontWeight.W_600,
                        color=ON_SURFACE),
                cb_comment, cb_duet, cb_stitch,
            ], spacing=4)

            # Commercial content disclosure
            commercial_toggle = ft.Switch(label="Cette vidéo fait la promotion d'une marque, d'un produit ou d'un service",
                                          value=False)
            cb_your_brand = ft.Checkbox(label="Mon propre contenu (Your Brand)", value=False, visible=False)
            cb_branded_content = ft.Checkbox(label="Marque tierce / partenariat rémunéré (Branded Content)",
                                              value=False, visible=False)
            brand_prompt = ft.Text("", size=11, color=WARN_C, visible=False)
            commercial_section = ft.Column([
                commercial_toggle,
                ft.Container(content=ft.Column([cb_your_brand, cb_branded_content, brand_prompt],
                                               spacing=4),
                             padding=ft.Padding.symmetric(horizontal=24, vertical=0)),
            ], spacing=4)

            # Déclaration légale dynamique
            DECLARATION_DEFAULT = "En publiant, vous acceptez la confirmation d'utilisation musicale de TikTok."
            DECLARATION_BRANDED = ("En publiant, vous acceptez la politique de contenu de marque de TikTok "
                                   "et la confirmation d'utilisation musicale.")
            declaration = ft.Text(DECLARATION_DEFAULT, size=11, color=OUTLINE,
                                  italic=True)
            processing_note = ft.Row([
                ft.Icon(ft.Icons.INFO_OUTLINE, size=14, color=OUTLINE),
                ft.Text("Après publication, la vidéo peut mettre quelques minutes à apparaître sur votre profil.",
                        size=11, color=OUTLINE, expand=True),
            ], spacing=6)

            # Indicateur du mode actif (DIRECT vs INBOX)
            direct_enabled = os.getenv("TIKTOK_DIRECT_POST_ENABLED", "0").strip() == "1"
            mode_label = (
                "Mode actuel : Publication directe sur ton profil"
                if direct_enabled else
                "Mode actuel : Envoi en brouillon (pré-audit) — tu finaliseras dans l'app TikTok"
            )
            mode_indicator = ft.Container(
                content=ft.Row([
                    ft.Icon(
                        ft.Icons.PUBLIC if direct_enabled else ft.Icons.DRAFTS,
                        size=14, color=SUCCESS if direct_enabled else WARN_C,
                    ),
                    ft.Text(mode_label, size=11,
                            color=SUCCESS if direct_enabled else WARN_C, expand=True),
                ], spacing=6),
                padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                bgcolor=("rgba(34,197,94,0.08)" if direct_enabled else "rgba(245,158,11,0.08)"),
                border_radius=8,
            )

            # Bouton Publier (initialement disabled)
            publish_btn = ft.FilledButton(
                "Publier" if direct_enabled else "Envoyer en brouillon",
                icon=ft.Icons.SEND, disabled=True,
                style=ft.ButtonStyle(bgcolor=ACCENT, color="white"),
            )

            # Aide visible sous le bouton expliquant pourquoi il est désactivé
            publish_hint = ft.Text(
                "⚠ Sélectionne d'abord la confidentialité ci-dessus pour activer le bouton.",
                size=11, color=WARN_C, visible=True,
            )

            # ── Logique conditionnelle ──
            def _refresh_conditions(e=None):
                commercial_on = commercial_toggle.value
                cb_your_brand.visible = commercial_on
                cb_branded_content.visible = commercial_on
                brand_prompt.visible = commercial_on
                if not commercial_on:
                    cb_your_brand.value = False
                    cb_branded_content.value = False

                # Branded Content interdit avec SELF_ONLY → on désactive l'option dans le dropdown
                # En Flet on ne peut pas désactiver une option spécifique facilement,
                # donc on signale via le prompt et on bloque le bouton.
                branded_with_private = cb_branded_content.value and privacy_dropdown.value == "SELF_ONLY"

                # Prompts label dynamiques
                if commercial_on:
                    if cb_your_brand.value and cb_branded_content.value:
                        brand_prompt.value = "Votre vidéo sera étiquetée 'Partenariat rémunéré'."
                    elif cb_branded_content.value:
                        brand_prompt.value = "Votre vidéo sera étiquetée 'Partenariat rémunéré'."
                    elif cb_your_brand.value:
                        brand_prompt.value = "Votre vidéo sera étiquetée 'Contenu promotionnel'."
                    else:
                        brand_prompt.value = "Vous devez indiquer si votre contenu fait la promotion de vous-même, d'un tiers, ou des deux."
                    brand_prompt.color = ERROR_C if (not cb_your_brand.value and not cb_branded_content.value) else WARN_C
                if branded_with_private:
                    brand_prompt.value = "La visibilité 'Privé' n'est pas compatible avec un Branded Content. Choisissez une autre option."
                    brand_prompt.color = ERROR_C
                    brand_prompt.visible = True

                # Déclaration légale
                if commercial_on and cb_branded_content.value:
                    declaration.value = DECLARATION_BRANDED
                else:
                    declaration.value = DECLARATION_DEFAULT

                # Bouton Publier — toutes les conditions
                # En mode INBOX (brouillon), la privacy n'est PAS envoyée à TikTok
                # (post_tiktok_inbox.py:121 — privacy obligatoire uniquement avec --direct).
                # Donc ne pas la rendre bloquante pour le bouton "Envoyer en brouillon".
                ok_privacy = bool(privacy_dropdown.value) if direct_enabled else True
                ok_commercial = (not commercial_on) or (cb_your_brand.value or cb_branded_content.value)
                ok_branded_privacy = not branded_with_private
                ok_duration = (max_dur == 0) or (duration is None) or (duration <= max_dur)
                publish_btn.disabled = not (ok_privacy and ok_commercial and ok_branded_privacy and ok_duration)

                # Aide dynamique : explique quelle condition manque
                if not ok_privacy:
                    publish_hint.value = "⚠ Sélectionne d'abord la confidentialité ci-dessus pour activer le bouton."
                    publish_hint.color = WARN_C
                    publish_hint.visible = True
                elif not ok_commercial:
                    publish_hint.value = "⚠ Indique si ton contenu fait la promotion d'une marque (case à cocher)."
                    publish_hint.color = WARN_C
                    publish_hint.visible = True
                elif not ok_branded_privacy:
                    publish_hint.value = "⚠ Branded Content incompatible avec 'Privé'. Change la confidentialité."
                    publish_hint.color = ERROR_C
                    publish_hint.visible = True
                elif not ok_duration:
                    publish_hint.value = f"⚠ Vidéo trop longue ({duration:.0f}s > max {max_dur}s)."
                    publish_hint.color = ERROR_C
                    publish_hint.visible = True
                else:
                    publish_hint.visible = False

                _clog("DEBUG", f"_refresh_conditions: btn_disabled={publish_btn.disabled} "
                                f"privacy={privacy_dropdown.value} commercial={commercial_on}")
                page.update()

            commercial_toggle.on_change = _refresh_conditions
            cb_your_brand.on_change = _refresh_conditions
            cb_branded_content.on_change = _refresh_conditions
            privacy_dropdown.on_change = _refresh_conditions

            # Évaluation initiale de l'état du bouton (sinon il reste disabled=True
            # tant que l'utilisateur n'a touché à aucun widget). En mode INBOX,
            # ok_privacy=True par défaut → bouton activé immédiatement.
            try:
                commercial_on = commercial_toggle.value
                ok_privacy_init = bool(privacy_dropdown.value) if direct_enabled else True
                ok_commercial_init = (not commercial_on) or (cb_your_brand.value or cb_branded_content.value)
                ok_duration_init = (max_dur == 0) or (duration is None) or (duration <= max_dur)
                publish_btn.disabled = not (ok_privacy_init and ok_commercial_init and ok_duration_init)
                if not direct_enabled and ok_duration_init:
                    publish_hint.visible = False
                _clog("INFO", f"État initial bouton: disabled={publish_btn.disabled} "
                               f"direct={direct_enabled}")
            except Exception as ex:
                _clog("WARNING", f"Init bouton: {ex}")

            # ── Action Publier ──
            def _on_publish(e):
                _clog("INFO", f"_on_publish CLIQUÉ — direct={direct_enabled} "
                               f"privacy={privacy_dropdown.value}")
                try:
                    metadata = {
                        "title": title_field.value or "",
                        "privacy_level": privacy_dropdown.value,
                        "allow_comment": bool(cb_comment.value),
                        "allow_duet": bool(cb_duet.value),
                        "allow_stitch": bool(cb_stitch.value),
                        "brand_organic": bool(commercial_toggle.value and cb_your_brand.value),
                        "brand_content": bool(commercial_toggle.value and cb_branded_content.value),
                    }
                    # Sauve la caption éditée pour les retries futurs
                    try:
                        if metadata["title"]:
                            (vdir / "caption.txt").write_text(metadata["title"], encoding="utf-8")
                    except Exception:
                        pass
                    _close()
                    if direct_enabled:
                        _clog("INFO", "→ _execute_direct_post()")
                        _execute_direct_post(vdir, metadata)
                    else:
                        # Pré-audit : upload INBOX. Les métadonnées sont sauvées dans
                        # caption.txt et serviront quand TIKTOK_DIRECT_POST_ENABLED=1.
                        _clog("INFO", "→ _post_video_inbox_simple()")
                        _post_video_inbox_simple(vdir)
                except Exception as ex:
                    import traceback
                    _clog("ERROR", f"_on_publish exception: {ex}\n{traceback.format_exc()}")
                    try:
                        toast(f"Erreur publication: {ex}", "error")
                    except Exception:
                        pass

            publish_btn.on_click = _on_publish

            # ── Composition finale du dialog ──
            form = ft.Column([
                header,
                ft.Divider(height=8, color=OUTLINE),
                preview_block,
                ft.Container(height=4),
                title_field,
                privacy_dropdown,
                interactions_block,
                ft.Divider(height=8, color=OUTLINE),
                commercial_section,
                ft.Divider(height=8, color=OUTLINE),
                processing_note,
                declaration,
                mode_indicator,
                publish_hint,
            ], spacing=10, scroll=ft.ScrollMode.AUTO, tight=False)

            def upd():
                try:
                    dlg.content = ft.Container(content=form, width=560, height=620)
                    dlg.actions = [
                        ft.TextButton("Annuler", on_click=lambda e: _close()),
                        publish_btn,
                    ]
                    try:
                        dlg.update()
                    except Exception:
                        pass
                    page.update()
                    _clog("INFO", "Dialog form rendu (page.update appelé)")
                except Exception as ex:
                    _clog("ERROR", f"upd erreur: {ex}")
            page.run_thread(upd)

        # ── Charger creator_info + durée + thumbnail en arrière-plan ──
        def _load_async():
            import traceback
            _clog("INFO", "_load_async démarré")
            try:
                _clog("INFO", "Appel query_creator_info...")
                try:
                    ci = query_creator_info(_active_tok)
                    _clog("INFO", f"creator_info OK: nickname={ci.get('creator_nickname')} "
                                   f"privacy_options={ci.get('privacy_level_options')} "
                                   f"max_dur={ci.get('max_video_post_duration_sec')}")
                except CreatorInfoError as ex:
                    _clog("ERROR", f"CreatorInfoError: {ex}")
                    _fail_load(f"creator_info indisponible : {ex}")
                    return
                except Exception as ex:
                    _clog("ERROR", f"Exception query_creator_info: {ex}")
                    _fail_load(f"Erreur réseau creator_info : {ex}")
                    return

                if ci.get("status") == "user_quota_exceeded" or ci.get("can_post") is False:
                    _clog("ERROR", f"Quota dépassé: status={ci.get('status')} can_post={ci.get('can_post')}")
                    _fail_load("Le créateur a atteint sa limite de posts pour les 24h. Réessayez plus tard.")
                    return

                state["creator_info"] = ci

                try:
                    state["duration"] = get_video_duration(vp)
                    _clog("INFO", f"Durée vidéo: {state['duration']}s")
                except Exception as ex:
                    _clog("WARNING", f"get_video_duration: {ex}")
                    state["duration"] = None

                try:
                    thumb = vdir / "thumbnail.jpg"
                    if not thumb.exists():
                        _clog("INFO", "Extraction thumbnail...")
                        extract_thumbnail(vp, thumb,
                                          timestamp_s=min(1.0, (state["duration"] or 1.0) / 2))
                    state["thumb_path"] = thumb if thumb.exists() else None
                    _clog("INFO", f"thumb_path={state['thumb_path']}")
                except Exception as ex:
                    _clog("WARNING", f"extract_thumbnail: {ex}")
                    state["thumb_path"] = None

                _clog("INFO", "Appel _build_form()...")
                try:
                    _build_form()
                    _clog("INFO", "_build_form() terminé")
                except Exception as ex:
                    tb = traceback.format_exc()
                    _clog("ERROR", f"Exception _build_form: {ex}\n{tb}")
                    _fail_load(f"Erreur construction formulaire : {ex}\n\n{tb[:600]}")
            except Exception as ex:
                tb = traceback.format_exc()
                _clog("ERROR", f"Exception _load_async: {ex}\n{tb}")
                _fail_load(f"Erreur inattendue : {ex}\n\n{tb[:600]}")

        threading.Thread(target=_load_async, daemon=True).start()

    def _execute_direct_post(vdir, metadata: dict):
        """
        Exécute un Direct Post TikTok avec les métadonnées validées par l'utilisateur
        via le dialog conforme. metadata contient : title, privacy_level, allow_comment,
        allow_duet, allow_stitch, brand_organic, brand_content.
        """
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
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            upload_log_path = LOGS_DIR / "upload_tiktok.log"

            def _log(level: str, msg: str):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                line = f"[{ts}] {level:<8} [upload] {msg}\n"
                try:
                    with upload_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(line)
                except Exception:
                    pass

            def _log_subprocess(raw: str):
                if raw.startswith("[ERR]"):
                    level = "ERROR"
                else:
                    level = "INFO"
                cleaned = re.sub(r'^\[(INFO|OK|ERR)\]\s*', '', raw)
                _log(level, cleaned)

            _log("INFO", f"=== Direct Post conforme depuis GUI: {vdir.name} ===")
            _log("INFO", f"Titre: {metadata.get('title','')[:120]}")
            _log("INFO", f"Privacy: {metadata.get('privacy_level')}")
            _log("INFO", f"Comment={metadata.get('allow_comment')} Duet={metadata.get('allow_duet')} Stitch={metadata.get('allow_stitch')}")
            _log("INFO", f"BrandOrganic={metadata.get('brand_organic')} BrandContent={metadata.get('brand_content')}")

            _accounts_data, _active_id = _load_accounts_data()
            _active_tok = ""
            if _active_id:
                for _a in _accounts_data:
                    if _a.get("id") == _active_id:
                        _active_tok = (_a.get("access_token") or "").strip()
                        break

            cmd = [PYTHON, "-u", str(BASE_DIR / "pipeline" / "post_tiktok_inbox.py"),
                   "--video", str(vp), "--poll", "--direct",
                   "--caption", metadata.get("title", ""),
                   "--privacy", metadata["privacy_level"]]
            if _active_tok:
                cmd += ["--token", _active_tok]
            if metadata.get("allow_comment"):
                cmd.append("--allow-comment")
            if metadata.get("allow_duet"):
                cmd.append("--allow-duet")
            if metadata.get("allow_stitch"):
                cmd.append("--allow-stitch")
            if metadata.get("brand_organic"):
                cmd.append("--brand-organic")
            if metadata.get("brand_content"):
                cmd.append("--brand-content")

            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            last_error = ""
            poll_count = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                _log_subprocess(line)
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
                        upload_bar_text.value = "Verification TikTok (peut prendre quelques minutes)..."
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

                if "success" in ll and "status" in ll:
                    def u4():
                        upload_bar_progress.value = 1.0
                        upload_bar_progress.color = SUCCESS
                        upload_bar_pct.value = "100%"
                        upload_bar_text.value = "Publication reussie !"
                        page.update()
                    page.run_thread(u4)

            proc.wait()
            success = proc.returncode == 0

            if success:
                _log("INFO", f"=== Direct Post réussi: {vdir.name} ===")
            else:
                _log("ERROR", f"=== Direct Post échoué (rc={proc.returncode}): {last_error or 'erreur inconnue'} ===")

            def finish():
                upload_running["ref"] = False
                if success:
                    win_notify("Mr Martin", "Vidéo publiée sur TikTok !")
                    try:
                        shutil.rmtree(vdir)
                        toast(f"{vdir.name} postee et supprimee", "success")
                    except Exception as ex:
                        toast(f"Postee mais erreur suppression: {ex}", "warning")
                    upload_bar_progress.value = 1.0
                    upload_bar_progress.color = SUCCESS
                    upload_bar_pct.value = "100%"
                    upload_bar_text.value = "Publie avec succes (peut prendre qq min pour apparaitre)"
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

    def post_video(vdir):
        """
        Point d'entrée GUI pour poster une vidéo pending.
        Ouvre toujours le dialog conforme TikTok (cohérence UX pour audit).
        Le mode réel d'upload (DIRECT vs INBOX) dépend de TIKTOK_DIRECT_POST_ENABLED.
        """
        _open_post_composer(vdir)

    def _post_video_inbox_simple(vdir):
        """Upload INBOX direct, sans dialog. Utilisé tant que l'app n'est pas auditée Direct Post."""
        vp = vdir / "video_final.mp4"
        if not vp.exists():
            toast(f"Pas de video dans {vdir.name}", "error")
            return

        upload_running["ref"] = True
        upload_bar.visible = True
        upload_bar_text.value = f"Upload {vdir.name} (brouillon)..."
        upload_bar_pct.value = "0%"
        upload_bar_progress.value = 0
        upload_bar_progress.color = ACCENT
        page.update()

        def do():
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            upload_log_path = LOGS_DIR / "upload_tiktok.log"

            def _log(level: str, msg: str):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    with upload_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"[{ts}] {level:<8} [upload] {msg}\n")
                except Exception:
                    pass

            _log("INFO", f"=== Upload INBOX manuel: {vdir.name} ===")

            _accounts_data, _active_id = _load_accounts_data()
            _active_tok = ""
            if _active_id:
                for _a in _accounts_data:
                    if _a.get("id") == _active_id:
                        _active_tok = (_a.get("access_token") or "").strip()
                        break

            cmd = [PYTHON, "-u", str(BASE_DIR / "pipeline" / "post_tiktok_inbox.py"),
                   "--video", str(vp), "--poll"]
            if _active_tok:
                cmd += ["--token", _active_tok]

            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            last_error = ""
            poll_count = 0

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                level = "ERROR" if line.startswith("[ERR]") else "INFO"
                _log(level, re.sub(r'^\[(INFO|OK|ERR)\]\s*', '', line))
                ll = line.lower()

                if any(w in ll for w in ["[err]", "spam_risk", "scope_not", "unaudited", "token_invalid"]):
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
                        upload_bar_text.value = "Brouillon envoye sur TikTok"
                        page.update()
                    page.run_thread(u4)

            proc.wait()
            success = proc.returncode == 0
            _log("INFO" if success else "ERROR",
                 f"=== Upload INBOX {'OK' if success else f'KO (rc={proc.returncode}): {last_error}'} — {vdir.name} ===")

            def finish():
                upload_running["ref"] = False
                if success:
                    win_notify("Mr Martin", "Vidéo envoyée en brouillon TikTok !")
                    try:
                        shutil.rmtree(vdir)
                        toast(f"{vdir.name} envoye en brouillon", "success")
                    except Exception as ex:
                        toast(f"Envoye mais erreur suppression: {ex}", "warning")
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
            deleted = 0
            for f in files:
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
            dlg.open = False
            toast(f"{deleted} vidéo(s) supprimée(s)", "success")
            refresh_all()
            page.update()

        def cancel(ev):
            dlg.open = False
            page.update()

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
            # Travail d'abord, fermeture dialog + update ensuite (pattern sûr Flet 0.84)
            err = None
            try:
                if PIPELINE_STATE_FILE.exists():
                    PIPELINE_STATE_FILE.unlink()
                pipeline_current["ref"] = -1
                on_new_generation()
                reset_pipeline()
            except Exception as ex:
                err = str(ex)
            dlg.open = False
            if err:
                toast(f"Erreur reset pipeline : {err}", "error")
            else:
                switch_view("pipeline")
                toast("Pipeline réinitialisé", "success")
            refresh_all()
            page.update()

        def cancel(ev):
            dlg.open = False
            page.update()

        dlg = ft.AlertDialog(
            title=ft.Text("Réinitialiser le pipeline ?"),
            content=ft.Text(
                "Supprime pipeline_state.json — le pipeline repartira de zéro au prochain run."
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

    # ── Helpers de formatage (partagés par le dashboard) ─────────────────────
    def _fmt_num(n):
        """1234 → '1.2K', 1234567 → '1.2M'."""
        if n is None:
            return "—"
        try:
            n = int(n)
        except Exception:
            return "—"
        if abs(n) >= 1_000_000:
            return f"{n/1_000_000:.1f}M".replace(".0M", "M")
        if abs(n) >= 1_000:
            return f"{n/1_000:.1f}K".replace(".0K", "K")
        return f"{n:,}".replace(",", " ")

    def _fmt_pct(num, denom):
        if not denom:
            return "—"
        return f"{(num / denom * 100):.1f}%"

    def _trend(curr, prev, fmt=_fmt_num):
        """Renvoie (text, color, icon) comparant curr à prev."""
        if not prev:
            return (None, None, None)
        delta = curr - prev
        if delta == 0:
            return ("=", MUTED, ft.Icons.REMOVE)
        pct = (delta / prev) * 100 if prev else 0
        if delta > 0:
            return (f"+{pct:.0f}%", SUCCESS, ft.Icons.ARROW_UPWARD)
        return (f"{pct:.0f}%", ERROR_C, ft.Icons.ARROW_DOWNWARD)

    # État global persistant pour le dashboard (période + onglet)
    _stats_state = globals().setdefault("_stats_state", {"period": 30, "tab": 0,
                                                          "video_sort": "views",
                                                          "video_search": "",
                                                          "tmpl_filter": None})

    def show_stats(e=None):
        if not HISTORY_FILE.exists():
            toast("Aucun historique d'upload trouvé", "warning")
            return
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            toast("Erreur lecture historique", "error")
            return

        from collections import defaultdict, Counter
        import datetime as _dt
        import csv as _csv
        import io as _io
        import webbrowser as _wb

        today = _dt.date.today()
        now   = _dt.datetime.now()

        # ── Filtre par période ───────────────────────────────────────────────
        period = _stats_state["period"]  # 7, 30, 90 ou 0 (tout)

        def _in_period(entry, p):
            if not p:
                return True
            ts = entry.get("timestamp", "")
            try:
                d = _dt.datetime.fromisoformat(ts).date()
            except Exception:
                return False
            return (today - d).days < p

        def _prev_period(entry, p):
            if not p:
                return False
            ts = entry.get("timestamp", "")
            try:
                d = _dt.datetime.fromisoformat(ts).date()
            except Exception:
                return False
            delta = (today - d).days
            return p <= delta < 2 * p

        history_period = [e for e in history if _in_period(e, period)]
        history_prev   = [e for e in history if _prev_period(e, period)]

        # ── Métriques globales (sur la période) ──────────────────────────────
        ok_entries    = [e for e in history_period if e.get("upload_ok")]
        fail_entries  = [e for e in history_period if not e.get("upload_ok")]
        total         = len(history_period)
        total_ok      = len(ok_entries)
        pct_ok        = int(total_ok / total * 100) if total else 0
        with_stats    = [e for e in ok_entries if e.get("view_count") is not None]
        total_views   = sum(e.get("view_count")    or 0 for e in with_stats)
        total_likes   = sum(e.get("like_count")    or 0 for e in with_stats)
        total_comms   = sum(e.get("comment_count") or 0 for e in with_stats)
        total_shares  = sum(e.get("share_count")   or 0 for e in with_stats)
        avg_views     = (total_views // len(with_stats)) if with_stats else 0
        median_views  = 0
        if with_stats:
            sv = sorted(e.get("view_count") or 0 for e in with_stats)
            median_views = sv[len(sv) // 2]
        max_view_entry = max(with_stats, key=lambda x: x.get("view_count") or 0,
                             default=None)
        engagement = 0.0
        if total_views:
            engagement = (total_likes + total_comms + total_shares) / total_views * 100

        # ── Métriques période précédente (pour les tendances) ────────────────
        prev_ok        = [e for e in history_prev if e.get("upload_ok")]
        prev_views     = sum((e.get("view_count") or 0) for e in prev_ok
                             if e.get("view_count") is not None)
        prev_likes     = sum((e.get("like_count") or 0) for e in prev_ok
                             if e.get("like_count") is not None)
        prev_uploads   = len(prev_ok)

        # ── Streak (sur l'historique complet, pas filtré) ────────────────────
        all_dates = {e["timestamp"][:10] for e in history
                     if e.get("upload_ok") and e.get("timestamp")}
        streak = 0
        cur = today
        while cur.isoformat() in all_dates:
            streak += 1
            cur -= _dt.timedelta(days=1)

        # Best streak historique
        best_streak = 0
        if all_dates:
            sorted_d = sorted(all_dates)
            run = 1
            best_streak = 1
            for i in range(1, len(sorted_d)):
                d0 = _dt.date.fromisoformat(sorted_d[i-1])
                d1 = _dt.date.fromisoformat(sorted_d[i])
                if (d1 - d0).days == 1:
                    run += 1
                    best_streak = max(best_streak, run)
                else:
                    run = 1

        active_days = len({e["timestamp"][:10] for e in ok_entries if e.get("timestamp")})

        # ── Données pour graphiques (sur la période) ─────────────────────────
        bar_days = period if period else 30
        days_range = [today - _dt.timedelta(days=i) for i in range(bar_days - 1, -1, -1)]
        by_day_uploads: dict = defaultdict(int)
        by_day_views:   dict = defaultdict(int)
        for e in ok_entries:
            d = e.get("timestamp", "")[:10]
            by_day_uploads[d] += 1
            by_day_views[d] += e.get("view_count") or 0
        day_counts = [by_day_uploads[d.isoformat()] for d in days_range]
        day_views  = [by_day_views[d.isoformat()]   for d in days_range]
        max_day    = max(day_counts, default=1) or 1
        max_dview  = max(day_views,  default=1) or 1

        # Par template (uploads + perf)
        by_template_uploads: dict = defaultdict(int)
        by_template_views:   dict = defaultdict(list)
        by_template_eng:     dict = defaultdict(list)
        for e in ok_entries:
            tmpl = (e.get("prompt_template") or "(inconnu)").replace(".txt", "")
            by_template_uploads[tmpl] += 1
            if e.get("view_count") is not None:
                vc = e.get("view_count") or 0
                by_template_views[tmpl].append(vc)
                if vc:
                    eng = ((e.get("like_count") or 0) + (e.get("comment_count") or 0)
                           + (e.get("share_count") or 0)) / vc * 100
                    by_template_eng[tmpl].append(eng)

        sorted_templates = sorted(by_template_uploads.items(),
                                  key=lambda x: x[1], reverse=True)
        max_tmpl = sorted_templates[0][1] if sorted_templates else 1

        # Heatmap : moyenne par jour de semaine × heure
        hour_buckets = [(0, 6), (6, 12), (12, 18), (18, 24)]
        weekdays_fr = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        heat: dict = defaultdict(lambda: {"count": 0, "views": 0})
        for e in ok_entries:
            ts = e.get("timestamp", "")
            try:
                d = _dt.datetime.fromisoformat(ts)
            except Exception:
                continue
            wd = d.weekday()
            for bi, (h0, h1) in enumerate(hour_buckets):
                if h0 <= d.hour < h1:
                    heat[(wd, bi)]["count"] += 1
                    heat[(wd, bi)]["views"] += e.get("view_count") or 0
                    break
        max_heat = max((v["count"] for v in heat.values()), default=1) or 1

        # Meilleure heure / meilleur jour (par vues moyennes)
        by_hour_views = defaultdict(list)
        by_wd_views   = defaultdict(list)
        for e in ok_entries:
            if e.get("view_count") is None:
                continue
            try:
                d = _dt.datetime.fromisoformat(e["timestamp"])
            except Exception:
                continue
            by_hour_views[d.hour].append(e.get("view_count") or 0)
            by_wd_views[d.weekday()].append(e.get("view_count") or 0)

        best_hour = None
        if by_hour_views:
            best_hour = max(by_hour_views.items(),
                            key=lambda x: sum(x[1]) / max(1, len(x[1])))[0]
        best_wd = None
        if by_wd_views:
            best_wd = max(by_wd_views.items(),
                          key=lambda x: sum(x[1]) / max(1, len(x[1])))[0]

        # Échecs par raison
        fail_reasons: Counter = Counter()
        for f in fail_entries:
            fail_reasons[f.get("reason") or "inconnu"] += 1

        # Date dernier sync
        last_sync = max((e.get("stats_fetched_at") or "" for e in history), default="")

        # ──────────────────────────────────────────────────────────────────────
        # Composants réutilisables
        # ──────────────────────────────────────────────────────────────────────
        def _kpi(icon, label, value, color=PRIMARY, sub=None, trend=None):
            """KPI card avec valeur + sous-texte + indicateur de tendance optionnel."""
            top_row = [
                ft.Icon(icon, size=13, color=color),
                ft.Text(label, size=9, color=MUTED, weight=ft.FontWeight.W_500),
            ]
            if trend and trend[0]:
                top_row.append(ft.Container(expand=True))
                top_row.append(ft.Row([
                    ft.Icon(trend[2], size=11, color=trend[1]),
                    ft.Text(trend[0], size=9, color=trend[1],
                            weight=ft.FontWeight.BOLD),
                ], spacing=2))
            return ft.Container(
                content=ft.Column([
                    ft.Row(top_row, spacing=5),
                    ft.Text(str(value), size=20, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(sub or "", size=9, color=OUTLINE),
                ], spacing=3, tight=True),
                bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                border_radius=10, padding=11, expand=True,
            )

        def _section_title(txt, extra=None):
            row = [
                ft.Text(txt, size=9, weight=ft.FontWeight.BOLD, color=MUTED),
                ft.Container(expand=True),
            ]
            if extra:
                row.append(extra)
            return ft.Row(row)

        # ──────────────────────────────────────────────────────────────────────
        # Sélecteur de période (chips)
        # ──────────────────────────────────────────────────────────────────────
        def _set_period(p):
            _stats_state["period"] = p
            _close_dialog(dlg_ref["v"])
            show_stats()

        def _chip(label, p):
            active = (_stats_state["period"] == p)
            return ft.Container(
                content=ft.Text(label, size=10,
                                color=ON_PRIMARY if active else ON_SURFACE,
                                weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500),
                bgcolor=PRIMARY if active else SURFACE_HIGH,
                border=ft.Border.all(1, PRIMARY if active else BORDER),
                border_radius=14,
                padding=ft.Padding.symmetric(horizontal=11, vertical=5),
                on_click=lambda ev, _p=p: _set_period(_p),
                ink=True,
            )

        period_chips = ft.Row([
            _chip("7j", 7), _chip("30j", 30), _chip("90j", 90), _chip("Tout", 0),
        ], spacing=6)

        # ──────────────────────────────────────────────────────────────────────
        # KPI row (8 indicateurs en 2 rangées de 4)
        # ──────────────────────────────────────────────────────────────────────
        kpi_grid = ft.Column([
            ft.Row([
                _kpi(ft.Icons.UPLOAD_FILE, "UPLOADS", total_ok, PRIMARY,
                     f"{len(fail_entries)} échec(s)",
                     trend=_trend(total_ok, prev_uploads)),
                _kpi(ft.Icons.VERIFIED, "TAUX SUCCÈS", f"{pct_ok}%", SUCCESS,
                     f"sur {total} tentatives"),
                _kpi(ft.Icons.VISIBILITY_OUTLINED, "VUES TOTALES",
                     _fmt_num(total_views) if with_stats else "—", WARN_C,
                     f"{len(with_stats)} vidéos trackées" if with_stats else "stats en attente",
                     trend=_trend(total_views, prev_views) if prev_views else None),
                _kpi(ft.Icons.TRENDING_UP, "VUES / VIDÉO",
                     _fmt_num(avg_views) if with_stats else "—", ACCENT,
                     f"médiane {_fmt_num(median_views)}" if with_stats else None),
            ], spacing=8),
            ft.Row([
                _kpi(ft.Icons.FAVORITE_OUTLINE, "LIKES",
                     _fmt_num(total_likes) if with_stats else "—", ERROR_C,
                     f"{_fmt_pct(total_likes, total_views)} taux"),
                _kpi(ft.Icons.CHAT_BUBBLE_OUTLINE, "COMMENTAIRES",
                     _fmt_num(total_comms) if with_stats else "—", "#60a5fa",
                     f"{_fmt_pct(total_comms, total_views)} taux"),
                _kpi(ft.Icons.AUTO_GRAPH, "ENGAGEMENT",
                     f"{engagement:.2f}%" if with_stats else "—", "#f472b6",
                     "likes + comm + partages / vues"),
                _kpi(ft.Icons.LOCAL_FIRE_DEPARTMENT, "STREAK",
                     f"{streak} j", "#fb923c",
                     f"record {best_streak} j" if best_streak else None),
            ], spacing=8),
        ], spacing=8)

        # ──────────────────────────────────────────────────────────────────────
        # Activité — combo bars (uploads) + ligne (vues)
        # ──────────────────────────────────────────────────────────────────────
        max_bars_visible = min(bar_days, 60)
        visible_range = days_range[-max_bars_visible:]
        visible_counts = day_counts[-max_bars_visible:]
        visible_views  = day_views[-max_bars_visible:]
        BAR_H = 70
        BAR_W = max(6, min(18, int(560 / max(1, len(visible_range)))))

        bars = []
        for d, cnt, vw in zip(visible_range, visible_counts, visible_views):
            h     = max(3, int(cnt / max_day * BAR_H)) if cnt else 3
            color = ACCENT if d == today else (PRIMARY if cnt > 0 else SURFACE_TOP)
            tip = f"{d.strftime('%a %d %b')}\n{cnt} upload(s)"
            if vw:
                tip += f"\n{_fmt_num(vw)} vues"
            bars.append(ft.Column([
                ft.Container(
                    width=BAR_W - 2, height=h, bgcolor=color,
                    border_radius=3, tooltip=tip,
                ),
            ], alignment=ft.MainAxisAlignment.END,
               horizontal_alignment=ft.CrossAxisAlignment.CENTER,
               width=BAR_W))

        # Ligne vues (sparkline overlay simulée par points colorés)
        view_dots = []
        for d, vw in zip(visible_range, visible_views):
            y = max(2, int(vw / max_dview * BAR_H)) if vw else 0
            view_dots.append(ft.Column([
                ft.Container(height=BAR_H - y),
                ft.Container(width=4, height=4, bgcolor=WARN_C, border_radius=2,
                             tooltip=f"{d.strftime('%d/%m')}: {_fmt_num(vw)} vues") if vw else
                ft.Container(width=4, height=4),
            ], width=BAR_W, alignment=ft.MainAxisAlignment.START,
               horizontal_alignment=ft.CrossAxisAlignment.CENTER))

        activity_section = ft.Column([
            _section_title(
                f"ACTIVITÉ — {bar_days} DERNIERS JOURS",
                ft.Row([
                    ft.Container(width=8, height=8, bgcolor=PRIMARY, border_radius=2),
                    ft.Text("uploads", size=9, color=OUTLINE),
                    ft.Container(width=8),
                    ft.Container(width=4, height=4, bgcolor=WARN_C, border_radius=2),
                    ft.Text("vues", size=9, color=OUTLINE),
                ], spacing=4)
            ),
            ft.Stack([
                ft.Container(content=ft.Row(bars, spacing=2,
                                            alignment=ft.MainAxisAlignment.START),
                             height=BAR_H + 4),
                ft.Container(content=ft.Row(view_dots, spacing=2,
                                            alignment=ft.MainAxisAlignment.START),
                             height=BAR_H + 4),
            ]),
            ft.Row([
                ft.Text(visible_range[0].strftime("%d/%m"), size=8, color=OUTLINE),
                ft.Container(expand=True),
                ft.Text(visible_range[len(visible_range)//2].strftime("%d/%m"),
                        size=8, color=OUTLINE),
                ft.Container(expand=True),
                ft.Text(visible_range[-1].strftime("%d/%m"), size=8, color=OUTLINE),
            ]),
        ], spacing=6)

        # ──────────────────────────────────────────────────────────────────────
        # Best moments
        # ──────────────────────────────────────────────────────────────────────
        best_panel = ft.Container(
            content=ft.Column([
                _section_title("MEILLEURS MOMENTS"),
                ft.Container(height=4),
                ft.Row([
                    ft.Icon(ft.Icons.CALENDAR_TODAY, size=14, color=SUCCESS),
                    ft.Text("Meilleur jour : ", size=11, color=ON_SURFACE),
                    ft.Text(weekdays_fr[best_wd] if best_wd is not None else "—",
                            size=11, color=SUCCESS, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.Text(f"{_fmt_num(int(sum(by_wd_views[best_wd]) / max(1, len(by_wd_views[best_wd]))))} vues/vid"
                            if best_wd is not None else "",
                            size=10, color=OUTLINE),
                ], spacing=6),
                ft.Row([
                    ft.Icon(ft.Icons.SCHEDULE, size=14, color=WARN_C),
                    ft.Text("Meilleure heure : ", size=11, color=ON_SURFACE),
                    ft.Text(f"{best_hour:02d}h" if best_hour is not None else "—",
                            size=11, color=WARN_C, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.Text(f"{_fmt_num(int(sum(by_hour_views[best_hour]) / max(1, len(by_hour_views[best_hour]))))} vues/vid"
                            if best_hour is not None else "",
                            size=10, color=OUTLINE),
                ], spacing=6),
                ft.Row([
                    ft.Icon(ft.Icons.WHATSHOT, size=14, color=ERROR_C),
                    ft.Text("Meilleure vidéo : ", size=11, color=ON_SURFACE),
                    ft.Text(
                        _fmt_num(max_view_entry.get("view_count")) if max_view_entry else "—",
                        size=11, color=ERROR_C, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    ft.Text(
                        (max_view_entry.get("prompt_template") or "").replace(".txt", "")[:18]
                        if max_view_entry else "",
                        size=10, color=OUTLINE),
                ], spacing=6),
            ], spacing=6),
            bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
            border_radius=10, padding=12,
        )

        # ──────────────────────────────────────────────────────────────────────
        # Heatmap weekday × tranche horaire
        # ──────────────────────────────────────────────────────────────────────
        def _heat_color(val):
            if not val:
                return SURFACE_TOP
            ratio = val / max_heat
            if ratio > 0.66:
                return ACCENT
            if ratio > 0.33:
                return PRIMARY
            return ACCENT_DIM

        heat_rows = []
        # Header (tranches horaires)
        heat_rows.append(ft.Row([
            ft.Container(width=34),
            *[ft.Container(
                content=ft.Text(f"{h0:02d}-{h1:02d}", size=8, color=OUTLINE,
                                text_align=ft.TextAlign.CENTER),
                width=46,
              ) for h0, h1 in hour_buckets],
        ], spacing=4))
        for wd in range(7):
            cells = []
            for bi in range(len(hour_buckets)):
                c = heat[(wd, bi)]["count"]
                v = heat[(wd, bi)]["views"]
                tip = f"{weekdays_fr[wd]} {hour_buckets[bi][0]:02d}h–{hour_buckets[bi][1]:02d}h\n{c} vidéo(s)"
                if v:
                    tip += f"\n{_fmt_num(v)} vues total"
                cells.append(ft.Container(
                    content=ft.Text(str(c) if c else "·", size=10,
                                    color=ON_SURFACE if c else MUTED,
                                    weight=ft.FontWeight.BOLD if c else ft.FontWeight.NORMAL,
                                    text_align=ft.TextAlign.CENTER),
                    width=46, height=26,
                    bgcolor=_heat_color(c),
                    border_radius=4,
                    alignment=ft.Alignment.CENTER,
                    tooltip=tip,
                ))
            heat_rows.append(ft.Row([
                ft.Container(content=ft.Text(weekdays_fr[wd], size=10, color=OUTLINE),
                             width=34),
                *cells,
            ], spacing=4))

        heat_panel = ft.Container(
            content=ft.Column([
                _section_title("RÉPARTITION JOUR × HEURE"),
                ft.Container(height=4),
                *heat_rows,
            ], spacing=3),
            bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
            border_radius=10, padding=12,
        )

        # ──────────────────────────────────────────────────────────────────────
        # ONGLET 2 — PROMPTS : tableau avec uploads / vues moy / engagement
        # ──────────────────────────────────────────────────────────────────────
        tmpl_rows = []
        # Header
        tmpl_rows.append(ft.Container(
            content=ft.Row([
                ft.Text("PROMPT", size=9, color=MUTED,
                        weight=ft.FontWeight.BOLD, expand=True),
                ft.Text("VIDÉOS", size=9, color=MUTED,
                        weight=ft.FontWeight.BOLD, width=55,
                        text_align=ft.TextAlign.RIGHT),
                ft.Text("VUES MOY.", size=9, color=MUTED,
                        weight=ft.FontWeight.BOLD, width=75,
                        text_align=ft.TextAlign.RIGHT),
                ft.Text("MAX", size=9, color=MUTED,
                        weight=ft.FontWeight.BOLD, width=55,
                        text_align=ft.TextAlign.RIGHT),
                ft.Text("ENGAG.", size=9, color=MUTED,
                        weight=ft.FontWeight.BOLD, width=58,
                        text_align=ft.TextAlign.RIGHT),
            ], spacing=4),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        ))
        # Tri par vues moyennes (descendant), fallback uploads
        def _tmpl_score(t):
            vs = by_template_views.get(t, [])
            return (sum(vs) / len(vs)) if vs else -1

        all_tmpls = sorted(by_template_uploads.keys(),
                           key=lambda t: (_tmpl_score(t), by_template_uploads[t]),
                           reverse=True)

        max_avg = max((_tmpl_score(t) for t in all_tmpls), default=1)
        if max_avg <= 0:
            max_avg = 1

        for tmpl in all_tmpls:
            cnt = by_template_uploads[tmpl]
            vs  = by_template_views.get(tmpl, [])
            es  = by_template_eng.get(tmpl, [])
            avg = int(sum(vs) / len(vs)) if vs else 0
            mx  = max(vs) if vs else 0
            eng = (sum(es) / len(es)) if es else 0
            bar_pct = int(avg / max_avg * 100) if max_avg else 0
            tmpl_rows.append(ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Text(tmpl[:32], size=11, color=ON_SURFACE, expand=True),
                        ft.Text(str(cnt), size=11, color=PRIMARY,
                                width=55, text_align=ft.TextAlign.RIGHT,
                                weight=ft.FontWeight.BOLD),
                        ft.Text(_fmt_num(avg) if avg else "—",
                                size=11, color=WARN_C, width=75,
                                text_align=ft.TextAlign.RIGHT),
                        ft.Text(_fmt_num(mx) if mx else "—",
                                size=10, color=OUTLINE, width=55,
                                text_align=ft.TextAlign.RIGHT),
                        ft.Text(f"{eng:.1f}%" if eng else "—",
                                size=10, color="#f472b6", width=58,
                                text_align=ft.TextAlign.RIGHT),
                    ], spacing=4),
                    ft.Row([
                        ft.Container(height=3, width=max(2, int(bar_pct * 4.6)),
                                     bgcolor=ACCENT, border_radius=2),
                        ft.Container(height=3, expand=True,
                                     bgcolor=SURFACE_TOP, border_radius=2),
                    ], spacing=0),
                ], spacing=4),
                bgcolor=SURFACE_HIGH, border_radius=8,
                padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            ))

        prompts_tab = ft.Column(
            tmpl_rows if all_tmpls else [
                ft.Container(
                    content=ft.Text("Aucun upload sur cette période.",
                                    size=12, color=OUTLINE,
                                    text_align=ft.TextAlign.CENTER),
                    alignment=ft.Alignment.CENTER, padding=30,
                ),
            ],
            spacing=6, scroll=ft.ScrollMode.AUTO,
        )

        # ──────────────────────────────────────────────────────────────────────
        # ONGLET 3 — VIDÉOS : recherche + tri + tableau
        # ──────────────────────────────────────────────────────────────────────
        def _video_sort_key(entry):
            mode = _stats_state["video_sort"]
            if mode == "likes":
                return entry.get("like_count") or 0
            if mode == "comments":
                return entry.get("comment_count") or 0
            if mode == "shares":
                return entry.get("share_count") or 0
            if mode == "engagement":
                vc = entry.get("view_count") or 0
                if not vc:
                    return 0
                return ((entry.get("like_count") or 0) + (entry.get("comment_count") or 0)
                        + (entry.get("share_count") or 0)) / vc
            if mode == "date":
                return entry.get("timestamp") or ""
            return entry.get("view_count") or 0

        search = _stats_state["video_search"].strip().lower()

        def _vid_match(e):
            if not search:
                return True
            blob = " ".join([
                str(e.get("title") or ""),
                str(e.get("prompt_template") or ""),
                str(e.get("timestamp") or ""),
                str(e.get("video_id") or ""),
            ]).lower()
            return search in blob

        filtered_vids = [e for e in ok_entries if _vid_match(e)]
        sorted_vids = sorted(filtered_vids, key=_video_sort_key, reverse=True)

        def _set_sort(mode):
            _stats_state["video_sort"] = mode
            _close_dialog(dlg_ref["v"])
            show_stats()

        def _sort_chip(label, mode):
            active = (_stats_state["video_sort"] == mode)
            return ft.Container(
                content=ft.Text(label, size=10,
                                color=ON_PRIMARY if active else ON_SURFACE,
                                weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500),
                bgcolor=PRIMARY if active else SURFACE_HIGH,
                border=ft.Border.all(1, PRIMARY if active else BORDER),
                border_radius=12,
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                on_click=lambda ev, _m=mode: _set_sort(_m),
                ink=True,
            )

        search_field = ft.TextField(
            value=_stats_state["video_search"],
            hint_text="Rechercher titre, prompt, ID...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            content_padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            text_size=11,
            border_color=BORDER,
            focused_border_color=PRIMARY,
            on_submit=lambda ev: (_stats_state.update({"video_search": ev.control.value}),
                                  _close_dialog(dlg_ref["v"]), show_stats()),
            expand=True,
        )

        def _open_tiktok(video_id):
            if not video_id:
                return
            _wb.open(f"https://www.tiktok.com/@_/video/{video_id}")

        vid_rows = [
            ft.Container(
                content=ft.Row([
                    ft.Text("#", size=9, color=MUTED, width=24,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("DATE", size=9, color=MUTED, width=80,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("PROMPT", size=9, color=MUTED, expand=True,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("VUES", size=9, color=MUTED, width=55,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("♥", size=9, color=MUTED, width=42,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("💬", size=9, color=MUTED, width=34,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("↗", size=9, color=MUTED, width=34,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text("ENG.", size=9, color=MUTED, width=44,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Container(width=22),
                ], spacing=4),
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            )
        ]
        for i, ev in enumerate(sorted_vids[:50], 1):
            tmpl  = (ev.get("prompt_template") or "?").replace(".txt", "")
            views = ev.get("view_count")
            likes = ev.get("like_count") or 0
            comms = ev.get("comment_count") or 0
            shares = ev.get("share_count") or 0
            vid_id = ev.get("video_id")
            eng = 0
            if views:
                eng = (likes + comms + shares) / views * 100
            vid_rows.append(ft.Container(
                content=ft.Row([
                    ft.Text(f"{i}", size=10, color=MUTED, width=24),
                    ft.Text((ev.get("timestamp") or "")[:10], size=10,
                            color=OUTLINE, width=80, font_family="Cascadia Code"),
                    ft.Text(tmpl[:28], size=10, color=ON_SURFACE, expand=True),
                    ft.Text(_fmt_num(views) if views is not None else "…",
                            size=10, color=PRIMARY if views else MUTED, width=55,
                            text_align=ft.TextAlign.RIGHT,
                            weight=ft.FontWeight.BOLD),
                    ft.Text(_fmt_num(likes), size=10, color=ERROR_C, width=42,
                            text_align=ft.TextAlign.RIGHT),
                    ft.Text(_fmt_num(comms), size=10, color="#60a5fa", width=34,
                            text_align=ft.TextAlign.RIGHT),
                    ft.Text(_fmt_num(shares), size=10, color=SUCCESS, width=34,
                            text_align=ft.TextAlign.RIGHT),
                    ft.Text(f"{eng:.1f}%" if eng else "—",
                            size=10, color="#f472b6", width=44,
                            text_align=ft.TextAlign.RIGHT),
                    ft.IconButton(
                        ft.Icons.OPEN_IN_NEW, icon_size=14, icon_color=MUTED,
                        tooltip="Ouvrir sur TikTok" if vid_id else "Vidéo non publiée",
                        disabled=not vid_id,
                        on_click=lambda evt, vi=vid_id: _open_tiktok(vi),
                    ),
                ], spacing=4),
                bgcolor=SURFACE_HIGH if i % 2 else SURFACE,
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            ))

        videos_tab = ft.Column([
            ft.Row([
                search_field,
                ft.Container(width=8),
                ft.Text(f"{len(sorted_vids)}", size=11, color=PRIMARY,
                        weight=ft.FontWeight.BOLD),
                ft.Text("résultats", size=10, color=OUTLINE),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([
                ft.Text("Tri :", size=10, color=MUTED),
                _sort_chip("Vues", "views"),
                _sort_chip("Likes", "likes"),
                _sort_chip("Comm.", "comments"),
                _sort_chip("Partages", "shares"),
                _sort_chip("Engagement", "engagement"),
                _sort_chip("Date", "date"),
            ], spacing=4, wrap=True),
            ft.Container(height=2),
            ft.Column(vid_rows if sorted_vids else [
                ft.Container(
                    content=ft.Text("Aucune vidéo trouvée." if search
                                    else "Aucun upload sur cette période.",
                                    size=12, color=OUTLINE,
                                    text_align=ft.TextAlign.CENTER),
                    alignment=ft.Alignment.CENTER, padding=30,
                )
            ], spacing=2, scroll=ft.ScrollMode.AUTO, expand=True),
        ], spacing=8, expand=True)

        # ──────────────────────────────────────────────────────────────────────
        # ONGLET 4 — TENDANCES & RÉPARTITIONS
        # ──────────────────────────────────────────────────────────────────────
        # Distribution des vues (histogramme)
        buckets = [(0, 500), (500, 1000), (1000, 5000), (5000, 10000),
                   (10000, 50000), (50000, 10**9)]
        bucket_labels = ["<500", "500-1k", "1-5k", "5-10k", "10-50k", "50k+"]
        bucket_counts = [0] * len(buckets)
        for ev in with_stats:
            v = ev.get("view_count") or 0
            for bi, (lo, hi) in enumerate(buckets):
                if lo <= v < hi:
                    bucket_counts[bi] += 1
                    break
        max_bucket = max(bucket_counts, default=1) or 1

        hist_rows = []
        for lbl, cnt in zip(bucket_labels, bucket_counts):
            fill = int(cnt / max_bucket * 280) if cnt else 0
            hist_rows.append(ft.Row([
                ft.Text(lbl, size=10, color=ON_SURFACE, width=58),
                ft.Container(width=fill, height=14, bgcolor=ACCENT, border_radius=3),
                ft.Container(width=4),
                ft.Text(str(cnt), size=10, color=PRIMARY,
                        weight=ft.FontWeight.BOLD),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=2))

        # Cumul vues
        cumul_views = []
        running = 0
        for v in day_views:
            running += v
            cumul_views.append(running)
        max_cumul = max(cumul_views, default=1) or 1

        # Sparkline cumul (ASCII style avec containers)
        SPARK_H = 60
        SPARK_W = max(3, int(560 / max(1, len(cumul_views))))
        spark_bars = []
        for v in cumul_views:
            h = max(2, int(v / max_cumul * SPARK_H))
            spark_bars.append(ft.Container(
                width=SPARK_W - 1, height=h,
                bgcolor=PRIMARY, border_radius=1,
                tooltip=f"Cumul : {_fmt_num(v)} vues"))
        spark_row = ft.Column(
            [ft.Row(spark_bars, spacing=1, alignment=ft.MainAxisAlignment.START)],
            alignment=ft.MainAxisAlignment.END,
        )

        trends_tab = ft.Column([
            ft.Container(
                content=ft.Column([
                    _section_title("CUMUL DES VUES — PROGRESSION",
                                   ft.Text(f"Total : {_fmt_num(total_views)}",
                                           size=10, color=PRIMARY,
                                           weight=ft.FontWeight.BOLD)),
                    ft.Container(height=4),
                    ft.Container(content=spark_row, height=SPARK_H + 4),
                ], spacing=4),
                bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                border_radius=10, padding=12,
            ),
            ft.Container(
                content=ft.Column([
                    _section_title("DISTRIBUTION DES VUES",
                                   ft.Text(f"{len(with_stats)} vidéos analysées",
                                           size=10, color=OUTLINE)),
                    ft.Container(height=4),
                    *hist_rows,
                ], spacing=6),
                bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                border_radius=10, padding=12,
            ),
            ft.Row([
                ft.Container(
                    content=ft.Column([
                        _section_title("VS PÉRIODE PRÉCÉDENTE"),
                        ft.Container(height=4),
                        ft.Row([
                            ft.Text("Uploads", size=10, color=ON_SURFACE, expand=True),
                            ft.Text(f"{prev_uploads} → {total_ok}", size=10,
                                    color=MUTED),
                            ft.Container(width=8),
                            *(([ft.Icon(_trend(total_ok, prev_uploads)[2], size=12,
                                        color=_trend(total_ok, prev_uploads)[1]),
                                ft.Text(_trend(total_ok, prev_uploads)[0], size=10,
                                        color=_trend(total_ok, prev_uploads)[1],
                                        weight=ft.FontWeight.BOLD)]
                              ) if _trend(total_ok, prev_uploads)[0] else
                             [ft.Text("—", size=10, color=OUTLINE)]),
                        ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Row([
                            ft.Text("Vues", size=10, color=ON_SURFACE, expand=True),
                            ft.Text(f"{_fmt_num(prev_views)} → {_fmt_num(total_views)}",
                                    size=10, color=MUTED),
                            ft.Container(width=8),
                            *(([ft.Icon(_trend(total_views, prev_views)[2], size=12,
                                        color=_trend(total_views, prev_views)[1]),
                                ft.Text(_trend(total_views, prev_views)[0], size=10,
                                        color=_trend(total_views, prev_views)[1],
                                        weight=ft.FontWeight.BOLD)]
                              ) if _trend(total_views, prev_views)[0] else
                             [ft.Text("—", size=10, color=OUTLINE)]),
                        ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Row([
                            ft.Text("Likes", size=10, color=ON_SURFACE, expand=True),
                            ft.Text(f"{_fmt_num(prev_likes)} → {_fmt_num(total_likes)}",
                                    size=10, color=MUTED),
                            ft.Container(width=8),
                            *(([ft.Icon(_trend(total_likes, prev_likes)[2], size=12,
                                        color=_trend(total_likes, prev_likes)[1]),
                                ft.Text(_trend(total_likes, prev_likes)[0], size=10,
                                        color=_trend(total_likes, prev_likes)[1],
                                        weight=ft.FontWeight.BOLD)]
                              ) if _trend(total_likes, prev_likes)[0] else
                             [ft.Text("—", size=10, color=OUTLINE)]),
                        ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ], spacing=6),
                    bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                    border_radius=10, padding=12, expand=True,
                ),
                ft.Container(
                    content=ft.Column([
                        _section_title("ÉCHECS"),
                        ft.Container(height=4),
                        *([ft.Row([
                                ft.Container(width=8, height=8, bgcolor=ERROR_C,
                                             border_radius=2),
                                ft.Text(r, size=11, color=ON_SURFACE, expand=True),
                                ft.Text(str(c), size=11, color=ERROR_C,
                                        weight=ft.FontWeight.BOLD),
                            ], spacing=8) for r, c in fail_reasons.most_common()]
                          if fail_reasons else
                          [ft.Container(
                              content=ft.Row([
                                  ft.Icon(ft.Icons.CHECK_CIRCLE, color=SUCCESS, size=18),
                                  ft.Text("Aucun échec sur cette période", size=11,
                                          color=SUCCESS),
                              ], spacing=8),
                              padding=ft.Padding.symmetric(vertical=8),
                          )]),
                    ], spacing=6),
                    bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                    border_radius=10, padding=12, expand=True,
                ),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=10, scroll=ft.ScrollMode.AUTO)

        # ──────────────────────────────────────────────────────────────────────
        # ONGLET 1 — VUE D'ENSEMBLE
        # ──────────────────────────────────────────────────────────────────────
        overview_tab = ft.Column([
            kpi_grid,
            ft.Container(
                content=activity_section,
                bgcolor=SURFACE_HIGH, border=ft.Border.all(1, BORDER),
                border_radius=10, padding=12,
            ),
            ft.Row([
                ft.Container(content=best_panel, expand=True),
                ft.Container(content=heat_panel, expand=True),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=10, scroll=ft.ScrollMode.AUTO)

        # ──────────────────────────────────────────────────────────────────────
        # Tabs + dialog
        # ──────────────────────────────────────────────────────────────────────
        dlg_ref = {"v": None}

        def _set_tab(idx):
            _stats_state["tab"] = idx
            if dlg_ref["v"]:
                _close_dialog(dlg_ref["v"])
            show_stats()

        def on_refresh(ev):
            if dlg_ref["v"]:
                _close_dialog(dlg_ref["v"])
            toast("Actualisation des stats...", "info")
            def do():
                try:
                    subprocess.run(
                        [PYTHON, str(BASE_DIR / "tiktok" / "fetch_stats.py"), "--update-only"],
                        cwd=str(BASE_DIR), capture_output=True, timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                except Exception:
                    pass
                page.run_thread(show_stats)
            threading.Thread(target=do, daemon=True).start()

        def on_export(ev):
            try:
                buf = _io.StringIO()
                w = _csv.writer(buf)
                w.writerow(["timestamp", "prompt_template", "upload_ok", "reason",
                            "video_id", "view_count", "like_count",
                            "comment_count", "share_count", "title",
                            "engagement_pct"])
                for e in history_period:
                    vc = e.get("view_count") or 0
                    eng = 0.0
                    if vc:
                        eng = ((e.get("like_count") or 0) + (e.get("comment_count") or 0)
                               + (e.get("share_count") or 0)) / vc * 100
                    w.writerow([
                        e.get("timestamp", ""),
                        e.get("prompt_template", ""),
                        e.get("upload_ok"),
                        e.get("reason", ""),
                        e.get("video_id") or "",
                        e.get("view_count") if e.get("view_count") is not None else "",
                        e.get("like_count") if e.get("like_count") is not None else "",
                        e.get("comment_count") if e.get("comment_count") is not None else "",
                        e.get("share_count") if e.get("share_count") is not None else "",
                        e.get("title", ""),
                        f"{eng:.2f}" if eng else "",
                    ])
                out_dir = BASE_DIR / "exports"
                out_dir.mkdir(parents=True, exist_ok=True)
                fname = out_dir / f"stats_{now.strftime('%Y%m%d_%H%M%S')}.csv"
                fname.write_text(buf.getvalue(), encoding="utf-8")
                toast(f"Export CSV → exports/{fname.name}", "success")
                try:
                    os.startfile(str(out_dir))
                except Exception:
                    pass
            except Exception as ex:
                toast(f"Échec export : {ex}", "error")

        def on_copy_summary(ev):
            try:
                summary = (
                    f"📊 Stats TikTok ({bar_days}j)\n"
                    f"• {total_ok} upload(s) — taux succès {pct_ok}%\n"
                    f"• {_fmt_num(total_views)} vues, {_fmt_num(total_likes)} likes\n"
                    f"• Engagement : {engagement:.2f}%\n"
                    f"• Streak : {streak} j (record {best_streak})\n"
                    f"• Meilleur prompt : "
                    f"{(sorted_templates[0][0] if sorted_templates else '—')}"
                )
                page.set_clipboard(summary)
                toast("Résumé copié dans le presse-papiers", "success")
            except Exception as ex:
                toast(f"Échec copie : {ex}", "error")

        last_sync_txt = "—"
        if last_sync:
            try:
                ls = _dt.datetime.fromisoformat(last_sync)
                delta = (now - ls)
                if delta.days >= 1:
                    last_sync_txt = f"il y a {delta.days}j"
                elif delta.seconds >= 3600:
                    last_sync_txt = f"il y a {delta.seconds // 3600}h"
                elif delta.seconds >= 60:
                    last_sync_txt = f"il y a {delta.seconds // 60}min"
                else:
                    last_sync_txt = "à l'instant"
            except Exception:
                last_sync_txt = last_sync[:16]

        tab_defs = [
            ("Vue d'ensemble", ft.Icons.DASHBOARD_OUTLINED, overview_tab),
            ("Prompts",        ft.Icons.AUTO_AWESOME_OUTLINED, prompts_tab),
            ("Vidéos",         ft.Icons.VIDEO_LIBRARY_OUTLINED, videos_tab),
            ("Tendances",      ft.Icons.INSIGHTS_OUTLINED, trends_tab),
        ]

        def _tab_btn(idx, label, icon):
            active = (_stats_state["tab"] == idx)
            return ft.Container(
                content=ft.Row([
                    ft.Icon(icon, size=15,
                            color=PRIMARY if active else MUTED),
                    ft.Text(label, size=11,
                            color=PRIMARY if active else MUTED,
                            weight=ft.FontWeight.BOLD if active else ft.FontWeight.W_500),
                ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=ACCENT_DIM if active else None,
                border=ft.Border.only(
                    bottom=ft.BorderSide(2, PRIMARY if active else "transparent"),
                ),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                on_click=lambda ev, _i=idx: _set_tab(_i),
                ink=True,
            )

        tab_bar = ft.Container(
            content=ft.Row(
                [_tab_btn(i, lbl, ic) for i, (lbl, ic, _) in enumerate(tab_defs)],
                spacing=2,
            ),
            border=ft.Border.only(bottom=ft.BorderSide(1, BORDER)),
        )

        current_tab_idx = max(0, min(_stats_state["tab"], len(tab_defs) - 1))
        current_content = tab_defs[current_tab_idx][2]

        tabs = ft.Column([
            tab_bar,
            ft.Container(
                content=current_content,
                padding=ft.Padding.only(top=14),
                expand=True,
            ),
        ], spacing=0, expand=True)

        header = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.INSIGHTS, color=PRIMARY, size=22),
                ft.Text("Dashboard TikTok", size=17,
                        weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.Row([
                    ft.Icon(ft.Icons.SYNC, size=12, color=MUTED),
                    ft.Text(f"Sync : {last_sync_txt}", size=10, color=MUTED),
                ], spacing=4),
                ft.IconButton(
                    ft.Icons.CONTENT_COPY, icon_color=MUTED, icon_size=16,
                    tooltip="Copier un résumé dans le presse-papiers",
                    on_click=on_copy_summary,
                ),
                ft.IconButton(
                    ft.Icons.FILE_DOWNLOAD, icon_color=MUTED, icon_size=18,
                    tooltip="Exporter en CSV",
                    on_click=on_export,
                ),
                ft.IconButton(
                    ft.Icons.REFRESH, icon_color=PRIMARY, icon_size=18,
                    tooltip="Actualiser les stats depuis TikTok",
                    on_click=on_refresh,
                ),
            ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([
                ft.Text("Période :", size=10, color=MUTED),
                period_chips,
                ft.Container(expand=True),
                ft.Text(
                    f"{total_ok} uploads · {len(with_stats)} avec stats",
                    size=10, color=OUTLINE,
                ),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=8)

        dlg = ft.AlertDialog(
            modal=True,
            title=header,
            content=ft.Container(
                content=tabs,
                width=920, height=620,
                padding=ft.Padding.symmetric(vertical=4),
            ),
            actions=[
                ft.TextButton("Fermer", on_click=lambda ev: _close_dialog(dlg)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE,
        )
        dlg_ref["v"] = dlg
        _open_dialog(dlg)

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
                # En mode auto, un fichier de requête est forcément stale
                # (laissé par un run précédent qui a crashé). On le purge.
                if matching_mode["ref"] != "manual":
                    try:
                        MATCHING_REQUEST_FILE.unlink(missing_ok=True)
                        MATCHING_RESPONSE_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
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
                err = None
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
                    avatar_path = BASE_DIR / "config" / "avatars" / f"{aid}.jpg"
                    if avatar_path.exists():
                        avatar_path.unlink()
                except Exception as ex:
                    err = str(ex)
                dlg.open = False
                if err:
                    toast(f"Erreur suppression : {err}", "error")
                else:
                    toast(f"Compte {display_name} supprimé", "warning")
                refresh_accounts()
                page.update()

            def cancel(ev):
                dlg.open = False
                page.update()

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
                # Filet de sécurité : si main_v3.py n'est pas en cours
                # (script à l'arrêt), il ne supprimera pas le request file
                # et la popup se ré-ouvrirait au prochain tick.
                try:
                    MATCHING_REQUEST_FILE.unlink(missing_ok=True)
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

    # ── Notifications Windows ─────────────────────────────
    def win_notify(title: str, body: str):
        if sys.platform != "win32":
            return
        try:
            t = title.replace('"', "'")
            b = body.replace('"', "'")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Application;"
                "$n.Visible=$True;"
                f'$n.ShowBalloonTip(7000,"{t}","{b}",'
                "[System.Windows.Forms.ToolTipIcon]::None);"
                "Start-Sleep 8;$n.Dispose()"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # ── View navigation ────────────────────────────────────
    active_view = {"ref": "pipeline"}
    _view_map   = {}   # populated after views are built
    _nav_btns   = {}   # {view_name: ft.Container}

    def switch_view(name: str):
        active_view["ref"] = name
        main_area.content  = _view_map.get(name)
        for vn, btn in _nav_btns.items():
            is_active      = vn == name
            btn.bgcolor    = ACCENT_DIM if is_active else None
            btn.border     = ft.border.all(1, ACCENT) if is_active else None
        page.update()
        if name == "queue":
            refresh_queue()
        elif name == "calendar":
            refresh_calendar()

    def _make_view_btn(icon, label, view_name):
        def _oc(e, n=view_name):
            switch_view(n)
        btn = ft.Container(
            content=ft.Row([
                ft.Container(
                    content=ft.Icon(icon, size=15, color=PRIMARY),
                    width=30, height=30, border_radius=8,
                    bgcolor=ACCENT_DIM, alignment=ft.Alignment.CENTER,
                ),
                ft.Text(label, size=12, color=ON_SURFACE, weight=ft.FontWeight.W_500),
            ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=8, on_click=_oc, ink=True,
            ink_color="rgba(124,58,237,0.06)",
        )
        _nav_btns[view_name] = btn
        return btn

    nav_pipeline_btn = _make_view_btn(ft.Icons.DASHBOARD,       "Vue Pipeline",    "pipeline")
    nav_calendar_btn = _make_view_btn(ft.Icons.CALENDAR_MONTH,  "Planning",        "calendar")
    nav_queue_btn    = _make_view_btn(ft.Icons.QUEUE,           "Gestion vidéos",  "queue")

    # Highlight pipeline view by default
    _nav_btns["pipeline"].bgcolor = ACCENT_DIM
    _nav_btns["pipeline"].border  = ft.border.all(1, ACCENT)

    # ── Scheduled posts CRUD ───────────────────────────────
    def _read_scheduled() -> list[dict]:
        try:
            if SCHEDULED_FILE.exists():
                return json.loads(SCHEDULED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _write_scheduled(posts: list[dict]):
        fd, tmp = tempfile.mkstemp(dir=str(SCHEDULED_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(posts, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SCHEDULED_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    # ── Queue view ─────────────────────────────────────────
    queue_dl_col   = ft.Column([], spacing=6, expand=True, scroll=ft.ScrollMode.AUTO)
    queue_pend_col = ft.Column([], spacing=6, expand=True, scroll=ft.ScrollMode.AUTO)

    def refresh_queue():
        # ── download/ section ──────────────────────────────
        queue_dl_col.controls.clear()
        dl_videos = []
        if DOWNLOAD_DIR.exists():
            dl_videos = sorted(
                [f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix == ".mp4"],
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
        for vf in dl_videos:
            mb = vf.stat().st_size / (1024 * 1024)
            def _mk_del_dl(fp=vf):
                def do(e):
                    try:
                        fp.unlink()
                        toast(f"{fp.name} supprimée", "success")
                    except Exception as ex:
                        toast(str(ex), "error")
                    refresh_queue()
                    refresh_all()
                return do
            queue_dl_col.controls.append(ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.VIDEO_FILE, size=16, color=PRIMARY),
                        width=32, height=32, border_radius=8,
                        bgcolor=ACCENT_DIM, alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column([
                        ft.Text(vf.name[:44], size=12, color=ON_SURFACE,
                                no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(f"{mb:.1f} MB", size=10, color=OUTLINE),
                    ], spacing=1, expand=True, tight=True),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16,
                                  icon_color=ERROR_C, on_click=_mk_del_dl(),
                                  tooltip="Supprimer"),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=SURFACE_HIGH, border_radius=10,
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                border=ft.border.all(1, BORDER),
            ))
        if not dl_videos:
            queue_dl_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=28, color=SUCCESS),
                    ft.Text("Aucune vidéo en attente", size=11, color=OUTLINE,
                            text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                alignment=ft.Alignment.CENTER, height=80,
            ))

        # ── pending_posts/ section ─────────────────────────
        queue_pend_col.controls.clear()
        pend_dirs = []
        if PENDING_DIR.exists():
            pend_dirs = sorted(
                [d for d in PENDING_DIR.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime, reverse=True,
            )
        for pd in pend_dirs:
            vp = pd / "video_final.mp4"
            reason = "?"
            try:
                m = json.loads((pd / "meta.json").read_text(encoding="utf-8"))
                reason = m.get("reason", "?")
            except Exception:
                pass
            mb = vp.stat().st_size / (1024 * 1024) if vp.exists() else 0

            def _mk_del_pend(dp=pd):
                def do(e):
                    try:
                        shutil.rmtree(dp)
                        toast(f"{dp.name} supprimée", "success")
                    except Exception as ex:
                        toast(str(ex), "error")
                    refresh_queue()
                    refresh_all()
                return do

            def _mk_post_pend(dp=pd):
                def do(e):
                    post_video(dp)
                return do

            queue_pend_col.controls.append(ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.ERROR_OUTLINE, size=16, color=WARN_C),
                        width=32, height=32, border_radius=8,
                        bgcolor=WARN_DIM, alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column([
                        ft.Text(pd.name.replace("Video_", "# ")[:40], size=12,
                                color=ON_SURFACE, no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(f"{reason} · {mb:.0f} MB", size=10, color=WARN_C),
                    ], spacing=1, expand=True, tight=True),
                    ft.Row([
                        ft.FilledTonalButton(
                            "Poster", icon=ft.Icons.SEND,
                            on_click=_mk_post_pend(),
                            style=ft.ButtonStyle(
                                bgcolor=ACCENT_DIM, color=PRIMARY,
                                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                            ),
                        ),
                        ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16,
                                      icon_color=ERROR_C, on_click=_mk_del_pend(),
                                      tooltip="Supprimer"),
                    ], spacing=4),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=SURFACE_HIGH, border_radius=10,
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                border=ft.border.all(1, "rgba(245,158,11,0.2)"),
            ))
        if not pend_dirs:
            queue_pend_col.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=28, color=SUCCESS),
                    ft.Text("Aucun échec d'upload", size=11, color=OUTLINE,
                            text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                alignment=ft.Alignment.CENTER, height=80,
            ))
        try:
            page.update()
        except Exception:
            pass

    # File picker — import vidéo vers download/ (tkinter natif, aucune dépendance Flet)
    def _open_file_picker():
        def do():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                paths = filedialog.askopenfilenames(
                    title="Sélectionner des vidéos à importer",
                    filetypes=[("Vidéos", "*.mp4 *.mov *.avi *.mkv"), ("Tous les fichiers", "*.*")],
                )
                root.destroy()
            except Exception as ex:
                page.run_thread(lambda: toast(f"Erreur ouverture dialog: {ex}", "error"))
                return
            if not paths:
                return
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            imported = 0
            for p in paths:
                try:
                    dst = DOWNLOAD_DIR / Path(p).name
                    shutil.copy(p, dst)
                    imported += 1
                except Exception as ex:
                    page.run_thread(lambda m=str(ex): toast(f"Erreur import: {m}", "error"))
            if imported:
                def finish():
                    toast(f"{imported} vidéo(s) importée(s) dans download/", "success")
                    refresh_queue()
                    refresh_all()
                page.run_thread(finish)
        threading.Thread(target=do, daemon=True).start()

    queue_view = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("Gestion des vidéos", size=20,
                        weight=ft.FontWeight.BOLD, color=ON_SURFACE),
                ft.Container(expand=True),
                ft.ElevatedButton(
                    "Importer une vidéo",
                    icon=ft.Icons.UPLOAD_FILE,
                    on_click=lambda _: _open_file_picker(),
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="white",
                                         shape=ft.RoundedRectangleBorder(radius=8)),
                ),
                ft.ElevatedButton(
                    "Actualiser",
                    icon=ft.Icons.REFRESH,
                    on_click=lambda _: (refresh_queue(), page.update()),
                    style=ft.ButtonStyle(bgcolor=SURFACE_TOP, color=OUTLINE,
                                         shape=ft.RoundedRectangleBorder(radius=8)),
                ),
            ], spacing=12),
            ft.Container(height=12),
            ft.Row([
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Container(
                                content=ft.Icon(ft.Icons.VIDEO_FILE, size=14, color=PRIMARY),
                                width=26, height=26, border_radius=6, bgcolor=ACCENT_DIM,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Text("En attente de traitement", size=13,
                                    weight=ft.FontWeight.W_600, color=ON_SURFACE),
                            ft.Text("download/", size=10, color=OUTLINE),
                        ], spacing=8),
                        ft.Container(height=8),
                        queue_dl_col,
                    ], spacing=0, expand=True),
                    bgcolor=SURFACE_HIGH, border_radius=14,
                    padding=ft.Padding.all(16),
                    border=ft.border.all(1, BORDER),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Container(
                                content=ft.Icon(ft.Icons.ERROR_OUTLINE, size=14, color=WARN_C),
                                width=26, height=26, border_radius=6, bgcolor=WARN_DIM,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Text("Échecs d'upload", size=13,
                                    weight=ft.FontWeight.W_600, color=ON_SURFACE),
                            ft.Text("pending_posts/", size=10, color=OUTLINE),
                        ], spacing=8),
                        ft.Container(height=8),
                        queue_pend_col,
                    ], spacing=0, expand=True),
                    bgcolor=SURFACE_HIGH, border_radius=14,
                    padding=ft.Padding.all(16),
                    border=ft.border.all(1, "rgba(245,158,11,0.2)"),
                    expand=True,
                ),
            ], spacing=12, expand=True),
        ], spacing=0, expand=True),
        expand=True,
        padding=ft.Padding.all(20),
        bgcolor=BG,
    )

    # ── Calendar view ──────────────────────────────────────
    import calendar as _cal

    cal_month      = {"ref": date.today().replace(day=1)}
    cal_month_lbl  = ft.Text("", size=16, weight=ft.FontWeight.BOLD, color=ON_SURFACE)
    cal_grid       = ft.GridView([], runs_count=7, spacing=4, run_spacing=4, expand=True)
    cal_sched_list = ft.ListView([], spacing=6, expand=True)

    _WDAY_LABELS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

    def _scheduled_by_day(yr: int, mo: int) -> dict[int, list[dict]]:
        day_map: dict[int, list[dict]] = {}
        for p in _read_scheduled():
            if p.get("status") != "pending":
                continue
            try:
                dt = datetime.fromisoformat(p["scheduled_at"])
                if dt.year == yr and dt.month == mo:
                    day_map.setdefault(dt.day, []).append(p)
            except Exception:
                pass
        return day_map

    # ── Pickers date/heure partagés entre appels à show_schedule_dialog ──────────
    _sched_state   = {"date": date.today(), "hour": 18, "minute": 0}
    _sched_ui      = {"date_lbl": None, "time_lbl": None}
    _pickers_ready = [False]

    def _fmt_sched_date() -> str:
        d = _sched_state["date"]
        return f"{d.day:02d}/{d.month:02d}/{d.year}"

    def _fmt_sched_time() -> str:
        return f"{_sched_state['hour']:02d}:{_sched_state['minute']:02d}"

    def _on_date_picked(e):
        v = getattr(e.control, "value", None)
        if v is not None:
            _sched_state["date"] = v.date() if hasattr(v, "date") else v
        lbl = _sched_ui.get("date_lbl")
        if lbl:
            lbl.value = _fmt_sched_date()
            page.update()

    def _on_time_picked(e):
        v = getattr(e.control, "value", None)
        if v is not None:
            _sched_state["hour"]   = v.hour
            _sched_state["minute"] = v.minute
        lbl = _sched_ui.get("time_lbl")
        if lbl:
            lbl.value = _fmt_sched_time()
            page.update()

    _date_picker = ft.DatePicker()
    _date_picker.on_change = _on_date_picked
    _time_picker = ft.TimePicker()
    _time_picker.on_change = _on_time_picked

    def show_schedule_dialog(for_date: date | None = None):
        if for_date:
            _sched_state["date"] = for_date

        # Ajouter les pickers à overlay une seule fois
        if not _pickers_ready[0]:
            page.overlay.extend([_date_picker, _time_picker])
            _pickers_ready[0] = True

        # Options — uniquement pending_posts/ (download/ = vidéos brutes non traitées)
        options = []
        if PENDING_DIR.exists():
            for pd_dir in sorted(PENDING_DIR.iterdir(), reverse=True):
                if pd_dir.is_dir():
                    vp = pd_dir / "video_final.mp4"
                    if vp.exists():
                        options.append(ft.dropdown.Option(
                            key=str(vp),
                            text=pd_dir.name[:50],
                        ))
        if not options:
            toast("Aucune vidéo en attente disponible à programmer", "warning")
            return

        # Panneau d'info vidéo
        info_folder = ft.Text("", size=12, color="white70", no_wrap=False, max_lines=2, expand=True)
        info_size   = ft.Text("", size=11, color="white38")

        def _update_info(vpath: str):
            try:
                p = Path(vpath)
                info_folder.value = p.parent.name
                sz = p.stat().st_size / (1024 * 1024)
                info_size.value   = f"{sz:.1f} Mo"
            except Exception:
                info_folder.value = ""
                info_size.value   = ""

        def _open_folder(e):
            if video_dd.value:
                try:
                    os.startfile(str(Path(video_dd.value).parent))
                except Exception:
                    pass

        info_panel = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Icon(ft.Icons.VIDEOCAM, size=14, color=ACCENT),
                    info_folder,
                ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.START),
                ft.Row([
                    ft.Icon(ft.Icons.STORAGE, size=13, color="white38"),
                    info_size,
                ], spacing=6),
                ft.TextButton(
                    "Ouvrir le dossier",
                    icon=ft.Icons.FOLDER_OPEN,
                    on_click=_open_folder,
                    style=ft.ButtonStyle(
                        color=ACCENT,
                        padding=ft.Padding.symmetric(horizontal=0, vertical=0),
                    ),
                ),
            ], spacing=4, tight=True),
            bgcolor=SURFACE_TOP,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border=ft.border.all(1, BORDER),
        )

        def on_video_change(e):
            _update_info(video_dd.value or "")
            page.update()

        video_dd = ft.Dropdown(
            options=options,
            label="Vidéo à programmer",
            text_size=12,
            bgcolor=SURFACE_TOP,
            border_color=BORDER,
            focused_border_color=ACCENT,
            value=options[0].key,
        )
        video_dd.on_change = on_video_change
        _update_info(options[0].key)

        # Labels date/heure mis à jour par les pickers
        date_lbl = ft.Text(_fmt_sched_date(), size=13, color="white", expand=True)
        time_lbl = ft.Text(_fmt_sched_time(), size=13, color="white", expand=True)
        _sched_ui["date_lbl"] = date_lbl
        _sched_ui["time_lbl"] = time_lbl

        def open_date(e):
            _date_picker.value = datetime(
                _sched_state["date"].year,
                _sched_state["date"].month,
                _sched_state["date"].day,
            )
            _date_picker.open = True
            page.update()

        def open_time(e):
            import datetime as _dt
            _time_picker.value = _dt.time(_sched_state["hour"], _sched_state["minute"])
            _time_picker.open = True
            page.update()

        err_lbl = ft.Text("", size=11, color=ERROR_C)

        def on_save(e):
            if not video_dd.value:
                err_lbl.value = "Sélectionne une vidéo"
                page.update()
                return
            d = _sched_state["date"]
            scheduled_at = (
                f"{d.year}-{d.month:02d}-{d.day:02d}"
                f"T{_sched_state['hour']:02d}:{_sched_state['minute']:02d}:00"
            )
            posts = _read_scheduled()
            posts.append({
                "id":           f"sched_{int(datetime.now().timestamp())}",
                "video_path":   video_dd.value,
                "scheduled_at": scheduled_at,
                "title":        Path(video_dd.value).parent.name,
                "status":       "pending",
                "created_at":   datetime.now().isoformat(timespec="seconds"),
            })
            _write_scheduled(posts)
            dlg.open = False
            _sched_ui["date_lbl"] = None
            _sched_ui["time_lbl"] = None
            refresh_calendar()
            toast(
                f"Publication programmée pour le {_fmt_sched_date()} à {_fmt_sched_time()}",
                "success",
            )
            page.update()

        def on_cancel(e):
            dlg.open = False
            _sched_ui["date_lbl"] = None
            _sched_ui["time_lbl"] = None
            page.update()

        picker_row_style = ft.ButtonStyle(
            bgcolor=SURFACE_HIGH, color=ACCENT,
            shape=ft.RoundedRectangleBorder(radius=6),
            padding=ft.Padding.symmetric(horizontal=14, vertical=6),
        )

        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.EVENT_AVAILABLE, color=ACCENT, size=20),
                ft.Text("Programmer une publication", size=14, weight=ft.FontWeight.BOLD),
            ], spacing=10),
            content=ft.Container(
                content=ft.Column([
                    video_dd,
                    info_panel,
                    # Sélection date
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.CALENDAR_MONTH, size=16, color=ACCENT),
                            ft.Text("Date :", size=12, color="white54", width=44),
                            date_lbl,
                            ft.FilledTonalButton("Choisir", on_click=open_date,
                                                 style=picker_row_style),
                        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        bgcolor=SURFACE_TOP,
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                        border=ft.border.all(1, BORDER),
                    ),
                    # Sélection heure
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.ACCESS_TIME, size=16, color=ACCENT),
                            ft.Text("Heure :", size=12, color="white54", width=44),
                            time_lbl,
                            ft.FilledTonalButton("Choisir", on_click=open_time,
                                                 style=picker_row_style),
                        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        bgcolor=SURFACE_TOP,
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                        border=ft.border.all(1, BORDER),
                    ),
                    err_lbl,
                ], spacing=10, tight=True),
                width=480,
                padding=ft.Padding.only(top=4, bottom=4),
            ),
            actions=[
                ft.TextButton("Annuler", on_click=on_cancel),
                ft.FilledButton(
                    "Programmer",
                    on_click=on_save,
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="white"),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE,
        )
        _open_dialog(dlg)

    def show_day_posts_dialog(for_date: date, posts: list[dict]):
        items = []
        for p in sorted(posts, key=lambda x: x.get("scheduled_at", "")):
            try:
                dt  = datetime.fromisoformat(p["scheduled_at"])
                tstr = dt.strftime("%H:%M")
            except Exception:
                tstr = "?"
            vid_name = Path(p.get("video_path", "")).name[:38]

            def _mk_cancel_day(pid=p["id"]):
                def do(e):
                    posts_now = _read_scheduled()
                    posts_now = [x for x in posts_now if x.get("id") != pid]
                    _write_scheduled(posts_now)
                    _close_dialog(dlg)
                    refresh_calendar()
                    toast("Publication annulée", "warning")
                return do

            items.append(ft.Container(
                content=ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.SCHEDULE, size=14, color=PRIMARY),
                        width=26, height=26, border_radius=6,
                        bgcolor=ACCENT_DIM, alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column([
                        ft.Text(f"à {tstr}", size=12, color=PRIMARY,
                                weight=ft.FontWeight.BOLD),
                        ft.Text(vid_name, size=10, color=OUTLINE,
                                no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=1, expand=True, tight=True),
                    ft.IconButton(ft.Icons.CANCEL_OUTLINED, icon_size=16,
                                  icon_color=ERROR_C, on_click=_mk_cancel_day(),
                                  tooltip="Annuler ce post"),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=SURFACE_HIGH, border_radius=8,
                padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                border=ft.border.all(1, BORDER),
            ))

        def on_add(e):
            _close_dialog(dlg)
            show_schedule_dialog(for_date)

        dlg = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.CALENDAR_TODAY, color=ACCENT, size=18),
                ft.Text(for_date.strftime("%d %B %Y"), size=14,
                        weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                ft.TextButton("+ Ajouter", on_click=on_add,
                              style=ft.ButtonStyle(color=PRIMARY)),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            content=ft.Container(
                content=ft.Column(items, spacing=6, scroll=ft.ScrollMode.AUTO),
                width=420,
                height=min(360, max(80, len(items) * 72 + 16)),
                padding=ft.Padding.only(top=4, bottom=4),
            ),
            actions=[ft.TextButton("Fermer", on_click=lambda _: _close_dialog(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE,
        )
        _open_dialog(dlg)

    def refresh_calendar():
        import locale as _loc
        yr, mo = cal_month["ref"].year, cal_month["ref"].month
        try:
            cal_month_lbl.value = cal_month["ref"].strftime("%B %Y").capitalize()
        except Exception:
            cal_month_lbl.value = f"{mo:02d}/{yr}"

        day_posts   = _scheduled_by_day(yr, mo)
        first_wd, n = _cal.monthrange(yr, mo)
        today       = date.today()

        cells = []

        # ── Row 0 : day-of-week headers ──────────────────────
        for lbl in _WDAY_LABELS:
            cells.append(ft.Container(
                content=ft.Text(lbl, size=10, weight=ft.FontWeight.W_600,
                                color=OUTLINE, text_align=ft.TextAlign.CENTER),
                height=28,
                alignment=ft.Alignment.CENTER,
                bgcolor=SURFACE,
                border_radius=6,
            ))

        # ── Empty cells before month starts ─────────────────
        for _ in range(first_wd):
            cells.append(ft.Container(height=90, bgcolor=SURFACE, border_radius=10))

        # ── Day cells ────────────────────────────────────────
        for d in range(1, n + 1):
            d_date   = date(yr, mo, d)
            p_today  = day_posts.get(d, [])
            is_today = d_date == today
            is_past  = d_date < today

            def _mk_tap(dd=d_date, pp=p_today):
                def do(e):
                    if pp:
                        show_day_posts_dialog(dd, pp)
                    else:
                        show_schedule_dialog(dd)
                return do

            dots = ft.Row(
                [ft.Container(width=6, height=6, border_radius=3, bgcolor=ACCENT)
                 for _ in p_today[:4]],
                spacing=2,
            ) if p_today else ft.Container(height=6)

            cells.append(ft.Container(
                content=ft.Column([
                    ft.Text(
                        str(d), size=13,
                        weight=ft.FontWeight.BOLD if is_today else None,
                        color=ACCENT if is_today else (MUTED if is_past else ON_SURFACE),
                    ),
                    dots,
                    ft.Text(
                        str(len(p_today)), size=9, color=PRIMARY,
                        weight=ft.FontWeight.W_600,
                        visible=len(p_today) > 0,
                    ),
                ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                   tight=True),
                height=90,
                bgcolor=ACCENT_DIM if is_today else (
                    "rgba(255,255,255,0.01)" if is_past else SURFACE_HIGH
                ),
                border_radius=10,
                border=ft.border.all(2, ACCENT) if is_today else ft.border.all(1, BORDER),
                on_click=_mk_tap(),
                ink=True, ink_color="rgba(124,58,237,0.06)",
                padding=ft.Padding.all(8),
                alignment=ft.Alignment.TOP_CENTER,
            ))

        cal_grid.controls.clear()
        cal_grid.controls.extend(cells)

        # ── Upcoming list ────────────────────────────────────
        cal_sched_list.controls.clear()
        upcoming = [
            (datetime.fromisoformat(p["scheduled_at"]), p)
            for p in _read_scheduled()
            if p.get("status") == "pending"
            and _safe_month_match(p.get("scheduled_at", ""), yr, mo)
        ]
        upcoming.sort(key=lambda x: x[0])

        if upcoming:
            for dt, p in upcoming:
                vid_name  = Path(p.get("video_path", "")).name[:40]
                is_overdue = dt < datetime.now()

                def _mk_cancel_list(pid=p["id"]):
                    def do(e):
                        posts_now = _read_scheduled()
                        posts_now = [x for x in posts_now if x.get("id") != pid]
                        _write_scheduled(posts_now)
                        refresh_calendar()
                        toast("Publication annulée", "warning")
                    return do

                cal_sched_list.controls.append(ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.WARNING if is_overdue else ft.Icons.SCHEDULE,
                                size=14,
                                color=WARN_C if is_overdue else PRIMARY,
                            ),
                            width=28, height=28, border_radius=6,
                            bgcolor=WARN_DIM if is_overdue else ACCENT_DIM,
                            alignment=ft.Alignment.CENTER,
                        ),
                        ft.Column([
                            ft.Text(dt.strftime("%d %b · %H:%M"), size=11,
                                    color=WARN_C if is_overdue else PRIMARY,
                                    weight=ft.FontWeight.W_600,
                                    font_family="Cascadia Code"),
                            ft.Text(vid_name, size=10, color=OUTLINE,
                                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                        ], spacing=1, expand=True, tight=True),
                        ft.IconButton(ft.Icons.CANCEL_OUTLINED, icon_size=14,
                                      icon_color=ERROR_C, on_click=_mk_cancel_list(),
                                      tooltip="Annuler"),
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=WARN_DIM if is_overdue else SURFACE_HIGH,
                    border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                    border=ft.border.all(1, WARN_C if is_overdue else BORDER),
                ))
        else:
            cal_sched_list.controls.append(ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.EVENT_AVAILABLE, size=28, color=OUTLINE),
                    ft.Text("Aucune publication programmée ce mois",
                            size=11, color=OUTLINE, text_align=ft.TextAlign.CENTER),
                    ft.TextButton(
                        "Programmer une publication",
                        icon=ft.Icons.ADD,
                        on_click=lambda _: show_schedule_dialog(),
                        style=ft.ButtonStyle(color=PRIMARY),
                    ),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
                alignment=ft.Alignment.CENTER, height=120,
            ))
        try:
            page.update()
        except Exception:
            pass

    def _safe_month_match(iso_str: str, yr: int, mo: int) -> bool:
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.year == yr and dt.month == mo
        except Exception:
            return False

    def cal_prev(e=None):
        yr, mo = cal_month["ref"].year, cal_month["ref"].month
        mo -= 1
        if mo == 0:
            mo, yr = 12, yr - 1
        cal_month["ref"] = date(yr, mo, 1)
        refresh_calendar()

    def cal_next(e=None):
        yr, mo = cal_month["ref"].year, cal_month["ref"].month
        mo += 1
        if mo == 13:
            mo, yr = 1, yr + 1
        cal_month["ref"] = date(yr, mo, 1)
        refresh_calendar()

    def cal_today(e=None):
        cal_month["ref"] = date.today().replace(day=1)
        refresh_calendar()

    calendar_view = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("Planning des publications", size=20,
                        weight=ft.FontWeight.BOLD, color=ON_SURFACE),
                ft.Container(expand=True),
                ft.ElevatedButton(
                    "Aujourd'hui", icon=ft.Icons.TODAY, on_click=cal_today,
                    style=ft.ButtonStyle(bgcolor=SURFACE_TOP, color=OUTLINE,
                                         shape=ft.RoundedRectangleBorder(radius=8)),
                ),
                ft.ElevatedButton(
                    "Programmer", icon=ft.Icons.ADD,
                    on_click=lambda _: show_schedule_dialog(),
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="white",
                                         shape=ft.RoundedRectangleBorder(radius=8)),
                ),
            ], spacing=12),
            ft.Container(height=12),
            ft.Row([
                # Calendar grid card
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_size=20,
                                          on_click=cal_prev, icon_color=ON_SURFACE),
                            ft.Container(
                                content=cal_month_lbl, expand=True,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_size=20,
                                          on_click=cal_next, icon_color=ON_SURFACE),
                        ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Container(height=6),
                        cal_grid,
                    ], spacing=2, expand=True),
                    bgcolor=SURFACE_HIGH, border_radius=14,
                    padding=ft.Padding.all(16),
                    border=ft.border.all(1, BORDER),
                    expand=True,
                ),
                # Scheduled list card
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Container(
                                content=ft.Icon(ft.Icons.LIST_ALT, size=14, color=ACCENT),
                                width=26, height=26, border_radius=6,
                                bgcolor=ACCENT_DIM, alignment=ft.Alignment.CENTER,
                            ),
                            ft.Text("Programmées ce mois", size=13,
                                    weight=ft.FontWeight.W_600, color=ON_SURFACE),
                        ], spacing=8),
                        ft.Container(height=8),
                        cal_sched_list,
                    ], spacing=0, expand=True),
                    bgcolor=SURFACE_HIGH, border_radius=14,
                    padding=ft.Padding.all(16),
                    border=ft.border.all(1, BORDER),
                    width=300,
                ),
            ], spacing=12, expand=True),
        ], spacing=0, expand=True),
        expand=True,
        padding=ft.Padding.all(20),
        bgcolor=BG,
    )

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

            # ── Navigation ──────────────────────────────────
            ft.Container(
                content=ft.Text("NAVIGATION", size=9, weight=ft.FontWeight.BOLD, color=OUTLINE),
                padding=ft.Padding.only(top=10, bottom=4, left=2),
            ),
            nav_pipeline_btn,
            nav_calendar_btn,
            nav_queue_btn,
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

    # Populate view map now that all views are defined
    _view_map["pipeline"] = main_content
    _view_map["calendar"] = calendar_view
    _view_map["queue"]    = queue_view

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
        refresh_calendar()
        start_scheduler()
        page.update()
    threading.Thread(target=_deferred_init, daemon=True).start()

    # Periodic refresh
    import time
    def refresh_loop():
        last_full_refresh     = 0
        last_accounts_refresh = 0
        last_calendar_refresh = 0
        last_queue_refresh    = 0
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
                # Refresh comptes toutes les 30s
                if now - last_accounts_refresh >= 30:
                    refresh_accounts()
                    last_accounts_refresh = now
                    needs_update = True
                # Refresh calendar toutes les 60s si vue active
                if now - last_calendar_refresh >= 60 and active_view["ref"] == "calendar":
                    refresh_calendar()
                    last_calendar_refresh = now
                # Refresh queue toutes les 10s si vue active
                if now - last_queue_refresh >= 10 and active_view["ref"] == "queue":
                    refresh_queue()
                    last_queue_refresh = now
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
            # Fermer tous les dialogs ouverts pour ne pas bloquer la fermeture
            try:
                for ov in list(page.overlay):
                    if hasattr(ov, "open"):
                        ov.open = False
                page.overlay.clear()
            except Exception:
                pass
            try:
                stop_scheduler()
            except Exception:
                pass
            page.window.prevent_close = False
            try:
                page.update()
            except Exception:
                pass
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
