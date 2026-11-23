"""Agent Harbor cho ForkCode: một agent CLI dạng fork của opencode, phát hành dưới dạng binary đóng.

Nạp bằng `harbor run -a forkcode_agent:ForkcodeAgent -m <provider>/<model>` với thư mục này trong
PYTHONPATH (backend dashboard tự thêm). Module chạy trong môi trường Python của Harbor trên máy host,
không phải của backend.

ForkCode không có lệnh chạy một lần. Mỗi trial, trong container của task:
  install()  cài python3 + gnome-keyring + dbus + python3-secretstorage (apt/dnf/apk), tải binary
             Linux của agent từ máy host vào.
  run()      tải lên: bản chụp DB đã đăng nhập (chỉ giữ credential, bỏ lịch sử session), secret
             OAuth, đề bài, forkcode_client.py. Rồi chạy dbus + gnome-keyring (mật khẩu ngẫu nhiên),
             nạp secret vào keyring, mở `<binary> serve`, và forkcode_client.py tạo session, gửi đề
             bài, tự duyệt quyền, chờ outcome. Xong thì dừng tiến trình và xoá keyring/DB.

Secret không nằm trong DB mà trong keychain của hệ điều hành. Trên Windows adapter đọc thẳng
Credential Manager (target `<brand>:cred:<id>`), nên không phải trích xuất lại mỗi khi token đổi.

Cấu hình bằng biến môi trường của tiến trình Harbor. Trên dashboard: ghi vào backend/.env và đặt
BENCH_HARBOR_PASS_ENV=["FORKCODE_*"] để backend chuyển chúng sang Harbor.

  FORKCODE_BRAND            tên nội bộ của bản fork (chữ thường), quyết định thư mục dữ liệu
                            (~/.local/share/<brand>), tên DB (<brand>*.db), mục keyring/Credential
                            Manager (<brand>:cred:<id>) và tiền tố biến môi trường mà binary đọc
                            (<BRAND>_SERVER_PASSWORD...). Mặc định `forkcode`.
  FORKCODE_BINARY           (bắt buộc) đường dẫn binary Linux x64 của agent trên máy host.
  FORKCODE_DATA_DIR         thư mục dữ liệu của agent đã đăng nhập trên host, mặc định
                            ~/.local/share/<brand> (chứa <brand>-<channel>.db).
  FORKCODE_CREDENTIAL_FILE  chỉ khi host không phải Windows: JSON {"id", "secret"} của credential.
  FORKCODE_VARIANT          low / medium / high (mặc định: phần sau `#` của model, không có thì
                            `default`).
  FORKCODE_WORKDIR          thư mục làm việc của agent trong container, mặc định WORKDIR của image.
  FORKCODE_TIMEOUT_SEC      tự ngắt session sau N giây (mặc định 0: chỉ theo timeout của task).
  FORKCODE_PORT             cổng của `<binary> serve` trong container, mặc định 4096.
  FORKCODE_TRANSIENT_RETRIES  session thất bại vì provider lỗi tạm thời (5xx, timeout) thì gửi
                            "continue" vào cùng session tối đa N lần (mặc định 3, 0 = tắt).
  FORKCODE_SHARE_REFRESH    =1 để gửi cả refresh token vào container. Mặc định không: server xoay
                            vòng refresh token, container tự làm mới sẽ đăng xuất agent trên host.
                            Thay vào đó adapter nhờ service của agent trên host làm mới sát hạn và
                            đẩy access token mới vào container (_push_tokens).

MCP: agent chỉ nhận MCP server qua API lúc chạy, và có build chỉ cho phép đích nằm trong allowlist
dựng sẵn. Task có MCP (hil-bench SQL, SWE ask_human) khi đó sẽ dừng với lỗi rõ ràng thay vì chạy
thiếu công cụ.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, PackageSpec, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

import forkcode_client as fc
from bench_apt_mirror import AptMirrorMixin

REMOTE_BIN = "/installed-agent/forkcode/agent"
REMOTE_RUN = "/tmp/forkcode-run"
PUSH_FILE = f"{REMOTE_RUN}/secret-push.json"
LOG_NAME = "forkcode.txt"
USAGE_NAME = "forkcode-usage.json"
# Làm mới token trên host khi còn chừng này giây. Service có thể bỏ qua yêu cầu khi token còn xa hạn
# (agent chỉ làm mới trong 60 giây cuối), _push_tokens khi đó thử lại sau 30 giây.
REFRESH_MARGIN_SEC = 120

# Chạy trong container: $1 = thư mục chạy. Biến cấu hình (kể cả mật khẩu) nằm trong run.env, không
# truyền qua env của lệnh exec vì Harbor ghi env đó vào log.
LAUNCHER = r"""#!/bin/bash
set -u
R="$1"
. "$R/run.env"
B="$VC_BRAND"        # tên nội bộ của bản fork: thư mục dữ liệu, tên DB, keyring
P="$VC_ENV_PREFIX"   # tiền tố biến môi trường mà binary đọc
export VC_BRAND
: "${HOME:=$(cd ~ 2>/dev/null && pwd || echo /tmp)}"
export HOME
WORKDIR="${VC_WORKDIR:-$PWD}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VDIR="$DATA/$B"
KDIR="$DATA/keyrings"
new_vdir=0; [ -e "$VDIR" ] || new_vdir=1
new_kdir=0; [ -e "$KDIR" ] || new_kdir=1
CDIR="${XDG_CONFIG_HOME:-$HOME/.config}/$B"
new_cdir=0; [ -e "$CDIR" ] || new_cdir=1
BG_PIDS=""

