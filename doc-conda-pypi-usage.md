# Conda 与 PyPI 镜像使用说明

> 本站镜像地址：**https://mirrors.tibhpc.net/**
> 仅限所内网络访问。下文将系统默认源替换为本站镜像，可显著提升包下载速度。

---

## 一、Conda 镜像

本站镜像了 Conda 官方及常用第三方 channel，目录结构：

| channel | 路径 | 说明 |
|---|---|---|
| `main` | `/soft_mirrors/conda/pkgs/main/` | Conda 官方主仓库 |
| `r` | `/soft_mirrors/conda/pkgs/r/` | R 语言包 |
| `msys2` | `/soft_mirrors/conda/pkgs/msys2/` | MSYS2（Windows） |
| `conda-forge` | `/soft_mirrors/conda/cloud/conda-forge/` | 社区源（最常用） |
| `bioconda` | `/soft_mirrors/conda/cloud/bioconda/` | 生物信息包 |

> 完整 channel 列表见 <https://mirrors.tibhpc.net/soft_mirrors/conda/>

### 方式一：临时使用（仅本次命令）

```bash
conda install -c https://mirrors.tibhpc.net/soft_mirrors/conda/cloud/conda-forge/ <包名>
```

### 方式二：永久配置（推荐）

编辑 `~/.condarc`（不存在则新建），写入：

```yaml
channels:
  - defaults
default_channels:
  - https://mirrors.tibhpc.net/soft_mirrors/conda/pkgs/main/
  - https://mirrors.tibhpc.net/soft_mirrors/conda/pkgs/free/
  - https://mirrors.tibhpc.net/soft_mirrors/conda/pkgs/r/
  - https://mirrors.tibhpc.net/soft_mirrors/conda/pkgs/msys2/
custom_channels:
  conda-forge: https://mirrors.tibhpc.net/soft_mirrors/conda/cloud
  bioconda:    https://mirrors.tibhpc.net/soft_mirrors/conda/cloud
  pytorch:     https://mirrors.tibhpc.net/soft_mirrors/conda/cloud
# 关闭默认的 ssl 验证绕过（内网 https 证书如非可信可临时关，验证后建议开启）
ssl_verify: true
```

配置后即可正常使用：

```bash
conda install numpy                    # 自动走 main 镜像
conda install -c conda-forge scanpy    # 自动走 conda-forge 镜像
conda install -c bioconda samtools     # 自动走 bioconda 镜像
```

### 方式三：Miniconda 安装器

如需新装 Miniconda，安装脚本同样可走本站：

```bash
wget https://mirrors.tibhpc.net/soft_mirrors/conda/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh
```

---

## 二、PyPI 镜像

本站 PyPI 镜像兼容 PEP 503 标准 simple 索引，`pip` 可直接使用。

- Simple 索引：<https://mirrors.tibhpc.net/soft_mirrors/pypi/web/simple/>

### 方式一：临时使用（仅本次命令）

```bash
pip install -i https://mirrors.tibhpc.net/soft_mirrors/pypi/web/simple/ <包名>
```

### 方式二：永久配置（推荐）

写入 `pip` 配置文件（Linux/macOS：`~/.config/pip/pip.conf` 或 `~/.pip/pip.conf`；Windows：`%APPDATA%\pip\pip.ini`）：

```ini
[global]
index-url = https://mirrors.tibhpc.net/soft_mirrors/pypi/web/simple/
trusted-host = mirrors.tibhpc.net
```

> 若已配置可信证书，可省略 `trusted-host` 一行。

配置后直接使用：

```bash
pip install numpy
pip install -r requirements.txt
```

### 方式三：uv / poetry 等工具

```bash
# uv
uv pip install --index-url https://mirrors.tibhpc.net/soft_mirrors/pypi/web/simple/ <包名>

# poetry（在 pyproject.toml 中）
[[tool.poetry.source]]
name = "tibhpc"
url = "https://mirrors.tibhpc.net/soft_mirrors/pypi/web/simple/"
priority = "primary"
```

---

## 三、验证是否生效

```bash
# Conda：看 channel 是否指向本站
conda config --show channels

# pip：verbose 安装看下载来源
pip install -v requests 2>&1 | grep -i "looking in\|mirrors.tibhpc.net"
```

下载链接指向 `mirrors.tibhpc.net` 即表示已走本站镜像。

---

## 常见问题

**Q：报 SSL 证书错误？**
内网 https 若使用自签证书，Conda 设 `ssl_verify: false`、pip 加 `trusted-host = mirrors.tibhpc.net` 临时绕过。

**Q：找不到某个包？**
本站每 2 小时同步一次（conda：偶数小时 31 分；pypi：偶数小时 17 分）。新发布的包可能要等下一轮同步。访问 <https://mirrors.tibhpc.net/> 首页可查看各镜像最近同步时间。

**Q：Conda 仍走官方源很慢？**
确认 `~/.condarc` 中 `default_channels` 用的是本站完整 URL（带 `https://`），而非 channel 名。第三方 channel 必须配在 `custom_channels` 下，并用 `-c conda-forge` 形式引用。
