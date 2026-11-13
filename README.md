# FBA 发票拆分工具（FIS）

处理包含多个 FBA 货件的发票并补充物流信息。旧版模板输出每货件一个 Sheet；赤道 2.0 模板将全部明细输出到同一个“模版”Sheet。

全部处理在本机完成，不上传任何数据。

## 赤道 2.0 新版模板

上传 50 列、表头位于第 4 行的新版领星发票后，自动进入赤道 2.0 编辑页。主数据已从官方模板的“产品库”“FBA地址库”“base”内置：455 条渠道、1,204 条仓库地址、8 个国内收货仓，不需要先导入旧版主数据。

1. 粘贴两列映射：仓库代码在前、FBA 货件号在后。逗号拼接的仓库列表不能代替逐货件映射。
2. 按名称或代码搜索物流渠道，选择后自动带出对应编码，并按该 FBA 仓库立即记住最近选择。
3. 选择国内收货仓；预计到仓时间优先带出源文件值，否则使用下个周四 00:00，可逐票或批量修改。勾选货件时批量仅作用于所选行。
4. 客户编码默认“深圳博致咨询有限公司”，可修改；点击输出预览核对每条明细。
5. 生成一个“已处理_原文件名.xlsx”工作簿，全部业务明细在“模版”Sheet，后三页保留参考字典。同名自动加序号，不覆盖原文件。

申报币别 AN 根据地址库的国家自动填写：US→USD、GB→GBP、CA→CAD、欧元区→EUR。其他国家或空国家提示“币种待配置”并阻止生成。只更新币种标签，不进行汇率换算，原单价和货值数值不变。

客户单号 B 列逐行填写所属 FBA 主单号，同一货件的多条明细重复填写该货件号，不沿用源文件中拼接的多个 FBA 编号。12 位 FBA 主单及锚定图片是否可实际导入，需要在赤道试传确认。新版字典与旧版主数据隔离，更新内置字典时应从新版官方模板重新提取。

开发者若已安装 Chrome，可用 `FIS_TEST_BROWSER_CHANNEL=chrome` 运行页面测试，CI 默认使用 Playwright 配套浏览器。

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

赤道 2.0 适配以 **GitHub 预发布版**供内部试用，下载并完整解压发布附件后双击 `FIS.exe`。预发布不代表已完成 Windows 冷机或赤道建单验收。

1. **冷机验收未完成。** CI 能证明构建成功，但证明不了在一台没装 Python 的 Windows 上可用。验收清单见 [docs/superpowers/specs/2026-09-13-windows-portable-release-design.md](docs/superpowers/specs/2026-09-13-windows-portable-release-design.md)，其中最关键的一条是：Excel 打开生成结果**不出现修复提示**。
2. **稳定版发布前仍需业务实测。** 先验证赤道实际上传、图片、逐货件客户单号和订单分组，再转为稳定版。Actions 产物 14 天过期；预发布附件提供较长期的下载入口。

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
