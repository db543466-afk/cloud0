import os
import sys
import hashlib
import shutil
import subprocess
import tempfile
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
APP_VERSION = "2.0"
ACCENT = "#ed28c7"
PURPLE = "#8b5cf6"


def fmt_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


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


class ScanWorker(QThread):
    progress = Signal(int, str)
    found = Signal(list)
    failed = Signal(str)

    def __init__(self, mode, roots=None, min_size_mb=100):
        super().__init__()
        self.mode = mode
        self.roots = roots or []
        self.min_size = int(min_size_mb * 1024 * 1024)

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
        self.pages_by_key = {}
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
            ("SMART SCAN", [("Smart scan", "overview")]),
            ("ЗАЩИТА", [("Поиск вирусов", "virus"), ("Уведомления сайтов", "sites")]),
            ("ОПТИМИЗАЦИЯ", [("Скан реестра", "registry"), ("Автозагрузка", "startup")]),
            ("ПАМЯТЬ", [("Очистка мусора", "cleanup"), ("Большие файлы", "large"),
                         ("Очистка корзины", "recycle"), ("Игровые файлы", "games"),
                         ("Дубликаты файлов", "duplicates"), ("Анализ дисков", "disks")]),
            ("ПРОГРАММА", [("Настройки", "settings")])
        ]
        for group, items in nav_groups:
            header = QLabel(group)
            header.setObjectName("navSection")
            sl.addWidget(header)
            for label, key in items:
                b = QPushButton(label)
                b.setObjectName("nav")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda checked=False, k=key: self.open_page(k))
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
        self.pages_by_key["cleanup"] = self.page_scan("Очистка мусора", "Временные файлы и известные кэши приложений.", "cleanup")
        self.pages_by_key["large"] = self.page_scan("Большие файлы", "Поиск больших файлов в выбранной папке.", "large")
        self.pages_by_key["duplicates"] = self.page_scan("Дубликаты файлов", "Поиск одинаковых файлов по размеру и SHA-256.", "duplicates")
        self.pages_by_key["games"] = self.page_scan("Игровые и приложенческие кэши", "Только известные папки кэша; сохранения не затрагиваются.", "games")
        self.pages_by_key["startup"] = self.page_startup()
        self.pages_by_key["virus"] = self.page_virus()
        self.pages_by_key["sites"] = self.page_sites()
        self.pages_by_key["registry"] = self.page_registry()
        self.pages_by_key["recycle"] = self.page_recycle()
        self.pages_by_key["disks"] = self.page_disks()
        self.pages_by_key["settings"] = self.page_settings()
        for key in ["overview", "virus", "sites", "registry", "startup", "cleanup", "large", "recycle", "games", "duplicates", "disks", "settings"]:
            self.pages.addWidget(self.pages_by_key[key])

        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)
        self.open_page("overview")

    def open_page(self, key):
        page = self.pages_by_key.get(key)
        if page:
            self.pages.setCurrentWidget(page)

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
        l.setSpacing(18)
        l.addWidget(self.header("Добро пожаловать в SexyClean", "Управляй очисткой и обслуживанием ПК в одном месте."))
        cards = QHBoxLayout()
        self.card_drives = self.card("Диски", "—", "Обнаруженные тома")
        self.card_free = self.card("Свободное место", "—", "На доступных дисках")
        self.card_scan = self.card("Последнее сканирование", "Готово", "Ничего не изменено")
        cards.addWidget(self.card_drives)
        cards.addWidget(self.card_free)
        cards.addWidget(self.card_scan)
        l.addLayout(cards)

        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel("Быстрые действия"))
        row = QHBoxLayout()
        for label, key in [("Сканировать мусор", "cleanup"), ("Большие файлы", "large"), ("Дубликаты", "duplicates")]:
            b = QPushButton(label)
            b.setObjectName("primary" if key == "cleanup" else "nav")
            b.clicked.connect(lambda checked=False, k=key: self.open_page(k))
            row.addWidget(b)
        pl.addLayout(row)
        status_row = QHBoxLayout()
        self.main_progress = QProgressBar()
        self.main_progress.setValue(0)
        status_row.addWidget(self.main_progress, 1)
        self.ring = ProgressRing()
        status_row.addWidget(self.ring)
        pl.addLayout(status_row)
        self.status = QLabel("Готово к сканированию.")
        self.status.setObjectName("muted")
        pl.addWidget(self.status)
        l.addWidget(panel)
        note = QLabel("SexyClean не удаляет файлы автоматически. Все действия требуют просмотра и подтверждения.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        l.addWidget(note)
        l.addStretch()
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

    def run_scan(self, mode, roots, min_mb, table, bar, summary):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, APP_NAME, "Сканирование уже выполняется.")
            return
        table.setRowCount(0)
        bar.setValue(0)
        self.main_progress.setValue(0)
        self.ring.setValue(0)
        summary.setText("Сканирование…")
        self.worker = ScanWorker(mode, roots, min_mb)
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
        table.setRowCount(len(items))
        total = 0
        for row, item in enumerate(items):
            total += item["size"]
            check = QCheckBox()
            check.setChecked(False)
            table.setCellWidget(row, 0, check)
            vals = [item["category"], fmt_size(item["size"]), item["modified"], item["path"]]
            for col, value in enumerate(vals, 1):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                table.setItem(row, col, cell)
        summary.setText(f"Найдено файлов: {len(items)} · Общий размер: {fmt_size(total)} · Ничего не удалено")
        self.card_scan.metric_label.setText("Готово")
        self.card_scan.detail_label.setText(f"{len(items)} кандидатов")
        self.status.setText("Сканирование завершено. Проверь список перед действием.")

    def clean_selected(self, table, mode):
        if mode not in ("cleanup", "games"):
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
        moved, errors = 0, []
        approved = [root.resolve() for _, root, _ in safe_roots()] if mode == "cleanup" else [p.resolve() for _, p in game_cache_roots()]
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

    def page_startup(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Автозагрузка", "Список программ, запускающихся вместе с Windows. Пока только безопасный просмотр."))
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("primary")
        table = self.make_table(["Источник", "Название", "Команда / путь"])
        info = QLabel("Пока ничего не загружено.")
        info.setObjectName("muted")
        refresh.clicked.connect(lambda: self.load_startup(table, info))
        l.addWidget(refresh)
        l.addWidget(table, 1)
        l.addWidget(info)
        QTimer.singleShot(200, lambda: self.load_startup(table, info))
        return page

    def load_startup(self, table, info):
        rows = []
        home = Path.home()
        startup_dirs = [
            ("Папка пользователя", home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"),
            ("Общая папка", Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs/Startup"),
        ]
        for label, folder in startup_dirs:
            try:
                if folder.exists():
                    for p in folder.iterdir():
                        rows.append((label, p.name, str(p)))
            except OSError:
                pass
        try:
            import winreg
            for hive, hname in [(winreg.HKEY_CURRENT_USER, "Пользовательский реестр"), (winreg.HKEY_LOCAL_MACHINE, "Системный реестр")]:
                for subkey in [r"Software\Microsoft\Windows\CurrentVersion\Run", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"]:
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            i = 0
                            while True:
                                try:
                                    name, value, _ = winreg.EnumValue(key, i)
                                    rows.append((hname, name, str(value)))
                                    i += 1
                                except OSError:
                                    break
                    except OSError:
                        continue
        except Exception:
            pass
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(val))
        info.setText(f"Найдено записей: {len(rows)}. Изменение автозагрузки пока отключено для безопасности.")

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

    def page_settings(self):
        page = QWidget()
        l = QVBoxLayout(page)
        l.addWidget(self.header("Настройки", "Настройки внешнего вида и сведения о программе."))
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.addWidget(QLabel(f"SexyClean версия {APP_VERSION}"))
        pl.addWidget(QLabel("Тема: тёмная, неоново-фиолетовая."))
        pl.addWidget(QLabel("Фон: assets/sexyclean_wallpaper.png"))
        pl.addWidget(QLabel("Безопасность: файлы очистки перемещаются в корзину после ручного подтверждения."))
        pl.addWidget(QLabel("Большие файлы и дубликаты доступны только для просмотра."))
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
