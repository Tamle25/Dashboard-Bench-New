"""Agent Harbor cho một agent CLI viết bằng Node mà mã nguồn KHÔNG public (gói nội bộ công ty).

Nạp bằng `harbor run -a node_agent:NodeAgent` với thư mục này trong PYTHONPATH (backend dashboard
tự thêm). Module chạy trong môi trường Python của Harbor, không phải của backend.

Mỗi task, Harbor:
  1. install(): cài Node (nvm) trong container của task, chép gói agent từ máy host vào rồi
     `npm install -g`. Mã nguồn chỉ đi từ máy này vào container Docker trên chính máy này
     (với BENCH_HARBOR_ENV=docker), không lên npm công khai hay dịch vụ ngoài.
  2. run(): chạy lệnh của agent MỘT LẦN, không tương tác, với đề bài của task. Agent sửa file
     trong container rồi thoát; verifier của task chấm điểm sau đó.

Cấu hình bằng biến môi trường của tiến trình Harbor. Trên dashboard: ghi vào backend/.env và
đặt BENCH_HARBOR_PASS_ENV=["NODE_AGENT_*"] để backend chuyển chúng sang Harbor.

  NODE_AGENT_PACKAGE   (bắt buộc) gói agent. Một trong:
                         - đường dẫn file .tgz trên máy host, tạo bằng `npm pack` (khuyên dùng);
                         - đường dẫn thư mục mã nguồn trên máy host (không nên chứa node_modules
                           build trên Windows: module native sẽ hỏng trong container Linux);
                         - tên gói trên registry nội bộ, vd. @congty/agent@1.2.3 (cần NODE_AGENT_NPMRC).
  NODE_AGENT_CMD       (bắt buộc) lệnh chạy agent không tương tác. Đề bài có trong biến
                       $AGENT_INSTRUCTION và file $AGENT_INSTRUCTION_FILE; hoặc viết {instruction}
                       để chèn đề bài (đã quote). Model chọn trên dashboard nằm trong $AGENT_MODEL.
                       Vd. company-agent run --yes --prompt-file "$AGENT_INSTRUCTION_FILE"
  NODE_AGENT_ENV       tên các biến (cách nhau dấu phẩy) chuyển vào container cho agent,
                       vd. COMPANY_API_KEY,COMPANY_BASE_URL. Chỉ những biến này vào container.
  NODE_AGENT_NPMRC     đường dẫn file .npmrc trên host (token registry nội bộ), chỉ dùng lúc cài
                       rồi xoá khỏi container.
  NODE_AGENT_NODE      phiên bản Node chính, mặc định 22.
  NODE_AGENT_SETUP     lệnh chạy thêm sau khi cài (tuỳ chọn), vd. ghi file cấu hình của agent.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.node_install import DEFAULT_NODE_MAJOR, nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from bench_apt_mirror import AptMirrorMixin

REMOTE_DIR = "/installed-agent/node-agent"
LOG_FILE = "/logs/agent/node-agent.txt"
INSTRUCTION_FILE = "/tmp/agent-instruction.md"
NVM = '. "$HOME/.nvm/nvm.sh"'


def _cfg(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"Chưa đặt {name} (xem backend/harbor_plugins/node_agent.py)")
    return value


class NodeAgent(AptMirrorMixin, BaseInstalledAgent):
    @staticmethod
    def name() -> str:
        return "node-agent"

    async def _upload_owned(self, environment: BaseEnvironment, source: Path, target: str, is_dir: bool) -> None:
        if is_dir:
            await environment.upload_dir(source, target)
        else:
            await environment.upload_file(source, target)
        if environment.default_user is not None:  # upload chạy bằng root; user của agent phải đọc được
            await self.exec_as_root(environment, command=f"chown -R {shlex.quote(str(environment.default_user))} {shlex.quote(target)}")

    async def install(self, environment: BaseEnvironment) -> None:
        package = _cfg("NODE_AGENT_PACKAGE", required=True)
        node_major = int(_cfg("NODE_AGENT_NODE") or DEFAULT_NODE_MAJOR)

        await self.ensure_system_dependencies(environment, ("curl", "git", "ca_certificates", "coreutils"))
        await self.exec_as_root(environment, command=f"mkdir -p {REMOTE_DIR} && chmod 777 {REMOTE_DIR}")

        local = Path(package)
        if local.is_file():
            target = f"{REMOTE_DIR}/{local.name}"
            await self._upload_owned(environment, local, target, is_dir=False)
            spec = target
        elif local.is_dir():
            target = f"{REMOTE_DIR}/src"
            await self._upload_owned(environment, local, target, is_dir=True)
            spec = target
        else:
            spec = package  # tên gói trên registry nội bộ

        npmrc = _cfg("NODE_AGENT_NPMRC")
        if npmrc:
            await self._upload_owned(environment, Path(npmrc), f"{REMOTE_DIR}/.npmrc", is_dir=False)

        npmrc_flag = f"--userconfig {REMOTE_DIR}/.npmrc " if npmrc else ""
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    f"{nvm_node_install_snippet(node_major)} && "
                    f"npm install -g {npmrc_flag}{shlex.quote(spec)}"
                ),
            )
        finally:
            if npmrc:  # token registry không ở lại container trong lúc agent làm bài
                await self.exec_as_root(environment, command=f"rm -f {REMOTE_DIR}/.npmrc")

    async def configure(self, environment: BaseEnvironment) -> None:
        setup = _cfg("NODE_AGENT_SETUP")
        if setup:
            await self.exec_as_agent(environment, command=f"{NVM}; {setup}", env=self._agent_env())

    def _agent_env(self) -> dict[str, str]:
        names = [n.strip() for n in _cfg("NODE_AGENT_ENV").split(",") if n.strip()]
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise ValueError(f"NODE_AGENT_ENV liệt kê biến chưa có giá trị: {', '.join(missing)}")
        env = {n: os.environ[n] for n in names}
        if self.model_name:
            env["AGENT_MODEL"] = self.model_name
        return env

    def populate_context_post_run(self, context: AgentContext) -> None:
        pass

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        cmd = _cfg("NODE_AGENT_CMD", required=True)
        with tempfile.TemporaryDirectory(prefix="node-agent-") as tmp:
            f = Path(tmp) / "instruction.md"
            f.write_text(instruction, encoding="utf-8")
            await self._upload_owned(environment, f, INSTRUCTION_FILE, is_dir=False)

        env = {**self._agent_env(), "AGENT_INSTRUCTION": instruction, "AGENT_INSTRUCTION_FILE": INSTRUCTION_FILE}
        cmd = cmd.replace("{instruction}", shlex.quote(instruction))
        await self.exec_as_agent(
            environment,
            command=f"{NVM}; ( {cmd} ) 2>&1 | stdbuf -oL tee {LOG_FILE}",
            env=env,
        )