pids_named() {  # tiến trình của user hiện tại có comm = $1 (không cần procps)
  for d in /proc/[0-9]*; do
    [ "$(cat "$d/comm" 2>/dev/null)" = "$1" ] && [ -O "$d" ] && echo "${d#/proc/}"
  done
}

cleanup() {
  rc=$?
  for p in $BG_PIDS $(pids_named gnome-keyring-d); do kill "$p" 2>/dev/null; done
  [ "$new_kdir" = 1 ] && rm -rf "$KDIR"
  [ "$new_vdir" = 1 ] && rm -rf "$VDIR"
  [ "$new_cdir" = 1 ] && rm -rf "$CDIR"
  rm -rf "$R"
  exit $rc
}
trap cleanup EXIT

mkdir -p "$VDIR"
# Tên DB có thể theo channel của binary; <P>_DISABLE_CHANNEL_DB=1 thì dùng <brand>.db.
for n in "$B.db" "$B-stag.db" "$B-staging.db"; do cp "$R/app.db" "$VDIR/$n"; done
export "${P}_DISABLE_CHANNEL_DB=1" "${P}_DISABLE_AUTOUPDATE=1"
# Skills của task (skills_dir trong task.toml): agent đọc skills ở <config>/skills.
if [ -n "${VC_SKILLS_DIR:-}" ] && [ -d "$VC_SKILLS_DIR" ]; then
  mkdir -p "$CDIR/skills" && cp -R "$VC_SKILLS_DIR/." "$CDIR/skills/" && echo "forkcode: đã nạp skills từ $VC_SKILLS_DIR"
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$R/run}"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/forkcode-bus"
DBUS_PID=$(dbus-daemon --session --fork --nopidfile --address="$DBUS_SESSION_BUS_ADDRESS" --print-pid) \
  || { echo "forkcode: không chạy được dbus-daemon"; exit 10; }
BG_PIDS="$BG_PIDS $DBUS_PID"
# Chạy headless, gnome-keyring chỉ tạo được login keyring khi --unlock bằng mật khẩu khác rỗng.
eval "$(printf '%s' "$VC_KEYRING_PASSWORD" | gnome-keyring-daemon --daemonize --unlock --components=secrets 2>"$R/keyring.err")"
export GNOME_KEYRING_CONTROL="${GNOME_KEYRING_CONTROL:-}"
# apt cài python3-secretstorage cho /usr/bin/python3; image kiểu python:3.x lại đặt python3
# khác (/usr/local/bin) lên trước trên PATH.
PY=""
for c in /usr/bin/python3 python3; do "$c" -c 'import secretstorage' 2>/dev/null && { PY="$c"; break; }; done
[ -n "$PY" ] || { echo "forkcode: không có python3 nào import được secretstorage"; exit 11; }
"$PY" -u "$R/forkcode_client.py" inject --secret-file "$R/secret.json" \
  || { echo "--- gnome-keyring ---"; cat "$R/keyring.err"; exit 11; }
