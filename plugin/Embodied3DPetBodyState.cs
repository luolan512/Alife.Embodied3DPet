using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace Marisa.Embodied3DPet;

internal static class Embodied3DPetBodyState
{
    static string Value(JsonElement value,string key,string fallback="未知")=>value.ValueKind==JsonValueKind.Object&&value.TryGetProperty(key,out var v)?v.ValueKind==JsonValueKind.String?v.GetString()??fallback:v.ToString():fallback;
    static bool Yes(JsonElement value,string key)=>Value(value,key)=="True"||Value(value,key)=="true";
    static string Label(string name)=>name switch {
        "wave"=>"挥手","stand"=>"站立","sit"=>"坐姿","kneel"=>"跪姿","lie"=>"躺姿","bow"=>"鞠躬","dance"=>"跳舞","jump"=>"跳跃","stretch"=>"伸懒腰","terminal-hold"=>"保持上一动作末姿态","terminal-idle"=>"自然待机",
        "queued"=>"待执行","preparing"=>"准备动作中","playing" or "running"=>"正在执行","completed"=>"已播放完毕","error"=>"执行失败","looping"=>"持续进行","waiting_for_speech"=>"等说完再做","interrupted"=>"已打断","cancelled"=>"已取消",
        "happy"=>"开心","sad"=>"难过","angry"=>"生气","relaxed"=>"放松","neutral"=>"平静","user"=>"看着用户","none"=>"自然视线",_=>name};
    internal static string Describe(JsonElement state,bool detailed)
    {
        var text=new StringBuilder();
        text.Append("感知时间：").Append(DateTimeOffset.Now.ToString("HH:mm:ss")).Append("。身体").Append(Yes(state,"connected")?"已连接":"未连接").Append(Yes(state,"ready")?"，动作系统就绪。":"，正在准备。");
        if(state.TryGetProperty("capabilities",out var cap))text.Append("外形：").Append(Value(cap,"model_name")).Append("。");
        if(state.TryGetProperty("current",out var current)&&current.ValueKind==JsonValueKind.Object) {
            text.Append("当前身体行为：").Append(Label(Value(current,"action"))).Append("；").Append(Yes(state,"playing")?"正在播放/维持":"暂停或保持姿势").Append("。");
            var description=Value(current,"prompt","");if(description.Length>0)text.Append("意图：").Append(description[..Math.Min(description.Length,220)]).Append("。");
        }else text.Append("当前没有任务动作；保留已呈现姿态或自然待机。");
        if(state.TryGetProperty("face",out var face)) {
            if(face.TryGetProperty("affect",out var affect))text.Append("表情情绪：").Append(Label(Value(affect,"emotion"))).Append("，有效强度 ").Append(Value(affect,"effective_intensity")).Append("。");
            text.Append("连续面部细节：").Append(Yes(face,"enabled")?Yes(face,"ready")?"运行中":"准备中":"关闭").Append("。");
            if(face.TryGetProperty("action",out var action)&&action.ValueKind==JsonValueKind.Object)text.Append("眼嘴显式控制：").Append(action.GetRawText()).Append("。");
        }
        if(state.TryGetProperty("rendered_face",out var rendered)&&state.TryGetProperty("rendered_face_age_s",out var age)&&age.GetDouble()<3) {
            text.Append("可见面部回报：").Append(Label(Value(rendered,"emotion"))).Append("；嘴部开合 ").Append(Value(rendered,"mouth_open")).Append("，闭眼幅度 ").Append(Value(rendered,"eyes_closed")).Append("；实际注视 ").Append(Label(Value(rendered,"gaze"))).Append("。");
        }
        text.Append("发声：").Append(Yes(state,"speaking")?"正在说话，嘴型随语音":"未在说话").Append("；注视：").Append(Label(Value(state,"gaze","未知"))).Append("。");
        if(state.TryGetProperty("tasks",out var tasks)&&tasks.ValueKind==JsonValueKind.Array) {
            foreach(var task in tasks.EnumerateArray().TakeLast(detailed?8:4))text.Append("任务 ").Append(Value(task,"task_id")).Append("：").Append(Value(task,"goal","")).Append("（").Append(Label(Value(task,"status"))).Append("）。");
        }
        if(state.TryGetProperty("plan_progress",out var progress)&&progress.TryGetProperty("plans",out var plans)&&plans.ValueKind==JsonValueKind.Array) {
            foreach(var plan in plans.EnumerateArray().TakeLast(1))if(plan.TryGetProperty("steps",out var steps))foreach(var step in steps.EnumerateArray().Where(s=>Value(s,"status")!="completed").Take(detailed?8:4))
                text.Append("计划：").Append(Value(step,"label",Label(Value(step,"action")))).Append("（").Append(Label(Value(step,"status"))).Append("，已播放 ").Append(Value(step,"elapsed_s","0")).Append(" 秒）。");
        }
        text.Append("动作可能需要排队和准备；计划里的秒数是播放时长，不是开始前等待时间。归正也属于计划中的动作。");
        if(state.TryGetProperty("generation_timing",out var timing)) {
            foreach(var pair in new[]{("motion_prepare_ms","最近一次动作准备"),("text_queue_wait_ms","最近一次文本排队"),("text_encode_compute_ms","最近一次文本编码"),("motion_generation_ms","最近一次动作推理")})
                if(timing.TryGetProperty(pair.Item1,out var value)&&value.TryGetDouble(out var milliseconds))text.Append(pair.Item2).Append("约 ").Append((milliseconds/1000).ToString("0.##",System.Globalization.CultureInfo.InvariantCulture)).Append(" 秒（历史测量，不是本次剩余时间）。");
        }
        text.Append("以上动作与表情是身体系统回报；播放完成只证明片段已执行，动作语义与目标姿态仍可能不准确，不能据此声称已完美完成。需要改变时控制动作、表情或注视；没有新需要时维持现有计划。");
        return text.ToString();
    }
}
