using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace Marisa.Embodied3DPet;

internal static class Embodied3DPetMotionSettings
{
    internal static readonly Dictionary<string, (double min, double max, bool integer)> Limits = new() {
        ["history_frames"]=(0,200,true), ["denoising_steps"]=(1,10,true),
        ["inactive_minutes"]=(1,180,false), ["settle_buffer_s"]=(2,20,false),
        ["cfg_text"]=(0,10,false), ["cfg_constraint"]=(0,10,false), ["seed"]=(0,2147483647,true)
    };
    internal static Dictionary<string, object> Read(string root, string file)
    {
        using var data = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "character-agent", "config", file)));
        return data.RootElement.EnumerateObject().ToDictionary(x => x.Name, x => (object)x.Value.Clone());
    }
    internal static void ValidateMotion(Dictionary<string, object> patch)
    {
        using var data = JsonDocument.Parse(JsonSerializer.Serialize(patch));
        foreach (var item in data.RootElement.EnumerateObject()) {
            if (Limits.TryGetValue(item.Name, out var limits)) {
                if (item.Value.ValueKind != JsonValueKind.Number || !item.Value.TryGetDouble(out double n) || !double.IsFinite(n) || n < limits.min || n > limits.max || limits.integer && n != Math.Floor(n)) throw new ArgumentException(item.Name + " 参数超出允许范围");
            } else if (item.Name == "allow_cached_idle_loops" || item.Name == "motion_prefetch") {
                if (item.Value.ValueKind != JsonValueKind.True && item.Value.ValueKind != JsonValueKind.False) throw new ArgumentException(item.Name + " 必须为开关");
            } else {
                string[] allowed = item.Name switch {
                    "behavior_mode" => new[] { "stream", "event", "hybrid" },
                    "static_protection" => new[] { "off", "freeze", "anchor" },
                    "inference_backend" => new[] { "none", "trt-fp16", "torch-compile" },
                    _ => throw new ArgumentException("未知动作设置：" + item.Name)
                };
                if (item.Value.ValueKind != JsonValueKind.String || !allowed.Contains(item.Value.GetString())) throw new ArgumentException("无效设置：" + item.Name);
            }
        }
    }
    internal static void SaveMotion(string root, Dictionary<string, object> patch)
    {
        ValidateMotion(patch);
        var merged = Read(root, "motion.json");
        foreach (var item in patch) merged[item.Key] = item.Value;
        if (patch.TryGetValue("behavior_mode", out var mode)) merged["event_mode"] = Convert.ToString(mode) != "stream";
        Write(root, "motion.json", merged);
    }
    internal static void SaveEncoder(string root, string mode, int layerChunk)
    {
        if (!new[] { "cpu", "vulkan-stream", "vulkan-resident" }.Contains(mode) || layerChunk < 1 || layerChunk > 8) throw new ArgumentException("文本编码参数无效");
        if (mode != "cpu" && !File.Exists(Path.Combine(root, "kimodo.cpp", "build", "vulkan", "kmd-encode-server.exe"))) throw new FileNotFoundException("GPU 编码器未安装");
        Write(root, "text-encoder.json", new Dictionary<string, object> { ["mode"]=mode, ["layer_chunk"]=layerChunk });
    }
    static void Write(string root, string file, Dictionary<string, object> values)
    {
        string target = Path.Combine(root, "character-agent", "config", file);
        string temporary = target + ".alife-" + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(values, new JsonSerializerOptions { WriteIndented=true }));
        File.Move(temporary, target, overwrite:true);
    }
}
