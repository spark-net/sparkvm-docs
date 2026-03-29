#!/usr/bin/env python3
# ============================================================================
# setup-pbr.py — 多网卡 / 多出口 VM 一键策略路由配置脚本
#
# 功能：
#   1. 读取 cloud-init 生成的 netplan 配置（50-cloud-init.yaml）
#   2. 自动提取所有接口的 IPv4/IPv6 地址、子网、网关信息
#   3. 生成带 metric 的路由配置（60-ethernet.yaml）
#   4. 生成策略路由 PBR 配置（90-pbr.yaml）
#   5. 通过 netplan try 安全应用
#
# 用法：
#   sudo python3 setup-pbr.py                          # 使用默认配置路径
#   sudo python3 setup-pbr.py /path/to/netplan.yaml    # 指定配置文件
#   sudo python3 setup-pbr.py --dry-run                # 仅预览，不写入
#
# 依赖：python3, python3-yaml (PyYAML)
# 适用：Debian 12 / Debian 13 (Netplan)
# ============================================================================

from __future__ import annotations

import argparse
import copy
import ipaddress
import os
import shutil
import subprocess
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 依赖检查 ──────────────────────────────────────────────────────────────────

try:
    import yaml
except ImportError:
    print("\033[0;31m[ERR]\033[0m  未找到 PyYAML，请先安装: apt install python3-yaml")
    sys.exit(1)

# ── 颜色输出 ──────────────────────────────────────────────────────────────────

class Log:
    RED    = "\033[0;31m"
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    CYAN   = "\033[0;36m"
    NC     = "\033[0m"

    @staticmethod
    def info(msg: str)  -> None: print(f"{Log.CYAN}[INFO]{Log.NC}  {msg}")
    @staticmethod
    def ok(msg: str)    -> None: print(f"{Log.GREEN}[ OK ]{Log.NC}  {msg}")
    @staticmethod
    def warn(msg: str)  -> None: print(f"{Log.YELLOW}[WARN]{Log.NC}  {msg}")
    @staticmethod
    def err(msg: str)   -> None: print(f"{Log.RED}[ERR]{Log.NC}   {msg}", file=sys.stderr)

# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class InterfaceInfo:
    name: str
    ipv4_subnets:  list[str] = field(default_factory=list)
    ipv4_gateways: list[str] = field(default_factory=list)
    ipv6_subnets:  list[str] = field(default_factory=list)
    ipv6_gateways: list[str] = field(default_factory=list)

    @property
    def has_default_route(self) -> bool:
        return bool(self.ipv4_gateways or self.ipv6_gateways)

    def summary(self) -> str:
        gws  = ", ".join(self.ipv4_gateways + self.ipv6_gateways)
        nets = ", ".join(self.ipv4_subnets  + self.ipv6_subnets)
        return f"{self.name}: 网关=[{gws}]  子网=[{nets}]"

# ── 解析 ──────────────────────────────────────────────────────────────────────

def parse_netplan(path: Path) -> dict[str, Any]:
    """读取并返回 netplan YAML 配置"""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        Log.err(f"YAML 解析失败: {e}")
        sys.exit(1)
    except OSError as e:
        Log.err(f"无法读取文件 {path}: {e}")
        sys.exit(1)


def extract_interfaces(config: dict[str, Any]) -> list[InterfaceInfo]:
    """从 netplan 配置中提取所有以太网接口信息"""
    ethernets = config.get("network", {}).get("ethernets", {})
    if not ethernets:
        Log.err("配置文件中未找到任何以太网接口 (network.ethernets)")
        sys.exit(1)

    interfaces: list[InterfaceInfo] = []

    for iface_name, iface_cfg in ethernets.items():
        info = InterfaceInfo(name=iface_name)

        # 提取地址 → 计算子网
        for addr_entry in iface_cfg.get("addresses", []):
            addr_str = (
                addr_entry
                if isinstance(addr_entry, str)
                else addr_entry.get("address", "")
            )
            if not addr_str:
                continue
            try:
                net = ipaddress.ip_interface(addr_str)
                target = info.ipv4_subnets if net.version == 4 else info.ipv6_subnets
                target.append(str(net.network))
            except ValueError:
                Log.warn(f"  跳过无法解析的地址: {addr_str}")

        # 提取网关（从 routes 字段）
        for route in iface_cfg.get("routes", []):
            to  = str(route.get("to", ""))
            via = str(route.get("via", ""))
            if not via:
                continue
            if to in ("default", "0.0.0.0/0"):
                info.ipv4_gateways.append(via)
            elif to == "::/0":
                info.ipv6_gateways.append(via)

        # 兼容旧式 gateway4 / gateway6（已弃用但仍可能存在）
        for attr, lst in [
            ("gateway4", info.ipv4_gateways),
            ("gateway6", info.ipv6_gateways),
        ]:
            gw = iface_cfg.get(attr)
            if gw and str(gw) not in lst:
                lst.append(str(gw))

        interfaces.append(info)

    return interfaces

