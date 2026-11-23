"""Bảng dữ liệu.

Source      repo Git chứa một hoặc nhiều bộ bench (khai báo trong manifest).
Bench       một bộ bench đọc từ manifest của Source.
ModelEndpoint  thí sinh và/hoặc giám khảo.
Run         một lần bấm "Chạy": một bộ bench, một commit đã ghim, nhiều thí sinh.
RunEntry    kết quả của một thí sinh trong một Run (một dòng ở bảng Kết quả).
SampleResult  kết quả từng mẫu của một RunEntry.
RunLog      log của Run, phát trực tiếp qua SSE.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timeutil import now


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(1000))
    host: Mapped[str] = mapped_column(String(40), default="Git")
    ref: Mapped[str] = mapped_column(String(200), default="main")
    sha: Mapped[str | None] = mapped_column(String(64))
    manifest_path: Mapped[str] = mapped_column(String(300), default="bench.yaml")
    auth: Mapped[str] = mapped_column(String(20), default="public")  # public | deploy_key | token
    sync_mode: Mapped[str] = mapped_column(String(20), default="webhook")  # webhook | hourly | manual
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | syncing | ok | warn | error
    message: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    benches: Mapped[list["Bench"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Bench(Base):
    __tablename__ = "benches"
    __table_args__ = (UniqueConstraint("source_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(String(500), default="")
    scoring_type: Mapped[str] = mapped_column(String(20))
    rubric_path: Mapped[str | None] = mapped_column(String(300))
    scorer: Mapped[str | None] = mapped_column(String(300))
    judge_required: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSONL (chat) hoặc thư mục chứa các task Harbor (scoring_type = harbor).
    data_path: Mapped[str] = mapped_column(String(300))
    size: Mapped[int] = mapped_column(Integer, default=0)
    # Tuỳ chọn từ manifest: {"prepare": [...], "exclude": [...]}.
    options: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # False khi bộ bench không còn trong manifest (giữ lại để lịch sử không bị gãy).
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    source: Mapped[Source] = relationship(back_populates="benches")


class ModelEndpoint(Base):
    __tablename__ = "model_endpoints"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="contestant")  # contestant | judge | both
    # openai: endpoint Chat Completions; harbor: agent Harbor tự lo model (oracle, claude-code…); mock: thử nghiệm
    kind: Mapped[str] = mapped_column(String(20), default="openai")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    # Agent Harbor dùng khi mô hình này chạy bộ bench dạng Harbor (mặc định terminus-2 cho kind openai).
    agent: Mapped[str | None] = mapped_column(String(100))
    # Giá trị gửi trong trường "model" của API; để trống = dùng id.
    model_name: Mapped[str | None] = mapped_column(String(200))
    # Tên bí mật (biến môi trường hoặc kho bí mật) chứa API key. Không lưu key trong DB.
    secret_name: Mapped[str | None] = mapped_column(String(200))
    # api_key: bí mật là API key. oauth: bí mật là token gói Claude Pro/Max (`claude setup-token`),
    # chỉ dùng cho agent Harbor claude-code. NULL (hàng cũ trước khi có cột) = api_key.
    auth: Mapped[str | None] = mapped_column(String(20), default="api_key")
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown | checking | ok | down
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    bench_id: Mapped[int] = mapped_column(ForeignKey("benches.id"))
    source_sha: Mapped[str | None] = mapped_column(String(64))
    judge_id: Mapped[str | None] = mapped_column(String(100))
    threshold: Mapped[float] = mapped_column(Float, default=6)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed | stopped
    warns: Mapped[int] = mapped_column(Integer, default=0)
    errs: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    bench: Mapped[Bench] = relationship()
    entries: Mapped[list["RunEntry"]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="RunEntry.id")


class RunEntry(Base):
    __tablename__ = "run_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(100))
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    review: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | failed | stopped
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="entries")


class SampleResult(Base):
    __tablename__ = "sample_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("run_entries.id"), index=True)
    sample_id: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(100), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[float] = mapped_column(Float, default=0)  # điểm chuẩn hoá 0..1
    raw_score: Mapped[str] = mapped_column(String(50), default="")  # "8/10", "1/1"…
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    level: Mapped[str] = mapped_column(String(10))  # INFO | WARN | ERROR | DONE
    message: Mapped[str] = mapped_column(Text)
