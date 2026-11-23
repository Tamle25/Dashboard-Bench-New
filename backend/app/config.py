from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Cấu hình đọc từ biến môi trường (tiền tố BENCH_) hoặc file backend/.env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BENCH_", extra="ignore", protected_namespaces=("settings_",))

    data_dir: Path = Path("./data")
    # Để trống = SQLite trong data_dir. Ví dụ Postgres: postgresql+psycopg://user:pass@host/bench
    database_url: str = ""
    # Bí mật dùng để xác thực webhook từ GitHub (X-Hub-Signature-256) / GitLab (X-Gitlab-Token).
    webhook_secret: str = ""
    # Cho phép nguồn bench là thư mục cục bộ (chỉ nên bật khi phát triển).
    allow_local_sources: bool = False
    cors_origins: list[str] = ["http://localhost:5173"]
    model_timeout_s: float = 60.0
    model_retries: int = 3
    # Thời gian chờ của mô hình mock (giây), để thử luồng chạy không cần GPU.
    mock_latency_s: tuple[float, float] = (0.05, 0.4)
    # Thời gian tối đa cho một lệnh git (giây). Repo bench lớn (vd. Terminal-Bench ~600MB) có thể
    # mất hàng chục phút cho lần tải đầu trên mạng chậm.
    git_timeout_s: int = 3600
    # Chu kỳ quét các nguồn có chế độ đồng bộ "mỗi giờ".
    hourly_sync_check_s: int = 300
    # --- Harbor (bộ bench dạng Terminal-Bench: mỗi task là môi trường Docker + verifier) ---
    # Lệnh gọi Harbor CLI, dạng danh sách JSON. Ví dụ: ["harbor"] hoặc ["uv", "run", "harbor"].
    harbor_cmd: list[str] = ["harbor"]
    # Môi trường chạy task của Harbor: docker (máy này), modal, daytona…
    harbor_env: str = "docker"
    # Tham số thêm cho mọi lệnh `harbor run`, ví dụ ["--timeout-multiplier", "0.5"].
    harbor_extra_args: list[str] = []
    # Gỡ image Docker của task sau khi chấm xong (chỉ khi harbor_env = docker). Mỗi task thường có
    # image riêng 1–9 GB; giữ lại thì ổ đĩa đầy sau vài chục task. Kết quả và log vẫn giữ nguyên.
    # Tắt (false) nếu muốn chạy lại cùng task nhiều lần mà không phải kéo/build lại image.
    harbor_cleanup_images: bool = True
    # Mirror Ubuntu thay cho archive/security.ubuntu.com khi agent cài gói hệ thống trong container
    # (vd. claude-code cần nodejs). Đặt khi đường ra quốc tế chậm, vd. http://mirror.bizflycloud.vn/ubuntu.
    # Để trống = không đổi. Xem backend/harbor_plugins/bench_apt_mirror.py.
    apt_mirror: str = ""
    # Bí mật trong backend/.env (tên hoặc mẫu glob) chuyển sang môi trường của Harbor, cho agent
    # tuỳ chỉnh cần nhiều biến, vd. ["NODE_AGENT_*", "COMPANY_API_KEY"]. Ô "Tên bí mật" của một
    # mô hình chỉ chuyển được một biến. Xem backend/harbor_plugins/node_agent.py.
    harbor_pass_env: list[str] = []
    # Thời gian tối đa cho bước "prepare" của manifest (giây).
    prepare_timeout_s: int = 900

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'bench.db').resolve().as_posix()}"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def worktree_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def secrets_file(self) -> Path:
        return self.data_dir / "secrets.json"


settings = Settings()
