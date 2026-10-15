# PR：实验性 Android TMP 字体资源覆盖工具

维护者：李伟

**此方案未经真机测试，需由维护者连接手机后，执行推送脚本并在游戏中实测验证后再合并。**

此修复未经过真机测试，需用 adb 部署资源、验证渲染效果后再 Squash and merge。这里的部署是覆盖原有外部资源，不是安装或重新签名 APK。

本 PR 新增独立字体工具，不修改 SQLCipher、`patch_db.py`、`patch_catalog.py` 或一键汉化入口。它提供 APK/资源扫描、Unity 字体构建、UnityPy 原包改写、重新解包校验、ADB 部署和恢复。当前结果是待验证的工具链，不能标记为 Android 字体问题已经解决。

## 必须先确认的条件

- 游戏确实使用 TMP，并且原字体来自一个可被覆盖、实际被客户端引用的外部 UnityFS bundle。APK 内的资源名不等于外部文件名，更不能证明缓存优先级。APK 可能根本不含后下载的字体，应扫描从设备拉取的 PUB 资源。
- 取得**当前版本原始 bundle**、其真实设备路径及字体授权允许使用的静态 TTF/OTF。不要猜 `UnityCache/Shared` 路径，不要凭空创建一个同名字体文件等待游戏发现。
- UnityPy 不负责从 TTF 烘焙 TMP 图集。自动构建需要与原包完全一致的 Unity Editor、匹配的 TMP 3.x、Android Build Support，以及已导入 TMP Essential Resources 的**临时 Unity 项目**。该项目不要同时在 Editor 打开；切换 Android 会更新其 Library/ProjectSettings。
- 当前只支持带完整 type tree、嵌入源字体、单初始图集的 TMP 字体资产。原包与 donor 的 Unity 版本及各对象序列化结构必须一致。仅支持两个官方 SDF shader，且原 TMP shader 必须在同包内；不支持旧版 TMP、缺失依赖、非 TMP 的 Unity UI.Text 或自定义 shader。
- 若原资源清单记录 bundle 的 CRC、Hash、Size 或签名，必须先识别并处理**真实资源清单**。本工具不会用 `TableCatalog.bytes` 的数据库 CRC 逻辑代替字体资源校验，也不会绕过签名。清单/缓存索引不兼容时，文件推送成功也可能被拒绝或重新下载。

不 Root、不修改 APK 签名不代表修改客户端资源没有账号风险；工具不承诺防封号。未确定实际加载入口时，仅使用本地扫描功能。

## 本地准备与资源名提取

在仓库根目录运行（以下为 PowerShell；其他系统替换路径即可）：

```powershell
python -m pip install -r tools/requirements-font.txt
python tools/push_font.py scan --input work/game.apk --extract-dir work/font-scan
python tools/push_font.py scan --input work/pulled-PUB --extract-dir work/font-scan
```

JSON 列出 bundle 内部名、container 路径、Unity Font 名、TMP 资源名、家族名、PathID、Unity 版本、平台、原始 SHA256 和扫描失败项。找到字体的 UnityFS 原包会按 SHA256 分目录提取，保留外层文件名。未发现 TMP 或 type tree 读取失败，不等于证明游戏没有 TMP；不得忽略错误直接部署。

维护者应记录游戏实际访问的外部文件路径及对应 catalog 条目；扫描结果无法单独证明加载路径。找不到可替换的外部字体资源时，本方案不可落地，停止在扫描阶段。

## 构建与静态校验

将占位路径替换为扫描出的原包、匹配 Editor、临时 Unity 项目和已授权字体。`--characters` 是 UTF-8 最终简体文本语料；未传入时只检查少量探针字，不能宣称完整中文覆盖。

```powershell
python tools/push_font.py build --template work/original/actual.bundle --font work/SourceHanSansSC-Regular.otf --unity 'C:/Unity/Editor/Unity.exe' --unity-project work/FontBuildProject --characters work/required-characters.txt --out-dir work/font-output
python tools/push_font.py verify --bundle work/font-output/actual.bundle --manifest work/font-output/actual.bundle.font.json
python -m unittest discover -s tools -p 'test*font*.py' -v
```

也可使用 Unity 已构建的兼容 Android donor：用 `--donor work/donor.bundle` 替代 `--unity` 与 `--unity-project`。donor 必须嵌入与 `--font` 完全一致的源字体。存在多个 TMP 时必须用扫描出的 `--target-id`、`--donor-id` 指定对象。输出目录须全新，避免将旧文件误认为本次成功结果。

