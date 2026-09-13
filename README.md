# FBA 发票拆分工具（FIS）

把一份 FBA 清关发票按货件拆开，生成一个可直接上传物流商下单系统的 Excel 工作簿：每个货件一个 Sheet，末页附渠道与 FBA 编码参考页。

全部处理在本机完成，不上传任何数据。

---

## 使用（Windows 便携版）

不需要安装 Python，机器上装没装 Python 都不影响。

### 1. 取得程序

目前仓库是私有的，需要先获得仓库访问权限，然后从 GitHub Actions 下载：

进入 [Actions](https://github.com/Patrick-Zuo-hub/fba-invoice-splitter/actions) → 点最近一次绿色的 CI → 页面底部 **Artifacts** → 下载 `FIS-windows-portable`。

> Actions 的构建产物 **14 天后过期**。需要长期或对外分发时应改用 Release，见下方「分发」。

### 2. 解压

把 `FIS-windows-portable.zip` 解压到一个**你有写入权限**的位置：桌面、`D:\`、或用户目录下的任意文件夹。

⚠️ **不要放在 `C:\Program Files\` 下** —— 那里写不进数据，程序会拒绝启动并提示你换个位置。

### 3. 首次运行

双击 `FIS.exe`。

程序未做代码签名，Windows 会弹出 SmartScreen 蓝色警告。点 **更多信息 → 仍要运行**。此后不再提示。

浏览器会自动打开工具页面。命令行窗口需要保持开着，关掉它就等于关掉程序。

### 4. 导入主数据（首次必做）

点右上角 **主数据** → 选择主数据表格 → **导入**。

表格需要包含三个工作表：

| Sheet 名称 | 列 |
| --- | --- |
| `FBA仓代码&详细地址` | A=仓代码，B=详细地址 |
| `渠道` | A=渠道名 |
| `国内交货仓` | A=仓库名 |

导入规则：**以表格为准覆盖**已有条目；表格里没有的条目**保留不动**（历史记录按名称引用它们）；整份表格在一个事务里写入，任何一行出错则整体回滚。

导入后会显示每类的新增与更新条数。

### 5. 处理发票

1. **上传并解析** 一份 `.xlsx` 清关发票，系统识别出全部货件
2. 从 Excel **粘贴两列**（仓库代码在前、货件单号在后）完成货件映射，地址会自动带出
3. 补齐运输渠道、提货日期、开船日期、交仓仓库；支持批量填充
4. **选择输出文件夹**
5. **生成工作簿**

输出为单个文件 `已处理_<原文件名>.xlsx`，每个货件一个 Sheet，末页是渠道参考页。

### 6. 退出

关闭浏览器页面，约 30 秒后程序自动退出。也可以直接关掉命令行窗口。

### 数据存放在哪

| 内容 | 位置 | 换机器时 |
| --- | --- | --- |
| 主数据、历史记录（`fis.db`） | 程序文件夹旁的 `data\` | **跟着文件夹走** |
| 上传的发票副本 | `%LOCALAPPDATA%\FBA-Invoice-Splitter\` | 不跟随，每次启动自动清理 |
| 生成的工作簿 | 你选的输出文件夹 | 不受影响 |

换电脑时**整个程序文件夹拷过去**即可，主数据和历史记录一并带走。升级时替换 `FIS.exe` 和 `_internal\`，保留 `data\`。

---

## 开发

需要 Python 3.12。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

启动：macOS 双击 `启动 FIS.command`，Windows 双击 `启动 FIS.bat`，或直接：

```bash
PYTHONPATH=src .venv/bin/python -m fis
```

> 这两个双击文件**只是省去敲命令**，它们走项目里的 `.venv`，需要完整开发环境。真正不依赖 Python 的是便携版 `FIS.exe`。

开发模式下数据在 `~/.fba-invoice-splitter`，可用 `FIS_DATA_ROOT` 覆盖。

### 检查

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src/fis --platform darwin
.venv/bin/mypy src/fis --platform win32
```

类型检查要**按平台各跑一次**：mypy 会按 `sys.platform` 收窄分支，单次运行只检查该平台能到达的那一半代码。

CI 在 `windows-latest` 与 `macos-latest` 上运行全部检查，并构建 Windows 便携版。Windows 是这套 CI 存在的主要理由——命名互斥锁、目录联接、目录句柄等用例在开发机上全部跳过，只有那里会真正执行。

---

## 分发

**当前尚不能对外分发**，有两个前提未满足：

1. **冷机验收未完成。** CI 能证明构建成功，但证明不了在一台没装 Python 的 Windows 上可用。验收清单见 [docs/superpowers/specs/2026-09-13-windows-portable-release-design.md](docs/superpowers/specs/2026-09-13-windows-portable-release-design.md)，其中最关键的一条是：Excel 打开生成结果**不出现修复提示**。
2. **没有 Release。** Actions 产物 14 天过期且需要登录后在工作流页面里翻找。要给他人使用，应打 tag 发布 Release 并附上 zip，得到稳定链接。

仓库私有，因此无论哪种方式，使用者都需要先获得仓库访问权限。

---

## 文档

| 路径 | 内容 |
| --- | --- |
| `docs/superpowers/specs/` | 各功能的设计文档 |
| `docs/superpowers/plans/` | 实施计划 |
| `docs/decisions/` | 架构决策记录 |
| `AGENTS.md` | 仓库开发规则 |

产品基线见 [docs/prd-baseline.md](docs/prd-baseline.md)，需求本身在独立的私有仓库 `waymerce-prds`。
