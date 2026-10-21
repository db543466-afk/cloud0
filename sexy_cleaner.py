import os
import sys
import hashlib
import shutil
import subprocess
import tempfile
import platform
import webbrowser
from pathlib import Path
from datetime import datetime

import psutil
from send2trash import send2trash
from PySide6.QtCore import Qt, QThread, Signal, QRect, QTimer
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QStackedWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QMessageBox, QFileDialog, QCheckBox,
    QAbstractItemView, QLineEdit, QComboBox, QListWidget, QListWidgetItem,
    QGridLayout, QScrollArea, QSpinBox
)

APP_NAME = "SexyClean"
APP_VERSION = "3.0"
ACCENT = "#ed28c7"
PURPLE = "#8b5cf6"


def fmt_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _existing_dirs(paths):
    """Resolve the given paths and keep only unique, existing directories."""
    out, seen = [], set()
    for p in paths:
        try:
            rp = Path(p).resolve()
            key = str(rp).lower()
            if key not in seen and rp.is_dir():
                out.append(rp)
                seen.add(key)
        except (OSError, RuntimeError):
            pass
    return out


def safe_roots():
    """Return only known cache/temp roots, with an explicit safe-clean boundary."""
    home = Path.home()
    candidates = [
        ("Windows TEMP", Path(os.environ.get("TEMP", str(home / "AppData/Local/Temp"))), True),
        ("User TEMP", home / "AppData/Local/Temp", True),
        ("Chrome cache", home / "AppData/Local/Google/Chrome/User Data/Default/Cache", True),
        ("Chrome Code Cache", home / "AppData/Local/Google/Chrome/User Data/Default/Code Cache", True),
        ("Edge cache", home / "AppData/Local/Microsoft/Edge/User Data/Default/Cache", True),
        ("Edge Code Cache", home / "AppData/Local/Microsoft/Edge/User Data/Default/Code Cache", True),
        ("Firefox cache", home / "AppData/Local/Mozilla/Firefox/Profiles", False),
        ("Discord cache", home / "AppData/Roaming/discord/Cache", True),
        ("Steam web cache", Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam/appcache/httpcache", True),
    ]
    roots, seen = [], set()
    for label, path, direct_only in candidates:
        try:
            resolved = path.resolve()
            key = str(resolved).lower()
            if key not in seen and resolved.exists() and resolved.is_dir():
                roots.append((label, resolved, direct_only))
                seen.add(key)
        except (OSError, RuntimeError):
            pass
    return roots


def game_cache_roots():
    home = Path.home()
    local = home / "AppData/Local"
    roaming = home / "AppData/Roaming"
    candidates = [
        ("Steam web cache", Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam/appcache/httpcache"),
        ("Discord cache", roaming / "discord/Cache"),
        ("Discord GPU cache", roaming / "discord/Code Cache"),
        ("NVIDIA DX cache", local / "NVIDIA/DXCache"),
        ("AMD DX cache", local / "AMD/DxCache"),
        ("D3DSCache", local / "D3DSCache"),
    ]
    result, seen = [], set()
    for label, p in candidates:
        try:
            p = p.resolve()
            if p.is_dir() and str(p).lower() not in seen:
                result.append((label, p))
                seen.add(str(p).lower())
        except OSError:
            pass
    return result


def clean_categories():
    """Central registry of cleanup categories (CCleaner-style "Custom Clean").

    Every category lists only well-known cache/temp directories so cleaning
    stays inside a safe boundary. ``recommended`` marks the categories used by
    the one-click Health Check.
    """
    home = Path.home()
    local = home / "AppData/Local"
    roaming = home / "AppData/Roaming"
    programdata = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    systemp = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Temp"
    pfx86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    temp_env = Path(os.environ.get("TEMP", str(local / "Temp")))

    firefox_profiles = local / "Mozilla/Firefox/Profiles"
    firefox_caches = []
    try:
        if firefox_profiles.is_dir():
            for profile in firefox_profiles.iterdir():
                cache2 = profile / "cache2"
                if cache2.is_dir():
                    firefox_caches.append(cache2)
    except OSError:
        pass

    defs = [
        ("win_user_temp", "Windows", "Временные файлы (пользователь)",
         "Содержимое папки %TEMP% текущего пользователя.",
         [temp_env, local / "Temp"], False, True),
        ("win_sys_temp", "Windows", "Временные файлы Windows",
         "C:\\Windows\\Temp — может требовать прав администратора.",
         [systemp], False, True),
        ("win_crashdumps", "Windows", "Аварийные дампы приложений",
         "Дампы упавших программ (CrashDumps).",
         [local / "CrashDumps"], False, True),
        ("win_wer", "Windows", "Отчёты об ошибках (WER)",
         "Windows Error Reporting — архивы и очередь отчётов.",
         [local / "Microsoft/Windows/WER", programdata / "Microsoft/Windows/WER"], False, False),
        ("win_thumbs", "Windows", "Кэш эскизов и значков",
         "thumbcache_*.db / iconcache_*.db (могут быть заняты Проводником).",
         [local / "Microsoft/Windows/Explorer"], True, False),
        ("win_recent", "Windows", "Недавние документы",
         "Ярлыки недавно открытых файлов (список «Недавние»).",
         [roaming / "Microsoft/Windows/Recent"], True, False),
        ("app_chrome", "Приложения", "Кэш Google Chrome",
         "Кэш веб-страниц Chrome (Cache и Code Cache).",
         [local / "Google/Chrome/User Data/Default/Cache",
          local / "Google/Chrome/User Data/Default/Code Cache"], False, True),
        ("app_edge", "Приложения", "Кэш Microsoft Edge",
         "Кэш веб-страниц Edge (Cache и Code Cache).",
         [local / "Microsoft/Edge/User Data/Default/Cache",
          local / "Microsoft/Edge/User Data/Default/Code Cache"], False, True),
        ("app_firefox", "Приложения", "Кэш Mozilla Firefox",
         "Каталоги cache2 всех профилей Firefox.",
         firefox_caches, False, False),
        ("app_discord", "Приложения", "Кэш Discord",
         "Cache и Code Cache клиента Discord.",
         [roaming / "discord/Cache", roaming / "discord/Code Cache"], False, True),
        ("app_steam", "Приложения", "Веб-кэш Steam",
         "appcache/httpcache клиента Steam.",
         [pfx86 / "Steam/appcache/httpcache"], False, False),
        ("app_nvidia", "Приложения", "Шейдерный кэш NVIDIA",
         "DXCache драйвера NVIDIA.",
         [local / "NVIDIA/DXCache"], False, False),
        ("app_dx", "Приложения", "Кэш DirectX",
         "Общий шейдерный кэш D3DSCache.",
         [local / "D3DSCache"], False, False),
    ]
    categories = []
    for key, group, label, desc, paths, direct_only, recommended in defs:
        categories.append({
            "key": key, "group": group, "label": label, "desc": desc,
            "roots": _existing_dirs(paths), "direct_only": direct_only,
            "recommended": recommended,
        })
    return categories


def startup_backup_dir():
    """Folder where disabled user-startup shortcuts are parked (reversible)."""
    return Path.home() / "AppData/Local/SexyClean/StartupDisabled"


def list_installed_programs():
    """Read installed programs from the Windows uninstall registry keys."""
    programs = {}
    try:
        import winreg
    except ImportError:
        return []
    skip_release = {"Security Update", "Update", "Hotfix"}
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            top = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with top:
            try:
                subkey_count = winreg.QueryInfoKey(top)[0]
            except OSError:
                continue
            for i in range(subkey_count):
                try:
                    sub = winreg.EnumKey(top, i)
                    with winreg.OpenKey(top, sub) as node:
                        def read(name):
                            try:
                                return winreg.QueryValueEx(node, name)[0]
                            except OSError:
                                return None
                        name = read("DisplayName")
                        if not name:
                            continue
                        name = str(name).strip()
                        if read("SystemComponent") == 1:
                            continue
                        if read("ParentKeyName"):
                            continue
                        if read("ReleaseType") in skip_release:
                            continue
                        if name.startswith("Update for") or "(KB" in name:
                            continue
                        uninstall = read("UninstallString") or read("QuietUninstallString")
                        size_kb = read("EstimatedSize")
                        programs[name] = {
                            "name": name,
                            "publisher": str(read("Publisher") or "—"),
                            "version": str(read("DisplayVersion") or "—"),
                            "size": int(size_kb) * 1024 if isinstance(size_kb, int) else 0,
                            "uninstall": uninstall,
                        }
                except OSError:
                    continue
    return sorted(programs.values(), key=lambda p: p["name"].lower())


def system_info():
    """Return read-only system facts for the Tools > System info page."""
    vm = psutil.virtual_memory()
    try:
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = str(datetime.now() - boot).split(".")[0]
    except Exception:
        uptime = "—"
    try:
        cpu_load = f"{psutil.cpu_percent(interval=0.2)}%"
    except Exception:
        cpu_load = "—"
    return [
        ("Операционная система", platform.platform()),
        ("Имя компьютера", platform.node() or "—"),
        ("Процессор", platform.processor() or "—"),
        ("Ядра (физические / логические)", f"{psutil.cpu_count(logical=False)} / {psutil.cpu_count(logical=True)}"),
        ("Текущая загрузка CPU", cpu_load),
        ("ОЗУ всего", fmt_size(vm.total)),
        ("ОЗУ занято", f"{fmt_size(vm.used)} ({vm.percent}%)"),
        ("Время работы (аптайм)", uptime),
        ("Архитектура", platform.machine() or "—"),
        ("Python", sys.version.split()[0]),
    ]


class ScanWorker(QThread):
    progress = Signal(int, str)
    found = Signal(list)
    failed = Signal(str)

    def __init__(self, mode, roots=None, min_size_mb=100, categories=None):
        super().__init__()
        self.mode = mode
        self.roots = roots or []
        self.min_size = int(min_size_mb * 1024 * 1024)
        self.categories = categories or []

    def run(self):
        try:
            if self.mode in ("cleanup", "games"):
                targets = safe_roots() if self.mode == "cleanup" else [(n, p, False) for n, p in game_cache_roots()]
                results = []
                for idx, item in enumerate(targets):
                    label, root = item[0], item[1]
                    direct = item[2] if len(item) > 2 else False
                    self.progress.emit(int(idx / max(1, len(targets)) * 100), f"Проверка: {label}")
                    results.extend(self._walk(root, label, 0, direct))
                self.progress.emit(100, "Сканирование завершено")
                self.found.emit(results)
            elif self.mode in ("custom", "health"):
                results = []
                total = sum(len(cat["roots"]) for cat in self.categories) or 1
                done = 0
                for cat in self.categories:
                    for root in cat["roots"]:
                        self.progress.emit(int(done / total * 100), f"Проверка: {cat['label']}")
                        results.extend(self._walk(Path(root), cat["label"], 0, cat.get("direct_only", False)))
                        done += 1
                self.progress.emit(100, "Проверка завершена")
                self.found.emit(results)
            elif self.mode in ("large", "duplicates", "disk"):
                collected = []
                for idx, root in enumerate(self.roots):
                    self.progress.emit(int(idx / max(1, len(self.roots)) * 100), f"Анализ: {root}")
                    collected.extend(self._walk(Path(root), "Файл", self.min_size if self.mode == "large" else 1))
                if self.mode == "duplicates":
                    groups = {}
                    for item in collected:
                        groups.setdefault(item["size"], []).append(item)
                    results = []
                    for group in groups.values():
                        if len(group) < 2:
                            continue
                        by_hash = {}
                        for item in group:
                            try:
                                digest = hashlib.sha256()
                                with open(item["path"], "rb") as stream:
                                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                        digest.update(chunk)
                                by_hash.setdefault(digest.hexdigest(), []).append(item)
                            except OSError:
                                continue
                        for same in by_hash.values():
                            if len(same) > 1:
                                results.extend(same)
                    collected = results
                self.progress.emit(100, "Анализ завершён")
                self.found.emit(collected)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _walk(self, root, category, min_size=0, direct_only=False):
        found = []
        try:
            if not root.exists() or not root.is_dir():
                return found
            iterator = (
                [(str(root), [], [p.name for p in root.iterdir() if p.is_file()])]
                if direct_only else os.walk(root, topdown=True, followlinks=False)
            )
            count = 0
            for current, dirs, files in iterator:
                dirs[:] = [d for d in dirs if d.lower() not in {
                    "windows", "system32", "winsxs", "program files", "program files (x86)",
                    "windowsapps", "recovery", "$recycle.bin", "saved", "saves", "userdata",
                    "documents", "desktop"
                }]
                for name in files:
                    path = Path(current) / name
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                        if stat.st_size < min_size:
                            continue
                        found.append({
                            "path": str(path), "size": stat.st_size, "category": category,
                            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        })
                        count += 1
                        if count % 250 == 0:
                            self.progress.emit(-1, f"Найдено файлов: {count}")
                    except OSError:
                        continue
        except OSError:
            pass
        return found


class WallpaperWidget(QWidget):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap(str(image_path))
        self.overlay = QColor(8, 7, 24, 165)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0b1b"))
        if not self.pixmap.isNull():
            scaled = self.pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            x = max(0, (scaled.width() - self.width()) // 2)
            y = max(0, (scaled.height() - self.height()) // 2)
            painter.drawPixmap(self.rect(), scaled, QRect(x, y, self.width(), self.height()))
        painter.fillRect(self.rect(), self.overlay)
        painter.end()


class ProgressRing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0
        self.setMinimumSize(150, 150)

    def setValue(self, value):
        self.value = max(0, min(100, int(value)))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 18
        rect = QRect((self.width()-side)//2, (self.height()-side)//2, side, side)
        p.setPen(QPen(QColor("#33285c"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 0, 360 * 16)
        p.setPen(QPen(QColor("#ed28c7"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, -int(360 * 16 * self.value / 100))
        p.setPen(QColor("#f3e8ff"))
        font = QFont("Segoe UI Variable", 19, QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{self.value}%")
        p.end()


class SexyClean(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 680)
        self.worker = None
        self.results_by_table = {}
        self.approved_by_table = {}
        self.pages_by_key = {}
        self.nav_buttons = {}
        self.clean_checks = {}
        self.startup_rows = []
        self.uninstall_rows = []
        self._build()
        self.refresh_overview()

    def _build(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(240)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(12, 22, 12, 16)
        sl.setSpacing(5)
        logo = QLabel("SexyClean")
        logo.setObjectName("logo")
        sl.addWidget(logo)
        tagline = QLabel("УМНАЯ ОЧИСТКА ПК")
        tagline.setObjectName("tagline")
        sl.addWidget(tagline)
        sl.addSpacing(14)

        self.pages = QStackedWidget()
        self.pages.setStyleSheet("QStackedWidget { background: transparent; }")
        nav_groups = [
            ("ПРОВЕРКА", [("Проверка состояния", "overview")]),
            ("ОЧИСТКА", [("Очистка мусора", "cleanup"), ("Игровые кэши", "games"),
                         ("Большие файлы", "large"), ("Дубликаты файлов", "duplicates"),
                         ("Очистка корзины", "recycle")]),
            ("ОПТИМИЗАЦИЯ", [("Автозагрузка", "startup"), ("Скан реестра", "registry")]),
            ("ИНСТРУМЕНТЫ", [("Удаление программ", "uninstall"), ("Анализ дисков", "disks"),
                             ("Сведения о системе", "sysinfo")]),
            ("ЗАЩИТА", [("Поиск вирусов", "virus"), ("Уведомления сайтов", "sites")]),
            ("ПРОГРАММА", [("Настройки", "settings")]),
        ]
        for group, items in nav_groups:
            header = QLabel(group)
            header.setObjectName("navSection")
            sl.addWidget(header)
            for label, key in items:
                b = QPushButton(label)
                b.setObjectName("nav")
                b.setCheckable(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda checked=False, k=key: self.open_page(k))
                self.nav_buttons[key] = b
                sl.addWidget(b)
        sl.addStretch()
        foot = QLabel("БЕЗОПАСНЫЙ РЕЖИМ\nУдаление только после подтверждения")
        foot.setObjectName("footnote")
        foot.setWordWrap(True)
        sl.addWidget(foot)

        image = Path(__file__).parent / "assets" / "sexyclean_wallpaper.png"
        content = WallpaperWidget(image)
        content.setObjectName("content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(30, 24, 30, 20)
        cl.addWidget(self.pages)

        self.pages_by_key["overview"] = self.page_overview()
        self.pages_by_key["cleanup"] = self.page_custom_clean()
        self.pages_by_key["games"] = self.page_scan("Игровые и приложенческие кэши", "Только известные папки кэша; сохранения не затрагиваются.", "games")
        self.pages_by_key["large"] = self.page_scan("Большие файлы", "Поиск больших файлов в выбранной папке.", "large")
        self.pages_by_key["duplicates"] = self.page_scan("Дубликаты файлов", "Поиск одинаковых файлов по размеру и SHA-256.", "duplicates")
        self.pages_by_key["recycle"] = self.page_recycle()
        self.pages_by_key["startup"] = self.page_startup()
        self.pages_by_key["registry"] = self.page_registry()
        self.pages_by_key["uninstall"] = self.page_uninstall()
        self.pages_by_key["disks"] = self.page_disks()
        self.pages_by_key["sysinfo"] = self.page_sysinfo()
        self.pages_by_key["virus"] = self.page_virus()
        self.pages_by_key["sites"] = self.page_sites()
        self.pages_by_key["settings"] = self.page_settings()
        for key in ["overview", "cleanup", "games", "large", "duplicates", "recycle",
                    "startup", "registry", "uninstall", "disks", "sysinfo", "virus",
                    "sites", "settings"]:
            self.pages.addWidget(self.pages_by_key[key])

        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)
        self.open_page("overview")

    def open_page(self, key):
        page = self.pages_by_key.get(key)
        if page:
            self.pages.setCurrentWidget(page)
        for k, btn in self.nav_buttons.items():
            btn.setChecked(k == key)

    def header(self, title, desc):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 14)
        t = QLabel(title)
        t.setObjectName("title")
        d = QLabel(desc)
        d.setObjectName("muted")
        d.setWordWrap(True)
        l.addWidget(t)
        l.addWidget(d)
        return w

    def card(self, title, value, detail):
        f = QFrame()
        f.setObjectName("card")
        l = QVBoxLayout(f)
        a = QLabel(title)
        a.setObjectName("muted")
        b = QLabel(value)
        b.setObjectName("metric")
        c = QLabel(detail)
        c.setObjectName("muted")
        c.setWordWrap(True)
        l.addWidget(a)
        l.addWidget(b)
        l.addWidget(c)
        f.metric_label = b
        f.detail_label = c
        return f

    def page_overview(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.setSpacing(16)
        l.addWidget(self.header("Проверка состояния", "Быстрый анализ мусора и кэшей. Очистка — только в корзину после подтверждения."))
        cards = QHBoxLayout()
        self.card_drives = self.card("Диски", "—", "Обнаруженные тома")
        self.card_free = self.card("Свободное место", "—", "На доступных дисках")
        self.card_scan = self.card("Найдено к очистке", "—", "Запусти проверку")
        cards.addWidget(self.card_drives)
        cards.addWidget(self.card_free)
        cards.addWidget(self.card_scan)
        l.addLayout(cards)

        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        top = QHBoxLayout()
        run = QPushButton("Запустить проверку")
        run.setObjectName("primary")
        clean = QPushButton("Очистить рекомендуемое")
        top.addWidget(run)
        top.addWidget(clean)
        top.addStretch()
        self.ring = ProgressRing()
        top.addWidget(self.ring)
        pl.addLayout(top)
        self.main_progress = QProgressBar()
        self.main_progress.setValue(0)
        pl.addWidget(self.main_progress)
        health_table = self.make_table(["Выбрать", "Категория", "Размер", "Изменён", "Путь"])
        pl.addWidget(health_table, 1)
        self.health_summary = QLabel("Проверка ещё не запускалась.")
        self.health_summary.setObjectName("muted")
        pl.addWidget(self.health_summary)
        self.status = QLabel("Готово к проверке.")
        self.status.setObjectName("muted")
        pl.addWidget(self.status)
        l.addWidget(panel, 1)
        note = QLabel("SexyClean не удаляет файлы автоматически и чистит только известные папки кэша и временных файлов.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        l.addWidget(note)

        self.results_by_table[id(health_table)] = []
        run.clicked.connect(lambda: self.run_scan(
            "health", [], 0, health_table, self.main_progress, self.health_summary,
            [c for c in clean_categories() if c["recommended"]]))
        clean.clicked.connect(lambda: self.clean_selected(health_table, "health"))
        return page

    def make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(len(headers)-1, QHeaderView.ResizeMode.Stretch)
        return table

    def page_custom_clean(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Очистка мусора", "Отметь категории, нажми «Анализировать», проверь список и очисти в корзину."))

        categories = clean_categories()
        panel = QFrame()
        panel.setObjectName("panel")
        pcols = QHBoxLayout(panel)
        for group in ["Windows", "Приложения"]:
            box = QVBoxLayout()
            gh = QLabel(group)
            gh.setObjectName("navSection")
            box.addWidget(gh)
            for cat in categories:
                if cat["group"] != group:
                    continue
                cb = QCheckBox(cat["label"])
                cb.setToolTip(cat["desc"])
                if not cat["roots"]:
                    cb.setEnabled(False)
                    cb.setText(cat["label"] + " — не найдено")
                else:
                    cb.setChecked(cat["recommended"])
                self.clean_checks[cat["key"]] = (cb, cat)
                box.addWidget(cb)
            box.addStretch()
            pcols.addLayout(box, 1)
        l.addWidget(panel)

        bar = QProgressBar()
        table = self.make_table(["Выбрать", "Категория", "Размер", "Изменён", "Путь"])
        summary = QLabel("Анализ ещё не запускался.")
        summary.setObjectName("muted")
        self.results_by_table[id(table)] = []

        row1 = QHBoxLayout()
        analyze = QPushButton("Анализировать")
        analyze.setObjectName("primary")
        rec = QPushButton("Рекомендуемые")
        clear_cats = QPushButton("Снять категории")
        row1.addWidget(analyze)
        row1.addWidget(rec)
        row1.addWidget(clear_cats)
        row1.addStretch()

        row2 = QHBoxLayout()
        select_all = QPushButton("Выбрать всё")
        deselect = QPushButton("Снять выбор")
        clean = QPushButton("Очистить выбранное")
        clean.setObjectName("primary")
        dns = QPushButton("Очистить DNS-кэш")
        clip = QPushButton("Очистить буфер обмена")
        row2.addWidget(select_all)
        row2.addWidget(deselect)
        row2.addWidget(clean)
        row2.addStretch()
        row2.addWidget(dns)
        row2.addWidget(clip)

        def do_analyze():
            chosen = [cat for _, (cb, cat) in self.clean_checks.items() if cb.isEnabled() and cb.isChecked()]
            if not chosen:
                QMessageBox.information(self, APP_NAME, "Отметь хотя бы одну категорию.")
                return
            self.run_scan("custom", [], 0, table, bar, summary, chosen)

        def set_recommended():
            for _, (cb, cat) in self.clean_checks.items():
                if cb.isEnabled():
                    cb.setChecked(cat["recommended"])

        def clear_all_cats():
            for _, (cb, cat) in self.clean_checks.items():
                if cb.isEnabled():
                    cb.setChecked(False)

        analyze.clicked.connect(do_analyze)
        rec.clicked.connect(set_recommended)
        clear_cats.clicked.connect(clear_all_cats)
        select_all.clicked.connect(lambda: self.toggle_all_rows(table, True))
        deselect.clicked.connect(lambda: self.toggle_all_rows(table, False))
        clean.clicked.connect(lambda: self.clean_selected(table, "custom"))
        dns.clicked.connect(self.flush_dns)
        clip.clicked.connect(self.clear_clipboard)

        l.addLayout(row1)
        l.addLayout(row2)
        l.addWidget(bar)
        l.addWidget(table, 1)
        l.addWidget(summary)
        return page

    def page_scan(self, title, desc, mode):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header(title, desc))
        controls = QHBoxLayout()
        path = QLineEdit(str(Path.home()))
        path.setReadOnly(True)
        browse = QPushButton("Выбрать папку")
        browse.clicked.connect(lambda: self.choose_folder(path))
        minimum = QComboBox()
        minimum.addItems(["100 МБ", "250 МБ", "500 МБ", "1 ГБ"])
        start = QPushButton("Сканировать")
        start.setObjectName("primary")
        bar = QProgressBar()
        table = self.make_table(["Выбрать", "Категория", "Размер", "Изменён", "Путь"])
        summary = QLabel("Сканирование ещё не запускалось.")
        summary.setObjectName("muted")
        clean = QPushButton("Переместить выбранное в корзину")
        clean.clicked.connect(lambda: self.clean_selected(table, mode))
        if mode in ("cleanup", "games"):
            path.hide()
            browse.hide()
            minimum.hide()
            start.clicked.connect(lambda: self.run_scan(mode, [], 0, table, bar, summary))
        else:
            clean.hide()
            start.clicked.connect(lambda: self.run_scan(mode, [path.text()], {"100 МБ":100,"250 МБ":250,"500 МБ":500,"1 ГБ":1024}[minimum.currentText()], table, bar, summary))
        controls.addWidget(path, 1)
        controls.addWidget(browse)
        controls.addWidget(minimum)
        controls.addWidget(start)
        controls.addWidget(clean)
        l.addLayout(controls)
        l.addWidget(bar)
        l.addWidget(table, 1)
        l.addWidget(summary)
        self.results_by_table[id(table)] = []
        return page

    def choose_folder(self, field):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку", field.text() or str(Path.home()))
        if folder:
            field.setText(folder)

    def toggle_all_rows(self, table, state):
        for row in range(table.rowCount()):
            cb = table.cellWidget(row, 0)
            if cb:
                cb.setChecked(state)

    def run_scan(self, mode, roots, min_mb, table, bar, summary, categories=None):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, APP_NAME, "Сканирование уже выполняется.")
            return
        if mode in ("custom", "health"):
            cats = categories or []
            if not cats:
                QMessageBox.information(self, APP_NAME, "Нет категорий для анализа.")
                return
            approved = []
            for cat in cats:
                for r in cat["roots"]:
                    try:
                        approved.append(Path(r).resolve())
                    except (OSError, RuntimeError):
                        pass
            self.approved_by_table[id(table)] = approved
        table.setRowCount(0)
        bar.setValue(0)
        self.main_progress.setValue(0)
        self.ring.setValue(0)
        summary.setText("Сканирование…")
        self.worker = ScanWorker(mode, roots, min_mb, categories)
        self.worker.progress.connect(lambda value, msg: self.update_progress(bar, value, msg))
        self.worker.found.connect(lambda items: self.display_results(items, table, summary, mode))
        self.worker.failed.connect(lambda error: QMessageBox.warning(self, APP_NAME, error))
        self.worker.start()
        self.status.setText("Сканирование…")

    def update_progress(self, bar, value, message):
        if value >= 0:
            bar.setValue(value)
            self.main_progress.setValue(value)
            self.ring.setValue(value)
        self.status.setText(message)

    def display_results(self, items, table, summary, mode):
        self.results_by_table[id(table)] = items
        preselect = mode in ("health", "custom")
        table.setRowCount(len(items))
        total = 0
        for row, item in enumerate(items):
            total += item["size"]
            check = QCheckBox()
            check.setChecked(preselect)
            table.setCellWidget(row, 0, check)
            vals = [item["category"], fmt_size(item["size"]), item["modified"], item["path"]]
            for col, value in enumerate(vals, 1):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                table.setItem(row, col, cell)
        summary.setText(f"Найдено файлов: {len(items)} · Общий размер: {fmt_size(total)} · Ничего не удалено")
        self.card_scan.metric_label.setText(fmt_size(total) if items else "0 B")
        self.card_scan.detail_label.setText(f"{len(items)} файлов найдено")
        self.status.setText("Готово. Проверь список перед очисткой.")

    def clean_selected(self, table, mode):
        if mode in ("large", "duplicates"):
            QMessageBox.information(self, APP_NAME, "Для этого раздела доступен только просмотр. Удаление не выполняется.")
            return
        items = self.results_by_table.get(id(table), [])
        selected = []
        for row in range(table.rowCount()):
            check = table.cellWidget(row, 0)
            if check and check.isChecked() and row < len(items):
                selected.append(items[row])
        if not selected:
            QMessageBox.information(self, APP_NAME, "Сначала отметь файлы.")
            return
        total = sum(x["size"] for x in selected)
        preview = "\n".join(x["path"] for x in selected[:6])
        if len(selected) > 6:
            preview += f"\n… и ещё {len(selected)-6}"
        answer = QMessageBox.question(
            self, "Подтверждение очистки",
            f"Переместить {len(selected)} файлов ({fmt_size(total)}) в корзину?\n\n{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if mode == "games":
            approved = [p.resolve() for _, p in game_cache_roots()]
        elif mode in ("custom", "health"):
            approved = self.approved_by_table.get(id(table), [])
        else:
            approved = []
        moved, errors = 0, []
        for item in selected:
            try:
                path = Path(item["path"]).resolve()
                if path.is_symlink() or not path.is_file():
                    continue
                if not any(path.is_relative_to(root) for root in approved):
                    errors.append(f"Пропущен файл вне разрешённой папки: {path}")
                    continue
                send2trash(str(path))
                moved += 1
            except Exception as exc:
                errors.append(f"{item['path']}: {exc}")
        msg = f"Перемещено в корзину: {moved}."
        if errors:
            msg += f"\nОшибок/пропусков: {len(errors)}\n" + "\n".join(errors[:3])
        QMessageBox.information(self, APP_NAME, msg)
        self.refresh_overview()

    def flush_dns(self):
        answer = QMessageBox.question(self, APP_NAME, "Очистить кэш распознавателя DNS (ipconfig /flushdns)?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                QMessageBox.information(self, APP_NAME, "Кэш DNS очищен.")
            else:
                QMessageBox.warning(self, APP_NAME, "Не удалось очистить DNS.\n" + (result.stderr or result.stdout)[-800:])
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Ошибка: {exc}")

    def clear_clipboard(self):
        answer = QMessageBox.question(self, APP_NAME, "Очистить буфер обмена Windows?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.run('cmd /c "echo off | clip"', shell=True, timeout=15)
            QMessageBox.information(self, APP_NAME, "Буфер обмена очищен.")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Ошибка: {exc}")

    def page_startup(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Автозагрузка", "Программы, запускающиеся с Windows. Ярлыки из папки пользователя можно включать/отключать (обратимо)."))
        row = QHBoxLayout()
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("primary")
        disable = QPushButton("Отключить выбранное")
        enable = QPushButton("Включить выбранное")
        row.addWidget(refresh)
        row.addWidget(disable)
        row.addWidget(enable)
        row.addStretch()
        table = self.make_table(["Статус", "Источник", "Название", "Команда / путь"])
        info = QLabel("Пока ничего не загружено.")
        info.setObjectName("muted")
        refresh.clicked.connect(lambda: self.load_startup(table, info))
        disable.clicked.connect(lambda: self.set_startup_enabled(table, info, False))
        enable.clicked.connect(lambda: self.set_startup_enabled(table, info, True))
        l.addLayout(row)
        l.addWidget(table, 1)
        l.addWidget(info)
        QTimer.singleShot(200, lambda: self.load_startup(table, info))
        return page

    def load_startup(self, table, info):
        rows = []
        home = Path.home()
        user_startup = home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        common_startup = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        backup = startup_backup_dir()
        for folder, src, actionable in [(user_startup, "Папка пользователя", True), (common_startup, "Общая папка", False)]:
            try:
                if folder.exists():
                    for p in folder.iterdir():
                        if p.name.lower() == "desktop.ini" or not p.is_file():
                            continue
                        rows.append({"status": "Включено", "source": src, "name": p.name,
                                     "detail": str(p), "kind": "folder", "enabled": True,
                                     "actionable": actionable, "path": p})
            except OSError:
                pass
        try:
            if backup.exists():
                for p in backup.iterdir():
                    if p.is_file():
                        rows.append({"status": "Отключено", "source": "Папка пользователя", "name": p.name,
                                     "detail": str(p), "kind": "folder", "enabled": False,
                                     "actionable": True, "path": p})
        except OSError:
            pass
        try:
            import winreg
            for hive, hname in [(winreg.HKEY_CURRENT_USER, "Реестр (HKCU)"), (winreg.HKEY_LOCAL_MACHINE, "Реестр (HKLM)")]:
                for subkey in [r"Software\Microsoft\Windows\CurrentVersion\Run", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"]:
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            i = 0
                            while True:
                                try:
                                    name, value, _ = winreg.EnumValue(key, i)
                                    rows.append({"status": "Включено", "source": hname, "name": name,
                                                 "detail": str(value), "kind": "registry", "enabled": True,
                                                 "actionable": False, "path": None})
                                    i += 1
                                except OSError:
                                    break
                    except OSError:
                        continue
        except Exception:
            pass
        self.startup_rows = rows
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate([row["status"], row["source"], row["name"], row["detail"]]):
                cell = QTableWidgetItem(val)
                cell.setToolTip(val)
                table.setItem(r, c, cell)
        info.setText(f"Записей: {len(rows)}. Включать/отключать можно только ярлыки из папки автозагрузки пользователя; записи реестра — только просмотр.")

    def set_startup_enabled(self, table, info, enable):
        row = table.currentRow()
        if row < 0 or row >= len(self.startup_rows):
            QMessageBox.information(self, APP_NAME, "Сначала выбери строку в списке.")
            return
        entry = self.startup_rows[row]
        if entry["kind"] != "folder" or not entry["actionable"]:
            QMessageBox.information(self, APP_NAME, "Эту запись можно только просматривать (реестр или общая папка). Изменение отключено для безопасности.")
            return
        home = Path.home()
        user_startup = home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        backup = startup_backup_dir()
        try:
            if enable:
                if entry["enabled"]:
                    QMessageBox.information(self, APP_NAME, "Эта запись уже включена.")
                    return
                user_startup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry["path"]), str(user_startup / entry["path"].name))
                message = f"Включено: {entry['name']}"
            else:
                if not entry["enabled"]:
                    QMessageBox.information(self, APP_NAME, "Эта запись уже отключена.")
                    return
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry["path"]), str(backup / entry["path"].name))
                message = f"Отключено: {entry['name']} (ярлык перемещён в резервную папку, можно вернуть обратно)."
            self.load_startup(table, info)
            QMessageBox.information(self, APP_NAME, message)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Не удалось изменить запись: {exc}")

    def page_uninstall(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Удаление программ", "Список установленных программ из реестра Windows. Запускает штатный деинсталлятор выбранной программы."))
        row = QHBoxLayout()
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("primary")
        uninstall = QPushButton("Удалить выбранное")
        row.addWidget(refresh)
        row.addWidget(uninstall)
        row.addStretch()
        table = self.make_table(["Название", "Издатель", "Версия", "Размер"])
        info = QLabel("Нажми «Обновить список».")
        info.setObjectName("muted")
        refresh.clicked.connect(lambda: self.load_programs(table, info))
        uninstall.clicked.connect(lambda: self.run_uninstaller(table, info))
        l.addLayout(row)
        l.addWidget(table, 1)
        l.addWidget(info)
        QTimer.singleShot(200, lambda: self.load_programs(table, info))
        return page

    def load_programs(self, table, info):
        programs = list_installed_programs()
        self.uninstall_rows = programs
        table.setRowCount(len(programs))
        for r, prog in enumerate(programs):
            values = [prog["name"], prog["publisher"], prog["version"],
                      fmt_size(prog["size"]) if prog["size"] else "—"]
            for c, val in enumerate(values):
                cell = QTableWidgetItem(str(val))
                cell.setToolTip(str(val))
                table.setItem(r, c, cell)
        if programs:
            info.setText(f"Найдено программ: {len(programs)}. Выбери строку и нажми «Удалить выбранное».")
        else:
            info.setText("Программы не найдены (реестр доступен только в Windows).")

    def run_uninstaller(self, table, info):
        row = table.currentRow()
        if row < 0 or row >= len(self.uninstall_rows):
            QMessageBox.information(self, APP_NAME, "Сначала выбери программу в списке.")
            return
        prog = self.uninstall_rows[row]
        command = prog.get("uninstall")
        if not command:
            QMessageBox.information(self, APP_NAME, "Для этой программы не указана команда удаления.")
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"Запустить удаление «{prog['name']}»?\n\nБудет вызван штатный деинсталлятор программы.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.Popen(command, shell=True)
            info.setText(f"Запущен деинсталлятор: {prog['name']}. Следуй инструкциям в его окне.")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Не удалось запустить деинсталлятор: {exc}")

    def page_virus(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Поиск вирусов", "Запуск встроенного Microsoft Defender, если он доступен в Windows."))
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel("SexyClean не заменяет антивирус. Здесь можно запросить быстрое сканирование Microsoft Defender."))
        btn = QPushButton("Запустить быстрое сканирование Defender")
        btn.setObjectName("primary")
        status = QLabel("Готово.")
        status.setObjectName("muted")
        btn.clicked.connect(lambda: self.run_defender_scan(status))
        pl.addWidget(btn)
        pl.addWidget(status)
        l.addWidget(panel)
        l.addStretch()
        return page

    def run_defender_scan(self, status):
        answer = QMessageBox.question(self, APP_NAME, "Запустить быстрое сканирование Microsoft Defender? Окно PowerShell может запросить права администратора.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.Popen([
                "powershell", "-NoProfile", "-Command",
                "Start-MpScan -ScanType QuickScan"
            ], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            status.setText("Команда отправлена Microsoft Defender. Проверяй состояние в приложении «Безопасность Windows».")
        except Exception as exc:
            status.setText(f"Не удалось запустить Defender: {exc}")

    def page_sites(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Уведомления сайтов", "Открой настройки уведомлений в своём браузере."))
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel("Выбери браузер. SexyClean откроет встроенную страницу настроек; сами разрешения меняются там."))
        for label, url in [
            ("Google Chrome", "chrome://settings/content/notifications"),
            ("Microsoft Edge", "edge://settings/content/notifications"),
            ("Mozilla Firefox", "about:preferences#privacy"),
        ]:
            b = QPushButton(f"Открыть настройки — {label}")
            b.clicked.connect(lambda checked=False, u=url: self.open_browser_settings(u))
            pl.addWidget(b)
        l.addWidget(panel)
        l.addStretch()
        return page

    def open_browser_settings(self, url):
        opened = False
        for browser in ["chrome", "msedge", "firefox"]:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", browser, url], shell=False)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            QMessageBox.information(self, APP_NAME, f"Не удалось определить браузер автоматически.\nОткрой браузер вручную и перейди по адресу:\n{url}")

    def page_registry(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Скан реестра", "Безопасная проверка наличия основных ключей автозапуска. Ничего не меняется."))
        btn = QPushButton("Проверить ключи автозапуска")
        btn.setObjectName("primary")
        table = self.make_table(["Статус", "Раздел", "Информация"])
        btn.clicked.connect(lambda: self.scan_registry(table))
        l.addWidget(btn)
        l.addWidget(table, 1)
        return page

    def scan_registry(self, table):
        rows = []
        try:
            import winreg
            checks = [
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU Run"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM Run"),
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU RunOnce"),
            ]
            for hive, keypath, label in checks:
                try:
                    with winreg.OpenKey(hive, keypath) as key:
                        count = winreg.QueryInfoKey(key)[1]
                        rows.append(("Найден", label, f"Записей: {count}"))
                except OSError:
                    rows.append(("Не найден", label, "Ключ отсутствует или недоступен"))
        except Exception as exc:
            rows.append(("Ошибка", "Реестр", str(exc)))
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(val))

    def page_recycle(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Очистка корзины", "Сначала проверь содержимое корзины в Проводнике."))
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel("Очистка корзины удаляет файлы окончательно, минуя обычное восстановление. Это действие необратимо."))
        btn = QPushButton("Открыть корзину")
        btn.clicked.connect(lambda: subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"]))
        empty = QPushButton("Очистить корзину…")
        empty.setObjectName("primary")
        empty.clicked.connect(self.empty_recycle_bin)
        pl.addWidget(btn)
        pl.addWidget(empty)
        l.addWidget(panel)
        l.addStretch()
        return page

    def empty_recycle_bin(self):
        answer = QMessageBox.warning(
            self, "Необратимое действие",
            "Очистить корзину Windows? Файлы будут удалены окончательно.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction Stop"],
                capture_output=True, text=True, timeout=90
            )
            if result.returncode == 0:
                QMessageBox.information(self, APP_NAME, "Команда очистки корзины выполнена.")
            else:
                QMessageBox.warning(self, APP_NAME, "Не удалось очистить корзину.\n" + (result.stderr or result.stdout)[-1200:])
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Ошибка: {exc}")

    def page_disks(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Анализ дисков", "Посмотри общий объём, занятое и свободное пространство."))
        refresh = QPushButton("Обновить")
        refresh.setObjectName("primary")
        table = self.make_table(["Диск", "Файловая система", "Всего", "Занято", "Свободно", "Использовано"])
        refresh.clicked.connect(lambda: self.load_disks(table))
        l.addWidget(refresh)
        l.addWidget(table, 1)
        QTimer.singleShot(200, lambda: self.load_disks(table))
        return page

    def load_disks(self, table):
        rows = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                rows.append((part.mountpoint, part.fstype or "—", fmt_size(usage.total), fmt_size(usage.used), fmt_size(usage.free), f"{usage.percent}%"))
            except OSError:
                continue
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(val))

    def page_sysinfo(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Сведения о системе", "Общая информация о компьютере. Только чтение."))
        refresh = QPushButton("Обновить")
        refresh.setObjectName("primary")
        table = self.make_table(["Параметр", "Значение"])
        refresh.clicked.connect(lambda: self.load_sysinfo(table))
        l.addWidget(refresh)
        l.addWidget(table, 1)
        QTimer.singleShot(200, lambda: self.load_sysinfo(table))
        return page

    def load_sysinfo(self, table):
        rows = system_info()
        table.setRowCount(len(rows))
        for r, (key, value) in enumerate(rows):
            kcell = QTableWidgetItem(key)
            vcell = QTableWidgetItem(str(value))
            vcell.setToolTip(str(value))
            table.setItem(r, 0, kcell)
            table.setItem(r, 1, vcell)

    def page_settings(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Настройки", "Настройки внешнего вида и сведения о программе."))
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel(f"SexyClean версия {APP_VERSION}"))
        pl.addWidget(QLabel("Тема: тёмная, неоново-фиолетовая. Фон: assets/sexyclean_wallpaper.png"))
        pl.addWidget(QLabel("Безопасность: файлы очистки перемещаются в корзину только после ручного подтверждения."))
        pl.addWidget(QLabel("Очистка строго ограничена известными папками кэша и временных файлов."))
        pl.addWidget(QLabel("Большие файлы и дубликаты доступны только для просмотра."))
        pl.addWidget(QLabel("Автозагрузка: обратимо включаются/отключаются только ярлыки из папки пользователя; реестр — только чтение."))
        pl.addWidget(QLabel(f"Резервная папка отключённой автозагрузки: {startup_backup_dir()}"))
        l.addWidget(panel)
        l.addStretch()
        return page

    def refresh_overview(self):
        count, free = 0, 0
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                count += 1
                free += usage.free
            except OSError:
                continue
        self.card_drives.metric_label.setText(str(count))
        self.card_drives.detail_label.setText("Обнаруженные тома")
        self.card_free.metric_label.setText(fmt_size(free))
        self.card_free.detail_label.setText("Суммарное свободное место")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #101022; color: #f4f1ff; font-family: "Segoe UI Variable", "Segoe UI", "Arial"; font-size: 10.5pt; }
        QFrame#sidebar { background: rgba(10, 10, 29, 245); border-right: 1px solid #29234d; }
        QLabel#logo { color: #f6eaff; font-family: "Segoe UI Variable Display", "Segoe UI", sans-serif; font-size: 23pt; font-weight: 750; }
        QLabel#tagline { color: #9b91bd; font-size: 8pt; letter-spacing: 1px; }
        QLabel#title { font-family: "Segoe UI Variable Display", "Segoe UI", sans-serif; font-size: 25pt; font-weight: 700; color: #ffffff; }
        QLabel#section { font-size: 14pt; font-weight: 650; }
        QLabel#muted, QLabel#footnote { color: #aaa4d0; }
        QLabel#navSection { color: #8f88b8; font-size: 8pt; font-weight: 700; padding: 9px 8px 2px 8px; letter-spacing: 1px; }
        QPushButton { background: #1b1a38; border: 1px solid #332c62; border-radius: 10px; padding: 10px 14px; }
        QPushButton:hover { background: #29204c; border-color: #ec35cf; }
        QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #f0189a, stop:1 #762cff); border: 1px solid #bd4cff; color: #fff; font-weight: 700; }
        QPushButton#primary:hover { background: #d62bd7; }
        QPushButton#nav { text-align: left; background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 10px 9px; color: #c9c3ec; font-size: 10pt; font-weight: 500; }
        QPushButton#nav:hover { background: #211d46; color: #fff; border-color: #3a2a72; }
        QPushButton#nav:pressed { background: #30245d; }
        QPushButton#nav:checked { background: #2a2256; color: #fff; border-color: #6d3bd6; }
        QFrame#panel, QFrame#card { background: rgba(22, 21, 52, 220); border: 1px solid #302957; border-radius: 15px; }
        QLabel#metric { font-size: 22pt; font-weight: 750; color: #e4a1ff; }
        QProgressBar { background: #211d42; border: 1px solid #302957; border-radius: 6px; height: 12px; text-align: center; color: white; }
        QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #fa168f, stop:1 #8835ff); border-radius: 5px; }
        QTableWidget { background: #131329; alternate-background-color: #1a1938; gridline-color: #302957; border: 1px solid #302957; border-radius: 9px; }
        QHeaderView::section { background: #211e43; color: #e2dcff; border: 0; padding: 9px; font-weight: 650; }
        QLineEdit, QComboBox { background: #171633; border: 1px solid #3a326b; border-radius: 8px; padding: 9px; }
        QCheckBox { spacing: 7px; }
    """)
    window = SexyClean()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
