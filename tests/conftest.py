"""Тесты не должны зависеть от настоящего .env: ключи, CRM и пароль админки в них подменены.
Переменные ставятся до импорта app.config; load_dotenv уже заданные значения не перезаписывает."""
import os

for _k, _v in {
    "GIGACHAT_CREDENTIALS": "", "GIGACHAT_CA_BUNDLE_FILE": "", "B24_URL": "", "B24_NOTIFY_USER_ID": "",
    "ADMIN_PASSWORD": "", "APP_TODAY": "", "DEMO_MODE": "false", "LOKI_URL": "",
}.items():
    os.environ[_k] = _v
