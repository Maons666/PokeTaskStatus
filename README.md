# PokeTask Status

Windows 上的宝可梦桌宠，默认使用巨金怪。用《宝可梦 黑／白》的原版像素帧，配合自行编排的彗星拳、巡逻和举牌动作，显示桌面 agent 的状态；也可以导入完整形象包。

## 使用

双击本机构建的 `release/PokeTaskStatus.exe` 即可运行，无需安装 Python。首次启动打开图鉴，此后只显示桌宠。单击桌宠打开面板，拖动移动，右键退出。菜单无系统标题栏，拖动顶部 POKETASK 区域移动窗口；“收起”或 Esc 隐藏面板，背包中的“离洞绳”完全退出。四个入口按 2×2 排列，页面内没有重复标题条。

- **图鉴**：当前会话状态、最近上报的账户额度、声音开关和用量详情。
- **队伍**：手动选择一个 Claude Code 或 Codex 会话；一次只展示一只宝可梦。滚轮或翻页按钮查看历史会话。
- **背包**：使用原版游戏道具制作菜单按钮。宝可梦之笛管理声音，气球切换置顶，城镇地图选择日志目录，代币盒查看用量，对战记录器预览动作。预览有明确的“演示”标记，可以返回真实状态。
- **盒子**：导入 ZIP 形象包，点选后按“使用”切换外观，重启自动恢复。包内需有头像、九种状态动画、代表招式和来源说明。切换外观不切换 agent 或用量。内置巨金怪始终保留，“导出示例”可以得到它的完整动画包，格式见 [PET_PACKS.md](docs/PET_PACKS.md)。目前随附角色只有巨金怪。
- **悬浮桌宠**：头顶像素文字气泡显示状态。工作时用 `.` 到 `......` 循环，巨金怪蓄力并挥出彗星拳，完成时举牌；询问时气泡出现未知图腾 `?`，失败或中断时出现未知图腾 `!`。
- 支持 `Tab` / `Enter` 操作面板，`Esc` 收起，`F1` 打开说明。

## 接入范围和数据含义

应用每秒读取本地会话日志，不需要 API key，不读取凭据文件，不上传对话，也不发起模型请求。它不会控制、停止或修改 agent 的任务。

| 来源 | 自动读取位置 | 状态与用量 | 剩余额度 |
| --- | --- | --- | --- |
| Codex（包括 VS Code 插件） | `%USERPROFILE%\.codex\sessions` | 本地任务事件、累计 token、最近上下文占用 | 日志实际提供的限额窗口快照 |
| Claude Code（本地 Code 会话和 VS Code 插件） | `%USERPROFILE%\.claude\projects` | 会话响应状态、去重累计 token；可选 hooks 增强 | 可选 statusLine 提供的真实快照；没有则显示未知 |

