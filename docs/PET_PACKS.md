# 宝可梦形象包格式

“盒子”导入完整的 ZIP 形象包。一个包包含头像、九种状态动画、名称和素材来源。选择包后会一起切换这些内容；内置巨金怪仍是独立选项。

在盒子点“导出示例”可以保存巨金怪的完整 ZIP，或从源码执行 `python scripts/export_pet.py`。示例含原版正面头像、空闲正反面动作、彗星拳与其余全部状态；可直接重新导入，ID 为 `metagross-example`。后续增加角色时，以官方像素图为基础制作九种状态，记录真实来源并使用新的 ID。点选卡片后按“使用”才会改变桌宠，切换外观不改变会话或用量。

包内只放角色与技能动画。头顶气泡、询问/失败符号和应用菜单由程序统一绘制，不要预先画进动画。

## ZIP 目录

`pet.json` 必须直接位于 ZIP 根目录，不能在外面再包一层文件夹。

```text
metagross-example.zip
├── pet.json
├── portrait.png
└── animations/
    ├── idle.gif
    ├── working.gif
    ├── asking.gif
    ├── completed.gif
    ├── waiting.gif
    ├── failed.gif
    ├── interrupted.gif
    ├── disconnected.gif
    └── unknown.gif
```

`pet.json` 示例：

```json
{
  "schema_version": 1,
  "id": "metagross-example",
  "name": "巨金怪 · 彗星拳",
  "species_id": 376,
  "signature_move": "彗星拳",
  "sources": [
    "填写官方游戏像素素材的具体出处与参考链接",
    "填写动画重新编排或原创技能效果的作者与说明"
  ],
  "portrait": "portrait.png",
  "canvas": [192, 112],
  "animations": {
    "idle": "animations/idle.gif",
    "working": "animations/working.gif",
    "asking": "animations/asking.gif",
    "completed": "animations/completed.gif",
    "waiting": "animations/waiting.gif",
    "failed": "animations/failed.gif",
    "interrupted": "animations/interrupted.gif",
    "disconnected": "animations/disconnected.gif",
    "unknown": "animations/unknown.gif"
  }
}
```

`sources` 示例中的文字需要换成真实出处。程序保留来源说明，不联网下载素材，也不能仅凭来源文字验证素材版权或官方身份。

## 状态与元数据

| 字段/状态 | 含义与要求 |
| --- | --- |
| `schema_version` | 整数 `1` |
| `id` | 1–64 个英文字母、数字、`-` 或 `_`；安装时忽略大小写检查冲突 |
| `name` | 非空单行名称，最多 80 字符 |
| `species_id` | 宝可梦编号，1–99999 的整数 |
| `signature_move` | 非空单行招式名称，最多 80 字符 |
| `sources` | 1–16 条非空单行来源说明，每条最多 1000 字符 |
| `idle` | 空闲、散步或休息 |
| `working` | 执行任务，可表现为训练或释放技能 |
| `asking` | 需要用户授权、选择或输入 |
| `completed` | 当前响应/任务完成 |
| `waiting` | 普通等待，保留与既有状态兼容 |
| `failed` | 任务或工具调用失败 |
| `interrupted` | 任务被中断 |
| `disconnected` | 会话结束或断开 |
| `unknown` | 无法确认状态 |

九种状态缺一不可；它们可以引用同一 GIF，但建议分别制作易辨认的表现。重复引用也会按各状态计入解码像素预算。`builtin-metagross` 是内置形象保留 ID。当前版本拒绝未知 JSON 字段与重复 JSON 字段。

## 图像要求

- `portrait` 是静态 PNG，宽度 1–192、高度 1–112。头像可以单独裁切，不必与动画画布同尺寸；不支持 APNG。
- `canvas` 是所有动画共用的 `[宽, 高]`，建议 `[192, 112]`，最大 `[192, 112]`。每帧解码后的画布必须完全相同。程序固定画布位置，用最近邻等比适配显示区域，不随每帧内容裁切；桌宠上方留出气泡空间。
- 动画必须是 GIF，每种状态至少 2 帧、最多 300 帧。
- 头像与每个动画帧都必须包含透明像素。Alpha 只能为 0 或 255，不支持半透明边缘。
- 不允许不透明的 `#FF00FF` 洋红色，因为它是 Windows 桌宠窗口的透明色键；完全透明像素的 RGB 不受此限制。
- GIF 可使用局部帧，但合成后的完整帧必须满足上述要求。导入器按 GIF 的帧处置方式合成，再转换为 RGBA。
- 帧时长会限制在 20–5000 毫秒；缺失或 0 毫秒时按 100 毫秒处理。
- 按整数像素绘制，避免平滑缩放产生半透明像素。

## 容量与导入规则

| 限制 | 上限 |
| --- | --- |
| ZIP 文件 | 20 MiB |
| 解压后的文件总大小 | 40 MiB |
| `pet.json` | 64 KiB |
| 文件与目录条目总数 | 64 |
| 头像及九状态全部解码像素之和 | 24,000,000，RGBA 像素约 92 MiB |
| 路径 | 最多 240 字符、8 层，只用 `/` 分隔 |

ZIP 只能使用 Store 或 Deflate 压缩，不能加密。包内只允许 `pet.json`、它声明的 PNG/GIF 和这些文件所需的父目录；README、脚本、可执行文件与其他附件不能夹带。

拒绝绝对路径、`..`、Windows 驱动器路径、反斜杠、设备保留名称、尾点/尾空格、控制字符、重复条目、大小写/Unicode 名称冲突、符号链接和联接。包内任何内容都不会作为代码执行。

导入器只在应用数据目录的 `pets/` 下创建临时目录，完整检查并解码成功后才安装为 `pets/<id>/`。失败会清理本次临时目录，已有相同 ID 的包不会被覆盖。制作新版包时使用新的 ID。

并发导入同一 ID 时使用系统文件锁；进程被强制结束也会释放锁。目录中保留的 `.install-<id>.lock` 是可复用的隐藏标记，不代表导入仍在运行。程序只复用自身格式的锁文件，不会删除或覆盖来源不明的同名文件。

## 开发接口

`poketask.petpacks` 提供：

- `load_pack(directory) -> PetPack`：完整校验目录，返回 RGBA 头像、九状态 RGBA 帧列表及毫秒时长列表。
- `import_pack(zip_path, pets_dir) -> PetPack`：校验 ZIP、安装后返回可直接渲染的形象包。
- `discover_pack_summaries(pets_dir) -> (summaries, errors)`：仅检查有大小上限的元数据与声明目录，每项包含 `id`、`name`、`species_id`、`signature_move`、`directory`。不解码图像，适合盒子列表。
- `discover_packs(pets_dir) -> (packs, errors)`：完整解码所有包，供测试或工具使用；大量包时应改用轻量摘要接口。
- `PackError`：`ValueError` 的子类，错误信息可展示给用户。

摘要检查通过不等于图像已经解码通过。选中包时仍需调用 `load_pack`，处理文件被外部删除或损坏等情况。来源文本只是记录，不会触发网络请求。
