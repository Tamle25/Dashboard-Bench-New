"""Agent Harbor bọc thêm bước đổi mirror apt trong container trước khi cài gói hệ thống.

Nạp bằng `harbor run -a bench_apt_mirror:<tên agent>` (vd. `bench_apt_mirror:claude-code`), với
thư mục này trong PYTHONPATH và biến BENCH_APT_MIRROR = URL mirror Ubuntu
(vd. http://mirror.bizflycloud.vn/ubuntu). Backend dashboard tự làm việc này khi đặt
BENCH_APT_MIRROR; module này chạy trong môi trường Python của Harbor, không phải của backend.

Lý do: agent cài đặt (claude-code, codex…) gọi `apt-get install nodejs npm curl…` trong
container khi image thiếu. Image task trỏ tới archive.ubuntu.com / security.ubuntu.com; khi
đường ra quốc tế nghẽn, bước này treo tới hết thời gian chờ cài agent (AgentSetupTimeoutError).
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import BaseInstalledAgent

MIRROR_ENV = "BENCH_APT_MIRROR"

# Đổi URL của Ubuntu (archive, security, <cc>.archive) trong cả định dạng cũ (sources.list)
# lẫn deb822 (ubuntu.sources của 24.04). Image Debian / Alpine không bị đụng tới.
_SCRIPT = r"""
command -v apt-get >/dev/null 2>&1 || exit 0
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$f" ] || continue
  sed -i -E "s#https?://([a-z]{2}\.)?(archive|security)\.ubuntu\.com/ubuntu/?#$MIRROR/#g" "$f"
done
exit 0
"""


class AptMirrorMixin:
    async def ensure_system_dependencies(self, environment, dependencies: tuple[str, ...]) -> None:
        mirror = os.environ.get(MIRROR_ENV, "").strip().rstrip("/")
        if mirror and dependencies:
            try:
                await self.exec_as_root(environment, command=f"MIRROR={shlex.quote(mirror)}; {_SCRIPT}")
            except Exception:  # không đổi được mirror thì vẫn cài như bình thường
                pass
        await super().ensure_system_dependencies(environment, dependencies)


_cache: dict[str, type] = {}


def __getattr__(name: str) -> Any:
    """`bench_apt_mirror:claude-code` → lớp ClaudeCode có thêm bước đổi mirror.

    Agent không cài gói hệ thống (oracle, nop, terminus-2…) được trả về nguyên bản.
    """
    if name.startswith("__"):
        raise AttributeError(name)
    if name not in _cache:
        try:
            base = AgentFactory.get_agent_class(name)
        except (KeyError, ValueError) as e:
            raise AttributeError(f"Harbor không có agent '{name}'") from e
        if isinstance(base, type) and issubclass(base, BaseInstalledAgent):
            _cache[name] = type(f"AptMirror{base.__name__}", (AptMirrorMixin, base), {"__module__": __name__})
        else:
            _cache[name] = base
    return _cache[name]