尊重 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`。背包可以选择自定义配置根目录或直接选择 `sessions` / `projects` 目录，重启后生效。远程、云端、WSL 中无法从 Windows 读取的日志不会自动出现。Claude Desktop 的普通 Chat / Cowork 不属于这里的 Code 会话。

“本轮完成”仅表示 agent 结束一次响应，不是代码质量或业务任务已通过验收。没有可靠事件的状态显示待确认；工作日志停更超过五分钟不推断任务成功或失败。历史会话仍可查看，不把历史日志存在当作进程在线。日志格式不是稳定公共 API，升级后的新格式可能需要适配。

“询问状态”独立于普通等待：只在来源提供明确的授权、选择或输入请求时出现，不凭助手文字中的问号推断。Claude 增强接入识别权限请求和相关输入通知；Codex 识别日志中实际记录的授权／输入事件及 `request_user_input` 调用，匹配到答复后恢复工作状态。未写入本地日志的提示无法检测。

账户额度与 token 用量独立：额度仅显示来源提供的最近快照及时间，超过 15 分钟或重置时刻后标记旧值，不能保证与其他设备的最新使用同步。缺失额度不显示成 0。缓存读取已计入输入，不能再次加到总量。声音仅针对当前选中且已发现会话的新完成事件，启动时不为旧任务播放提醒。完成庆祝约四秒后恢复待命动作，完成结果文字保留。

### Claude 可选增强接入

在背包中点击“西尔佛检视镜 / Claude 增强接入”，查看修改范围后点“安装接入”。应用备份并合并 Claude 的 `settings.json`，添加只记录必要字段的 PowerShell hooks。已有 hooks 保留；已有自定义 `statusLine` 不覆盖。可在同一页面移除本应用增加的配置。安装后重开 Claude 会话。请将 EXE 放在稳定目录再使用。由旧版本升级时，再点一次安装可更新本应用的桥接脚本和事件配置。

官方 [statusLine](https://code.claude.com/docs/en/statusline) 可以为部分订阅在响应后提供五小时和七日用量窗口；并非所有运行入口或账户都会提供这些字段。Desktop / IDE 没有触发状态栏时额度仍然未知。安装成功、收到 hooks 事件和收到额度数据是不同事情，不把配置安装当作账号连通验证。桥接使用 Windows PowerShell，不依赖 Python，不读取登录凭据。

### 验证范围

本机已只读识别真实 VS Code Codex（`codex_vscode`）与 Claude Code（`claude-vscode`）日志结构，验证提取的会话状态、用量以及 Codex 实际限额字段。合成夹具验证快速完成、失败、中断、等待、询问／答复和桥接链路。Claude Desktop Code 的单独实机任务、各插件从发起任务到声音提醒的完整人工验收，以及全新 Windows 机器测试仍需用户在自己的环境中确认；目前不把这些写成已通过。

## 从源码运行和构建

Windows，Python 3.11 或更新版本（含 Tk）。

```powershell
python -m pip install -r requirements-build.txt
python scripts/fetch_assets.py
python run.py
python scripts/check.py
python scripts/build.py
python scripts/smoke_release.py
```

素材下载只发生在构建准备时；已打包的应用可离线启动。巨金怪形象来自原版正面／背面 GIF。彗星拳将画面右侧的原版前肢像素分离，抬至右上，延长持拳蓄力并轻微晃动，再带着粒子快速横扫到左侧。挥拳只保留一道手臂残影和短弧光迹，左侧命中时短暂停顿再收势。3.8 秒循环中的举拳蓄力为 1.12 秒，挥拳为 0.16 秒；工作动画约 30 帧／秒。前肢使用最近邻旋转与伸展，被遮挡的后腿由原版肢体及爪部像素、原图调色板重构，独立放在身体和前拳后面，前拳移开后仍可见。衔接关节、粒子、牌子和气泡由程序绘制。询问／中断符号使用原版未知图腾精灵，道具图只用于菜单，不参与攻击表现。不使用生成式美术。通知音为程序生成的短音，不是游戏音频。

演示：`python run.py --demo`。自动 GUI 冒烟：`python run.py --demo --smoke-test .local/preview`，输出仅应用面板图片并自动退出。

`python scripts/smoke_ui.py --meteor-sweep` 用隔离数据执行自动界面流程（挥拳固定阶段、九种状态及气泡、七个菜单页面、键盘开关、会话切换、设置保存、收起展开、无边框窗口拖动）；先运行 `python scripts/export_pet.py`，再为 GUI 脚本加 `--pet-pack release/metagross.pokepet.zip` 可验证完整包导入与切换。`smoke_release.py` 将 EXE 复制到独立目录、清除 Python PATH 后验证运行和合成数据；也支持同一 `--pet-pack` 参数，检查打包程序恢复导入形象。`python scripts/preview_animation.py --output .local/meteor-power-preview` 生成彗星拳 GIF、动作分镜和状态预览。它们属于自动 QA，不能代替真人使用和真实插件端到端验收。

## 仓库与本地文件

- 上传 GitHub：`src/`、`tests/`、`scripts/`、`assets/manifest.json`、依赖文件、本文档和素材声明。
- 不上传：`.local/`（三轮报告、截图、测试数据、构建缓存）、`release/`、`build/`、`dist/`、`assets/vendor/`（下载的游戏图与字体）、虚拟环境、日志及私有设置。均已加入 `.gitignore`。
- 运行设置保存在 `%LOCALAPPDATA%\PokeTaskStatus`，形象包在该目录的 `pets/` 中；开发/测试可通过 `POKETASK_DATA_DIR` 改到隔离目录。

此目录未自动发布到 GitHub。构建产物可以另行作为 Release 附件分发；素材权利不随源码转移，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 接入参考

- [Claude Code hooks](https://code.claude.com/docs/en/hooks)：官方生命周期事件语义。
- [Claude Code Desktop](https://code.claude.com/docs/en/desktop)：Code 会话范围。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：官方任务和用量事件语义；当前实现从已有本地日志读取，不创建新 agent 会话。
- [Codex IDE](https://learn.chatgpt.com/docs/codex/ide)：VS Code 插件。
