using System.ComponentModel;

namespace Marisa.Embodied3DPet;

public sealed class Embodied3DPetConfig
{
    [Description("启动时显示 3D 身体。关闭后仍可使用 BodyConnect 启动。")]
    public bool AutoStart { get; set; } = true;
    [Description("本地 HTTP 端口；WebSocket 使用下一端口。修改后重新启用模块。")]
    public int Port { get; set; } = 18196;
    [Description("VRM 文件名，不含 .vrm。修改后重新启用模块。")]
    public string Model { get; set; } = "miku-nt";
    [Description("是否启用本地 CTEG 连续表情。关闭仍保留情绪和眼嘴控制。")]
    public bool DynamicFace { get; set; } = true;
    [Description("把点击/拖动等互动送给 Alife；普通完成事件不触发聊天。")]
    public bool ForwardInteractions { get; set; } = false;
    public bool EnableDragging { get; set; } = true;
}
