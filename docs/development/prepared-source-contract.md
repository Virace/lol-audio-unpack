# 本地数据源验证合同

供维护者和外部准备工具作者使用，合同版本为 Prepared Game Source Contract v1。
用户入口与目录树见 [本地游戏目录](../api/prepared_source.md)。

## 共享基础预检

- `game_path`、`Game/DATA/FINAL` 和选中的 LCU game-data 位置是可读取目录。
- LCU game-data 位置可以是 `LeagueClient/Plugins/rcp-be-lol-game-data` 或
  `Plugins/rcp-be-lol-game-data`；同一输入的预检、更新与来源扫描使用相同布局选择规则。
- `Game/content-metadata.json` 是有效 JSON 对象，并包含非空字符串 `version`。
- 选中 LCU 目录中的 `description.json` 是有效 JSON 对象，并包含对象类型的 `riotMeta`。
- `LeagueClient.exe` 不是合同的一部分，外部准备目录无需复制它。

布局识别优先检查声明文件所在位置，再检查实际 game-data 目录。两套独立声明都存在时拒绝
混合消费；指向相同真实 game-data 位置的候选去重。选中声明损坏时直接报告原错误，不换用
另一套资源。没有 LCU 标志时，候选位置仅用于提供缺失诊断，不能代替基础预检。

`system.yaml` 和 `Config/LeagueClientSettings.yaml` 只提供可选诊断，分别读取
`default_region` 与 `install.globals.region`；它们不属于必需资源，缺失、损坏或地区字段无效
均不改变输入可用性。日志记录客户端地区和资源语言，字段来源使用相对于游戏根的路径。

可选安装诊断由来源选择/应用初始化入口负责，GUI 随选定目录保存到会话状态，资源语言变化单独记录。
基础结构与任务所需文件按原边界重新检查。

| 校验层 | 资源要求 |
| --- | --- |
| 基础预检 | 确认共享结构，目录骨架不能代替真实资源 |
| 界面发现 | 按 LCU 完整名单和语言声明检查必需 LCU bundle、基础 GAME WAD 与所选语言 WAD |
| update / extract / mapping | 处理前复检所选范围；缺文件整体阻止任务，不静默跳过 |
| 内容解析 | 文件存在不代表可解析；BIN、BNK/WPK 内容与精确 binding 在处理阶段验证 |

| 特殊资源约定 | 规则 |
| --- | --- |
| 英语 | LCU 使用 `default`，GAME 使用独立 `en_US.wad.client` |
| 地图 | 需要 Common/地图 0 的基础与当前语言文件 |
| Jade | 使用普通英雄共享 WAD |
| 历史资源包 | 仅检查显式选择的物理 WAD，不按普通英雄语言模板扩张选择 |

## Python 接入

```python
from lol_audio_unpack.app import LolAudioUnpackApp, OperationOptions, create_app_context

ctx = create_app_context(
    settings={
        "GAME_PATH": "D:/prepared/lol-client",
        "OUTPUT_PATH": "./output",
        "GAME_REGION": "zh_CN",
    }
)
app = LolAudioUnpackApp(ctx)

update_result = app.update(OperationOptions(champion_ids=(1, 103)))
extract_result = app.extract(OperationOptions(champion_ids=(1, 103)), include_maps=False)
```

外部准备器应保证 GAME 与 LCU 版本匹配，并在交付前完成获取完整性校验。
应用验证消费所需的本地事实；外部工具的网络、缓存和重试协议由外部工具负责。

## 验证阶段与失败边界

| 阶段 | 检查与失败处理 |
| --- | --- |
| 创建上下文 | `create_app_context(...)` 执行基础预检；失败抛出 `AppContextValidationError`，早于文件日志、manifest、音频、映射和报告初始化 |
| 界面目录发现与任务启动 | 保留完整名单；缺资源实体可浏览已有结果，但不能加入源任务。刷新重建文件快照，启动任务再次验证文件存在性 |
| update 与消费 | 建立 resource schema v2 bindings，每条保存根相对 WAD identity 和 entry hash；解包、映射只消费成功 binding，不从 alias、分类名或历史标记猜测资源 |

