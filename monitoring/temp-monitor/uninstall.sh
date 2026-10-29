#!/usr/bin/env bash
# Удаление temp-monitor, установленного через install.sh.
# Запускать от обычного пользователя, без sudo.
set -Eeuo pipefail

# Запрет запуска от root
if (( EUID == 0 )); then
    echo 'Ошибка: запускайте ./uninstall.sh без sudo.' >&2
    exit 1
fi

# Каталог установленной программы
INSTALL_DIR="${HOME}/.local/share/temp-monitor"
# Путь к системной команде
COMMAND_PATH="/usr/local/bin/tempmon"

# Создаём временный файл для проверки tempmon
expected="$(mktemp)"
# Удаление временного файла при завершении скрипта
trap 'rm -f -- "${expected}"' EXIT

# Формируем содержимое команды из нашего install.sh
printf '#!/bin/bash\nexec /usr/bin/python3 %q "$@"\n' \
    "${INSTALL_DIR}/monitor.py" > "${expected}"

# Проверка существования файла или ссылки
if [[ -e "${COMMAND_PATH}" || -L "${COMMAND_PATH}" ]]; then
    # Проверка совпадения файл и содержимого с командой нашего установщика
    if [[ -f "${COMMAND_PATH}" && ! -L "${COMMAND_PATH}" ]] && \
       cmp -s -- "${expected}" "${COMMAND_PATH}"; then
        # Удаляем только подтверждённую команду tempmon
        sudo rm -- "${COMMAND_PATH}"
        echo "Удалена команда: ${COMMAND_PATH}"
    else
        # Не удаляем чужую или изменённую команду
        echo "Пропускаю ${COMMAND_PATH}: файл не совпадает с install.sh." >&2
        exit 1
    fi
else
    echo "Команда ${COMMAND_PATH} не найдена."
fi

# Не удаляем каталог, если он является символической ссылкой
if [[ -L "${INSTALL_DIR}" ]]; then
    echo "Не удаляю ${INSTALL_DIR}: это символическая ссылка." >&2
    exit 1
elif [[ -d "${INSTALL_DIR}" ]]; then
    # Проверка основных файлов temp-monitor
    if [[ -f "${INSTALL_DIR}/monitor.py" && \
          -f "${INSTALL_DIR}/config.py" && \
          -f "${INSTALL_DIR}/sensors/nvme.py" && \
          -f "${INSTALL_DIR}/ui/terminal.py" ]]; then
        # Удаляем каталог установленной программы
        rm -rf -- "${INSTALL_DIR}"
        echo "Удалён каталог: ${INSTALL_DIR}"
    else
        # Не удаляем каталог с неизвестным содержимым
        echo "Не удаляю ${INSTALL_DIR}: неизвестное содержимое." >&2
        exit 1
    fi
else
    echo "Каталог ${INSTALL_DIR} не найден."
fi

# Результат удаления
printf '\nУдаление завершено.\n'
echo 'Исходники проекта, nvme-cli и резервные копии сохранены.'
echo 'Предыдущая команда tempmon автоматически не восстанавливается.'