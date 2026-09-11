# NVIDIA Fan Control

本项目提供 NVIDIA Fan Control 控制脚本和依赖说明。

```bash
./nvidia-fan-control.py
```

它同时负责三件事：

1. 交互式为每张显卡分别选择风扇策略。
2. 交互式为每张显卡分别选择功率上限。
3. 作为 systemd 后台服务执行手动风扇曲线，并在服务启动时应用功率上限。

## 使用方式

克隆项目并运行 Textual 中文终端界面：

```bash
git clone https://github.com/Quantatirsk/nvidia-fan-control.git
cd nvidia-fan-control
sudo ./nvidia-fan-control.py
```

脚本会自动检测并使用 `Textual` 提供全屏终端界面。界面分为两组：第一组集中设置所有显卡的风扇策略，第二组集中设置所有显卡的功率档位；每张卡仍然独立选择，支持鼠标点击和键盘操作。硬件摘要会显示每张卡的型号、显存大小、风扇能力和功率范围。通过非交互终端运行时，会自动回退到中文文本菜单；Gum 可用时作为次级交互回退。

逐卡风扇策略和逐卡功率会保存到：

```bash
/etc/nvidia-fan-control/config.json
```

## 策略说明

### 风扇策略

| 策略 | 含义 |
| --- | --- |
| 默认 `default` | 使用 NVIDIA 自带的自动风扇控制，不接管风扇。 |
| 中档 `medium` | 自适应曲线：45°C 以下 0% → 50°C 30% → 55°C 45% → 60°C 60% → 70°C 75% → 80°C 80% → 85°C 90% → 90°C 100%。轮询间隔 2 秒。 |
| 高档 `high` | 平滑升速的自适应曲线：45°C 以下 0% → 50°C 30% → 55°C 45% → 60°C 60% → 65°C 70% → 70°C 80% → 75°C 90% → 80°C 100%。 |

中档和高档都不是固定转速：脚本每隔一段时间读取显卡温度，并按曲线插值计算目标转速，温度越高，目标转速越高。两种手动曲线在 45°C 以下都保持 0%；中档 90°C 达到 100%，高档 80°C 达到 100%。

### 逐卡功率选项

功率不再有全局预设，也不允许手填。脚本读取每张 GPU 的 NVML 功率范围，为该卡生成独立选项：

- `驱动默认`：恢复该卡的驱动默认功率上限。
- 在硬件允许范围内，按 `50W` 为间隔生成选项，例如 `150W`、`200W`、`250W`。
- 每张卡的选项根据自己的最小/最大功率范围生成，互不影响。
- 配置值存储在 `per_gpu_power` 中，单位为瓦；不支持功率控制的显卡不会写入该字段。

例如，两张显卡可以分别选择 `200W` 和 `550W`，不会再受到一个全局功率选项影响。

配置文件示例：

```json
{
  "mode": "default",
  "interval": 1.0,
  "per_gpu_mode": {
    "0": "medium",
    "1": "high"
  },
  "per_gpu_power": {
    "0": 500,
    "1": 550
  },
  "updated_at": "2026-06-11T12:00:00+00:00"
}
```

## 服务行为

只要任意一张显卡选择中档/高档风扇策略，或选择非默认功率上限，脚本就会自动安装或更新：

```bash
/etc/systemd/system/nvidia-fan-control.service
```

然后自动启动或重启服务。

如果所有显卡都选择默认风扇策略且功率都选择“驱动默认”，脚本会停止该服务，让系统回到 NVIDIA 默认自动控制。

如果只有部分显卡选择手动风扇策略，服务只接管这些显卡；其他显卡继续使用 NVIDIA 默认控制。如果只有功率被修改，服务会持续重新应用逐卡功率，但不会接管风扇。

## 常用命令

查看服务状态：

```bash
systemctl status nvidia-fan-control.service --no-pager
```

查看实时日志：

```bash
journalctl -u nvidia-fan-control.service -f
```

查看 GPU 温度和风扇：

```bash
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed --format=csv
```

查看 GPU 功率上限：

```bash
nvidia-smi --query-gpu=index,name,power.limit,power.default_limit,power.min_limit,power.max_limit --format=csv
```

## 依赖

首次执行脚本时会自动检查并安装 Python 依赖，因此在项目目录中直接启动即可：

```bash
sudo ./nvidia-fan-control.py
```

脚本内置检查和安装 `nvidia-ml-py>=12.0.0`、`textual>=8.2.8`。如果缺失，会使用当前 Python 的 `pip` 自动下载并安装，然后继续本次启动；依赖已经存在时不会重复安装。内置安装不依赖 `requirements.txt`。

仍需要以下运行环境：

- Python 3
- 已安装并正常工作的 NVIDIA 驱动（提供 `nvidia-smi` 和 NVML）
- `nvidia-ml-py`（提供 `pynvml` 模块，脚本会自动安装）
- `textual`（提供可鼠标操作的可视化终端界面，脚本会自动安装）
- `gum`（可选的旧版终端交互回退组件）
- `systemd`（仅在安装后台服务时需要）
- root 权限，因为脚本需要写入 `/etc` 并通过 NVML 控制风扇和功率上限

### 手动安装依赖

如果希望提前安装，也可以执行：

```bash
python3 -m pip install -r requirements.txt
```

脚本优先使用 Textual；在 Textual 不可用但系统存在 `gum` 时，会使用 Gum 交互；两者都不可用时仍可使用中文文本菜单运行。Gum 是可选的外部命令，不由 `pip` 安装，可从 [官方 Releases](https://github.com/charmbracelet/gum/releases) 下载。

### 使用 Debian/Ubuntu 软件包安装

也可以手动使用系统软件包安装 NVML Python 模块：

```bash
sudo apt update
sudo apt install python3-pynvml
```

安装后可以验证 Python 模块是否可用：

```bash
python3 -c "import pynvml; print('pynvml 已安装')"
```

## 智能检测与兼容性

启动交互菜单时，脚本会先通过 NVML 检测每张显卡的风扇数量、风扇控制能力和功率范围，然后根据检测结果决定后续操作：

- 如果没有显卡风扇控制接口，自动使用 NVIDIA 默认风扇控制，不再询问手动风扇策略。
- 如果只有部分显卡支持风扇或功率控制，只对支持的显卡执行对应动作，其余显卡自动跳过。
- 启动时会显示每张显卡的显存总量，按整数 `GB` 显示（例如 `96 GB`）；如果驱动无法提供该信息，会显示为未知。
- 如果显卡不支持功率设置，会自动跳过该卡的功率选项；功率选项始终按该卡的硬件范围和 50W 间隔生成。
- 后台服务每次启动都会重新检测硬件，因此同一份配置可以安全用于不同型号的 NVIDIA 显卡。

数据中心被动散热卡（例如常见的 A100）通常没有由 NVML 管理的板载风扇。这类设备可以使用功率控制，但风扇转速通常由服务器机箱风扇或 BMC 管理。

交互提示全部使用中文。配置文件只使用新的 `per_gpu_mode` 和 `per_gpu_power` 字段；不再读取旧的全局 `power_preset` 或旧风扇模式字段。
