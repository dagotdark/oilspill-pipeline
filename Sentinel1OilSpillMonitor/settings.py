import os
from pathlib import Path


ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env(path=ENV_FILE):
    """
    Прочитать KEY=VALUE из .env в os.environ.

    Переменные, уже заданные в окружении, не перезаписываются.
    """

    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():

        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def require_env(name):
    """
    Получить обязательную переменную окружения.
    """

    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set "
            f"(see {ENV_FILE.name}.example)"
        )

    return value
