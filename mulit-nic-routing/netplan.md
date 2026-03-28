# 多网卡 / 多出口 VM 网络配置指南

## 概述

对于多网卡、多出口的虚拟机，必须完成以下两项配置才能确保网络正常工作：

1. **为主路由表的默认路由添加 Metric**——控制本机发起流量的出口优先级
2. **开启策略路由（PBR）**——确保从某一接口进入的流量，其回程报文从同一接口返回

以下配置基于 **Debian 12 / Debian 13**，使用 Netplan 作为网络管理工具。

---

## 前提假设

| 接口 | IPv4 子网 | IPv4 网关 | IPv6 子网 | IPv6 网关 | 用途 |
|------|-----------|-----------|-----------|-----------|------|
| `eth0` | `10.10.0.0/16` | `10.10.0.1` | `fd10:10::/48` | `fd10:10::1` | 备选出口（metric 50） |
| `eth1` | `10.20.0.0/16` | `10.20.0.1` | `fd10:20::/48` | `fd10:20::1` | 优先出口（metric 25） |

> 请根据实际环境替换上述 IP 地址和子网。示例中的 IPv6 使用 ULA（`fd00::/8`）地址，实际环境中请替换为分配到的 GUA 或 ULA 地址。

---

## 步骤一：为主路由表的默认路由添加 Metric

cloud-init 生成的默认配置不包含 metric 值，会导致多条等价默认路由冲突。需要复制并修改该配置文件，为每个出口设置不同的 metric。

### 1. 复制并备份 cloud-init 配置

```bash
# 复制 cloud-init 生成的网络配置文件，以进一步修改
sudo cp /etc/netplan/50-cloud-init.yaml /etc/netplan/60-ethernet.yaml

# 备份原文件（重命名使其不再被 Netplan 加载）
sudo mv /etc/netplan/50-cloud-init.yaml /etc/netplan/50-cloud-init.yaml.bak
```

### 2. 编辑 `60-ethernet.yaml`

在 `eth0` 和 `eth1` 的默认路由中分别添加 `metric` 字段：

```diff
--- 50-cloud-init.yaml
+++ 60-ethernet.yaml
@@ eth0 默认路由 @@
       routes:
       - to: "default"
         via: "10.10.0.1"
+        # 设置 metric=50，作为备选出口
+        metric: 50
+      - to: "::/0"
+        via: "fd10:10::1"
+        # IPv6 默认路由同样设置 metric=50
+        metric: 50

@@ eth1 默认路由 @@
       routes:
       - to: "default"
         via: "10.20.0.1"
+        # 设置 metric=25，作为优先出口（本机发起流量优先走此路径）
+        metric: 25
+      - to: "::/0"
+        via: "fd10:20::1"
+        # IPv6 默认路由同样设置 metric=25
+        metric: 25
```

> **说明：** metric 值越小，优先级越高。上述配置使本机主动发起的流量优先从 `eth1`（metric 25）出站。

---

## 步骤二：添加策略路由（PBR）配置

创建独立的 PBR 配置文件，为每个接口建立专属路由表，并通过源地址匹配规则将回程流量导向正确的出口。

### 创建 `/etc/netplan/90-pbr.yaml`

```yaml
# /etc/netplan/90-pbr.yaml
network:
  version: 2
  ethernets:
    eth0:
      routes:
        # IPv4：在路由表 10 中添加默认路由
        - to: default
          via: 10.10.0.1
          table: 10
        # IPv6：在路由表 10 中添加默认路由
        - to: "::/0"
          via: "fd10:10::1"
          table: 10
      routing-policy:
        # IPv4：源地址为 10.10.0.0/16 的报文转发到路由表 10
        - from: 10.10.0.0/16
          table: 10
        # IPv6：源地址为 fd10:10::/48 的报文转发到路由表 10
        - from: "fd10:10::/48"
          table: 10

    eth1:
      routes:
        # IPv4：在路由表 20 中添加默认路由
        - to: default
          via: 10.20.0.1
          table: 20
        # IPv6：在路由表 20 中添加默认路由
        - to: "::/0"
          via: "fd10:20::1"
          table: 20
      routing-policy:
        # IPv4：源地址为 10.20.0.0/16 的报文转发到路由表 20
        - from: 10.20.0.0/16
          table: 20
        # IPv6：源地址为 fd10:20::/48 的报文转发到路由表 20
        - from: "fd10:20::/48"
          table: 20
```

---

## 步骤三：应用配置

```bash
sudo netplan try
```

`netplan try` 会临时应用配置并等待用户确认。如果配置导致网络中断，120 秒后将自动回滚，避免远程操作时失联。确认无误后按 **Enter** 永久生效。

---

## 验证

应用配置后，可通过以下命令检查路由表和策略路由是否生效：

```bash
# 查看 IPv4 主路由表（确认 metric 已设置）
ip route show

# 查看 IPv6 主路由表（确认 metric 已设置）
ip -6 route show

# 查看 IPv4 策略路由规则
ip rule show

# 查看 IPv6 策略路由规则
ip -6 rule show

# 查看自定义路由表内容（IPv4）
ip route show table 10
ip route show table 20

# 查看自定义路由表内容（IPv6）
ip -6 route show table 10
ip -6 route show table 20
```

### 预期结果

**IPv4：**

- `ip route show` 中应出现两条默认路由，分别带有 `metric 25` 和 `metric 50`
- `ip rule show` 中应出现 `from 10.10.0.0/16 lookup 10` 和 `from 10.20.0.0/16 lookup 20` 规则
- 各自定义路由表中应包含对应网关的默认路由

**IPv6：**

- `ip -6 route show` 中应出现两条 `::/0` 默认路由，分别带有 `metric 25` 和 `metric 50`
- `ip -6 rule show` 中应出现 `from fd10:10::/48 lookup 10` 和 `from fd10:20::/48 lookup 20` 规则
- 各自定义路由表中应包含对应 IPv6 网关的默认路由