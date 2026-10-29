#!/usr/bin/env bash
# Установка temp-monitor и команды tempmon для текущего пользователя.
# Установщик запускается от обычного пользователя.
# sudo используется только для отдельных системных операций:
# nvme-cli (если пакет отсутствует) и команды /usr/local/bin/tempmon.

set -Eeuo pipefail

# Запрет запуска от root
if [[ "${EUID}" -eq 0 ]]; then
    echo 'Ошибка: запустите ./install.sh без sudo.' >&2
    exit 1
fi

# Каталог исходного проекта
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# Каталог установленной программы
INSTALL_DIR="${HOME}/.local/share/temp-monitor"
# Путь к системной команде
COMMAND_PATH="/usr/local/bin/tempmon"

# Список файлов для проверки и копирования.
FILES=(
    monitor.py
    config.py
    sensors/__init__.py
    sensors/cpu.py
    sensors/nvme.py
    sensors/fans.py
    ui/__init__.py
    ui/terminal.py
)

# Проверка всех необходимых файлов
for file in "${FILES[@]}"; do
    if [[ ! -f "${SOURCE_DIR}/${file}" ]]; then
        echo "Ошибка: не найден ${SOURCE_DIR}/${file}" >&2
        exit 1
    fi
done

# Проверка Python
command -v python3 >/dev/null || {
    echo 'Требуется Python 3.10+ (sudo apt install python3)' >&2
    exit 1
}
python3 - <<'PY_VERSION'
import sys

if sys.version_info < (3, 10):
    raise SystemExit('Ошибка: нужен Python 3.10 или новее')
PY_VERSION

# Установка nvme-cli, если он отсутствует
if ! command -v nvme >/dev/null 2>&1; then
    echo 'nvme-cli не найден. Устанавливаем через apt...'
    sudo apt-get update
    sudo apt-get install -y nvme-cli
fi

# Создание каталогов
mkdir -p "${INSTALL_DIR}/sensors" "${INSTALL_DIR}/ui"

# Копируем Python-модули с правами 0644.
for file in "${FILES[@]}"; do
    install -m 0644 "${SOURCE_DIR}/${file}" "${INSTALL_DIR}/${file}"
done

# Права на выполнение главного файла
install -m 0755 "${SOURCE_DIR}/monitor.py" "${INSTALL_DIR}/monitor.py"

# Создание временного файла запуска
wrapper="$(mktemp)"
# Удаление временного файла при завершении установщика
trap 'rm -f -- "${wrapper}"' EXIT

# Команда запуска Python-монитора
# Аргументы tempmon передаются в monitor.py.
printf '#!/bin/bash\nexec /usr/bin/python3 %q "$@"\n' \
    "${INSTALL_DIR}/monitor.py" > "${wrapper}"

# Резервное копирование tempmon
if sudo test -e "${COMMAND_PATH}" || sudo test -L "${COMMAND_PATH}"; then
    # Добавляем дату, время и PID к имени копии.
    suffix="$(date +%Y%m%d-%H%M%S)-$$"
    backup="${COMMAND_PATH}.backup-${suffix}"
    sudo cp -a -- "${COMMAND_PATH}" "${backup}"
    echo "Предыдущий tempmon сохранён: ${backup}"
fi

# Установка tempmon в /usr/local/bin.
sudo install -m 0755 "${wrapper}" "${COMMAND_PATH}"

# Результат установки
printf '\nУстановка завершена.\nКоманда: %s\nФайлы: %s\n' \
    "${COMMAND_PATH}" "${INSTALL_DIR}"
echo 'Проверка: tempmon --help'
echo 'Запуск:  tempmon 0.5'