- **语言**：界面可留空保存；控制台/API 未提供时默认 `zh_CN`，显式空语言不可启动源任务。
- **基础结构通过**：只证明可以进入主链，不证明任意英雄或地图的资源齐全。缺必需文件会在处理前失败，不属于自动 update 可修复问题。
- **内容验证**：条目缺失和容器解析错误由处理阶段报告 `partial` / `failed`。
- **大厅语言**：选人、禁用语音只读取所选语言，缺失不回退英语；语言无关 SFX 使用共享 `default`。

### 皮肤音频状态

英雄 banks artifact 在首次或强制生成时附带以皮肤 ID 为键的 `skinAudio`，分别记录 `VO`、`SFX`：

| 状态 | 含义 |
| --- | --- |
| `independent` | 独立资源 |
| `shared` | 复用其他皮肤的物理资源 |
| `absent` | BIN 已解析，未声明该类音频 |
| `unknown` | BIN 或资源未能确认 |

共享按实际 WAD/entry binding 判断，无声明不推断具体来源，也不等于资源丢失。
这是 v2 附加信息；旧清单缺少字段时保持未知，可通过“重新生成实体数据”补齐。

### 事件与共享来源

- 英雄 events 与 mapping 的 `skinAudioVersion: 1` 标识完整事件和共享来源规则。普通 update 重建缺少标记的旧 events，恢复以前按 Base 名称过滤的声明。
- 解包报告、原始及整合 mapping 附带 `sharedAudio`，按皮肤 ID/category 记录 `status`（`shared` / `mixed` / `unknown`）和 `sources`（`skinId`、`category`）；原始 v2 binding 不变。
- 同源容器仅在规范皮肤归属落盘，优先基础皮肤及结构化父级。完全继承的事件省略；同名事件的引用增加、替换或减少时保留完整列表。
- 例如事件从四条扩为六条，六条全部保留，前四条 `audioPaths` 引用原文件；不能按 ID 全局合并不同来源。
- 旧缓存中同一物理 bank 可复用已知 Base 事件，兼容恢复记录到 `mappingDiagnostics.sharedEventCategories`；独立资源缺失、不同 bank 或解析失败仍保留不完整诊断。

### 旧共享副本诊断

重新 mapping 只读核对纯共享皮肤目录中的旧文件，两个诊断字段都保留对应文件：

| 字段 | 记录内容 |
| --- | --- |
| `mappingDiagnostics.sharedCopyPaths` | 与规范来源逐字节相同的文件 |
| `unverifiedSharedPaths` | 内容不同、来源文件缺失或无法读取的文件 |

这些是上次映射的快照，不表示已经清理。界面全部音频仍枚举实际目录。

### 容器与结果报告

- 解析成功但无内嵌 WEM 的 BNK 记为 `no_audio`，正常跳过，音频可能由配套 WPK 提供。
- 空字节、容器解析失败或存在条目却没有可写音频仍是错误。
- 炫彩和普通皮肤按真实 ID、名称及 bindings 解包和整合映射，不因炫彩元数据嵌套而漏项。
- 界面终态逐阶段列出结果，异常摘要保留对象和原因；不将同一英雄在多阶段的处理次数合并为一个成功对象计数。

<a id="remote-migration"></a>

## 旧远程模式迁移

旧配置中的以下项目已不再支持：

- `source_mode`
- `remote_live_region`
- `cleanup_remote`
- `remote_version`
- `remote_lcu_manifest_url`
- `remote_game_manifest_url`

INI 读取器会把这些项目视为未知配置并记录警告，然后忽略它们。请删除旧项目，并把已经准备好的
目录直接填写到 `game_path`。控制台与 Python API 不再提供对应的远程参数、类型或工作流方法。

升级不会自动删除历史 `_prepared_game`、`cache/remote`、`manifest/<version>/bin_input` 或
`.use_local_bin`。如需释放空间，请在确认目录不再被其他工具使用后自行处理；当前版本不会读取
这些历史产物，也不会用它们覆盖本地 WAD 索引。
