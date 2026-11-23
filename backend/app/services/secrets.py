"""Kho bí mật tối giản.

Thứ tự tra: biến môi trường → data/secrets.json → backend/.env (hai nguồn sau chỉ dành
cho máy phát triển). Khi triển khai thật, thay lớp này bằng Vault / AWS Secrets Manager /
Kubernetes Secret, giữ nguyên giao diện get/set/delete.

Phải đọc .env riêng ở đây: pydantic-settings chỉ lấy từ .env những khoá có tiền tố
BENCH_ cho các field của Settings, nó KHÔNG đổ file vào os.environ. Không có bước này
thì API key đặt trong .env (vd. OPENAI_API_KEY) sẽ không bao giờ tới được agent.
"""

import json
import os
import threading
from pathlib import Path

from ..config import settings


class SecretStore:
    def __init__(self, path: Path, dotenv_path: Path | None = None):
        self.path = path
        self.dotenv_path = dotenv_path
        self._lock = threading.Lock()
        self._dotenv_cache: tuple[float, dict[str, str]] | None = None

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _dotenv(self) -> dict[str, str]:
        """Đọc .env (nhớ theo mtime để không mở file ở mỗi lần tra)."""
        if self.dotenv_path is None:
            return {}
        try:
            mtime = self.dotenv_path.stat().st_mtime
        except OSError:
            return {}
        if self._dotenv_cache and self._dotenv_cache[0] == mtime:
            return self._dotenv_cache[1]
        from dotenv import dotenv_values

        data = {k: v for k, v in dotenv_values(self.dotenv_path, encoding="utf-8").items() if v}
        self._dotenv_cache = (mtime, data)
        return data

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str | None) -> str | None:
        if not name:
            return None
        return os.environ.get(name) or self._load().get(name) or self._dotenv().get(name)

    def names(self) -> set[str]:
        """Tên các bí mật trong data/secrets.json và .env (không gồm biến môi trường)."""
        return set(self._load()) | set(self._dotenv())

    def has(self, name: str | None) -> bool:
        return bool(self.get(name))

    def set(self, name: str, value: str) -> None:
        with self._lock:
            data = self._load()
            data[name] = value
            self._save(data)

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(name, None) is not None:
                self._save(data)


secrets = SecretStore(settings.secrets_file, Path(".env").resolve())


def source_secret_name(source_id: int) -> str:
    return f"BENCH_SOURCE_{source_id}_SECRET"
