# Windows 便携版设计

## 目标

产出一个 Windows 文件夹，拷到任意一台机器双击即可运行：不需要安装 Python、不需要源码、不需要联网。

本设计**作废** `docs/superpowers/plans/2026-09-07-fis-v1-portable-release.md`。那份计划写于代码重组之前，它列出的 10 个文件路径（`src/fis/launcher.py`、`src/fis/platform.py`、`src/fis/web/routes/system.py` 等）**没有一个存在**，照做只会做出错的东西。

## 范围

**仅 Windows。** macOS 侧用现有的 `启动 FIS.command` 即可，且未公证的 `.app` 在他人的 Mac 上会被 Gatekeeper 拦住，做了也分发不出去。

## 产物形态

PyInstaller **one-folder**（非单文件）：

```
FIS/
├── FIS.exe
├── _internal/            ← Python 运行时与依赖
└── data/                 ← 用户数据，见下
```

不用 one-file：单文件每次启动都要把几十兆解压到临时目录，拖慢启动，而且解压出的临时路径会让单实例锁与运行目录校验失去意义。

预估体积 60–80 MB，大头是 `lxml`(18M) 与 `Pillow`(14M)。

## 数据分层

这是本设计的核心决定。数据按**是否应当跟着程序走**分为两处：

| 内容 | 位置 | 理由 |
| --- | --- | --- |
| `fis.db` | `<程序文件夹>/data/` | 主数据与历史记录，换机器要带走 |
| `runtime/` | `<程序文件夹>/data/` | 单实例锁以数据目录为身份，必须与 `fis.db` 同源 |
| `batches/` | `%LOCALAPPDATA%\FBA-Invoice-Splitter\` | 上传源文件副本，本机临时产物，不跟着走 |

`runtime/` 必须留在 `data/` 旁：`runtime_identity` 用运行目录路径的摘要区分实例，若挪到机器级目录，两份各带各自 `data/` 的便携副本会互相误认为同一实例——正是刚修掉的那个缺陷。

`batches/` 分出去有两个实际好处：拷贝便携文件夹时不会莫名其妙拖着几十甚至几百兆的历史源文件；程序文件夹放在只读介质上时也只有 `data/` 需要可写。

### 配置形状

`Settings` 增加一个可选的 `work_root`，缺省回落到 `data_root`，现有调用与全部测试不受影响：

```python
@dataclass(frozen=True)
class Settings:
    data_root: Path
    work_root: Path | None = None   # None 表示与 data_root 同处
```

`BatchFiles` 改为接收 `settings.work_root`。它对该根目录的安全校验（句柄链、路径校验）与目录位置无关，语义不变。

### 运行时如何定位

冻结运行时（`sys.frozen`）：`data_root = Path(sys.executable).parent / "data"`，`work_root = %LOCALAPPDATA%\FBA-Invoice-Splitter`。

开发运行时：维持现状，`~/.fba-invoice-splitter`，`work_root` 与之同处。

`FIS_DATA_ROOT` 环境变量继续覆盖 `data_root`，测试与排查都依赖它。

### 写不进去怎么办

程序文件夹放在 `C:\Program Files\` 下时 `data/` 不可写。启动时即检测并给出明确中文提示，指出应把文件夹放在桌面、`D:\` 或用户目录下——**不**静默回落到用户目录，否则用户会以为数据在程序旁，换机器时只拷了文件夹却丢了数据。

## 资源收集

三处路径在冻结后必须仍然解析得到，它们是打包最容易出错的地方：

| 用途 | 代码位置 | 收集方式 |
| --- | --- | --- |
| Jinja 模板 | `app.py` 的 `_WEB_ROOT / "templates"` | `datas` 收进 `fis/web/templates` |
| 静态资源 | `app.py` 的 `_WEB_ROOT / "static"` | `datas` 收进 `fis/web/static` |
| 渠道参考页 | `importlib.resources.files("fis.xlsx.assets")` | `datas` 收进 `fis/xlsx/assets` |

`_WEB_ROOT` 取自 `Path(__file__).resolve().parent`，冻结后指向 `_internal/fis/web`，因此收集的相对布局必须与源码一致。

## 构建与验收

- `packaging/fis.spec`：one-folder 定义，显式列出上述 `datas`。
- `scripts/build_windows.ps1`：可重复构建。
- CI 增加一个 `windows-latest` 任务，构建并上传 artifact；构建前先跑现有测试。

**不做代码签名。** 未签名的 exe 在 Windows 上会触发 SmartScreen 警告，首次运行需要点「更多信息 → 仍要运行」。签名需要证书，属于采购问题而非工程问题；分发说明里写清楚即可。

### 冷机验收清单

必须在一台**没有装 Python** 的 Windows 上完成，这是唯一能证明打包成立的检验：

1. 双击 `FIS.exe`，浏览器自动打开本机页面。
2. 主数据抽屉上传主数据表格，条数正确。
3. 上传真实清关单，填写并生成，输出为单个工作簿且末页为渠道参考页。
4. Excel 打开生成结果，**不出现修复提示**。
5. 关闭页面，30 秒后进程自动退出。
6. 把整个文件夹拷到另一台机器，主数据与历史记录仍在。

## 验证

自动化能覆盖的：配置分层的默认与覆盖、冻结路径的定位函数、`data/` 不可写时的报错。

自动化**不能**覆盖的：PyInstaller 产物在冷机上能否运行。本项目的开发机是 macOS，无法构建或运行 Windows 产物，CI 也只能证明构建成功、不能证明冷机可用。因此上面的冷机验收必须人工完成，在此之前不能称此功能已交付。
