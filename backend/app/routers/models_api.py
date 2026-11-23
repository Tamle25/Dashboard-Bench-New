import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ModelEndpoint, Run, RunEntry
from ..services import harbor, llm
from ..services.runner import ACTIVE, manager
from ..services.secrets import secrets
from ..timeutil import iso, now

router = APIRouter(prefix="/api/models", tags=["models"])

Role = Literal["contestant", "judge", "both"]
Kind = Literal["openai", "mock", "harbor"]
Auth = Literal["api_key", "oauth"]


class ModelIn(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")
    note: str = ""
    role: Role = "contestant"
    kind: Kind = "openai"
    base_url: str = Field(default="", max_length=500)
    model_name: str | None = None
    secret_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # Agent Harbor (oracle, nop, terminus-2, claude-code, codex…). Bắt buộc khi kind = harbor.
    agent: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_.:@/-]+$")
    # oauth: dùng gói Claude Pro/Max thay API key (chỉ agent claude-code).
    auth: Auth = "api_key"

    @field_validator("model_name", "secret_name", "agent", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        """Ô để trống = không khai báo. Nếu không có bước này, chuỗi rỗng rơi vào kiểm tra
        pattern và báo lỗi khó hiểu cho một ô người dùng cố tình bỏ trống."""
        return None if isinstance(v, str) and not v.strip() else v


class ModelPatch(BaseModel):
    """Sửa mô hình đã có. Chỉ các trường được gửi mới bị thay đổi; `id` không đổi được vì
    lịch sử chạy tham chiếu tới nó. Ô gửi lên rỗng = xoá giá trị cũ."""

    note: str | None = None
    role: Role | None = None
    kind: Kind | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model_name: str | None = None
    secret_name: str | None = Field(default=None, pattern=r"^$|^[A-Za-z_][A-Za-z0-9_]*$")
    agent: str | None = Field(default=None, max_length=100, pattern=r"^$|^[A-Za-z0-9_.:@/-]+$")
    auth: Auth | None = None


def _normalize(
    kind: str, base_url: str, agent: str | None, secret_name: str | None, auth: str | None, model_name: str | None,
) -> tuple[str, str | None, str | None, str]:
    """Kiểm tra và dọn các trường phụ thuộc kiểu kết nối.

    Mỗi kiểu chỉ dùng một phần trong form, nên phải xoá phần không dùng: nếu không, đổi
    kiểu sau khi đã gõ sẽ để lại giá trị cũ (vd. mô hình mock mang theo một agent Harbor
    và một tên bí mật mà nó không bao giờ dùng tới).
    """
    base_url = (base_url or "").strip()
    agent = (agent or "").strip() or None
    secret_name = (secret_name or "").strip() or None
    auth = auth or "api_key"
    if auth == "oauth" and (kind != "harbor" or agent != harbor.OAUTH_AGENT):
        raise HTTPException(422, f"Gói Claude (OAuth) chỉ dùng được với kiểu Agent Harbor và agent {harbor.OAUTH_AGENT}")
    if kind == "openai":
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "Base URL phải bắt đầu bằng http:// hoặc https://")
        return base_url, agent, secret_name, auth
    if kind == "harbor":
        if not agent:
            raise HTTPException(422, "Agent Harbor là bắt buộc (vd. oracle, terminus-2, claude-code)")
        if auth == "oauth":
            # Harbor từ chối OAuth khi model thuộc provider khác Anthropic. Báo ở đây thay vì
            # để từng task hỏng giữa lượt chạy.
            provider = (model_name or "").strip().partition("/")[0] if "/" in (model_name or "") else None
            if provider and provider != "anthropic":
                raise HTTPException(422, "Gói Claude (OAuth) chỉ chạy model Anthropic (vd. anthropic/claude-haiku-5-5)")
            secret_name = secret_name or harbor.OAUTH_ENV
        return "", agent, secret_name, auth  # agent Harbor tự lo endpoint của model
    return base_url or "mock://0.7", None, None, "api_key"  # mock: không gọi mạng nên agent/API key vô nghĩa


def _history(db: Session, model_id: str) -> bool:
    """Mô hình đã xuất hiện trong lượt chạy nào chưa (thí sinh hoặc giám khảo)."""
    return bool(
        db.scalar(select(RunEntry.id).where(RunEntry.model_id == model_id).limit(1))
        or db.scalar(select(Run.id).where(Run.judge_id == model_id).limit(1))
    )


def _in_active_run(db: Session, model_id: str) -> bool:
    """Đang được dùng trong lượt chạy chưa kết thúc. Sửa hay xoá lúc này sẽ đổi cấu hình
    giữa lượt chạy, nên phải chặn."""
    return bool(
        db.scalar(
            select(Run.id).join(RunEntry, RunEntry.run_id == Run.id)
            .where(Run.status.in_(ACTIVE), RunEntry.model_id == model_id).limit(1)
        )
        or db.scalar(select(Run.id).where(Run.status.in_(ACTIVE), Run.judge_id == model_id).limit(1))
    )


def model_out(m: ModelEndpoint, used: bool | None = None) -> dict:
    return {
        "id": m.id, "note": m.note, "role": m.role, "kind": m.kind, "base_url": m.base_url, "model_name": m.model_name, "agent": m.agent,
        "secret_name": m.secret_name, "secret_present": secrets.has(m.secret_name) if m.secret_name else None,
        "auth": m.auth or "api_key",
        "status": m.status, "latency_ms": m.latency_ms, "last_error": m.last_error, "checked_at": iso(m.checked_at),
        "used": used,
    }


