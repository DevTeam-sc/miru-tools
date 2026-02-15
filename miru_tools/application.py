import argparse
import codecs
import errno
import numbers
import os
import platform
import re
import select
import shlex
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any, Callable, List, Mapping, Optional, Tuple, TypeVar, Union

if platform.system() == "Windows":
    import msvcrt

import colorama
import miru
import miru._miru as _miru

from miru_tools.config import read_setting, write_setting
from miru_tools.reactor import Reactor

AUX_OPTION_PATTERN = re.compile(r"(.+)=\((string|bool|int)\)(.+)")

T = TypeVar("T")
TargetType = Union[List[str], re.Pattern, int, str]
TargetTypeTuple = Tuple[str, TargetType]

UI_LANG_FLAGS = {
    "--TH": "th",
    "--EN": "en",
    "--AUTO": "auto",
}
UI_LANG_VALUES = {"en", "th", "both", "auto"}


def _get_prog_name() -> str:
    name = Path(sys.argv[0] or "miru").stem
    return name or "miru"


def _strip_ui_lang_flags(args: List[str]) -> Tuple[List[str], Optional[str]]:
    requested: Optional[str] = None
    remaining: List[str] = []

    for arg in args:
        key = arg.upper()
        if key in UI_LANG_FLAGS:
            requested = UI_LANG_FLAGS[key]
        else:
            remaining.append(arg)

    return remaining, requested


def _get_terminal_columns(default: int = 120) -> int:
    try:
        columns = shutil.get_terminal_size(fallback=(default, 25)).columns
    except Exception:
        columns = default
    return max(60, int(columns))


class MiruHelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str):
        width = _get_terminal_columns()
        max_help_position = min(44, max(24, (width // 2) - 4))
        super().__init__(prog, max_help_position=max_help_position, width=width)


def _sanitize_user_message(message: str) -> str:
    # Branding-only sanitization for user-facing output; keep legacy/internal
    # identifiers intact as much as possible.
    sanitized = message
    sanitized = re.sub(r"\bfrida-server\b", "miru-server", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"(?<![A-Z0-9_])Frida(?![A-Z0-9_])", "Miru", sanitized)
    sanitized = re.sub(r"(?<![A-Z0-9_])frida(?![A-Z0-9_])", "miru", sanitized)
    return sanitized


def input_with_cancellable(cancellable: miru.Cancellable) -> str:
    if platform.system() == "Windows":
        result = ""
        done = False

        while not done:
            while msvcrt.kbhit():
                c = msvcrt.getwche()
                if c in ("\x00", "\xe0"):
                    msvcrt.getwche()
                    continue

                result += c

                if c == "\n":
                    done = True
                    break

            cancellable.raise_if_cancelled()
            time.sleep(0.05)

        return result
    elif platform.system() in ["Darwin", "FreeBSD"]:
        while True:
            try:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
            except OSError as e:
                if e.args[0] != errno.EINTR:
                    raise e

            cancellable.raise_if_cancelled()

            if sys.stdin in rlist:
                return sys.stdin.readline()
    else:
        with cancellable.get_pollfd() as cancellable_fd:
            try:
                rlist, _, _ = select.select([sys.stdin, cancellable_fd], [], [])
            except OSError as e:
                if e.args[0] != errno.EINTR:
                    raise e

        cancellable.raise_if_cancelled()

        return sys.stdin.readline()


def await_enter(reactor: Reactor) -> None:
    try:
        input_with_cancellable(reactor.ui_cancellable)
    except miru.OperationCancelledError:
        pass
    except KeyboardInterrupt:
        print("")


def await_ctrl_c(reactor: Reactor) -> None:
    while True:
        try:
            input_with_cancellable(reactor.ui_cancellable)
        except miru.OperationCancelledError:
            break
        except KeyboardInterrupt:
            break


def deserialize_relay(value: str) -> miru.Relay:
    address, username, password, kind = value.split(",")
    return miru.Relay(address, username, password, kind)


def create_target_parser(target_type: str) -> Callable[[str], TargetTypeTuple]:
    def parse_target(value: str) -> TargetTypeTuple:
        if target_type == "file":
            return (target_type, [value])
        if target_type == "gated":
            return (target_type, re.compile(value))
        if target_type == "pid":
            return (target_type, int(value))
        return (target_type, value)

    return parse_target


class ConsoleState:
    EMPTY = 1
    STATUS = 2
    TEXT = 3


class ConsoleApplication:
    """
    ConsoleApplication is the base class for all of Miru tools, which contains
    the common arguments of the tools. Each application can implement one or
    more of several methods that can be inserted inside the flow of the
    application.

    The subclass should not expose any additional methods aside from __init__
    and run methods that are defined by this class. These methods should not be
    overridden without calling the super method.
    """

    _target: Optional[TargetTypeTuple] = None

    def __init__(
        self,
        run_until_return: Callable[["Reactor"], None] = await_enter,
        on_stop: Optional[Callable[[], None]] = None,
        args: Optional[List[str]] = None,
    ):
        plain_terminal = os.environ.get("TERM", "").lower() == "none"
        raw_args = sys.argv[1:] if args is None else list(args)
        filtered_args, requested_ui_lang = _strip_ui_lang_flags(raw_args)

        env_ui_lang = os.environ.get("MIRU_UI_LANG")
        if requested_ui_lang is not None:
            write_setting("ui_lang", requested_ui_lang)
            if env_ui_lang is None:
                os.environ["MIRU_UI_LANG"] = requested_ui_lang

            if len(filtered_args) == 0:
                prog = _get_prog_name()
                print(f"{prog}: UI language saved: {requested_ui_lang}")
                raise SystemExit(0)
        elif env_ui_lang is None:
            persisted = read_setting("ui_lang")
            persisted_norm = persisted.strip().lower() if persisted else None
            if persisted_norm in UI_LANG_VALUES:
                os.environ["MIRU_UI_LANG"] = persisted_norm
            else:
                os.environ["MIRU_UI_LANG"] = "auto"

        self._ui_lang = self._select_ui_language(os.environ.get("MIRU_UI_LANG"))
        self._enable_unicode_output()

        # Windows doesn't have SIGPIPE
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)

        # If true, emit text without colors.  https://no-color.org/
        no_color = plain_terminal or bool(os.environ.get("NO_COLOR"))

        colorama.init(strip=True if no_color else None)

        parser = self._initialize_arguments_parser()
        real_args = compute_real_args(parser, args=filtered_args)
        options = parser.parse_args(real_args)

        # handle scripts that don't need a target
        if not hasattr(options, "args"):
            options.args = []

        self._initialize_device_arguments(parser, options)
        self._initialize_target_arguments(parser, options)

        self._reactor = Reactor(run_until_return, on_stop)
        self._device: Optional[miru.core.Device] = None
        self._schedule_on_output = lambda pid, fd, data: self._reactor.schedule(lambda: self._on_output(pid, fd, data))
        self._schedule_on_device_lost = lambda: self._reactor.schedule(self._on_device_lost)
        self._spawned_pid: Optional[int] = None
        self._spawned_argv = None
        self._selected_spawn: Optional[_miru.Spawn] = None
        self._target_pid: Optional[int] = None
        self._session: Optional[miru.core.Session] = None
        self._schedule_on_session_detached = lambda reason, crash: self._reactor.schedule(
            lambda: self._on_session_detached(reason, crash)
        )
        self._started = False
        self._resumed = False
        self._exit_status: Optional[int] = None
        self._console_state = ConsoleState.EMPTY
        self._have_terminal = sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("TERM", "") == "dumb"
        self._plain_terminal = plain_terminal
        self._quiet = False
        self._java_bridge_in_progress = False
        self._java_bridge_last_done_at = 0.0
        self._auto_install_attempted = False
        if sum(map(lambda v: int(v is not None), (self._device_id, self._device_type, self._host))) > 1:
            parser.error(
                self._ui(
                    "Only one of -D, -U, -R, and -H may be specified",
                    "ระบุได้อย่างมาก 1 ตัวเลือกจาก -D, -U, -R และ -H เท่านั้น",
                )
            )

        self._initialize_target(parser, options)

        try:
            self._initialize(parser, options, options.args)
        except Exception as e:
            parser.error(str(e))

    def _select_ui_language(self, raw_value: Optional[str]) -> str:
        """
        User-surface language selector for CLI output.

        Supported values:
          - MIRU_UI_LANG=en   (English)
          - MIRU_UI_LANG=th   (Thai)
          - MIRU_UI_LANG=both (English + Thai)
          - MIRU_UI_LANG=auto (Thai-first default)
        """

        value = (raw_value or "").strip().lower()

        # Thai-first defaults (as requested).
        if value in ("", "default", "auto"):
            return "th"

        if value in ("en", "english", "en-us", "en_us"):
            return "en"
        if value in ("th", "thai", "th-th", "th_th"):
            return "th"
        if value in ("both", "bilingual", "two", "2", "en+th", "th+en"):
            return "both"

        return "th"

    def _ui(self, en: str, th: str) -> str:
        lang = getattr(self, "_ui_lang", "th")
        if lang == "both":
            return f"{en} / {th}"
        if lang == "en":
            return en
        return th

    def _ui_block(self, en: str, th: str) -> str:
        lang = getattr(self, "_ui_lang", "th")
        if lang == "both":
            return en.rstrip("\n") + "\n\n" + th.lstrip("\n")
        if lang == "en":
            return en
        return th

    def _enable_unicode_output(self) -> None:
        if platform.system() != "Windows":
            return
        if getattr(self, "_ui_lang", "en") == "en":
            return

        enc = (sys.stdout.encoding or "").lower()
        if enc in ("utf-8", "utf8", "cp65001", "cp874"):
            return

        if sys.stdout.isatty() and sys.stderr.isatty():
            try:
                os.system("chcp 65001 >NUL")
            except Exception:
                pass

        for stream in (sys.stdout, sys.stderr):
            try:
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                try:
                    if hasattr(stream, "reconfigure"):
                        stream.reconfigure(errors="replace")
                except Exception:
                    pass

    def _stdout_supports_unicode(self) -> bool:
        enc = (sys.stdout.encoding or "").lower()
        return enc in ("utf-8", "utf8", "cp65001")

    def _encode_for_stdout(self, text: str) -> str:
        if self._stdout_supports_unicode():
            return text

        encoding = sys.stdout.encoding or "UTF-8"
        try:
            return text.encode(encoding, errors="backslashreplace").decode(encoding)
        except LookupError:
            return text.encode("utf-8", errors="backslashreplace").decode("utf-8")

    def _initialize_device_arguments(self, parser: argparse.ArgumentParser, options: argparse.Namespace) -> None:
        if self._needs_device():
            self._device_id = options.device_id
            self._device_type = options.device_type
            self._host = options.host
            if all([x is None for x in [self._device_id, self._device_type, self._host]]):
                self._device_id = os.environ.get("MIRU_DEVICE")
                if self._device_id is None:
                    self._host = os.environ.get("MIRU_HOST")
            self._certificate = options.certificate or os.environ.get("MIRU_CERTIFICATE")
            self._origin = options.origin or os.environ.get("MIRU_ORIGIN")
            self._token = options.token or os.environ.get("MIRU_TOKEN")
            self._keepalive_interval = options.keepalive_interval
            self._session_transport = options.session_transport
            self._stun_server = options.stun_server
            self._relays = options.relays
        else:
            self._device_id = None
            self._device_type = None
            self._host = None
            self._certificate = None
            self._origin = None
            self._token = None
            self._keepalive_interval = None
            self._session_transport = "multiplexed"
            self._stun_server = None
            self._relays = None

    def _initialize_target_arguments(self, parser: argparse.ArgumentParser, options: argparse.Namespace) -> None:
        if self._needs_target():
            self._stdio = options.stdio
            self._aux = options.aux
            self._realm = options.realm
            self._runtime = options.runtime
            self._enable_debugger = options.enable_debugger
            self._squelch_crash = options.squelch_crash
        else:
            self._stdio = "inherit"
            self._aux = []
            self._realm = "native"
            self._runtime = "qjs"
            self._enable_debugger = False
            self._squelch_crash = False

    def _initialize_target(self, parser: argparse.ArgumentParser, options: argparse.Namespace) -> None:
        if self._needs_target():
            target = getattr(options, "target", None)
            if target is None:
                if len(options.args) < 1:
                    parser.error(self._ui("target must be specified", "ต้องระบุเป้าหมาย (target)"))
                target = infer_target(options.args[0])
                options.args.pop(0)
            target = expand_target(target)
            if target[0] == "file":
                if not isinstance(target[1], list):
                    raise ValueError("file target must be a list of strings")
                argv = target[1]
                argv.extend(options.args)
                options.args = []
            self._target = target
        else:
            self._target = None

    def _initialize_arguments_parser(self) -> argparse.ArgumentParser:
        parser = self._initialize_base_arguments_parser()
        self._add_options(parser)
        return parser

    def _initialize_base_arguments_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(usage=self._usage(), add_help=False, formatter_class=MiruHelpFormatter)

        if self._needs_device():
            self._add_device_arguments(parser)

        if self._needs_target():
            self._add_target_arguments(parser)

        parser.add_argument(
            "-h",
            "--help",
            action="help",
            help=self._ui("show this help message and exit", "แสดงข้อความช่วยเหลือนี้แล้วออก"),
        )
        parser.add_argument(
            "-O",
            "--options-file",
            help=self._ui(
                "text file containing additional command line options",
                "ไฟล์ข้อความที่มีตัวเลือกเพิ่มเติมของบรรทัดคำสั่ง",
            ),
            metavar="FILE",
        )
        parser.add_argument(
            "--version",
            action="version",
            version=miru.__version__,
            help=self._ui("show program's version number and exit", "แสดงเวอร์ชันของโปรแกรมแล้วออก"),
        )

        return parser

    def _add_device_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-D",
            "--device",
            help=self._ui("connect to device with the given ID", "เชื่อมต่อกับอุปกรณ์ตาม ID ที่ระบุ"),
            metavar="ID",
            dest="device_id",
        )
        parser.add_argument(
            "-U",
            "--usb",
            help=self._ui("connect to USB device", "เชื่อมต่อกับอุปกรณ์ผ่าน USB"),
            action="store_const",
            const="usb",
            dest="device_type",
        )
        parser.add_argument(
            "-R",
            "--remote",
            help=self._ui("connect to remote miru-server", "เชื่อมต่อกับ miru-server ระยะไกล"),
            action="store_const",
            const="remote",
            dest="device_type",
        )
        parser.add_argument(
            "-H",
            "--host",
            help=self._ui("connect to remote miru-server on HOST", "เชื่อมต่อกับ miru-server ระยะไกลที่ HOST"),
        )
        parser.add_argument(
            "--certificate",
            help=self._ui("speak TLS with HOST, expecting CERTIFICATE", "ใช้ TLS กับ HOST โดยคาดหวัง CERTIFICATE"),
        )
        parser.add_argument(
            "--origin",
            help=self._ui(
                "connect to remote server with 'Origin' header set to ORIGIN",
                "เชื่อมต่อกับเซิร์ฟเวอร์ระยะไกลโดยตั้งค่าเฮดเดอร์ Origin เป็น ORIGIN",
            ),
        )
        parser.add_argument(
            "--token", help=self._ui("authenticate with HOST using TOKEN", "ยืนยันตัวตนกับ HOST ด้วย TOKEN")
        )
        parser.add_argument(
            "--keepalive-interval",
            help=self._ui(
                "set keepalive interval in seconds, or 0 to disable (defaults to -1 to auto-select based on transport)",
                "ตั้งค่า keepalive เป็นวินาที หรือ 0 เพื่อปิด (ค่าเริ่มต้น -1 ให้เลือกอัตโนมัติตาม transport)",
            ),
            metavar="INTERVAL",
            type=int,
        )
        parser.add_argument(
            "--p2p",
            help=self._ui("establish a peer-to-peer connection with target", "เชื่อมต่อแบบ peer-to-peer กับเป้าหมาย"),
            action="store_const",
            const="p2p",
            dest="session_transport",
            default="multiplexed",
        )
        parser.add_argument(
            "--stun-server",
            help=self._ui(
                "set STUN server ADDRESS to use with --p2p", "ตั้งค่า STUN server เป็น ADDRESS สำหรับใช้กับ --p2p"
            ),
            metavar="ADDRESS",
        )
        parser.add_argument(
            "--relay",
            help=self._ui("add relay to use with --p2p", "เพิ่ม relay สำหรับใช้กับ --p2p"),
            metavar="address,username,password,turn-{udp,tcp,tls}",
            dest="relays",
            action="append",
            type=deserialize_relay,
        )

    def _add_target_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-f",
            "--file",
            help=self._ui("spawn FILE", "สั่ง spawn FILE"),
            dest="target",
            type=create_target_parser("file"),
        )
        parser.add_argument(
            "-F",
            "--attach-frontmost",
            help=self._ui("attach to frontmost application", "แนบ (attach) กับแอพที่อยู่หน้าสุด"),
            dest="target",
            action="store_const",
            const=("frontmost", None),
        )
        parser.add_argument(
            "-n",
            "--attach-name",
            help=self._ui("attach to NAME", "แนบ (attach) กับ NAME"),
            metavar="NAME",
            dest="target",
            type=create_target_parser("name"),
        )
        parser.add_argument(
            "-N",
            "--attach-identifier",
            help=self._ui("attach to IDENTIFIER", "แนบ (attach) กับ IDENTIFIER"),
            metavar="IDENTIFIER",
            dest="target",
            type=create_target_parser("identifier"),
        )
        parser.add_argument(
            "-p",
            "--attach-pid",
            help=self._ui("attach to PID", "แนบ (attach) กับ PID"),
            metavar="PID",
            dest="target",
            type=create_target_parser("pid"),
        )
        parser.add_argument(
            "-W",
            "--await",
            help=self._ui("await spawn matching PATTERN", "รอ spawn ที่ตรงกับ PATTERN"),
            metavar="PATTERN",
            dest="target",
            type=create_target_parser("gated"),
        )
        parser.add_argument(
            "--stdio",
            help=self._ui(
                "stdio behavior when spawning (defaults to 'inherit')",
                "พฤติกรรม stdio ตอน spawn (ค่าเริ่มต้น 'inherit')",
            ),
            choices=["inherit", "pipe"],
            default="inherit",
        )
        parser.add_argument(
            "--aux",
            help=self._ui(
                "set aux option when spawning, such as 'uid=(int)42' (supported types are: string, bool, int)",
                "ตั้งค่า aux option ตอน spawn เช่น 'uid=(int)42' (ชนิดที่รองรับ: string, bool, int)",
            ),
            metavar="option",
            action="append",
            dest="aux",
            default=[],
        )
        parser.add_argument(
            "--realm",
            help=self._ui("realm to attach in", "realm ที่จะ attach"),
            choices=["native", "emulated"],
            default="native",
        )
        parser.add_argument(
            "--runtime", help=self._ui("script runtime to use", "runtime ของสคริปต์ที่จะใช้"), choices=["qjs", "v8"]
        )
        parser.add_argument(
            "--debug",
            help=self._ui(
                "enable the Node.js compatible script debugger", "เปิด debugger ของสคริปต์ที่เข้ากันได้กับ Node.js"
            ),
            action="store_true",
            dest="enable_debugger",
            default=False,
        )
        parser.add_argument(
            "--squelch-crash",
            help=self._ui(
                "if enabled, will not dump crash report to console", "ถ้าเปิด จะไม่พิมพ์รายงาน crash ลงคอนโซล"
            ),
            action="store_true",
            default=False,
        )
        parser.add_argument(
            "args", help=self._ui("extra arguments and/or target", "อาร์กิวเมนต์เพิ่มเติม และ/หรือ เป้าหมาย"), nargs="*"
        )

    def run(self) -> None:
        if self._needs_device():
            mgr = miru.get_device_manager()

            on_devices_changed = lambda: self._reactor.schedule(self._try_start)
            mgr.on("changed", on_devices_changed)

            self._reactor.schedule(self._try_start)
            self._reactor.schedule(self._show_message_if_no_device, delay=1)
        else:
            mgr = None
            self._reactor.schedule(self._transition_to_started)

        signal.signal(signal.SIGTERM, self._on_sigterm)

        self._reactor.run()

        if self._started:
            try:
                self._perform_on_background_thread(self._stop)
            except miru.OperationCancelledError:
                pass

        if self._session is not None:
            self._session.off("detached", self._schedule_on_session_detached)
            try:
                self._perform_on_background_thread(self._session.detach)
            except miru.OperationCancelledError:
                pass
            self._session = None

        if self._device is not None:
            self._device.off("output", self._schedule_on_output)
            self._device.off("lost", self._schedule_on_device_lost)

        if mgr is not None:
            mgr.off("changed", on_devices_changed)

        miru.shutdown()
        sys.exit(self._exit_status)

    def _respawn(self) -> None:
        self._session.off("detached", self._schedule_on_session_detached)
        self._stop()
        self._session = None

        self._device.kill(self._spawned_pid)
        self._spawned_pid = None
        self._spawned_argv = None
        self._resumed = False

        self._attach_and_instrument()
        self._resume()

    def _add_options(self, parser: argparse.ArgumentParser) -> None:
        """
        override this method if you want to add custom arguments to your
        command. The parser command is an argparse object, you should add the
        options to him.
        """

    def _initialize(self, parser: argparse.ArgumentParser, options: argparse.Namespace, args: List[str]) -> None:
        """
        override this method if you need to have additional initialization code
        before running, maybe to use your custom options from the `_add_options`
        method.
        """

    def _usage(self) -> str:
        """
        override this method if to add a custom usage message
        """

        return self._ui("%(prog)s [options]", "%(prog)s [ตัวเลือก]")

    def _needs_device(self) -> bool:
        """
        override this method if your command need to get a device from the user.
        """

        return True

    def _needs_target(self) -> bool:
        """
        override this method if your command does not need to get a target
        process from the user.
        """

        return False

    def _start(self) -> None:
        """
        override this method with the logic of your command, it will run after
        the class is fully initialized with a connected device/target if you
        required one.
        """

    def _stop(self) -> None:
        """
        override this method if you have something you need to do at the end of
        your command, maybe cleaning up some objects.
        """

    def _resume(self) -> None:
        if self._resumed:
            return
        if self._spawned_pid is not None:
            assert self._device is not None
            self._device.resume(self._spawned_pid)
            assert self._target is not None
            if self._target[0] == "gated":
                self._device.disable_spawn_gating()
                self._device.off("spawn-added", self._on_spawn_added)
        self._resumed = True

    def _exit(self, exit_status: int) -> None:
        self._exit_status = exit_status
        self._reactor.stop()

    def _auto_install_server_enabled(self) -> bool:
        value = os.environ.get("MIRU_AUTO_INSTALL_SERVER", "1").strip().lower()
        return value not in ("0", "false", "no", "off")

    def _get_server_port(self) -> int:
        raw = (os.environ.get("MIRU_SERVER_PORT") or "27042").strip()
        try:
            port = int(raw)
        except ValueError as e:
            raise ValueError("MIRU_SERVER_PORT must be an integer") from e
        if not (1 <= port <= 65535):
            raise ValueError("MIRU_SERVER_PORT must be between 1 and 65535")
        return port

    def _looks_like_server_not_running_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        if "unable to connect" in msg and "server" in msg:
            return True
        if "server not running" in msg:
            return True
        if "connection refused" in msg or "connection closed" in msg:
            return True
        return False

    def _maybe_auto_install_android_server(self, error: Optional[Exception] = None) -> bool:
        if self._device_type != "usb":
            return False
        if not self._auto_install_server_enabled():
            return False
        if getattr(self, "_auto_install_attempted", False):
            return False
        if error is not None and not self._looks_like_server_not_running_error(error):
            return False

        self._auto_install_attempted = True

        try:
            port = self._get_server_port()
        except Exception as e:
            self._update_status(
                self._ui(
                    f"Invalid MIRU_SERVER_PORT: {e}",
                    f"MIRU_SERVER_PORT ไม่ถูกต้อง: {e}",
                )
            )
            self._exit(1)
            return False

        try:
            from miru_tools.android import ensure_android_server_running_via_adb

            self._update_status(
                self._ui(
                    "Installing miru-server to Android device via adb...",
                    "กำลังติดตั้ง miru-server ลง Android ผ่าน adb...",
                )
            )
            ensure_android_server_running_via_adb(
                version=miru.__version__,
                port=port,
                status_cb=self._update_status,
            )
        except Exception as e:
            if "No Android device detected via adb" in str(e):
                return False
            self._update_status(
                self._ui(
                    f"Auto-install failed: {e}",
                    f"ติดตั้งอัตโนมัติล้มเหลว: {e}",
                )
            )
            self._exit(1)
            return False

        if self._device is None:
            for delay in (0.0, 0.2, 0.5):
                if delay:
                    time.sleep(delay)
                dev = find_device("usb")
                if dev is not None:
                    self._device = dev
                    break

        if self._device is not None:
            for delay in (0.0, 0.2, 0.5):
                try:
                    self._device.enumerate_processes(scope="minimal")
                    break
                except Exception:
                    time.sleep(delay)

        return True

    def _try_start(self) -> None:
        if self._device is not None:
            return
        if self._device_id is not None:
            try:
                self._device = miru.get_device(self._device_id)
            except:
                self._update_status(f"Device '{self._device_id}' not found")
                self._exit(1)
                return
        elif (self._host is not None) or (self._device_type == "remote"):
            host = self._host

            options = {}
            if self._certificate is not None:
                options["certificate"] = self._certificate
            if self._origin is not None:
                options["origin"] = self._origin
            if self._token is not None:
                options["token"] = self._token
            if self._keepalive_interval is not None:
                options["keepalive_interval"] = self._keepalive_interval

            if host is None and len(options) == 0:
                self._device = miru.get_remote_device()
            else:
                self._device = miru.get_device_manager().add_remote_device(
                    host if host is not None else "127.0.0.1", **options
                )
        elif self._device_type is not None:
            self._device = find_device(self._device_type)
            if self._device is None:
                if self._device_type == "usb":
                    self._maybe_auto_install_android_server()
                return
        else:
            self._device = miru.get_local_device()

        if self._device_type == "usb" and self._device is not None and not self._auto_install_attempted:
            try:
                self._device.enumerate_processes(scope="minimal")
            except Exception as e:
                self._maybe_auto_install_android_server(e)
                if self._device is None:
                    return
        self._on_device_found()
        self._device.on("output", self._schedule_on_output)
        self._device.on("lost", self._schedule_on_device_lost)
        self._attach_and_instrument()

    def _attach_and_instrument(self) -> None:
        if self._target is not None:
            target_type, target_value = self._target

            if target_type == "gated":
                self._device.on("spawn-added", self._on_spawn_added)
                try:
                    self._device.enable_spawn_gating()
                except Exception as e:
                    if self._maybe_auto_install_android_server(e):
                        self._attach_and_instrument()
                        return
                    self._update_status(
                        self._ui(f"Failed to enable spawn gating: {e}", f"เปิด spawn gating ไม่สำเร็จ: {e}")
                    )
                    self._exit(1)
                    return
                self._update_status(self._ui("Waiting for spawn to appear...", "กำลังรอ spawn ให้ปรากฏ..."))
                return

            spawning = True
            try:
                if target_type == "frontmost":
                    try:
                        app = self._device.get_frontmost_application()
                    except Exception as e:
                        self._update_status(
                            self._ui(
                                f"Unable to get frontmost application on {self._device.name}: {e}",
                                f"ไม่สามารถดึงแอพที่อยู่หน้าสุดบน {self._device.name}: {e}",
                            )
                        )
                        self._exit(1)
                        return
                    if app is None:
                        self._update_status(
                            self._ui(
                                f"No frontmost application on {self._device.name}",
                                f"ไม่พบแอพที่อยู่หน้าสุดบน {self._device.name}",
                            )
                        )
                        self._exit(1)
                        return
                    self._target = ("name", app.name)
                    attach_target = app.pid
                elif target_type == "identifier":
                    spawning = False
                    app_list = self._device.enumerate_applications()
                    app_identifier_lc = target_value.lower()
                    matching = [app for app in app_list if app.identifier.lower() == app_identifier_lc]
                    if len(matching) == 1 and matching[0].pid != 0:
                        attach_target = matching[0].pid
                    elif len(matching) > 1:
                        raise miru.ProcessNotFoundError(
                            "ambiguous identifier; it matches: %s"
                            % ", ".join([f"{process.identifier} (pid: {process.pid})" for process in matching])
                        )
                    else:
                        raise miru.ProcessNotFoundError("unable to find process with identifier '%s'" % target_value)
                elif target_type == "file":
                    argv = target_value
                    if not self._quiet:
                        self._update_status(
                            self._ui(f"Launching `{' '.join(argv)}`...", f"กำลังเรียกใช้ `{' '.join(argv)}`...")
                        )

                    aux_kwargs = {}
                    if self._aux is not None:
                        aux_kwargs = dict([parse_aux_option(o) for o in self._aux])

                    self._spawned_pid = self._device.spawn(argv, stdio=self._stdio, **aux_kwargs)
                    self._spawned_argv = argv
                    attach_target = self._spawned_pid
                else:
                    attach_target = target_value
                    if not isinstance(attach_target, numbers.Number):
                        attach_target = self._device.get_process(attach_target).pid
                    if not self._quiet:
                        self._update_status(self._ui("Attaching...", "กำลังแนบ (attach)..."))
                spawning = False
                self._attach(attach_target)
            except miru.OperationCancelledError:
                self._exit(0)
                return
            except Exception as e:
                if self._maybe_auto_install_android_server(e):
                    self._attach_and_instrument()
                    return
                if spawning:
                    self._update_status(self._ui(f"Failed to spawn: {e}", f"เริ่มโปรเซสไม่สำเร็จ: {e}"))
                else:
                    self._update_status(self._ui(f"Failed to attach: {e}", f"แนบ (attach) ไม่สำเร็จ: {e}"))
                self._exit(1)
                return
        self._transition_to_started()

    def _transition_to_started(self) -> None:
        self._start()
        self._started = True

    def _pick_worker_pid(self) -> int:
        try:
            frontmost = self._device.get_frontmost_application()
            if frontmost is not None and frontmost.identifier == "re.miru.Gadget":
                return frontmost.pid
        except:
            pass
        return 0

    def _attach(self, pid: int) -> None:
        self._target_pid = pid

        assert self._device is not None
        self._session = self._device.attach(pid, realm=self._realm)
        self._session.on("detached", self._schedule_on_session_detached)

        if self._session_transport == "p2p":
            peer_options = {}
            if self._stun_server is not None:
                peer_options["stun_server"] = self._stun_server
            if self._relays is not None:
                peer_options["relays"] = self._relays
            self._session.setup_peer_connection(**peer_options)

    def _on_script_created(self, script: miru.core.Script) -> None:
        if self._enable_debugger:
            script.enable_debugger()
            self._print("Chrome Inspector server listening on port 9229\n")

    def _show_message_if_no_device(self) -> None:
        if self._device is None:
            self._print("Waiting for USB device to appear...")

    def _on_sigterm(self, n: int, f: Optional[FrameType]) -> None:
        self._reactor.cancel_io()
        self._exit(0)

    def _on_spawn_added(self, spawn: _miru.Spawn) -> None:
        thread = threading.Thread(target=self._handle_spawn, args=(spawn,))
        thread.start()

    def _handle_spawn(self, spawn: _miru.Spawn) -> None:
        pid = spawn.pid

        pattern = self._target[1]
        if pattern.match(spawn.identifier) is None or self._selected_spawn is not None:
            self._print(
                colorama.Fore.YELLOW + colorama.Style.BRIGHT + "Ignoring: " + str(spawn) + colorama.Style.RESET_ALL
            )
            try:
                if self._device is not None:
                    self._device.resume(pid)
            except:
                pass
            return

        self._selected_spawn = spawn

        self._print(colorama.Fore.GREEN + colorama.Style.BRIGHT + "Handling: " + str(spawn) + colorama.Style.RESET_ALL)
        try:
            self._attach(pid)
            self._reactor.schedule(lambda: self._on_spawn_handled(spawn))
        except Exception as e:
            error = e
            self._reactor.schedule(lambda: self._on_spawn_unhandled(spawn, error))

    def _on_spawn_handled(self, spawn: _miru.Spawn) -> None:
        self._spawned_pid = spawn.pid
        self._start()
        self._started = True

    def _on_spawn_unhandled(self, spawn: _miru.Spawn, error: Exception) -> None:
        self._update_status(f"Failed to handle spawn: {error}")
        self._exit(1)

    def _on_output(self, pid: int, fd: int, data: Optional[bytes]) -> None:
        if pid != self._target_pid or data is None:
            return
        if fd == 1:
            prefix = "stdout> "
            stream = sys.stdout
        else:
            prefix = "stderr> "
            stream = sys.stderr
        encoding = stream.encoding or "UTF-8"
        text = data.decode(encoding, errors="replace")
        if text.endswith("\n"):
            text = text[:-1]
        lines = text.split("\n")
        self._print(prefix + ("\n" + prefix).join(lines))

    def _on_device_found(self) -> None:
        pass

    def _on_device_lost(self) -> None:
        if self._exit_status is not None:
            return
        self._print(self._ui("Device disconnected.", "อุปกรณ์ตัดการเชื่อมต่อแล้ว"))
        self._exit(1)

    def _on_session_detached(self, reason: str, crash) -> None:
        if crash is None:
            message = reason[0].upper() + reason[1:].replace("-", " ")
        else:
            message = "Process crashed: " + crash.summary
        self._print(colorama.Fore.RED + colorama.Style.BRIGHT + message + colorama.Style.RESET_ALL)
        if crash is not None:
            if self._squelch_crash is True:
                self._print("\n*** Crash report was squelched due to user setting. ***")
            else:
                self._print("\n***\n{}\n***".format(crash.report.rstrip("\n")))
        self._exit(1)

    def _clear_status(self) -> None:
        if self._console_state == ConsoleState.STATUS:
            print(colorama.Cursor.UP() + (80 * " "))

    def _update_status(self, message: str) -> None:
        message = _sanitize_user_message(message)
        message = self._encode_for_stdout(message)
        if self._have_terminal:
            if self._console_state == ConsoleState.STATUS:
                cursor_position = colorama.Cursor.UP()
            else:
                cursor_position = ""
            print("%-80s" % (cursor_position + colorama.Style.BRIGHT + message + colorama.Style.RESET_ALL,))
            self._console_state = ConsoleState.STATUS
        else:
            print(colorama.Style.BRIGHT + message + colorama.Style.RESET_ALL)

    def _print(self, *args: Any, **kwargs: Any) -> None:
        encoded_args: List[Any] = []
        encoding = sys.stdout.encoding or "UTF-8"
        if encoding == "UTF-8":
            encoded_args = list(args)
        else:
            for arg in args:
                if isinstance(arg, str):
                    encoded_args.append(arg.encode(encoding, errors="backslashreplace").decode(encoding))
                else:
                    encoded_args.append(arg)
        print(*encoded_args, **kwargs)
        self._console_state = ConsoleState.TEXT

    def _handle_java_bridge_log(self, level: str, text: str) -> bool:
        del level
        if not text.startswith("Miru Java Bridge:"):
            return False

        now = time.time()
        if (not self._java_bridge_in_progress) and (now - self._java_bridge_last_done_at) < 1.0:
            return True

        fancy = self._have_terminal and not self._plain_terminal

        if text == "Miru Java Bridge: Initializing...":
            self._java_bridge_in_progress = True
            if fancy:
                self._update_status("Java bridge: initializing (1/3)")
            else:
                self._print("Java bridge: initializing")
            return True

        if text == "Miru Java Bridge: Assigned globalThis.Java":
            self._java_bridge_in_progress = True
            if fancy:
                self._update_status("Java bridge: exporting Java (2/3)")
            return True

        if text == "Miru Java Bridge: Loaded.":
            if fancy:
                self._update_status("Java bridge: ready (3/3)")
            else:
                self._print("Java bridge: ready")
            self._java_bridge_in_progress = False
            self._java_bridge_last_done_at = now
            return True

        return False

    def _log(self, level: str, text: str) -> None:
        text = _sanitize_user_message(text)
        if self._handle_java_bridge_log(level, text):
            return
        if level == "info":
            self._print(text)
        else:
            color = colorama.Fore.RED if level == "error" else colorama.Fore.YELLOW
            text = color + colorama.Style.BRIGHT + text + colorama.Style.RESET_ALL
            if level == "error":
                self._print(text, file=sys.stderr)
            else:
                self._print(text)

    def _perform_on_reactor_thread(self, f: Callable[[], T]) -> T:
        completed = threading.Event()
        result = [None, None]

        def work() -> None:
            try:
                result[0] = f()
            except Exception as e:
                result[1] = e
            completed.set()

        self._reactor.schedule(work)

        while not completed.is_set():
            try:
                completed.wait()
            except KeyboardInterrupt:
                self._reactor.cancel_io()
                continue

        error = result[1]
        if error is not None:
            raise error

        return result[0]

    def _perform_on_background_thread(self, f: Callable[[], T], timeout: Optional[float] = None) -> T:
        result = [None, None]

        def work() -> None:
            with self._reactor.io_cancellable:
                try:
                    result[0] = f()
                except Exception as e:
                    result[1] = e

        worker = threading.Thread(target=work)
        worker.start()

        try:
            worker.join(timeout)
        except KeyboardInterrupt:
            self._reactor.cancel_io()

        if timeout is not None and worker.is_alive():
            self._reactor.cancel_io()
            while worker.is_alive():
                try:
                    worker.join()
                except KeyboardInterrupt:
                    pass

        error = result[1]
        if error is not None:
            raise error

        return result[0]

    def _get_default_miru_dir(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".miru")

    def _get_windows_miru_dir(self) -> str:
        appdata = os.environ["LOCALAPPDATA"]
        return os.path.join(appdata, "miru")

    def _get_or_create_config_dir(self) -> str:
        config_dir = os.path.join(self._get_default_miru_dir(), "config")
        if platform.system() == "Linux":
            xdg_config_home = os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            config_dir = os.path.join(xdg_config_home, "miru")
        elif platform.system() == "Windows":
            config_dir = os.path.join(self._get_windows_miru_dir(), "Config")
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        return config_dir

    def _get_or_create_data_dir(self) -> str:
        data_dir = os.path.join(self._get_default_miru_dir(), "data")
        if platform.system() == "Linux":
            xdg_data_home = os.getenv("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
            data_dir = os.path.join(xdg_data_home, "miru")
        elif platform.system() == "Windows":
            data_dir = os.path.join(self._get_windows_miru_dir(), "Data")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        return data_dir

    def _get_or_create_state_dir(self) -> str:
        state_dir = os.path.join(self._get_default_miru_dir(), "state")
        if platform.system() == "Linux":
            xdg_state_home = os.getenv("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
            state_dir = os.path.join(xdg_state_home, "miru")
        elif platform.system() == "Windows":
            appdata = os.environ["LOCALAPPDATA"]
            state_dir = os.path.join(appdata, "miru", "State")
        if not os.path.exists(state_dir):
            os.makedirs(state_dir)
        return state_dir

    def try_handle_bridge_request(self, message: Mapping[Any, Any], script: miru.core.Script) -> bool:
        if message["type"] != "send":
            return False

        payload = message.get("payload")
        if not isinstance(payload, dict):
            return False

        t = payload.get("type")
        if t != "miru:load-bridge":
            return False

        stem = payload["name"].lower()

        bridge_dir = Path(__file__).parent / "bridges"
        if not bridge_dir.is_dir():
            bridge_dir = Path(__file__).parent.parent / "bridges"

        bridge = bridge_dir / f"{stem}.js"
        if not bridge.is_file():
            matches = [p for p in bridge_dir.glob("*.js") if p.stem.lower() == stem]
            if len(matches) == 0:
                self._update_status(f"Failed to load bridge '{stem}': missing JS asset")
                return False
            bridge = matches[0]

        script.post(
            {
                "type": "miru:bridge-loaded",
                "filename": bridge.name,
                "source": bridge.read_text(encoding="utf-8"),
            }
        )

        return True


def compute_real_args(parser: argparse.ArgumentParser, args: Optional[List[str]] = None) -> List[str]:
    if args is None:
        args = sys.argv[1:]
    real_args = normalize_options_file_args(args)

    files_processed = set()
    while True:
        offset = find_options_file_offset(real_args, parser)
        if offset == -1:
            break

        file_path = os.path.abspath(real_args[offset + 1])
        if file_path in files_processed:
            parser.error(f"File '{file_path}' given twice as -O argument")

        if os.path.isfile(file_path):
            with codecs.open(file_path, "r", "utf-8") as f:
                new_arg_text = f.read()
        else:
            parser.error(f"File '{file_path}' following -O option is not a valid file")

        real_args = insert_options_file_args_in_list(real_args, offset, new_arg_text)
        files_processed.add(file_path)

    return real_args


def normalize_options_file_args(raw_args: List[str]) -> List[str]:
    result = []
    for arg in raw_args:
        if arg.startswith("--options-file="):
            result.append(arg[0:14])
            result.append(arg[15:])
        else:
            result.append(arg)
    return result


def find_options_file_offset(arglist: List[str], parser: argparse.ArgumentParser) -> int:
    for i, arg in enumerate(arglist):
        if arg in ("-O", "--options-file"):
            if i < len(arglist) - 1:
                return i
            else:
                parser.error("No argument given for -O option")
    return -1


def insert_options_file_args_in_list(args: List[str], offset: int, new_arg_text: str) -> List[str]:
    new_args = shlex.split(new_arg_text)
    new_args = normalize_options_file_args(new_args)
    new_args_list = args[:offset] + new_args + args[offset + 2 :]
    return new_args_list


def find_device(device_type: str) -> Optional[miru.core.Device]:
    for device in miru.enumerate_devices():
        if device.type == device_type:
            return device
    return None


def infer_target(target_value: str) -> TargetTypeTuple:
    if (
        target_value.startswith(".")
        or target_value.startswith(os.path.sep)
        or (
            platform.system() == "Windows"
            and target_value[0].isalpha()
            and target_value[1] == ":"
            and target_value[2] == "\\"
        )
    ):
        return ("file", [target_value])

    try:
        return ("pid", int(target_value))
    except:
        pass

    return ("name", target_value)


def expand_target(target: TargetTypeTuple) -> TargetTypeTuple:
    target_type, target_value = target
    if target_type == "file" and isinstance(target_value, list):
        target_value = [target_value[0]]
    return (target_type, target_value)


def parse_aux_option(option: str) -> Tuple[str, Union[str, bool, int]]:
    m = AUX_OPTION_PATTERN.match(option)
    if m is None:
        raise ValueError("expected name=(type)value, e.g. 'uid=(int)42'; supported types are: string, bool, int")

    name = m.group(1)
    type_decl = m.group(2)
    raw_value = m.group(3)
    if type_decl == "string":
        value = raw_value
    elif type_decl == "bool":
        value = bool(raw_value)
    else:
        value = int(raw_value)

    return (name, value)
