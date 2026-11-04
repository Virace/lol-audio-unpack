# 控制台本地打包与验收

用户使用说明见 [控制台独立包](../cli-package.md)。

## 本地构建

开发机需要原生 Windows x64、项目支持的 x64 Python 和 uv，原生依赖必须具有匹配的 wheel。先在仓库根目录执行：

```powershell
uv run --no-sync python scripts/pyinstaller/build_cli.py
```

入口只用标准库，也可直接由已安装 Python 执行。它为每次构建创建 `.temp/cli-package/` 下独立目录，通过锁文件安装核心与 build 依赖，不修改仓库根 `.venv`，不安装 Qt。

默认产物为单文件 `dist/LolAudioUnpack-CLI.exe`；旁边有 `LolAudioUnpack-CLI.exe.sha256` 和 `CLI-README.md`，构建根目录有 `build.log`、`build.json`、依赖环境与 PyInstaller 分析记录。

控制台使用 `scripts/pyinstaller/assets/cli_icon.ico`，在界面图标的基础上增加终端角标。
同目录的 `cli_icon.png` 保留图标原图；ICO 包含 16、24、32、48、64、128、256 像素尺寸，
仅作为构建时的 EXE 图标资源，不引入界面运行依赖。

支持 `--output-root <新目录>`、`--mode onedir` 和无文件副作用的 `--dry-run`。已存在的输出根目录会被拒绝，不自动覆盖或清理历史产物。目录模式仅用于排障，最终仍需验收单文件。

构建目录默认保留，确认不再需要环境、日志和产物后由用户删除对应任务目录。

## 验收边界

构建成功不等于验收完成。应直接运行该份 EXE，核对版本、退出码、不同调用目录、中文/空格路径、INI、重定向、真实 update/extract/mapping/WAV 和取消行为，并检查构建归档不含 Qt。

在未安装 Python/Qt 的独立 Windows 环境运行后，才能确认免开发环境交付；清空 PATH 不替代此项。真实音频需独立解码核对或人工试听，历史样本与外部后端缺少条件时分别记录为未验证。

本地试用通过后再安排发布；本构建命令不修改 workflow、不上传文件，也不创建 Release。

## Release 附件

| Release 构建 | 规则 |
| --- | --- |
| 触发 | `.github/workflows/release-build.yml` 收到包含于 main 的提交的 tag 推送 |
| 版本 | 界面/控制台共用去掉 `v` 的 tag 版本，附件准备时核对 EXE 与 tag |
| 检查 | 离线与界面行为检查、控制台工具自检、两份 EXE 版本一致性检查通过后发布同一 Release |
| 控制台环境 | 仅安装核心和 build 依赖 |

附件包括：

- `LolAudioUnpack-<tag>-windows-x64.exe` 及对应 `.sha256`
- `LolAudioUnpack-CLI-<tag>-windows-x64.exe` 及对应 `.sha256`
- `LolAudioUnpack-CLI-README.md`

发布说明优先读取 `.github/release-notes/<tag>.md`，没有对应文件时自动生成。
本地执行构建命令不会触发发布。

构建因 runner 或发布流程问题中断时，可在 Actions 手动运行 `release-build`，填写已有 tag。
手动入口使用当前工作流，但校验并检出该 tag 的精确提交，只接受已包含于 `main` 的提交；
不会移动 tag，也不会把当前 `main` 的产品代码冒充该版本发布。

## Gitee 成品分发

GitHub Release 发布成功后，`release-build` 的独立 `sync-gitee` job 调用
`scripts/sync_gitee_release.py`，将实际发布的说明、预发布标记和全部附件同步到
[Gitee 分发仓库](https://gitee.com/x-item/lol-audio-unpack/releases)。不镜像源代码，
Gitee 标签只指向该仓库自身的 README 提交；README 模板保存在 `.github/gitee/README.md`。
网盘不进入自动化，由维护者后续手动上传。

GitHub 仓库需要 `GITEE_TOKEN` repository secret，Token 对目标 Gitee 仓库须有 Release
与附件的读写权限，以及 README 文件的更新权限。Token 仅通过环境变量和请求认证使用，不写入参数、文件或日志。
脚本只依赖 Python 标准库，以及 runner 已提供的 `gh` 和 `curl`。

同步 job 最长运行 45 分钟，单个附件上传最多等待 20 分钟，并设置 `continue-on-error: true`。它依赖 GitHub 发布成功，
文档部署独立依赖 `build-release`，因此 Gitee 失败不会阻塞 GitHub Release 或文档部署。
失败会在 Actions 日志与 job 摘要中留下警告；同一仓库的同步串行执行，避免不同版本同时更新 README。

README 的版本区域自动记录当前版本、GitHub 发布时间、已核对的附件数量与最近同步时间。
上传后通过公开下载地址核对 SHA-256，全部通过才显示“已同步”；失败时尽可能记录“同步未完成”。
正文和文档链接保留在状态区域之外。补传旧版本会保留 README 中发布时间较新的版本信息。

补传不必重新构建程序。在已有 `gh` GitHub 登录的环境执行：

```powershell
uv run --no-sync python scripts/sync_gitee_release.py --tag <已有标签> --token-file <Token文件> --dry-run
uv run --no-sync python scripts/sync_gitee_release.py --tag <已有标签> --token-file <Token文件>
```

只读预检不下载附件、不修改 Gitee。实际同步先校验 GitHub 下载的大小与可用的 SHA-256
摘要，已有 Gitee 同名附件会下载核对摘要，一致则跳过，缺失则补传；同名内容不同时停止，
保留现有文件供维护者处理。上传中断可直接重跑同一命令。
本地下载文件保留在 `.temp/.gitee-sync/<本次任务目录>/`，确认不再需要后手动清理对应目录。