自动构建的 donor、TMP 包版本信息和 Unity 日志保留在输出目录的 `.ba-font-donor-*` 子目录，构建失败时也保留，便于审查。

构建保留原 bundle 文件名、内部目录和对象 PathID；移植完整字形表、FaceInfo、材质及图集，并保留原脚本、shader 与资源名称哈希。包内 `.resS` 像素会读取原始格式后内联，不从未知外部路径寻找资源。其他对象原始字节必须保持一致。输出包会立即重新解包；验证失败不写入结果。

校验打印 TMP 名、家族名、Android 平台、源字体 SHA256、指定语料覆盖数与 fallback 列表。预期**本地 fallback 列表为空**：这是让被替换的主字体覆盖中文，而非把新字体追加到旧主字体之后。它不修改游戏的 `TMP_Settings` 全局 fallback、其他组件的主字体、字号或材质覆盖；缺字仍可能触发全局回退。静态校验不证明 Unity 能加载、动态图集能在目标 GPU 上生成或所有游戏界面使用该资产。

## 真机验证与恢复

维护者已有官方客户端及对应游戏资源；不使用本工具安装 APK。首先确认设备路径和资源清单条件，再预览部署计划：

```powershell
python tools/push_font.py push --bundle work/font-output/actual.bundle --manifest work/font-output/actual.bundle.font.json --remote /sdcard/Android/data/com.nexon.bluearchive/files/REAL/PATH/actual.bundle
```

默认不调用 ADB，不改动手机。确认目标路径后显式指定设备部署：

```powershell
adb devices
python tools/push_font.py push --bundle work/font-output/actual.bundle --manifest work/font-output/actual.bundle.font.json --serial DEVICE_SERIAL --remote /sdcard/Android/data/com.nexon.bluearchive/files/REAL/PATH/actual.bundle --apply
```

部署检查 shell UID 为 2000、设备原文件 SHA256 与模板一致，停止游戏，将原文件备份到 `work/font-backups`，同目录临时上传并验证 SHA256，再核对原文件并重命名。所有 ADB 命令绑定指定设备；不调用 `adb root`、`su`、`run-as`，不自动启动游戏。权限不足或设备资源已更新时会失败，不尝试绕过访问控制。

真机必须核对：实际加载了替换后的资产；没有因资源校验被恢复；菜单、剧情、学生名、弹窗和长段落中繁简同形字与简体独有字的大小、字重、基线、换行、描边均正常；粗体、斜体、不同字号仍正常；冷启动、重启和动态多图集扩展后无缺字、粉字、白块、崩溃或明显卡顿。记录客户端版本、Unity/TMP 版本、Android/机型、原路径及前后截图，再 Squash and merge。

若需恢复，用部署 JSON 返回的 `backup_path` 和 `replacement_sha256`：

```powershell
python tools/push_font.py restore --backup work/font-backups/EXACT_BACKUP.backup --current-sha256 DEPLOYED_BUNDLE_SHA256 --serial DEVICE_SERIAL --remote /sdcard/Android/data/com.nexon.bluearchive/files/REAL/PATH/actual.bundle --apply
```

恢复要求保留备份旁的 `.backup.json` 元数据，并核对备份哈希、原设备、包名、目标路径和当前已部署哈希。恢复同样先备份当前文件、校验其已知哈希并原子替换，不覆盖旧备份。ADB 断连或最终校验失败时，先读取设备当前文件 SHA256，再决定重试或恢复。

## 验证记录

- 已执行：本地 Python 语法/CLI、22 项离线测试通过，包括合成 UnityFS 二进制往返、字体覆盖、引用映射、资源校验和模拟 ADB 部署。测试使用自编 TMP/字形数据，不能代替 Unity 渲染验证。
- 未执行：Unity Editor/C# 实际编译、真实游戏 bundle 烘焙及重新解包、Android 设备部署和游戏渲染。当前工作区没有原游戏资源、目标字体、匹配 Editor 或 Android 设备。

参考：[Unity AssetBundle 构建](https://docs.unity3d.com/ScriptReference/BuildPipeline.BuildAssetBundles.html)、[UnityPy](https://github.com/K0lb3/UnityPy)、[TMP fallback 顺序](https://docs.unity.cn/Packages/com.unity.textmeshpro@3.2/manual/FontAssetsFallback.html)。
