# emotionAdapter

本项目独立实现的轻量渲染控制模块。源码直接位于本目录：

- `NaturalBlink.ts`：唯一自动眨眼控制器，3–7 秒随机间隔，闭合/保持/打开约 260 毫秒。
- `PetExpressionController.ts`：组合眨眼和基础微笑权重，保留平滑过渡；情绪变化不重置眨眼计时。
- `index.ts`：模块入口。

`Room` 每帧获取这些权重。CTEG/GeneratedFace 接管时基础微笑回退不参与显示；眨眼仍传给眼睑冲突协调和 EyeMicro 的闭眼抑制。手动 wink 由 Room 单独处理。

本模块没有动作计划、语音处理、VAD 规划、空闲动作调度或模型权重。它不负责 CTEG 表情生成，也不负责身体动作。由 character-agent 的 Vite 直接构建。
