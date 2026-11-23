# Bench Console

Dashboard đánh giá mô hình ngôn ngữ: chọn bộ bench, thí sinh, giám khảo → bấm chạy → xem log trực tiếp → xem chi tiết từng mẫu.
Các bộ bench nằm ở **repo Git riêng**; hệ thống đọc file `bench.yaml` trong mỗi repo và ghim commit cho từng lượt chạy.

```
frontend/   React + Vite + TypeScript (6 màn: Kết quả, Chạy mới, Đang chạy, Chi tiết, Nguồn bench, Mô hình)
backend/    FastAPI + SQLAlchemy (SQLite mặc định), chạy benchmark bằng asyncio, log qua SSE
examples/bench-demo/   repo bench mẫu (3 bộ bench + 1 bộ cố ý sai để thấy cảnh báo)
```

## Chạy thử trong 3 phút (không cần GPU)

Yêu cầu: Python 3.11+, Node 20+, Git.

```bash
# 1. Backend
cd backend
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # bật BENCH_ALLOW_LOCAL_SOURCES cho bản demo
python -m app.seed                # thêm nguồn examples/bench-demo + 3 mô hình mock
python dev.py                     # KHÔNG dùng `uvicorn --reload` trực tiếp, xem ghi chú dưới

# 2. Frontend (terminal khác)
cd frontend
npm install
npm run dev                       # http://localhost:5173 (tự proxy /api sang :8000)
```

Dùng `python dev.py`, **đừng** chạy `uvicorn app.main:app --reload` trực tiếp. Repo bench
được clone vào `backend/data/sources/` và mỗi lượt chạy tạo thêm worktree trong
`backend/data/worktrees/` — Terminal-Bench có hơn 700 file `.py`, tất cả rơi vào thư mục mà
uvicorn theo dõi. Hậu quả: uvicorn khởi động lại máy chủ ngay giữa `git fetch` (nguồn kẹt ở
"đang tải toàn bộ repo…") hoặc giữa lượt chạy bench (lượt chạy bị huỷ, trạng thái `stopped`).

`--reload-dir app` **không** chữa được, dù nghe có vẻ hợp lý: `WatchFilesReload.__init__`
của uvicorn bỏ qua mọi thư mục theo dõi nằm bên trong thư mục hiện hành rồi thay bằng chính
thư mục hiện hành, nên vẫn là theo dõi toàn bộ `backend/`. Chỉ `reload_excludes` với đường
dẫn **tuyệt đối** mới ăn — `dev.py` tự dựng đường dẫn đó từ `settings.data_dir`.

Nếu phải tự gõ lệnh, đây là dạng đúng (đường dẫn tương đối sẽ không có tác dụng):

```bash
uvicorn app.main:app --reload --reload-exclude "$(pwd)/data" --port 8000
```

Vào **Chạy mới** → chọn "Tóm tắt mini" → chọn `mock-strong`, `mock-weak`, giám khảo `mock-judge` → **Chạy**.

Triển khai một tiến trình: `npm run build` trong `frontend/`, sau đó `uvicorn app.main:app` tự phục vụ luôn giao diện ở `/`.

Kiểm thử backend: `cd backend && python -m pytest -q`.

## Kết nối repo bench thật

### 1. Chuẩn hoá repo bench

Đặt ở gốc repo (xem mẫu trong [examples/bench-demo](examples/bench-demo)):

```yaml
# bench.yaml
version: 1
benches:
  - id: vnqa                     # [a-z0-9_-]
    name: VN-QA
    task: Hỏi đáp kiến thức tiếng Việt
    data: data/vnqa.jsonl
    scoring:
      type: exact_match          # exact_match | unit_test | llm_judge | custom
      rubric: rubrics/x.md       # tuỳ chọn, cho llm_judge
      scorer: scorers/s.py:score # bắt buộc khi type = custom
    judge_required: false        # mặc định = (type == llm_judge)
```

Mỗi dòng của file JSONL:

```json
{"id": "12", "category": "Viết", "input": "…đề bài… hoặc [{\"role\":\"user\",\"content\":\"…\"}]", "reference": "…", "test": "assert … (chỉ cho unit_test)"}
```

| `scoring.type` | Cách chấm | Điểm |
|---|---|---|
| `exact_match` | So khớp sau khi chuẩn hoá (chữ thường, khoảng trắng, dấu câu); khớp cả dòng cuối | tỉ lệ đúng |
| `unit_test` | Chạy code của thí sinh + `test` trong tiến trình Python riêng | tỉ lệ pass |
| `llm_judge` | Giám khảo chấm 1–10 theo rubric, trả JSON | trung bình điểm × 10 |
| `custom` | Gọi `score(sample, output) -> {"score": 0..1, "passed": bool, "note": str}` trong repo | trung bình `score` × 100 |

