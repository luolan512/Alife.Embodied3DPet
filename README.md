# Alife.Embodied3DPet · 具身3D桌宠

为 Alife 角色提供可以控制的 VRM 身体、本地动作生成、表情、注视与桌面互动。插件标识：`Marisa.Embodied3DPet`。

Alife 负责对话、人设、记忆及主动事件；本插件执行身体动作并在模型调用前反馈身体状态。无需另建对话子代理。模型默认允许拖动，拖动不发送聊天消息；一次性动作可补充自然归正步骤。动作生成有准备时间，动作意图与最终生成结果可能存在差异。

## 当前状态

这是基础源码版本，尚不是可供全新用户直接安装的发行包。已实现 VRM 导入、动作参数、文本编码选择、身体工具、状态反馈，以及折叠的环境检查、下载代理、模型下载修复和手动导入。右键动作设置与 Alife 模块设置共享配置。

首次安装仍依赖旧完整 `Runtime.zip` 和其 SHA256。轻量运行环境包的自动下载、解压和无环境首次安装流程尚未接入；仓库不包含原始安装包及运行补丁。不要将源码 ZIP 当成完整插件安装包。

## 源码目录

| 目录 | 内容 |
|---|---|
| `plugin/` | Alife 动态编译 C# 插件、模块 UI、身体工具和模型清单 |
| `frontend/` | Three.js / VRM 桌宠与聊天、动作配置界面 |
| `emotionAdapter/` | 表情、自然眨眼适配代码 |
| `desktop-host/` | Windows WPF / WebView2 透明桌面窗口 |
| `runtime/character-agent/` | Python 动作、表情、状态及 Alife 通信服务 |
| `runtime/scripts/` | 独立环境检查、启动及停止脚本 |
| `runtime/ardy/` | 所使用的 ARDY Python 源码，保留上游许可 |

不包含模型权重、VRM 文件、Python/Torch/CUDA/TensorRT 二进制、Kimodo 二进制、个人角色、聊天记录或密钥。所有模型及算力依赖均为必备组件，下载来源及文件校验信息见 `plugin/Environment-Models.json`。

## 构建

Windows，Node.js >= 22.12，.NET 8 SDK。

```powershell
cd frontend
npm ci
npm run typecheck
npm run build
cd ../desktop-host
dotnet publish -c Release -o ../artifacts/desktop
```

前端输出在 `frontend/dist/web`。桌面宿主输出在 `artifacts/desktop`。构建这两项不等于完成所有运行环境安装。C# 插件由 Alife 加载编译，依赖 `Alife.Function.DeskPet` 和 `Alife.Function.FunctionCaller`；当前 manifest 基于 Alife 4.5.2。

## 运行布局与发布计划

构建后的网页与宿主分别放入独立运行目录中的 `character-agent/dist/web` 和 `character-agent/dist/desktop`。启动脚本位于其 `scripts`；模型按环境清单的相对路径安装。插件目录仅存放插件代码和受控发布资料，避免 Alife 递归扫描 Python/Torch DLL。

发布目标为插件 ZIP + 独立必备环境 7z，插件配置界面自动下载环境和模型。现阶段独立环境试压约 1.75 GiB，但安装引导尚未整合。运行依赖隔离于本插件，不修改 Alife 官方 PythonService 或其他插件。

## 第三方依赖

参见 [THIRD_PARTY.md](THIRD_PARTY.md)。模型、依赖和上游代码遵循各自许可证。本项目原创代码采用 Apache-2.0，见 LICENSE。第三方代码保留原许可；模型及素材不受本项目许可证重新授权。