# ── Metric 分配 ───────────────────────────────────────────────────────────────

def assign_metrics(count: int) -> list[int]:
    """
    为 n 个接口分配 metric 值。
    排在后面的接口优先级越高（metric 越小）。
    两个接口时：第一个 50，最后一个 25。
    """
    if count == 1:
        return [100]
    return [50 - (50 - 25) * i // (count - 1) for i in range(count)]

# ── 生成配置 ──────────────────────────────────────────────────────────────────

def generate_ethernet_config(
    original: dict[str, Any],
    routed: list[InterfaceInfo],
    metrics: list[int],
) -> dict[str, Any]:
    """深拷贝原始配置，为所有默认路由注入 metric，并将旧式 gateway4/gateway6 转为 routes"""
    config   = copy.deepcopy(original)
    name_idx = {iface.name: i for i, iface in enumerate(routed)}

    for iface_name, iface_cfg in config.get("network", {}).get("ethernets", {}).items():
        if iface_name not in name_idx:
            continue
        metric = metrics[name_idx[iface_name]]

        # 禁用 RA，防止路由器通告覆盖策略路由
        iface_cfg["accept-ra"] = False

        # 为现有默认路由添加 metric
        for route in iface_cfg.get("routes", []):
            if str(route.get("to", "")) in ("default", "0.0.0.0/0", "::/0"):
                route["metric"] = metric

        # 将旧式 gateway4 转为带 metric 的 default route
        gw4 = iface_cfg.pop("gateway4", None)
        if gw4:
            iface_cfg.setdefault("routes", []).append(
                {"to": "default", "via": str(gw4), "metric": metric}
            )

        # 将旧式 gateway6 转为带 metric 的 default route
        gw6 = iface_cfg.pop("gateway6", None)
        if gw6:
            iface_cfg.setdefault("routes", []).append(
                {"to": "::/0", "via": str(gw6), "metric": metric}
            )

    return config


def generate_pbr_config(routed: list[InterfaceInfo]) -> dict[str, Any]:
    """为每个接口生成独立路由表和源地址策略路由"""
    pbr_ethernets: dict[str, Any] = {}
    table_id = 10

    for iface in routed:
        routes: list[dict[str, Any]] = []
        policy: list[dict[str, Any]] = []

        # IPv4
        for gw in iface.ipv4_gateways:
            routes.append({"to": "default", "via": gw, "table": table_id})
        for subnet in iface.ipv4_subnets:
            policy.append({"from": subnet, "table": table_id})

        # IPv6
        for gw in iface.ipv6_gateways:
            routes.append({"to": "::/0", "via": gw, "table": table_id})
        for subnet in iface.ipv6_subnets:
            policy.append({"from": subnet, "table": table_id})

        if routes:
            pbr_ethernets[iface.name] = {
                "routes": routes,
                "routing-policy": policy,
            }

        table_id += 10

    return {"network": {"version": 2, "ethernets": pbr_ethernets}}

# ── YAML 输出 ─────────────────────────────────────────────────────────────────

def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def write_config(path: Path, data: dict[str, Any], description: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f"# {path.name}\n"
        f"# {description}\n"
        f"# 由 setup-pbr.py 自动生成 · {now}\n"
    )
    path.write_text(header + dump_yaml(data), encoding="utf-8")

# ── 备份与回滚 ────────────────────────────────────────────────────────────────

def backup_cloud_init(src: Path) -> Path:
    """备份并禁用原始 cloud-init 配置，返回备份路径"""
    bak = src.with_suffix(".yaml.bak")
    if not bak.exists():
        shutil.copy2(src, bak)
        Log.ok(f"已备份: {src} -> {bak}")
    # 重命名使 Netplan 不再加载
    if src.exists() and src.suffix == ".yaml":
        src.rename(bak)
        Log.ok(f"已禁用原始配置: {src} -> {bak}")
    return bak

# ── 应用配置 ──────────────────────────────────────────────────────────────────

def apply_netplan() -> bool:
    """执行 netplan try，返回是否成功"""
    Log.info("正在执行 netplan try（120 秒内未确认将自动回滚）...")
    result = subprocess.run(["netplan", "try"], check=False)
    return result.returncode == 0

# ── 打印辅助 ──────────────────────────────────────────────────────────────────

def print_file(path: Path) -> None:
    Log.info(f"===== {path.name} =====")
    print(path.read_text(encoding="utf-8"))


def print_verification() -> None:
    print()
    Log.info("验证命令：")
    cmds = [
        ("ip route show",              "IPv4 主路由表"),
        ("ip -6 route show",           "IPv6 主路由表"),
        ("ip rule show",               "IPv4 策略路由规则"),
        ("ip -6 rule show",            "IPv6 策略路由规则"),
        ("ip route show table 10",     "自定义路由表 10（IPv4）"),
        ("ip -6 route show table 10",  "自定义路由表 10（IPv6）"),
    ]
    for cmd, desc in cmds:
        print(f"    {cmd:<35s}# {desc}")


def print_rollback(cloud_init: Path, ethernet: Path, pbr: Path) -> None:
    bak = cloud_init.with_suffix(".yaml.bak")
    print()
    Log.info("如需回滚：")
    print(f"    sudo mv {bak} {cloud_init}")
    print(f"    sudo rm -f {ethernet} {pbr}")
    print(f"    sudo netplan apply")

# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="多网卡 / 多出口 VM 一键策略路由配置（Netplan PBR）",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="/etc/netplan/50-cloud-init.yaml",
        help="cloud-init 生成的 netplan 配置文件路径（默认 /etc/netplan/50-cloud-init.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览生成的配置，不写入文件、不应用",
    )
    args = parser.parse_args()

    cloud_init_path = Path(args.config)
    netplan_dir     = cloud_init_path.parent
    ethernet_path   = netplan_dir / "60-ethernet.yaml"
    pbr_path        = netplan_dir / "90-pbr.yaml"
    dry_run: bool   = args.dry_run

    # ── 前置检查 ──
    if not dry_run and os.geteuid() != 0:
        Log.err("请使用 root 或 sudo 执行此脚本")
        sys.exit(1)

    if not dry_run and not shutil.which("netplan"):
        Log.err("未找到 netplan，此脚本仅适用于使用 Netplan 的系统")
        sys.exit(1)

    if not cloud_init_path.is_file():
        Log.err(f"找不到配置文件: {cloud_init_path}")
        sys.exit(1)

    Log.info(f"源配置文件: {cloud_init_path}")
    if dry_run:
        Log.warn("Dry-run 模式：仅预览，不写入文件")

    # ── 解析 ──
    config     = parse_netplan(cloud_init_path)
    interfaces = extract_interfaces(config)
    routed     = [i for i in interfaces if i.has_default_route]

    if len(routed) < 2:
        Log.warn(
            f"仅发现 {len(routed)} 个带默认路由的接口，策略路由通常需要 >= 2 个"
        )
        if len(routed) == 0:
            Log.err("没有可配置的接口，退出")
            sys.exit(1)

    metrics = assign_metrics(len(routed))

    print()
    Log.info(f"发现 {len(routed)} 个带默认路由的接口：")
    for i, iface in enumerate(routed):
        print(f"         - {iface.summary()}  metric={metrics[i]}")

    # ── 生成配置 ──
    ethernet_data = generate_ethernet_config(config, routed, metrics)
    pbr_data      = generate_pbr_config(routed)

    # ── Dry-run：打印后退出 ──
    if dry_run:
        print()
        Log.info(f"===== {ethernet_path.name}（预览） =====")
        print(dump_yaml(ethernet_data))
        Log.info(f"===== {pbr_path.name}（预览） =====")
        print(dump_yaml(pbr_data))
        print_verification()
        return

    # ── 写入文件 ──
    backup_cloud_init(cloud_init_path)

    write_config(ethernet_path, ethernet_data, "基于 cloud-init 配置添加 metric")
    Log.ok(f"已生成: {ethernet_path}")

    write_config(pbr_path, pbr_data, "策略路由（PBR）配置")
    Log.ok(f"已生成: {pbr_path}")

    # ── 展示配置 ──
    print()
    print_file(ethernet_path)
    print_file(pbr_path)

    # ── 应用 ──
    Log.warn("如果您通过 SSH 连接，请确保有备用访问方式（如控制台）")
    print()
    try:
        confirm = input(f"{Log.YELLOW}是否立即应用？[y/N]: {Log.NC}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"
        print()

    if confirm == "y":
        if apply_netplan():
            Log.ok("配置已成功应用！")
        else:
            Log.err("netplan try 失败或已回滚，请检查配置文件")
            sys.exit(1)
    else:
        Log.info("已跳过应用，你可以稍后手动执行:")
        print("    sudo netplan try")

    # ── 后续提示 ──
    print_verification()
    print_rollback(cloud_init_path, ethernet_path, pbr_path)


if __name__ == "__main__":
    main()