# Cho adapter đẩy access token mới vào keyring này giữa chừng (forkcode_agent._push_tokens).
printf 'export DBUS_SESSION_BUS_ADDRESS=%q\nVC_PY=%q\nexport VC_BRAND=%q\n' "$DBUS_SESSION_BUS_ADDRESS" "$PY" "$B" > "$R/bus.env"

cd "$WORKDIR" || exit 12
env "${P}_SERVER_PASSWORD=$VC_SERVER_PASSWORD" "${P}_MCP_ALLOWED_ORIGINS=$VC_MCP_ORIGINS" \
  "$VC_BIN" serve --port "$VC_PORT" >"$R/serve.log" 2>&1 </dev/null &
SERVE_PID=$!
BG_PIDS="$BG_PIDS $SERVE_PID"

VC_SERVER_PASSWORD="$VC_SERVER_PASSWORD" "$PY" -u "$R/forkcode_client.py" run \
  --port "$VC_PORT" --serve-pid "$SERVE_PID" --model "$VC_MODEL" --variant "$VC_VARIANT" \
  --workdir "$WORKDIR" --instruction-file "$R/instruction.md" --mcp-file "$R/mcp.json" \
  --usage-file "$VC_USAGE" --timeout "$VC_TIMEOUT" --transient-retries "$VC_RETRIES"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "--- serve log ---"; tail -n 100 "$R/serve.log"
  echo "--- app logs ---"; tail -n 200 "$VDIR"/log/*.log 2>/dev/null
fi
exit "$rc"
"""

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _cfg(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"Chưa đặt {name} (xem backend/harbor_plugins/forkcode_agent.py)")
    return value


def parse_model(model_name: str | None, variant: str = "") -> tuple[str, str]:
    """`my-provider/my-model#high` → ("my-provider/my-model", "high")."""
    if not model_name or "/" not in model_name:
        raise ValueError(f"Model của ForkCode phải có dạng provider/model, vd. my-provider/my-model (nhận: {model_name!r})")
    model, _, suffix = model_name.partition("#")
    return model, variant or suffix or "default"


def mcp_config(servers) -> tuple[list[dict], str]:
    """MCP server của task → (cấu hình cho API của agent, danh sách origin cho allowlist)."""
    out, origins = [], []
    for server in servers or []:
        if server.transport == "stdio" or not server.url:
            raise ValueError(f"MCP server '{server.name}' chạy bằng lệnh (stdio): ForkCode không hỗ trợ loại này")
        headers = {k: _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v) for k, v in (server.headers or {}).items()}
        config = {"type": "remote", "url": server.url, "oauth": False}
        if headers:
            config["headers"] = headers
        out.append({"name": server.name, "config": config})
        parts = urllib.parse.urlsplit(server.url)
        origins.append(f"{parts.scheme}://{parts.netloc}")
    return out, ",".join(dict.fromkeys(origins))


class ForkcodeAgent(AptMirrorMixin, BaseInstalledAgent):
    capabilities = AgentCapabilities(mcp_servers=True, skills=True)

    SYSTEM_PACKAGES = {
        **BaseInstalledAgent.SYSTEM_PACKAGES,
        "python3": PackageSpec.standard("python3"),
        "keyring": PackageSpec(
            commands=("gnome-keyring-daemon", "dbus-daemon"),
            packages={
                "apt-get": ("gnome-keyring", "dbus", "libsecret-1-0"),
                "dnf": ("gnome-keyring", "dbus-daemon", "libsecret"),
                "yum": ("gnome-keyring", "dbus", "libsecret"),
                "apk": ("gnome-keyring", "dbus", "libsecret"),
            },
        ),
        "libsecret": PackageSpec(
            commands=(),
            packages={
                "apt-get": ("libsecret-1-0",),
                "dnf": ("libsecret",),
                "yum": ("libsecret",),
                "apk": ("libsecret",),
            },
            always_install=True,
        ),
        "secretstorage": PackageSpec(
            commands=(),
            packages={
                "apt-get": ("python3-secretstorage",),
                "dnf": ("python3-secretstorage",),
                "yum": ("python3-secretstorage",),
                "apk": ("py3-secretstorage",),
            },
            always_install=True,
        ),
    }

    @staticmethod
    def name() -> str:
        return "forkcode"

    def get_version_command(self) -> str:
        return f"{REMOTE_BIN} --version"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip().split()[-1] if stdout.strip() else ""

    async def _check(self, environment: BaseEnvironment, command: str) -> bool:
        return (await environment.exec(command=command, user="root")).return_code == 0

    async def install(self, environment: BaseEnvironment) -> None:
        binary = Path(_cfg("FORKCODE_BINARY", required=True)).expanduser()
        if not binary.is_file():
            raise FileNotFoundError(f"FORKCODE_BINARY={binary}: không có file binary Linux của agent")

        deps = ["python3", "keyring"]
        # Binary nạp libsecret qua FFI: có gnome-keyring-daemon chưa chắc đã có thư viện này.
        if not await self._check(environment, "ldconfig -p 2>/dev/null | grep -q libsecret-1.so || ls /usr/lib/*/libsecret-1.so.0 /usr/lib*/libsecret-1.so.0 >/dev/null 2>&1"):
            deps.append("libsecret")
        if not await self._check(environment, "/usr/bin/python3 -c 'import secretstorage' 2>/dev/null || python3 -c 'import secretstorage' 2>/dev/null"):
            deps.append("secretstorage")
        if not await self._check(environment, "[ -s /etc/ssl/certs/ca-certificates.crt ] || [ -s /etc/pki/tls/certs/ca-bundle.crt ] || [ -s /etc/ssl/cert.pem ]"):
            deps.append("ca_certificates")
        if not await self._check(environment, "command -v apt-get || command -v dnf || command -v yum || command -v apk"):
            raise RuntimeError("Image của task không có apt-get/dnf/yum/apk: không cài được keyring cho agent")
        await self.ensure_system_dependencies(environment, tuple(deps))
        # libdbus cần machine-id; image tối giản thường không có.
        await self.exec_as_root(
            environment,
            command="[ -s /etc/machine-id ] || dbus-uuidgen --ensure=/etc/machine-id; "
                    "mkdir -p /var/lib/dbus && { [ -s /var/lib/dbus/machine-id ] || cp /etc/machine-id /var/lib/dbus/machine-id; }; "
                    f"mkdir -p {shlex.quote(str(Path(REMOTE_BIN).parent.as_posix()))}",
        )
        await environment.upload_file(binary, REMOTE_BIN)
        await self.exec_as_root(environment, command=f"chmod 755 {REMOTE_BIN}")
        # Báo lỗi sớm khi binary sai kiến trúc / thiếu glibc (vd. image Alpine).
        await self.exec_as_agent(environment, command=f"{REMOTE_BIN} --version")

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / USAGE_NAME
        if not path.is_file():
            return
        try:
            usage = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return
        tokens = usage.get("tokens") or {}
        cache = tokens.get("cache") or {}
        context.n_input_tokens = (tokens.get("input") or 0) + (cache.get("read") or 0) + (cache.get("write") or 0)
        context.n_cache_tokens = cache.get("read") or 0
        context.n_output_tokens = (tokens.get("output") or 0) + (tokens.get("reasoning") or 0)
        if usage.get("cost") is not None:
            context.cost_usd = usage["cost"]

    def _stage_files(self, tmp: Path, instruction: str) -> None:
        """Ghi vào `tmp` mọi thứ run.sh cần (sẽ tải nguyên thư mục vào REMOTE_RUN)."""
        model, variant = parse_model(self.model_name, _cfg("FORKCODE_VARIANT"))
        data_dir = Path(_cfg("FORKCODE_DATA_DIR") or fc.default_data_dir()).expanduser()
        integration = model.partition("/")[0]
        db, cred = fc.find_credential(data_dir, integration)
        fc.snapshot_db(db, tmp / "app.db", cred["id"])
        secret = fc.read_secret(cred["id"], _cfg("FORKCODE_CREDENTIAL_FILE") or None)
        if _cfg("FORKCODE_SHARE_REFRESH") != "1":
            secret = fc.strip_refresh(secret)
        # Trạng thái cho _push_tokens(): token container đang giữ hết hạn lúc nào.
        self._token_state = {"data_dir": data_dir, "integration": integration, "pushed_exp": fc.token_expiry_ms(cred) or 0}
        servers, origins = mcp_config(self.mcp_servers)

        def write(name: str, text: str) -> None:
            (tmp / name).write_text(text, encoding="utf-8", newline="\n")

        write("secret.json", json.dumps({"id": cred["id"], "secret": secret}))
        write("instruction.md", instruction)
        write("mcp.json", json.dumps(servers))
        write("run.sh", LAUNCHER)
        shutil.copyfile(Path(fc.__file__), tmp / "forkcode_client.py")
        env = {
            "VC_BIN": REMOTE_BIN,
            "VC_BRAND": fc.brand(),
            "VC_ENV_PREFIX": fc.env_prefix(),
            "VC_MODEL": model,
            "VC_VARIANT": variant,
            "VC_PORT": _cfg("FORKCODE_PORT") or "4096",
            "VC_WORKDIR": _cfg("FORKCODE_WORKDIR"),
            "VC_TIMEOUT": _cfg("FORKCODE_TIMEOUT_SEC") or "0",
            "VC_RETRIES": _cfg("FORKCODE_TRANSIENT_RETRIES") or "3",
            "VC_USAGE": (self.environment_logs_dir / USAGE_NAME).as_posix(),
            "VC_MCP_ORIGINS": origins,
            "VC_SKILLS_DIR": self.skills_dir or "",
            "VC_SERVER_PASSWORD": secrets.token_urlsafe(24),
            "VC_KEYRING_PASSWORD": secrets.token_urlsafe(24),
        }
        write("run.env", "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        with tempfile.TemporaryDirectory(prefix="forkcode-") as tmp:
            self._stage_files(Path(tmp), instruction)
            await self.exec_as_root(environment, command=f"rm -rf {REMOTE_RUN} && mkdir -p {REMOTE_RUN}")
            await environment.upload_dir(tmp, REMOTE_RUN)
        self._owner = await self._agent_owner(environment)
        owner = f"chown -R {self._owner} {REMOTE_RUN} && " if self._owner else ""
        await self.exec_as_root(environment, command=f"{owner}chmod -R go-rwx {REMOTE_RUN}")
        log = (self.environment_logs_dir / LOG_NAME).as_posix()
        pusher = asyncio.create_task(self._push_tokens(environment)) if _cfg("FORKCODE_SHARE_REFRESH") != "1" else None
        try:
            await self.exec_as_agent(
                environment,
                command=f"bash {REMOTE_RUN}/run.sh {REMOTE_RUN} 2>&1 | tee {shlex.quote(log)}",
            )
        finally:
            if pusher:
                pusher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pusher

    async def _push_tokens(self, environment: BaseEnvironment) -> None:
        """Giữ access token trong container luôn còn hạn suốt trial.

        Access token sống vài giờ; container không có refresh token (server xoay vòng refresh
        token, để container làm mới sẽ đăng xuất agent trên host). Agent chỉ làm mới trong 60 giây
        cuối trước khi hết hạn, nên: chờ tới sát hạn, nhờ service của agent trên host làm mới,
        rồi ghi access token mới vào keyring trong container. Gặp 401, agent đọc lại keyring và
        dùng token mới nếu khác token vừa bị từ chối. Khi host đã có token mới hơn token của
        container (trial khác vừa làm mới) thì đẩy vào ngay.
        """
        st = self._token_state
        hhmm = lambda ms: time.strftime("%H:%M", time.gmtime(ms / 1000))  # noqa: E731
        self.logger.info(f"ForkCode: token trong container hết hạn {hhmm(st['pushed_exp'])} UTC; sẽ làm mới trước "
                         f"{REFRESH_MARGIN_SEC // 60} phút")
        while True:
            try:
                # Thức dậy mỗi phút: một lần ngủ dài nhiều giờ không đáng tin (có trial dài không hề
                # được đẩy token), và trial khác có thể đã làm mới token trên host sớm hơn.
                _, cred = await asyncio.wait_for(
                    asyncio.to_thread(fc.find_credential, st["data_dir"], st["integration"]), 60)
                host_exp = fc.token_expiry_ms(cred) or 0
                if host_exp <= st["pushed_exp"]:
                    if host_exp - time.time() * 1000 > REFRESH_MARGIN_SEC * 1000:
                        await asyncio.sleep(60)
                        continue
                    self.logger.info(f"ForkCode: token hết hạn {hhmm(host_exp)} UTC, nhờ service trên host làm mới")
                    ok = await asyncio.wait_for(asyncio.to_thread(fc.host_service_refresh), 150)
                    _, cred = await asyncio.wait_for(
                        asyncio.to_thread(fc.find_credential, st["data_dir"], st["integration"]), 60)
                    host_exp = fc.token_expiry_ms(cred) or 0
                    if host_exp <= st["pushed_exp"]:
                        self.logger.warning(f"ForkCode: host chưa có token mới (refresh={'ok' if ok else 'lỗi'}), thử lại sau 30 giây")
                        await asyncio.sleep(30)
                        continue
                secret = fc.strip_refresh(fc.read_secret(cred["id"], _cfg("FORKCODE_CREDENTIAL_FILE") or None))
                if await asyncio.wait_for(self._inject_secret(environment, cred["id"], secret), 180):
                    st["pushed_exp"] = host_exp
                    self.logger.info(f"ForkCode: đã đẩy access token mới vào container (hạn {hhmm(host_exp)} UTC)")
                else:
                    self.logger.warning("ForkCode: chưa đẩy được token vào container, thử lại sau 10 giây")
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # không để lỗi ở đây làm hỏng trial
                self.logger.warning(f"ForkCode: đẩy token mới vào container lỗi: {type(e).__name__}: {e}")
                await asyncio.sleep(30)

    async def _agent_owner(self, environment: BaseEnvironment) -> str:
        """`uid:gid` mà lệnh của agent chạy dưới đó, để chown thư mục chạy ("" nếu là root).

        task.toml không khai báo user thì default_user là None và lệnh chạy theo USER của image —
        vd. risk-scorer-replay có `USER nobody`: file do root tạo (go-rwx) thì agent không đọc được."""
        if environment.default_user is not None:
            return shlex.quote(str(environment.default_user))
        r = await self.exec_as_agent(environment, command="echo $(id -u):$(id -g)")
        ids = (r.stdout or "").strip().splitlines()[-1:] or [""]
        uid_gid = ids[0].strip()
        if not re.fullmatch(r"\d+:\d+", uid_gid) or uid_gid.startswith("0:"):
            return ""
        return uid_gid

    async def _inject_secret(self, environment: BaseEnvironment, cred_id: str, secret: str) -> bool:
        """Ghi secret vào keyring đang chạy trong container (bus.env do run.sh ghi sau khi dựng dbus)."""
        if (await environment.exec(command=f"test -f {REMOTE_RUN}/bus.env")).return_code != 0:
            return False  # run.sh chưa dựng xong keyring (hoặc đã dọn)
        with tempfile.TemporaryDirectory(prefix="forkcode-") as tmp:
            f = Path(tmp) / "secret-push.json"
            f.write_text(json.dumps({"id": cred_id, "secret": secret}), encoding="utf-8")
            await environment.upload_file(f, PUSH_FILE)
        if self._owner:
            await environment.exec(command=f"chown {self._owner} {PUSH_FILE} && chmod 600 {PUSH_FILE}", user="root")
        r = await environment.exec(
            command=f'. {REMOTE_RUN}/bus.env && "$VC_PY" {REMOTE_RUN}/forkcode_client.py inject --secret-file {PUSH_FILE}',
        )
        return r.return_code == 0
