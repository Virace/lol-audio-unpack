<div align="center">

<img src="./src/lol_audio_unpack/gui/assets/app_icon.svg" alt="Lol Audio Unpack 程序图标" width="160">

# Lol Audio Unpack

[![最新发布版本](https://img.shields.io/github/v/release/Virace/lol-audio-unpack?style=flat&logo=github&label=release)](https://github.com/Virace/lol-audio-unpack/releases/latest)
[![GitHub 下载量](https://img.shields.io/github/downloads/Virace/lol-audio-unpack/total?style=flat&logo=github&label=downloads)](https://github.com/Virace/lol-audio-unpack/releases)
[![Python 3.10 或更新版本](https://img.shields.io/badge/python-%3E%3D3.10-3776AB?style=flat&logo=python&logoColor=white)](#从仓库运行)
[![GPLv3 许可证](https://img.shields.io/badge/license-GPLv3-blue?style=flat)](LICENSE)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Virace/lol_extract_voice)

<!-- project-summary -->
提取、查找、试听并导出英雄联盟语音和音效的桌面工具。
<!-- /project-summary -->

</div>

[GitHub 下载](https://github.com/Virace/lol-audio-unpack/releases/latest) · [Gitee 下载](https://gitee.com/x-item/lol-audio-unpack/releases) · [界面使用](docs/gui/usage.md) · [控制台使用](docs/cli-package.md) · [Python API](docs/api/python_api.md)

![实体总览中查找与试听游戏音频](docs/images/gui.png)

## 你可以做什么

- **提取游戏音频**：处理英雄、地图和明确选定的本地资源包，保留原始 WEM 文件与音频 ID。
- **查找与试听**：按事件浏览语音、音效和音乐；没有事件映射时，也能从全部音频中试听。
- **导出与自动化**：选择需要的音频导出 WAV，或使用控制台 / Python API 批量处理。

程序读取本地游戏文件。使用前需要已安装的英雄联盟客户端，或符合要求的
[本地资源目录](docs/api/prepared_source.md)；客户端资源由用户自行准备。

## 下载与开始使用

在 [GitHub Releases](https://github.com/Virace/lol-audio-unpack/releases/latest) 中选择需要的附件。
中国境内用户也可到 [Gitee Releases](https://gitee.com/x-item/lol-audio-unpack/releases) 下载同一版本的程序。
新版本先在 GitHub 发布，再同步到 Gitee；同步未完成时，Gitee 的版本可能稍晚。网盘由维护者手动更新。
发布页目前提供 Windows x64 打包版；Linux、macOS 等系统可 [从仓库运行](#从仓库运行)，无需打包。

| 版本 | 附件名称 | 使用方式 |
| --- | --- | --- |
| Windows x64 界面版 | `LolAudioUnpack-<版本>-windows-x64.exe` | 下载后直接打开，无需安装 Python |
| Windows x64 控制台版 | `LolAudioUnpack-CLI-<版本>-windows-x64.exe` | 在 PowerShell 等终端中运行，参数见 [控制台指南](docs/cli-package.md) |

界面的基本流程：

1. 设置游戏目录和输出目录，等待英雄与地图列表准备完成。
2. 在执行中心选择对象和处理动作，创建任务并核对范围。
3. 在实体总览查找、试听，选择需要的音频导出 WAV。

操作方法见 [界面使用说明](docs/gui/usage.md)，常见疑问见 [常见问题](docs/faq.md)。
**编辑音频前，请先导出或复制到输出目录之外。**

## 从仓库运行

需要 Python 3.10 或更新版本，以及 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
Linux（如 Ubuntu、Debian、Fedora）、macOS 和其他 UNIX 系统可直接使用源码，安装项目依赖后运行。
游戏目录须符合 [本地资源要求](docs/api/prepared_source.md)；macOS 的实际客户端目录与完整流程尚未实测。

在终端中执行，PowerShell、bash、zsh 均可：

```sh
git clone https://github.com/Virace/lol-audio-unpack.git lol_audio_unpack
cd lol_audio_unpack
uv sync --extra gui
uv run unpack-gui
```

只使用控制台 / Python API 时执行 `uv sync` 即可。例如更新并提取 Annie 的音频：

```powershell
uv run unpack update extract --champions Annie --game-path "<游戏目录>"
```

## 文档

| 我想做什么 | 文档 |
| --- | --- |
| 用界面提取、试听和导出 | [界面使用说明](docs/gui/usage.md) |
| 批量处理、使用配置文件 | [控制台使用说明](docs/cli-package.md) · [示例 INI](config/lol-audio-unpack.example.ini) |
| 在 Python 脚本中调用 | [API 总览](docs/API.md) · [Python API](docs/api/python_api.md) |
| 了解本地输入与输出目录 | [本地游戏目录](docs/api/prepared_source.md) · [音频文件与输出目录](docs/audio-and-output.md) |
| 参与开发、打包或维护网站 | [开发文档](docs/development/README.md) · [网站维护](docs/development/doc-site.md) |

全部入口见 [文档导航](docs/README.md)。

## 维护者与感谢

由 [Virace](https://github.com/Virace) 维护，个人博客：[孤独的未知数](https://x-item.com)。

感谢 [bnk-extract](https://github.com/Morilli/bnk-extract)、
[lol-file-parser](https://github.com/Pupix/lol-file-parser)、
[CDTB](https://github.com/CommunityDragon/CDTB) 和 [vgmstream](https://github.com/vgmstream/vgmstream)。

## 许可证

[GPLv3](LICENSE)
