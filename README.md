# SexyClean 2.0

## Запуск
```bat
python -m pip install -r requirements.txt
python sexy_cleaner.py
```

## Сборка EXE
```bat
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name "SexyClean" --add-data "assets;assets" sexy_cleaner.py
```

Включены: обзор дисков, очистка известных временных файлов и кэшей с подтверждением, поиск больших файлов, точных дубликатов, игровых кэшей, просмотр автозагрузки, запуск быстрого сканирования Microsoft Defender, переход к настройкам уведомлений браузера, проверка ключей автозапуска в реестре (только чтение), очистка корзины с предупреждением, анализ дисков и настройки.

Большие файлы и дубликаты показываются только для просмотра. Автозагрузка и реестр не изменяются. SexyClean не заменяет антивирус.
