#!/usr/bin/env python3
"""
NVIDIA Fan Control 风扇和功率控制服务。

交互模式:
  sudo ./nvidia-fan-control.py

后台服务模式:
  sudo ./nvidia-fan-control.py --daemon
"""

import argparse
import importlib.util
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Tuple


def ensure_python_dependencies() -> None:
    required_modules = {
        "pynvml": "nvidia-ml-py>=12.0.0",
        "textual": "textual>=8.2.8",
    }
    missing = [
        package
        for module, package in required_modules.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return

    print(f"检测到缺少依赖：{', '.join(missing)}")
    print("正在自动下载并安装依赖，请稍候...")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    if os.geteuid() != 0 and sys.prefix == sys.base_prefix:
        command.append("--user")
    command.extend(required_modules.values())

    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        print("依赖安装失败，无法继续启动。", file=sys.stderr)
        if result.stdout:
            print(result.stdout.strip(), file=sys.stderr)
        raise SystemExit(result.returncode or 1)
    print("依赖安装完成，继续启动脚本。")


ensure_python_dependencies()
import pynvml

TEXTUAL_AVAILABLE = False
try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Button, Footer, Header, Label, Select, Static

    TEXTUAL_AVAILABLE = True
except ImportError:
    pass

CONFIG_PATH = "/etc/nvidia-fan-control/config.json"
SERVICE_NAME = "nvidia-fan-control.service"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
SCRIPT_PATH = os.path.abspath(__file__)

DEFAULT_MODE = "default"
DEFAULT_INTERVAL = 2.0

MEDIUM_CURVE = [
    (45, 0),
    (50, 30),
    (55, 45),
    (60, 60),
    (70, 75),
    (80, 80),
    (85, 90),
    (90, 100),
]

HIGH_CURVE = [
    (45, 0),
    (50, 30),
    (55, 45),
    (60, 60),
    (65, 70),
    (70, 80),
    (75, 90),
    (80, 100),
]

POLICIES = {
    "default": {
        "label": "默认",
        "description": "规格：使用 NVIDIA 自动风扇控制，不接管风扇。",
        "interval": 2.0,
        "manual": False,
        "curve": None,
    },
    "medium": {
        "label": "中档",
        "description": "规格：45°C 以下 0% → 50°C 30% → 55°C 45% → 60°C 60% → 70°C 75% → 80°C 80% → 85°C 90% → 90°C 100%。",
        "interval": 2.0,
        "manual": True,
        "curve": MEDIUM_CURVE,
    },
    "high": {
        "label": "高档",
        "description": "规格：45°C 以下 0% → 50°C 30% → 55°C 45% → 60°C 60% → 65°C 70% → 70°C 80% → 75°C 90% → 80°C 100%。",
        "interval": 1.0,
        "manual": True,
        "curve": HIGH_CURVE,
    },
}

log = logging.getLogger("nvidia-fan-control")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def save_config(
    per_gpu_mode: Dict[int, str],
    per_gpu_power: Dict[int, int],
) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    intervals = [
        POLICIES[mode]["interval"]
        for mode in per_gpu_mode.values()
        if mode in POLICIES
    ]
    config = {
        "mode": DEFAULT_MODE,
        "interval": min(intervals, default=DEFAULT_INTERVAL),
        "per_gpu_mode": {str(k): v for k, v in per_gpu_mode.items()},
        "per_gpu_power": {str(k): v for k, v in per_gpu_power.items()},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp_path = f"{CONFIG_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2, ensure_ascii=False)
        config_file.write("\n")
    os.replace(tmp_path, CONFIG_PATH)


def resolve_policy() -> Tuple[str, float, Dict[int, int], Dict[int, str]]:
    config = load_config()
    mode = config.get("mode", DEFAULT_MODE)
    if mode not in POLICIES:
        mode = DEFAULT_MODE

    try:
        interval = float(config.get("interval", POLICIES[mode]["interval"]))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL

    if interval <= 0:
        interval = DEFAULT_INTERVAL

    per_gpu_power: Dict[int, int] = {}
    raw = config.get("per_gpu_power", {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                per_gpu_power[int(k)] = int(v)
            except (TypeError, ValueError):
                pass

    per_gpu_mode: Dict[int, str] = {}
    raw_modes = config.get("per_gpu_mode", {})
    if isinstance(raw_modes, dict):
        for k, value in raw_modes.items():
            if value in POLICIES:
                try:
                    per_gpu_mode[int(k)] = value
                except (TypeError, ValueError):
                    pass

    return mode, interval, per_gpu_power, per_gpu_mode


def format_power(limit_mw: int) -> str:
    watts = limit_mw / 1000
    if watts.is_integer():
        return f"{int(watts)}W"
    return f"{watts:.1f}W"


def format_memory(bytes_total: int | None) -> str:
    if bytes_total is None:
        return "未知"
    return f"{round(bytes_total / (1024 ** 3))} GB"


def gpu_name(handle) -> str:
    name = pynvml.nvmlDeviceGetName(handle)
    if isinstance(name, bytes):
        return name.decode("utf-8", errors="replace")
    return str(name)


def inspect_gpu_capabilities(handle, index: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "index": index,
        "name": gpu_name(handle),
        "memory_total": None,
        "fan_count": 0,
        "fan_control": False,
        "fan_readable": False,
        "power_control": False,
        "power_min": None,
        "power_max": None,
        "power_default": None,
        "power_current": None,
    }

    if hasattr(pynvml, "nvmlDeviceGetMemoryInfo"):
        try:
            info["memory_total"] = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        except pynvml.NVMLError:
            pass

    if hasattr(pynvml, "nvmlDeviceGetNumFans"):
        try:
            info["fan_count"] = pynvml.nvmlDeviceGetNumFans(handle)
        except pynvml.NVMLError:
            info["fan_count"] = 0

    info["fan_control"] = bool(
        info["fan_count"]
        and hasattr(pynvml, "nvmlDeviceSetFanControlPolicy")
        and hasattr(pynvml, "nvmlDeviceSetFanSpeed_v2")
    )
    if info["fan_count"] and hasattr(pynvml, "nvmlDeviceGetFanSpeed_v2"):
        for fan_index in range(info["fan_count"]):
            try:
                pynvml.nvmlDeviceGetFanSpeed_v2(handle, fan_index)
                info["fan_readable"] = True
                break
            except pynvml.NVMLError:
                continue

    if hasattr(pynvml, "nvmlDeviceGetPowerManagementLimitConstraints"):
        try:
            min_limit, max_limit = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
            info["power_min"] = min_limit
            info["power_max"] = max_limit
        except pynvml.NVMLError:
            pass

    if hasattr(pynvml, "nvmlDeviceGetPowerManagementDefaultLimit"):
        try:
            info["power_default"] = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
        except pynvml.NVMLError:
            pass

    if hasattr(pynvml, "nvmlDeviceGetPowerManagementLimit"):
        try:
            info["power_current"] = pynvml.nvmlDeviceGetPowerManagementLimit(handle)
        except pynvml.NVMLError:
            pass

    info["power_control"] = bool(
        hasattr(pynvml, "nvmlDeviceSetPowerManagementLimit")
        and (
            info["power_min"] is not None
            or info["power_default"] is not None
        )
    )
    return info


def detect_gpus() -> Tuple[List[Dict[str, Any]], str | None]:
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        infos = []
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            infos.append(inspect_gpu_capabilities(handle, index))
        return infos, None
    except pynvml.NVMLError as e:
        return [], str(e)
    finally:
        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            pass


def power_choices_for_gpu(info: Dict[str, Any]) -> Dict[str, int]:
    choices = {"驱动默认": 0}
    min_limit = info.get("power_min")
    max_limit = info.get("power_max")
    if min_limit is None or max_limit is None:
        return choices

    min_watts = ((int(min_limit) + 49999) // 50000) * 50
    max_watts = (int(max_limit) // 50000) * 50
    for watts in range(min_watts, max_watts + 1, 50):
        choices[f"{watts}W"] = watts
    return choices


def prepare_gpu_settings(
    gpu_infos: List[Dict[str, Any]],
    default_mode: str,
    raw_modes: Dict[int, str],
    raw_power: Dict[int, int],
) -> Tuple[Dict[int, str], Dict[int, int]]:
    modes: Dict[int, str] = {}
    powers: Dict[int, int] = {}

    for info in gpu_infos:
        index = info["index"]
        mode = raw_modes.get(index, default_mode)
        if mode not in POLICIES or not info["fan_control"]:
            mode = DEFAULT_MODE
        modes[index] = mode

        if info["power_control"]:
            choices = power_choices_for_gpu(info)
            valid_watts = list(choices.values())
            if index in raw_power:
                requested_watts = raw_power[index]
            else:
                requested_watts = 0
            powers[index] = min(
                valid_watts,
                key=lambda watts: abs(watts - requested_watts),
            )

    return modes, powers


def format_gpu_capabilities(infos: List[Dict[str, Any]]) -> str:
    lines = [f"检测到 {len(infos)} 张 NVIDIA 显卡。", ""]
    for info in infos:
        if info["fan_control"]:
            fan_status = f"可控制风扇（{info['fan_count']} 个）"
        else:
            fan_status = "未检测到可由 NVML 控制的风扇"

        if info["power_control"]:
            if info["power_min"] is not None and info["power_max"] is not None:
                power_status = (
                    f"可设置功率（{format_power(info['power_min'])}-"
                    f"{format_power(info['power_max'])}）"
                )
            else:
                power_status = "可设置功率"
        else:
            power_status = "不支持通过 NVML 设置功率"

        lines.extend(
            [
                f"显卡 {info['index']}：{info['name']}（显存：{format_memory(info['memory_total'])}）",
                f"  风扇：{fan_status}",
                f"  功率：{power_status}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def print_gpu_capabilities(infos: List[Dict[str, Any]]) -> None:
    print("\n--- 硬件能力检测 ---")
    print(format_gpu_capabilities(infos))


def gum_available() -> bool:
    return bool(shutil.which("gum")) and sys.stdin.isatty() and sys.stdout.isatty()


def gum_run(arguments: List[str]) -> Tuple[int, str]:
    result = subprocess.run(
        ["gum", *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode, result.stdout.strip()


def gum_style(message: str) -> None:
    subprocess.run(
        [
            "gum",
            "style",
            "--border",
            "rounded",
            "--border-foreground",
            "99",
            "--padding",
            "1 2",
            message,
        ],
        check=False,
    )


def gum_choose(
    title: str,
    options: List[Tuple[str, str, str]],
    current_value: str | None = None,
) -> str | None:
    display_to_value: Dict[str, str] = {}
    display_options = []
    for value, label, description in options:
        current_marker = "  [当前]" if value == current_value else ""
        display = f"{label}{current_marker}  -  {description}"
        display_options.append(display)
        display_to_value[display] = value

    code, selected = gum_run(
        [
            "choose",
            "--header",
            title,
            "--height",
            str(max(6, min(14, len(display_options) + 2))),
            "--cursor",
            "➜ ",
            *display_options,
        ]
    )
    if code != 0:
        return None
    return display_to_value.get(selected)


def gum_capability_summary(infos: List[Dict[str, Any]]) -> None:
    gum_style(format_gpu_capabilities(infos))


def textual_available() -> bool:
    return TEXTUAL_AVAILABLE and sys.stdin.isatty() and sys.stdout.isatty()


if TEXTUAL_AVAILABLE:

    class FanControlApp(App):
        TITLE = "NVIDIA Fan Control"
        SUB_TITLE = "鼠标点击或键盘操作"
        CSS = """
        Screen {
            background: #10131c;
        }

        #body {
            padding: 1 2;
            height: 1fr;
            overflow-y: auto;
        }

        #hardware {
            border: round #7c5cff;
            padding: 1 2;
            height: auto;
            color: #d6d8e0;
        }

        .section-title {
            margin: 1 0 0 0;
            color: #7cdbff;
            text-style: bold;
        }

        .hint {
            color: #8d93a6;
            height: auto;
        }

        Select {
            width: 1fr;
            margin: 0 0 1 0;
        }

        .strategy-group {
            border: round #4d5878;
            padding: 0 1;
            margin: 1 0;
            height: auto;
        }

        .gpu-title {
            color: #ffffff;
            text-style: bold;
            height: auto;
        }

        .field-hint {
            color: #8d93a6;
            height: auto;
        }

        #actions {
            height: 4;
            align: center middle;
            padding: 0 2;
        }

        Button {
            margin: 0 1;
        }
        """
        BINDINGS = [
            ("escape", "cancel", "取消"),
            ("ctrl+c", "cancel", "退出"),
        ]

        def __init__(
            self,
            gpu_infos: List[Dict[str, Any]],
            current_modes: Dict[int, str],
            current_powers: Dict[int, int],
        ) -> None:
            super().__init__()
            self.gpu_infos = gpu_infos
            self.selected_modes = dict(current_modes)
            self.selected_powers = dict(current_powers)
            self.selection: Tuple[Dict[int, str], Dict[int, int]] | None = None

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(id="body"):
                yield Static(self.hardware_summary(), id="hardware")
                yield Label("风扇策略组（每张显卡独立选择）", classes="section-title")
                yield Static(
                    "这一组只设置风扇策略。默认使用 NVIDIA 自动控制；中档和高档按温度自适应。",
                    classes="hint",
                )
                with Vertical(classes="strategy-group"):
                    for info in self.gpu_infos:
                        index = info["index"]
                        yield Label(
                            f"显卡 {index}：{info['name']}（显存：{format_memory(info['memory_total'])}）",
                            classes="gpu-title",
                        )
                        if info["fan_control"]:
                            yield Select(
                                [
                                    (f"{policy['label']}：{policy['description']}", mode)
                                    for mode, policy in POLICIES.items()
                                ],
                                value=self.selected_modes.get(index, DEFAULT_MODE),
                                id=f"fan-{index}",
                            )
                        else:
                            yield Static(
                                "默认：使用 NVIDIA 自动风扇控制（本卡不支持 NVML 风扇接管）。",
                                classes="field-hint",
                            )

                yield Label("功率策略组（每张显卡独立选择）", classes="section-title")
                yield Static(
                    "这一组只设置功率上限。每张卡按自己的硬件范围提供 50W 间隔选项，不提供手填。",
                    classes="hint",
                )
                with Vertical(classes="strategy-group"):
                    for info in self.gpu_infos:
                        index = info["index"]
                        yield Label(
                            f"显卡 {index}：{info['name']}（显存：{format_memory(info['memory_total'])}）",
                            classes="gpu-title",
                        )
                        if info["power_control"]:
                            choices = power_choices_for_gpu(info)
                            yield Select(
                                [
                                    (
                                        "驱动默认：恢复本卡默认功率"
                                        if watts == 0
                                        else f"{label}：限制本卡功率上限",
                                        watts,
                                    )
                                    for label, watts in choices.items()
                                ],
                                value=self.selected_powers.get(index, 0),
                                id=f"power-{index}",
                            )
                        else:
                            yield Static(
                                "本卡不支持通过 NVML 设置功率，已自动跳过。",
                                classes="field-hint",
                            )

            with Horizontal(id="actions"):
                yield Button("应用策略", variant="success", id="apply")
                yield Button("取消", id="cancel")
            yield Footer()

        def hardware_summary(self) -> str:
            lines = [f"检测到 {len(self.gpu_infos)} 张 NVIDIA 显卡"]
            for info in self.gpu_infos:
                fan_text = (
                    f"可控制风扇（{info['fan_count']} 个）"
                    if info["fan_control"]
                    else "无可由 NVML 控制的风扇"
                )
                if info["power_control"] and info["power_min"] is not None:
                    power_text = (
                        f"功率 {format_power(info['power_min'])}-"
                        f"{format_power(info['power_max'])}"
                    )
                elif info["power_control"]:
                    power_text = "支持功率设置"
                else:
                    power_text = "不支持功率设置"
                lines.append(
                    f"显卡 {info['index']}：{info['name']} | 显存：{format_memory(info['memory_total'])} | "
                    f"风扇：{fan_text} | {power_text}"
                )
            return "\n".join(lines)

        def on_select_changed(self, event: Select.Changed) -> None:
            widget_id = event.select.id or ""
            try:
                index = int(widget_id.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                return
            if widget_id.startswith("fan-") and isinstance(event.value, str):
                self.selected_modes[index] = event.value
            elif widget_id.startswith("power-") and isinstance(event.value, (int, str)):
                try:
                    self.selected_powers[index] = int(event.value)
                except (TypeError, ValueError):
                    return

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.action_cancel()
                return
            if event.button.id != "apply":
                return

            self.selection = (dict(self.selected_modes), dict(self.selected_powers))
            self.exit()

        def action_cancel(self) -> None:
            self.selection = None
            self.exit()


def run_textual_selection(
    gpu_infos: List[Dict[str, Any]],
    current_modes: Dict[int, str],
    current_powers: Dict[int, int],
) -> Tuple[Dict[int, str], Dict[int, int]] | None:
    app = FanControlApp(gpu_infos, current_modes, current_powers)
    app.run()
    return app.selection


def set_power_limit_for_gpu(
    handle,
    gpu_index: int,
    emit: Callable[[str], None],
    watts: int,
) -> None:
    if not hasattr(pynvml, "nvmlDeviceSetPowerManagementLimit"):
        emit(f"显卡 {gpu_index}：当前 NVML 不支持设置功率上限")
        return

    try:
        if watts > 0:
            if not hasattr(pynvml, "nvmlDeviceGetPowerManagementLimitConstraints"):
                emit(f"显卡 {gpu_index}：当前 NVML 不支持读取功率范围")
                return
            min_limit, max_limit = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
            requested_limit = int(watts) * 1000
            target_limit = min(max(requested_limit, min_limit), max_limit)
            target_note = f"选择 {watts}W"
            if target_limit != requested_limit:
                emit(
                    f"显卡 {gpu_index}：{target_note} 超出允许范围 "
                    f"{format_power(min_limit)}-{format_power(max_limit)}，"
                    f"改用 {format_power(target_limit)}"
                )
        else:
            if not hasattr(pynvml, "nvmlDeviceGetPowerManagementDefaultLimit"):
                emit(f"显卡 {gpu_index}：当前 NVML 不支持读取默认功率上限")
                return
            target_limit = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
            target_note = "驱动默认"

        pynvml.nvmlDeviceSetPowerManagementLimit(handle, target_limit)
        emit(f"显卡 {gpu_index}：已设置为 {format_power(target_limit)}（{target_note}）")
    except pynvml.NVMLError as e:
        emit(f"显卡 {gpu_index}：设置功率上限失败：{e}")


def set_power_limits_for_handles(
    handles: List[object],
    emit: Callable[[str], None],
    per_gpu_power: Dict[int, int],
    gpu_infos: List[Dict[str, Any]] | None = None,
) -> None:
    emit("正在应用逐卡功率设置")

    for gpu_index, handle in enumerate(handles):
        try:
            name = gpu_name(handle)
            emit(f"显卡 {gpu_index}：{name}")
            info = (
                gpu_infos[gpu_index]
                if gpu_infos and gpu_index < len(gpu_infos)
                else inspect_gpu_capabilities(handle, gpu_index)
            )
            if not info["power_control"]:
                emit(f"显卡 {gpu_index}：不支持通过 NVML 设置功率，已跳过")
                continue
            set_power_limit_for_gpu(
                handle,
                gpu_index,
                emit,
                per_gpu_power.get(gpu_index, 0),
            )
        except pynvml.NVMLError as e:
            emit(f"显卡 {gpu_index}：无法读取或设置功率上限：{e}")


def apply_power_settings(
    per_gpu_power: Dict[int, int],
    emit: Callable[[str], None] = print,
) -> None:
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        handles = [pynvml.nvmlDeviceGetHandleByIndex(index) for index in range(count)]
        gpu_infos = [
            inspect_gpu_capabilities(handle, index)
            for index, handle in enumerate(handles)
        ]
        set_power_limits_for_handles(
            handles,
            emit,
            per_gpu_power=per_gpu_power,
            gpu_infos=gpu_infos,
        )
    except pynvml.NVMLError as e:
        emit(f"无法初始化 NVML 或读取显卡：{e}")
    finally:
        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            pass


def run_systemctl(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def service_exists() -> bool:
    result = run_systemctl(["status", SERVICE_NAME, "--no-pager"])
    return result.returncode in (0, 3)


def service_is_active() -> bool:
    result = run_systemctl(["is-active", "--quiet", SERVICE_NAME])
    return result.returncode == 0


def service_file_content() -> str:
    return f"""[Unit]
Description=NVIDIA Fan Control service
After=nvidia-persistenced.service
Wants=nvidia-persistenced.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {SCRIPT_PATH} --daemon
ExecStop=/bin/kill -SIGTERM $MAINPID
Restart=on-failure
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nvidia-fan-control

[Install]
WantedBy=multi-user.target
"""


def ensure_service_installed() -> bool:
    desired = service_file_content()
    current = None
    if os.path.exists(SERVICE_PATH):
        try:
            with open(SERVICE_PATH, "r", encoding="utf-8") as service_file:
                current = service_file.read()
        except OSError:
            current = None

    if current == desired:
        return True

    try:
        with open(SERVICE_PATH, "w", encoding="utf-8") as service_file:
            service_file.write(desired)
    except OSError as e:
        print(f"\n无法写入服务文件 {SERVICE_PATH}: {e}")
        return False

    result = run_systemctl(["daemon-reload"])
    if result.returncode != 0:
        print("\n无法重新加载 systemd。")
        if result.stderr:
            print(result.stderr.strip())
        return False

    run_systemctl(["enable", SERVICE_NAME])
    print(f"\n已安装或更新 {SERVICE_NAME}。")
    return True


def activate_service() -> None:
    if not ensure_service_installed():
        print("\n已保存配置，但服务未启动。")
        return

    if not service_is_active():
        result = run_systemctl(["start", SERVICE_NAME])
        if result.returncode == 0:
            print(f"\n已启动 {SERVICE_NAME}，新策略已生效。")
            return

        print(f"\n无法启动 {SERVICE_NAME}。")
        if result.stderr:
            print(result.stderr.strip())
        return

    result = run_systemctl(["restart", SERVICE_NAME])
    if result.returncode == 0:
        print(f"\n已重启 {SERVICE_NAME}，新策略已生效。")
        return

    print(f"\n无法重启 {SERVICE_NAME}。")
    if result.stderr:
        print(result.stderr.strip())


def stop_service_if_active() -> None:
    if not service_exists():
        print(f"\n服务 {SERVICE_NAME} 尚未安装。已保存默认策略。")
        return

    if not service_is_active():
        print(f"\n服务 {SERVICE_NAME} 当前未运行。系统将继续使用 NVIDIA 默认自动风扇控制。")
        return

    result = run_systemctl(["stop", SERVICE_NAME])
    if result.returncode == 0:
        print(f"\n已停止 {SERVICE_NAME}。系统将使用 NVIDIA 默认自动风扇控制。")
        return

    print(f"\n无法停止 {SERVICE_NAME}。")
    if result.stderr:
        print(result.stderr.strip())


def read_choice(title: str, options: dict) -> str | None:
    values = list(options)

    while True:
        try:
            choice = input(f"\n请选择{title}：").strip().lower()
        except EOFError:
            print("\n未检测到交互输入，未做任何修改。")
            return None
        if choice in ("q", "quit", "exit"):
            return None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(values):
                return values[index - 1]
        if choice in options:
            return choice
        print("选择无效，请输入编号或 q。")


def format_per_gpu_summary(per_gpu_power: Dict[int, int] | None) -> str:
    """Format per-GPU power settings for display."""
    if not per_gpu_power:
        return ""
    parts = []
    for gpu_idx in sorted(per_gpu_power.keys()):
        watts = per_gpu_power[gpu_idx]
        if watts is None or watts == 0:
            parts.append(f"显卡 {gpu_idx}：默认")
        else:
            parts.append(f"显卡 {gpu_idx}：{watts}W")
    return " | ".join(parts)


def format_per_gpu_modes(per_gpu_mode: Dict[int, str]) -> str:
    parts = []
    for gpu_idx in sorted(per_gpu_mode):
        mode = per_gpu_mode[gpu_idx]
        parts.append(f"显卡 {gpu_idx}：{POLICIES.get(mode, POLICIES[DEFAULT_MODE])['label']}")
    return " | ".join(parts)


def print_fan_text_options(info: Dict[str, Any], current_mode: str) -> None:
    index = info["index"]
    print(f"\n显卡 {index}：{info['name']}（显存：{format_memory(info['memory_total'])}）")
    if info["fan_control"]:
        for option_index, (mode, policy) in enumerate(POLICIES.items(), start=1):
            marker = " 当前" if mode == current_mode else ""
            print(f"  {option_index}. {policy['label']}{marker}：{policy['description']}")
    else:
        print("  默认：使用 NVIDIA 自动风扇控制（本卡不支持 NVML 风扇接管）")


def print_power_text_options(info: Dict[str, Any], current_power: int) -> None:
    index = info["index"]
    print(f"\n显卡 {index}：{info['name']}（显存：{format_memory(info['memory_total'])}）")
    if not info["power_control"]:
        print("  不支持通过 NVML 设置功率，已自动跳过。")
        return
    range_text = ""
    if info["power_min"] is not None and info["power_max"] is not None:
        range_text = (
            f"，硬件范围 {format_power(info['power_min'])}-"
            f"{format_power(info['power_max'])}"
        )
    print(f"  功率上限选项（每 50W 一档{range_text}）：")
    for option_index, (label, watts) in enumerate(power_choices_for_gpu(info).items(), start=1):
        marker = " 当前" if watts == current_power else ""
        detail = "恢复本卡驱动默认功率" if watts == 0 else f"限制本卡功率到 {watts}W"
        print(f"  {option_index}. {label}{marker}：{detail}")


def run_text_selection(
    gpu_infos: List[Dict[str, Any]],
    current_modes: Dict[int, str],
    current_powers: Dict[int, int],
) -> Tuple[Dict[int, str], Dict[int, int]] | None:
    selected_modes = dict(current_modes)
    selected_powers = dict(current_powers)
    print("NVIDIA Fan Control")
    print(f"配置文件：{CONFIG_PATH}")
    print("设置分为两组：先选择所有显卡的风扇策略，再选择所有显卡的功率上限；输入 q 可退出。")

    print("\n=== 风扇策略组 ===")
    for info in gpu_infos:
        index = info["index"]
        print_fan_text_options(info, selected_modes.get(index, DEFAULT_MODE))
        if info["fan_control"]:
            selected_mode = read_choice(f"显卡 {index} 的风扇策略", POLICIES)
            if selected_mode is None:
                return None
            selected_modes[index] = selected_mode

    print("\n=== 功率策略组 ===")
    for info in gpu_infos:
        index = info["index"]
        print_power_text_options(info, selected_powers.get(index, 0))
        if info["power_control"]:
            choices = power_choices_for_gpu(info)
            selected_label = read_choice(f"显卡 {index} 的功率上限", choices)
            if selected_label is None:
                return None
            selected_powers[index] = choices[selected_label]

    return selected_modes, selected_powers


def run_gum_selection(
    gpu_infos: List[Dict[str, Any]],
    current_modes: Dict[int, str],
    current_powers: Dict[int, int],
) -> Tuple[Dict[int, str], Dict[int, int]] | None:
    gum_capability_summary(gpu_infos)
    selected_modes = dict(current_modes)
    selected_powers = dict(current_powers)
    gum_style("第一组：分别选择每张显卡的风扇策略")
    for info in gpu_infos:
        index = info["index"]
        if info["fan_control"]:
            fan_options = [
                (mode, policy["label"], policy["description"])
                for mode, policy in POLICIES.items()
            ]
            selected_mode = gum_choose(
                f"显卡 {index}：选择风扇策略（本卡独立）",
                fan_options,
                selected_modes.get(index, DEFAULT_MODE),
            )
            if selected_mode is None:
                return None
            selected_modes[index] = selected_mode
        else:
            gum_style(f"显卡 {index}：不支持 NVML 风扇控制，使用 NVIDIA 默认风扇控制。")

    gum_style("第二组：分别选择每张显卡的功率上限（每 50W 一档）")
    for info in gpu_infos:
        index = info["index"]
        if info["power_control"]:
            choices = power_choices_for_gpu(info)
            power_options = [
                (
                    str(watts),
                    label,
                    "恢复本卡驱动默认功率" if watts == 0 else f"限制本卡功率到 {watts}W",
                )
                for label, watts in choices.items()
            ]
            selected_power = gum_choose(
                f"显卡 {index}：选择功率上限（本卡独立，每 50W 一档）",
                power_options,
                str(selected_powers.get(index, 0)),
            )
            if selected_power is None:
                return None
            selected_powers[index] = int(selected_power)
        else:
            gum_style(f"显卡 {index}：不支持 NVML 功率设置，已跳过。")

    return selected_modes, selected_powers


def run_menu() -> int:
    if os.geteuid() != 0:
        print("请使用 root 权限运行，以便保存策略和管理 systemd 服务。", file=sys.stderr)
        return 1

    default_mode, _, raw_power, raw_modes = resolve_policy()
    gpu_infos, detect_error = detect_gpus()
    if detect_error:
        print(f"无法检测 NVIDIA 显卡：{detect_error}", file=sys.stderr)
        return 1
    if not gpu_infos:
        print("未检测到 NVIDIA 显卡。", file=sys.stderr)
        return 1

    current_modes, current_powers = prepare_gpu_settings(
        gpu_infos,
        default_mode,
        raw_modes,
        raw_power,
    )

    if textual_available():
        selection = run_textual_selection(
            gpu_infos,
            current_modes,
            current_powers,
        )
    elif gum_available():
        selection = run_gum_selection(
            gpu_infos,
            current_modes,
            current_powers,
        )
    else:
        print_gpu_capabilities(gpu_infos)
        selection = run_text_selection(
            gpu_infos,
            current_modes,
            current_powers,
        )

    if selection is None:
        print("未做任何修改。")
        return 0

    selected_modes, selected_powers = selection

    save_config(selected_modes, selected_powers)
    interval = min(
        (POLICIES[mode]["interval"] for mode in selected_modes.values()),
        default=DEFAULT_INTERVAL,
    )
    print(f"\n已保存逐卡风扇策略：{format_per_gpu_modes(selected_modes)}")
    print(f"轮询间隔：{interval} 秒")
    print(f"已保存逐卡功率：{format_per_gpu_summary(selected_powers)}")

    print("\n正在应用逐卡功率设置...")
    apply_power_settings(selected_powers, print)

    has_manual_fan = any(
        POLICIES.get(mode, POLICIES[DEFAULT_MODE])["manual"]
        for mode in selected_modes.values()
    )
    has_custom_power = any(value not in (None, 0) for value in selected_powers.values())
    needs_service = has_manual_fan or has_custom_power
    if needs_service:
        activate_service()
    else:
        stop_service_if_active()

    return 0


class FanController:
    def __init__(
        self,
        default_mode: str,
        interval: float,
        per_gpu_modes: Dict[int, str],
        per_gpu_power: Dict[int, int],
    ):
        self.default_mode = default_mode
        self.interval = interval
        self.per_gpu_modes = dict(per_gpu_modes)
        self.per_gpu_power = dict(per_gpu_power)
        self.handles = []
        self.fan_counts = []
        self.gpu_infos: List[Dict[str, Any]] = []
        self.curves: List[List[Tuple[int, int]] | None] = []
        self.running = True

    def init(self) -> None:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        log.info(f"发现 {count} 张 NVIDIA 显卡")

        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            info = inspect_gpu_capabilities(handle, index)
            fan_count = info["fan_count"]

            self.handles.append(handle)
            self.fan_counts.append(fan_count)
            self.gpu_infos.append(info)
            if not info["power_control"]:
                log.info(f"显卡 {index}：未检测到可由 NVML 控制的风扇，将使用默认自动控制")
            log.info(f"显卡 {index}：{info['name']}，风扇数量：{fan_count}")

        self.per_gpu_modes, self.per_gpu_power = prepare_gpu_settings(
            self.gpu_infos,
            self.default_mode,
            self.per_gpu_modes,
            self.per_gpu_power,
        )
        for info in self.gpu_infos:
            mode = self.per_gpu_modes.get(info["index"], DEFAULT_MODE)
            policy = POLICIES[mode]
            curve = policy["curve"] if info["fan_control"] else None
            self.curves.append(sorted(curve, key=lambda point: point[0]) if curve else None)
            log.info(f"显卡 {info['index']}：风扇策略 {policy['label']}（{policy['description']}）")

        set_power_limits_for_handles(
            self.handles,
            log.info,
            per_gpu_power=self.per_gpu_power,
            gpu_infos=self.gpu_infos,
        )

        for index, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            if self.curves[index] is None:
                continue
            for fan_index in range(fan_count):
                try:
                    pynvml.nvmlDeviceSetFanControlPolicy(
                        handle, fan_index, pynvml.NVML_FAN_POLICY_MANUAL
                    )
                except pynvml.NVMLError as e:
                    log.warning(f"显卡 {index} 风扇 {fan_index}：无法切换到手动控制：{e}")

    @staticmethod
    def fan_speed_for_temp(temp: int, curve: List[Tuple[int, int]]) -> int:
        if temp <= curve[0][0]:
            return curve[0][1]
        if temp >= curve[-1][0]:
            return curve[-1][1]

        for index in range(len(curve) - 1):
            temp_a, speed_a = curve[index]
            temp_b, speed_b = curve[index + 1]
            if temp_a <= temp <= temp_b:
                ratio = (temp - temp_a) / (temp_b - temp_a)
                return int(speed_a + ratio * (speed_b - speed_a))

        return curve[-1][1]

    def update(self) -> None:
        for gpu_index, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            curve = self.curves[gpu_index]
            if curve is None:
                continue
            try:
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                target_speed = self.fan_speed_for_temp(temp, curve)

                current_speeds = []
                for fan_index in range(fan_count):
                    try:
                        current_speeds.append(pynvml.nvmlDeviceGetFanSpeed_v2(handle, fan_index))
                    except pynvml.NVMLError:
                        current_speeds.append(-1)

                for fan_index in range(fan_count):
                    try:
                        pynvml.nvmlDeviceSetFanSpeed_v2(handle, fan_index, target_speed)
                    except pynvml.NVMLError as e:
                        log.error(f"显卡 {gpu_index} 风扇 {fan_index}：设置风扇失败：{e}")

                log.info(f"显卡 {gpu_index}：{temp}°C -> {target_speed}%（原转速：{current_speeds}）")
            except pynvml.NVMLError as e:
                log.error(f"显卡 {gpu_index}：读取温度失败：{e}")

    def restore_auto(self) -> None:
        log.info("正在恢复 NVIDIA 自动风扇控制")
        for gpu_index, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            if self.curves[gpu_index] is None:
                continue
            for fan_index in range(fan_count):
                try:
                    if hasattr(pynvml, "nvmlDeviceSetDefaultFanSpeed_v2"):
                        pynvml.nvmlDeviceSetDefaultFanSpeed_v2(handle, fan_index)
                    else:
                        pynvml.nvmlDeviceSetFanControlPolicy(
                            handle,
                            fan_index,
                            pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW,
                        )
                    log.info(f"显卡 {gpu_index} 风扇 {fan_index}：已恢复自动控制")
                except pynvml.NVMLError as e:
                    log.error(f"显卡 {gpu_index} 风扇 {fan_index}：恢复自动控制失败：{e}")

    def stop(self, signum=None, frame=None) -> None:
        if signum is not None:
            log.info(f"收到信号 {signum}")
        self.running = False

    def run(self) -> None:
        try:
            self.init()
            has_fan_action = any(curve is not None for curve in self.curves)
            has_power_action = any(value not in (None, 0) for value in self.per_gpu_power.values())
            if not has_fan_action and not has_power_action:
                log.info("当前显卡没有需要持续执行的控制动作，服务将退出")
                return
            log.info(f"轮询间隔：{self.interval} 秒")
            log.info(f"逐卡风扇策略：{format_per_gpu_modes(self.per_gpu_modes)}")
            log.info(f"逐卡功率：{format_per_gpu_summary(self.per_gpu_power) or '全部使用驱动默认'}")
            log.info("风扇和功率控制服务已启动")
            while self.running:
                if has_fan_action:
                    self.update()
                time.sleep(self.interval)
        finally:
            self.restore_auto()
            pynvml.nvmlShutdown()
            log.info("风扇和功率控制服务已停止")


def run_daemon() -> int:
    setup_logging()
    default_mode, interval, per_gpu_power, per_gpu_modes = resolve_policy()

    log.info("NVIDIA Fan Control，正在读取逐卡策略")
    log.info(f"配置文件：{CONFIG_PATH}")
    controller = FanController(
        default_mode,
        interval,
        per_gpu_modes,
        per_gpu_power,
    )
    signal.signal(signal.SIGTERM, controller.stop)
    signal.signal(signal.SIGINT, controller.stop)
    controller.run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NVIDIA Fan Control")
    parser.add_argument("--daemon", action="store_true", help="以 systemd 后台服务模式运行")
    args = parser.parse_args()

    if args.daemon:
        return run_daemon()
    return run_menu()


if __name__ == "__main__":
    raise SystemExit(main())