async def _ping(db: Session, m: ModelEndpoint) -> None:
    if m.kind == "harbor":
        # Agent Harbor không có endpoint để ping: kiểm tra Harbor CLI + môi trường (Docker…)
        # và tên agent thay thế. Lỗi tên agent được ưu tiên hiện, vì đó là thứ sai ngay trong
        # form này — còn Docker chưa chạy thì sửa ở chỗ khác.
        problems: list[str] = []
        try:
            await asyncio.to_thread(harbor.preflight)
        except harbor.HarborError as e:
            problems.append(str(e))
        if m.agent:
            try:
                await asyncio.to_thread(harbor.check_agent, m.agent)
            except harbor.HarborError as e:
                problems.insert(0, str(e))
        if m.auth == "oauth" and not secrets.has(m.secret_name):
            problems.insert(0, f"Chưa đặt token OAuth {m.secret_name}: chạy `claude setup-token` rồi ghi vào backend/.env.")
        m.status = "down" if problems else "ok"
        m.latency_ms = None
        m.last_error = " ".join(problems) or None
        m.checked_at = now()
        db.commit()
        return
    try:
        m.latency_ms = await llm.ping(manager.client, m)
        m.status, m.last_error = "ok", None
    except llm.ModelCallError as e:
        m.status, m.latency_ms, m.last_error = "down", None, str(e)
    m.checked_at = now()
    db.commit()


@router.get("")
def list_models(db: Session = Depends(get_db)):
    used = {mid for (mid,) in db.execute(select(RunEntry.model_id).distinct())}
    used |= {jid for (jid,) in db.execute(select(Run.judge_id).distinct()) if jid}
    return [model_out(m, m.id in used) for m in db.scalars(select(ModelEndpoint).order_by(ModelEndpoint.id))]


@router.post("", status_code=201)
async def create_model(body: ModelIn, db: Session = Depends(get_db)):
    if db.get(ModelEndpoint, body.id):
        raise HTTPException(409, f"Đã có mô hình {body.id}")
    data = body.model_dump()
    data["base_url"], data["agent"], data["secret_name"], data["auth"] = _normalize(
        body.kind, body.base_url, body.agent, body.secret_name, body.auth, body.model_name
    )
    m = ModelEndpoint(**data)
    db.add(m)
    db.commit()
    await _ping(db, m)
    return model_out(m, False)


@router.patch("/{model_id}")
async def update_model(model_id: str, body: ModelPatch, db: Session = Depends(get_db)):
    m = db.get(ModelEndpoint, model_id)
    if m is None:
        raise HTTPException(404, "Không tìm thấy mô hình")
    if _in_active_run(db, model_id):
        raise HTTPException(409, "Mô hình đang được dùng trong lượt chạy. Chờ xong hoặc dừng lượt chạy trước.")

    patch = body.model_dump(exclude_unset=True)
    kind = patch.get("kind", m.kind)
    agent = patch.get("agent", m.agent)
    # Đổi kiểu/agent mà không gửi auth: bỏ OAuth cũ thay vì báo lỗi cho một ô không ai đụng tới.
    keep_auth = kind == "harbor" and (agent or "").strip() == harbor.OAUTH_AGENT
    m.kind, m.base_url, m.agent, m.secret_name, m.auth = kind, *_normalize(
        kind, patch.get("base_url", m.base_url), agent, patch.get("secret_name", m.secret_name),
        patch.get("auth", m.auth if keep_auth else None), patch.get("model_name", m.model_name),
    )
    if "note" in patch:
        m.note = (patch["note"] or "").strip()
    if "role" in patch:
        m.role = patch["role"]
    if "model_name" in patch:
        val = patch["model_name"]
        m.model_name = (val.strip() or None) if isinstance(val, str) else val
    m.status, m.latency_ms, m.last_error, m.checked_at = "unknown", None, None, None
    db.commit()
    await _ping(db, m)
    return model_out(m, _history(db, model_id))


@router.post("/{model_id}/ping")
async def ping_model(model_id: str, db: Session = Depends(get_db)):
    m = db.get(ModelEndpoint, model_id)
    if m is None:
        raise HTTPException(404, "Không tìm thấy mô hình")
    await _ping(db, m)
    return model_out(m, _history(db, model_id))


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str, force: bool = False, db: Session = Depends(get_db)):
    """Xoá endpoint của mô hình. Kết quả đã chạy KHÔNG bị xoá: bảng kết quả chỉ lưu tên mô
    hình dạng chuỗi. Vì vậy mô hình đã có lịch sử vẫn xoá được, nhưng phải truyền force=true
    để người dùng biết mình đang gỡ một mô hình còn hiện trên bảng xếp hạng."""
    m = db.get(ModelEndpoint, model_id)
    if m is None:
        raise HTTPException(404, "Không tìm thấy mô hình")
    if _in_active_run(db, model_id):
        raise HTTPException(409, "Mô hình đang được dùng trong lượt chạy. Chờ xong hoặc dừng lượt chạy trước.")
    if not force and _history(db, model_id):
        raise HTTPException(409, "Mô hình đã có trong lịch sử chạy. Xoá kèm force=true nếu vẫn muốn gỡ (kết quả cũ được giữ nguyên).")
    db.delete(m)
    db.commit()
