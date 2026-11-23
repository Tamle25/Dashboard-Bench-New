import json
import os
import sys
import tempfile
from pathlib import Path

# Cấu hình phải đặt TRƯỚC khi import app (settings đọc biến môi trường lúc import).
_tmp = tempfile.mkdtemp(prefix="bench-test-data-")
os.environ["BENCH_DATA_DIR"] = _tmp
os.environ["BENCH_ALLOW_LOCAL_SOURCES"] = "true"
os.environ["BENCH_MOCK_LATENCY_S"] = "[0.001, 0.005]"
os.environ["BENCH_WEBHOOK_SECRET"] = "test-secret"
# Harbor giả: chạy được luồng Harbor mà không cần Docker.
os.environ["BENCH_HARBOR_CMD"] = json.dumps([sys.executable, str(Path(__file__).with_name("fake_harbor.py"))])
os.environ["BENCH_HARBOR_ENV"] = "fake"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "bench-demo"