### 1b. Bộ bench dạng Harbor / Terminal-Bench

Repo kiểu [terminal-bench](https://github.com/harbor-framework/terminal-bench): mỗi task là một thư mục gồm `task.toml`, `instruction.md`, `environment/Dockerfile` và `tests/`. Agent làm bài trong container, sau đó verifier của task ghi reward. Bench Console gọi `harbor run -p <task> -a <agent> …` cho từng task rồi đọc `result.json`.

- **Không có `bench.yaml`**: hệ thống tự nhận diện các thư mục chứa task. Nó bỏ qua `scripts/`, `leaderboard/`, thư mục bắt đầu bằng `_`, và thư mục chỉ có 1 task.
- **Có `bench.yaml`**: khai báo `scoring.type: harbor`, `data:` là thư mục chứa task. Có thể thêm:
  - `prepare:`: lệnh sinh task trước khi chạy, ví dụ `build_variants.py`.
  - `exclude:`: danh sách task bỏ qua, theo tên chính xác hoặc mẫu glob (vd. `"*-ask_human"`).
- Dọn dẹp tự động (môi trường `docker`): chấm xong một task là gỡ image Docker của task đó (image kéo
  sẵn và image Harbor tự build), vì mỗi task có thể chiếm 1–9 GB. Kết quả và log trong
  `data/harbor-jobs/` được giữ nguyên. Tắt bằng `BENCH_HARBOR_CLEANUP_IMAGES=false`. Khi khởi động,
  backend cũng xoá worktree còn sót của các lượt chạy bị ngắt giữa chừng.

  Mẫu cho repo vn-bench: [examples/terminal-bench-lct/bench.yaml](examples/terminal-bench-lct/bench.yaml).
- **Thí sinh** cho bộ bench Harbor:
  - Mô hình OpenAI-compatible: chạy qua agent `terminus-2` (đổi được trong ô "Agent").
  - Hoặc kiểu **Agent Harbor**: `oracle` (chạy lời giải mẫu, dùng để kiểm tra task có hỏng không), `claude-code`, `codex`…
- **Cần trên máy chủ**:
  - Harbor CLI (`uv tool install harbor`).
  - Docker đang chạy. Hoặc đặt `BENCH_HARBOR_ENV=modal` / `daytona` để chạy trên cloud.
- **Lưu ý tài nguyên**: nhiều task cần 4–32 GB RAM và chạy hàng giờ. Trên laptop nên để "Task song song" = 1–2.

### 2. Kết nối trên giao diện

**Nguồn bench → Kết nối repo**:
1. Nhập URL HTTPS/SSH, nhánh/tag/commit, đường dẫn manifest.
2. Chọn cách xác thực:
   - **Công khai**: không cần gì thêm.
   - **Personal access token**: chỉ cần quyền đọc repo.
   - **Deploy key**: khoá SSH chỉ đọc.
3. Bấm **Kiểm tra kết nối**. Hệ thống chỉ tải cấu trúc thư mục và manifest (partial clone), nên mất vài giây kể cả với repo lớn.
4. Bấm **Thêm nguồn**. Lần đồng bộ đầu tải toàn bộ repo ở chế độ nền; repo vài trăm MB có thể mất vài chục phút.

Token/khoá được ghi vào kho bí mật phía máy chủ (biến môi trường, hoặc `backend/data/secrets.json` khi phát triển), không lưu trong DB và không trả về trình duyệt.

### 3. Tự đồng bộ khi có push

1. Đặt `BENCH_WEBHOOK_SECRET` trong `backend/.env`.
2. Thêm webhook trỏ về URL hiện dưới mỗi repo ở màn Nguồn bench: `https://<máy-chủ>/api/webhooks/git/<id>`.
   - GitHub: content type JSON, Secret = giá trị trên (xác thực `X-Hub-Signature-256`).
   - GitLab: Secret token = giá trị trên (`X-Gitlab-Token`).

Máy chủ phải truy cập được từ GitHub/GitLab. Nếu không, chọn chế độ "Mỗi giờ" hoặc "Thủ công".

### 4. Thêm mô hình

**Mô hình → Thêm mô hình**, kiểu "OpenAI-compatible":
- Nhập Base URL, ví dụ `http://gpu-node:8000/v1` (vLLM, TGI, Ollama, LM Studio và hầu hết nhà cung cấp API đều hỗ trợ).
- Nếu cần API key: điền **tên** biến bí mật (ví dụ `MODEL_X_API_KEY`), rồi đặt giá trị trong `backend/.env`.

Kiểu "Agent Harbor": điền tên agent (`oracle`, `nop`, `terminus-2`, `claude-code`, `codex`…).
Tên agent được đối chiếu với `harbor agent list`, nên gõ sai sẽ báo ngay ở cột Trạng thái kèm
gợi ý — không phải chờ tới lúc chạy task mới biết. Agent tuỳ chỉnh dạng `module.path:Class`
hoặc `acp:opencode@1.3.9` được bỏ qua bước kiểm này.

**Chạy model Claude bằng gói Pro/Max thay API key** (chỉ agent `claude-code`):
1. Chạy `claude setup-token`, ghi `CLAUDE_CODE_OAUTH_TOKEN=<token>` vào `backend/.env`.
2. Thêm mô hình kiểu "Agent Harbor", agent `claude-code`, chọn **Xác thực → Gói Claude Pro/Max (OAuth)**,
   model ví dụ `anthropic/claude-haiku-5-5`. Ô tên bí mật để trống = `CLAUDE_CODE_OAUTH_TOKEN`.

Backend sẽ đặt `CLAUDE_FORCE_OAUTH=1` và gỡ `ANTHROPIC_API_KEY` khỏi môi trường của Harbor, nên
lượt chạy không tính tiền theo API. Lượt chạy tính vào hạn mức của gói: chạy ít task song song.

Mô hình **được lưu trước, rồi mới thử kết nối**: kết nối hỏng vẫn lưu để bạn sửa lại. Mỗi
dòng có nút **Sửa** (đổi được mọi thứ trừ tên, vì kết quả đã chạy tham chiếu tới tên) và
**Xoá**. Xoá mô hình đã có lịch sử chạy cần xác nhận thêm; **kết quả cũ vẫn được giữ** vì bảng
kết quả chỉ lưu tên mô hình dạng chuỗi. Không sửa/xoá được mô hình đang có trong lượt chạy dở.

Giám khảo phải là mô hình kiểu OpenAI-compatible (hoặc mock): agent Harbor không có endpoint
Chat Completions nên không chấm được theo rubric, và hệ thống từ chối ngay khi tạo lượt chạy.

## Kiến trúc

```
React ──/api──▶ FastAPI ──▶ SQLite/Postgres  (sources, benches, model_endpoints, runs, run_entries, sample_results, run_logs)
   ▲  SSE           │
   └── log ◀── RunManager (asyncio) ──▶ git worktree @ commit đã ghim ──▶ đọc JSONL ──▶ gọi thí sinh ──▶ chấm (exact/unit/judge/custom)
                    │
                    └── Đồng bộ nguồn: git fetch --depth 1 <ref> → đọc bench.yaml → kiểm tra schema → cập nhật benches
```

| Màn | API |
|---|---|
| Kết quả | `GET /api/stats`, `GET /api/leaderboard?bench_id=`, `GET /api/results?bench_id=` |
| Chạy mới | `GET /api/benches`, `GET /api/models`, `POST /api/runs` |
| Đang chạy | `GET /api/runs/active`, `GET /api/runs/:id/stream` (SSE), `POST /api/runs/:id/cancel` |
| Chi tiết | `GET /api/results/:id`, `GET /api/results/:id/samples?filter=`, `GET /api/results/:id/export` |
| Nguồn bench | `GET/POST /api/sources`, `POST /api/sources/test`, `PATCH /api/sources/:id`, `POST /api/sources/:id/sync`, `DELETE /api/sources/:id`, `POST /api/webhooks/git/:id` |
| Mô hình | `GET/POST /api/models`, `PATCH /api/models/:id`, `POST /api/models/:id/ping`, `DELETE /api/models/:id?force=` |

Tài liệu API tương tác: http://localhost:8000/docs

## Trước khi đưa lên môi trường thật

- **Cách ly code chạy thử**: `unit_test` và `custom` scorer thực thi code (của thí sinh / của repo bench) trong tiến trình con, **không phải sandbox**. Hãy chạy backend/worker trong container không có mạng ra ngoài và không có bí mật, hoặc tách phần chấm sang sandbox riêng (gVisor, Firecracker…). Chỉ kết nối repo bench bạn tin tưởng.
- **Đăng nhập**: chưa có xác thực người dùng. Đặt sau reverse proxy có SSO, hoặc thêm OAuth/OIDC vào FastAPI.
- **Kho bí mật**: thay `app/services/secrets.py` bằng Vault / AWS Secrets Manager / Kubernetes Secret (giữ giao diện `get/set/delete`).
- **Nhiều worker**: `RunManager` hiện chạy trong tiến trình API (một lượt chạy một lúc). Khi cần song song nhiều lượt hoặc nhiều máy, chuyển `_execute` sang hàng đợi (Arq/Celery) và pub/sub log qua Redis. API và giao diện giữ nguyên.
- **Postgres**: đặt `BENCH_DATABASE_URL=postgresql+psycopg://…` và thêm `psycopg[binary]` vào requirements. Nên thêm Alembic để quản lý migration.
- `BENCH_ALLOW_LOCAL_SOURCES` phải là `false`.
