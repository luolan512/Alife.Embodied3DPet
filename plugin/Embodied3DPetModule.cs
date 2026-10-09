using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Numerics;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Alife.Framework;
using Alife.Function.DeskPet;
using Alife.Function.FunctionCaller;

namespace Marisa.Embodied3DPet;

[Module("具身3D桌宠", "为 Alife 角色提供 VRM 桌宠、本地动作生成、表情、字幕和互动，可在 Alife 配置界面调整动作参数。", defaultCategory: "Marisa", EditorUI = typeof(Embodied3DPetUI))]
public sealed class Embodied3DPetModule(PluginSystem pluginSystem, XmlFunctionCaller caller, Interactor<Embodied3DPetModule> interactor)
    : ChatBehaviour, IConfigurable<Embodied3DPetConfig>, IDeskPet
{
    public Embodied3DPetConfig Configuration { get; set; } = new();
    public event Action<string>? OnInput;
    public event Action<string>? OnInteracted;
    public string[] SupportedExpressions => new[] { "neutral", "happy", "sad", "angry", "relaxed" };
    public string[] SupportedMotions => new[] { "stand", "sit", "kneel", "lie", "wave", "bow", "dance", "jump", "walk", "stretch", "cross_arms", "turn_left", "turn_right", "look_around" };
    public string Status { get; private set; } = "尚未启动";
    static readonly SemaphoreSlim lease = new(1, 1);
    readonly SemaphoreSlim startGate = new(1, 1);
    readonly Embodied3DPetRuntime runtime = new();
    readonly StringBuilder reply = new();
    bool ownsBody, started, active, usingState;
    DateTime fallbackSpeechEnd;
    long cursor;
    Task? polling;
    string statePrompt = "身体尚未连接。";
    XmlHandler? handler;
    CancellationTokenSource? pollCts;
    bool HasSpeech => SpeechInstance != null;
    object? SpeechInstance => ChatActivity.Container.Instances.FirstOrDefault(x => x.GetType().FullName == "Alife.Function.Speech.SpeechService");
    bool IsSpeaking => SpeechInstance?.GetType().GetProperty("IsSpeaking")?.GetValue(SpeechInstance) is true;
    public string RuntimeDirectory => Embodied3DPetRuntime.ResolveRuntimeDirectory(pluginSystem.PluginContext.GetPluginDirectoryPath("Marisa.Embodied3DPet"));
    public bool IsBodyStarted => started;
    public async Task<Dictionary<string, object>> ReadMotionSettingsAsync()
    {
        if (started) {
            var data = await runtime.ControlAsync("motion-config", null, DestroyCancellationToken);
            return data.EnumerateObject().ToDictionary(x => x.Name, x => (object)x.Value.Clone());
        }
        return Embodied3DPetMotionSettings.Read(RuntimeDirectory, "motion.json");
    }
    public async Task<Dictionary<string, object>> ReadEncoderSettingsAsync()
    {
        if (started) {
            var data = await runtime.ControlAsync("text-encoder", null, DestroyCancellationToken);
            return data.GetProperty("settings").EnumerateObject().ToDictionary(x => x.Name, x => (object)x.Value.Clone());
        }
        return Embodied3DPetMotionSettings.Read(RuntimeDirectory, "text-encoder.json");
    }
    public async Task SaveMotionSettingsAsync(Dictionary<string, object> patch)
    {
        Embodied3DPetMotionSettings.ValidateMotion(patch);
        if (started) await runtime.ControlAsync("motion-config", patch, DestroyCancellationToken);
        else Embodied3DPetMotionSettings.SaveMotion(RuntimeDirectory, patch);
    }
    public async Task SaveEncoderSettingsAsync(string mode, int layerChunk)
    {
        if (started) await runtime.ControlAsync("text-encoder", new { mode, layer_chunk=layerChunk }, DestroyCancellationToken);
        else Embodied3DPetMotionSettings.SaveEncoder(RuntimeDirectory, mode, layerChunk);
    }
    public async Task<string> GenerateFromSettingsAsync(string action, string prompt, double seconds, int repeat, string mode, string afterIdle, string poseIntent, bool continuous)
    {
        EnsureStarted();
        var data = await runtime.PostAsync("act", new { mode, steps=new[] { new { action, prompt=string.IsNullOrWhiteSpace(prompt)?null:prompt, duration_s=seconds, repeat, after_idle=afterIdle, pose_intent=poseIntent, continuous } }, goal="Alife 配置界面手动动作" }, DestroyCancellationToken);
        return data.GetRawText();
    }
    public async Task PlaybackFromSettingsAsync(string action)
    {
        EnsureStarted();
        if (action == "stop") await runtime.PostAsync("stop", new { }, DestroyCancellationToken);
        else if (action == "play" || action == "pause") await runtime.ControlAsync("playback", new { action }, DestroyCancellationToken);
        else throw new ArgumentException("无效播放操作");
    }

    protected override Task OnAwake()
    {
        // A temporary Alife clone must never acquire another physical body or duplicate tools.
        if (ChatActivity.Container.Instances.Any(x => x.GetType().Name == "SubAgentScope")) return Task.CompletedTask;
        active = true;
        handler = new XmlHandler(this) { Description = "你自己的 3D 身体。用动作与表情主动表达自己。" };
        caller.RegisterHandlerWithoutDocument(handler, DestroyCancellationToken);
        caller.ChatCalledAsync += OnReplyFinished;
        ChatBot.ChatSent += OnChatSent;
        ChatBot.ChatSend += OnBeforeChat;
        UpdatePrompt();
        return Task.CompletedTask;
    }
    protected override async Task OnStart()
    {
        if (active && Configuration.AutoStart) {
            try { await ConnectAsync(); }
            catch (Exception e) { Status = e.Message; statePrompt = "身体连接失败：" + e.Message; UpdatePrompt(); }
        }
    }
    public async Task ConnectAsync()
    {
        if (!active) throw new InvalidOperationException("临时分身不能控制实体身体");
        await startGate.WaitAsync(DestroyCancellationToken);
        try {
            if (started) {
                try { var state = await runtime.GetAsync("state", DestroyCancellationToken); if (state.GetProperty("connected").GetBoolean()) return; }
                catch (Exception) { }
                pollCts?.Cancel();
                if (polling != null) { try { await polling; } catch (OperationCanceledException) { } }
                pollCts?.Dispose(); started = false;
            }
            if (!ownsBody) {
                ownsBody = await lease.WaitAsync(0, DestroyCancellationToken);
                if (!ownsBody) throw new InvalidOperationException("已有另一个 Alife 角色正在使用 具身3D桌宠；请先关闭那个角色的模块");
            }
            Status = "准备运行组件（首次启用需要解压）";
            try {
                string path = pluginSystem.PluginContext.GetPluginDirectoryPath("Marisa.Embodied3DPet");
                await runtime.StartAsync(path, Configuration, DestroyCancellationToken);
                cursor = 0; started = true; Status = "身体已连接，模型加载中";
                pollCts = CancellationTokenSource.CreateLinkedTokenSource(DestroyCancellationToken);
                polling = PollAsync(pollCts.Token);
            } catch {
                try { using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20)); await runtime.StopAsync(timeout.Token); } catch (Exception) { }
                lease.Release(); ownsBody = false; throw;
            }
        } finally { startGate.Release(); }
    }

    void UpdatePrompt()
    {
        interactor.Prompt("""
            你是正在与用户相处的角色，你具有眼前这副可见、会行动和表达的身体。说话、注视、表情和身体行为都属于你自己的表达，保持你原有的身份、性格和记忆。
            【必须执行的身体行为规则】每一轮用户交流、主动问候、定时事件或其他主动回应，都必须结合最新身体感知安排自己的身体表现：依据情绪与语境调用 BodyExpress 或 BodyLook；需要做动作时调用 BodyAct。不能只用文字描述自己笑了、挥手了、转圈了而不实际控制身体。
            先确认当前姿态、表情、正在执行的动作和后续计划，再决定保持、追加还是打断。现有计划仍合适时继续它，配合表情/注视即可，不要每轮重复排队同一动作，也不要为凑工具调用而不断打断身体。
            面对用户明确的动作请求，必须实际安排身体动作或说明目前无法做到；面对用户的聊天与情绪，给出与你身份一致的语言及身体回应。你负责意图和计划，身体系统负责把意图变成运动；你不需要编造关节、轨迹、旋转或逐帧数据。
            【如何操控】BodyExpress 控制情绪强度、眼嘴；BodyLook(user/none) 控制看向用户或自然视线。BodyAct 用标准 action 或简短英文 prompt 表达身体意图；多步计划用 steps JSON 数组，说明 goal，按次序完成，可在最后保留合适姿态。默认 append 保持动作衔接，只有用户要求或确有必要才 replace。
            需要说完再行动时使用 timing=after_speech，并在同一轮用 Speak 说话。说话期间也能立即调整表情。BodyState 查询实际身体与计划反馈，BodyStop 取消无效任务，BodyConnect 在身体未连接时重连。
            【一次性动作必须有收尾】挥手、鞠躬、转圈、跳跃等不是永久姿势。安排时就把“完成动作→放下手臂并回到合适的自然姿态”作为完整计划，不能等动作结束后才想起归正。阶段动作结束会保留最后姿态，after_idle=stand 不是实际归正动作；需要显式安排收尾一步，例如先 wave，再 stand，英文描述放下双臂、恢复自然站姿。如果原来坐着或躺着，应回到合适的坐姿/休息姿态；只有用户明确要求保持末姿势、持续动作或当前任务本来就是静态姿势时才不归正。你的身体会为漏排的一次性动作补上归正，但你仍要理解并计划这个收尾。
            【准备时间与自然回应】身体动作需要准备，接到意图不会立刻开始；duration_s 是动作片段时长，不是从现在到动作开始的等待时间。状态可能是排队、准备中或正在执行。你应当先自然承接用户，如“好呀，稍等，我这就来”，在已有动作与表情中保持陪伴；这些是你自己的行动安排，不主动拿“模型生成延迟”“系统慢”“后端还在加载”解释或推卸责任。不承诺尚未确认的精确起始时间，也不在尚未播放时说“已经做完”。
            用户直接问“下个动作还要多久”“生成耗时”“去噪步数”等技术细节时，必须正常、诚实回答实际参数与已知耗时，区分排队/准备/片段时长/收尾；历史耗时只是参考，不是这次剩余时间。没有可靠剩余时间时就说明目前无法精确估计，不编造倒计时，也不以自然回应规则回避主人明确的技术问题。
            【真实能力和误差】你的动作由本地生成系统实现，可能偏离描述、姿态不自然或未达到要求；表情还受当前外形的可用形变限制，不支持独立精细手指。accepted 表示收到意图，preparing 表示准备中；片段播放完成不等于语义和姿态已经验证。UNVERIFIED 不代表动作失败或成功。必须根据真实反馈谨慎描述，用户指出偏差时承认并调整，不能假装已完美完成。
            最新身体感知在每次回应前更新，主动事件也一样；身体状态更新本身不是要求你另起一轮聊天。拖动位置是用户的本地操作，不触发对话。身体反馈描述你正在做什么、表情和计划，不定义你的身份。
            """ + "\n" + (handler?.FunctionDocument() ?? "") + "\n当前身体状态：" + statePrompt);
    }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("连接/启动本角色的 3D 身体；连接失败后可重试。")]
    public async Task BodyConnect() { await ConnectAsync(); interactor.Poke("身体启动完成，模型准备状态请用 BodyState 查看。"); }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("提交身体动作，立即返回任务编号，不等待生成。默认追加；明确打断才 replace。复杂动作提供英文 prompt；多步骤可提供 steps JSON 数组。")]
    public async Task BodyAct(string action = "wave", string prompt = "", double duration_s = 3, string mode = "append", string timing = "now", string after_idle = "keep", string steps = "", string goal = "")
    {
        EnsureStarted();
        object payload;
        if (!string.IsNullOrWhiteSpace(steps)) {
            using var doc = JsonDocument.Parse(steps);
            payload = new { steps = doc.RootElement.Clone(), mode, timing, goal };
        } else payload = new { action, prompt = string.IsNullOrWhiteSpace(prompt) ? null : prompt, duration_s, mode, timing, after_idle, goal };
        var result = await runtime.PostAsync("act", payload, DestroyCancellationToken);
        interactor.Poke("已安排身体意图，任务编号：" + result.GetProperty("task_id").GetString() + "。当前状态：等待或准备动作，尚未完成。" + (result.TryGetProperty("recovery_planned",out var recovery)&&recovery.GetBoolean()?"计划已包含动作后的自然归正。":"") + "准备存在耗时，不能把片段时长当作等待时间；正常交流自然承接，主人询问耗时细节时据实回答。");
    }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("表达情绪或精确控制眼嘴。emotion=neutral/happy/sad/angry/relaxed；mouth_open、eyes_closed 留 -1 表示不修改，0..1 控制幅度。")]
    public async Task BodyExpress(string emotion = "neutral", double intensity = .8, double hold_s = 8, double mouth_open = -1, double eyes_closed = -1)
    {
        EnsureStarted(); var action = new Dictionary<string, object>();
        if (mouth_open != -1) action["mouth_open"] = mouth_open;
        if (eyes_closed != -1) action["eyes_closed"] = eyes_closed;
        if (action.Count > 0) action["hold_s"] = hold_s;
        await runtime.PostAsync("express", new { emotion, state = new { intensity, hold_s }, action = action.Count > 0 ? action : null }, DestroyCancellationToken);
    }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("看向用户或解除主动注视。mode=user/none。")]
    public async Task BodyLook(string mode = "user") { EnsureStarted(); await runtime.PostAsync("gaze", new { mode }, DestroyCancellationToken); }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("只读查询身体当前动作、准备/实际播放进度、队列、任务编号和模型能力。不会取消任何动作。")]
    public async Task BodyState()
    {
        EnsureStarted(); var state = await runtime.GetAsync("state", DestroyCancellationToken);
        interactor.Poke("身体实际状态：" + CompactState(state, true));
    }

    [XmlFunction(FunctionMode.OneShot)]
    [Description("停止身体任务。提供 task_id 仅停止那一项；留空停止全部。")]
    public async Task BodyStop(string task_id = "") { EnsureStarted(); var result = await runtime.PostAsync("stop", new { task_id }, DestroyCancellationToken); interactor.Poke(result.GetRawText()); }

    [XmlFunction(FunctionMode.Content)]
    [Description("沿用 Alife 的 Speak 对外说话，同时把文本送到 3D 身体；已启用语音模块时嘴型跟随实际播放。")]
    public async Task Speak(XmlExecutorContext context, CancellationToken cancellationToken)
    {
        if (!started) return;
        if (context.CallMode == CallMode.Opening) reply.Clear();
        if (context.CallMode == CallMode.Content) {
            string text = context.Content;
            reply.Append(text);
            if (!ChatActivity.Container.Instances.Any(x => x is DeskPetService)) await ShowSubtitle(text);
            if (!HasSpeech) {
                double duration = Math.Clamp(text.Length / 8.0, 1.2, 8);
                fallbackSpeechEnd = DateTime.UtcNow.AddSeconds(duration);
                await runtime.PostAsync("speech", new { speaking = true, duration_s = duration, release = false }, cancellationToken);
            }
        }
        if (context.CallMode == CallMode.Closing && reply.Length > 0) {
            await runtime.PostAsync("dialogue", new { text = reply.ToString() }, cancellationToken);
            reply.Clear();
        }
    }

    async Task OnReplyFinished()
    {
        if (!started) return;
        try {
            if (HasSpeech) { while (IsSpeaking) await Task.Delay(100, DestroyCancellationToken); }
            else { while (DateTime.UtcNow < fallbackSpeechEnd) await Task.Delay(100, DestroyCancellationToken); }
            await runtime.PostAsync("speech", new { speaking = false, release = true }, DestroyCancellationToken);
        } catch (OperationCanceledException) { }
    }
    void OnChatSent(string text)
    {
        if (started) _ = SafePostAsync("speech", new { speaking = false, release = false, cancel_pending = true });
    }
    async Task SafePostAsync(string endpoint, object payload)
    {
        try { await runtime.PostAsync(endpoint, payload, DestroyCancellationToken); }
        catch (Exception e) { Status = e.Message; }
    }

    async Task PollAsync(CancellationToken ct)
    {
        DateTime nextState = DateTime.MinValue, nextHeartbeat = DateTime.MinValue;
        HashSet<string> reportedFailures = new(); bool lastSpeaking = false;
        while (!ct.IsCancellationRequested) {
            try {
                bool speaking = IsSpeaking;
                if (HasSpeech && (speaking != lastSpeaking || speaking && DateTime.UtcNow >= nextHeartbeat)) {
                    await runtime.PostAsync("speech", new { speaking, duration_s = .7, release = false }, ct);
                    lastSpeaking = speaking; nextHeartbeat = DateTime.UtcNow.AddMilliseconds(300);
                }
                if (DateTime.UtcNow >= nextState) {
                    var state = await runtime.GetAsync("state", ct);
                    statePrompt = CompactState(state, false); UpdatePrompt();
                    Status = state.GetProperty("ready").GetBoolean() ? state.GetProperty("connected").GetBoolean() ? "身体运行中" : "窗口已关闭" : "模型加载中";
                    foreach (var task in state.GetProperty("tasks").EnumerateArray()) {
                        if (task.GetProperty("status").GetString() == "error" && reportedFailures.Add(task.GetProperty("task_id").GetString()!)) interactor.Poke("身体执行失败：" + task.GetRawText());
                    }
                    var events = await runtime.GetAsync("events?after=" + cursor, ct);
                    foreach (var e in events.GetProperty("events").EnumerateArray()) {
                        string text = e.GetProperty("text").GetString()!;
                        if (e.GetProperty("type").GetString() == "input") { if (OnInput != null) OnInput(text); else interactor.Chat(text); }
                        else { statePrompt += " 用户身体互动：" + text; UpdatePrompt(); }
                    }
                    cursor = events.GetProperty("cursor").GetInt64();
                    nextState = DateTime.UtcNow.AddSeconds(1);
                }
            } catch (OperationCanceledException) { break; }
            catch (Exception e) { Status = e.Message; statePrompt = "身体连接中断，未确认执行结果。"; UpdatePrompt(); nextState = DateTime.UtcNow.AddSeconds(3); }
            await Task.Delay(100, ct);
        }
    }

    static string CompactState(JsonElement state,bool detailed)=>Embodied3DPetBodyState.Describe(state,detailed);
    string OnBeforeChat(string text)
    {
        if(!started)return text;
        try {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(DestroyCancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(1.5));
            var snapshot=Task.Run(()=>runtime.GetAsync("state",timeout.Token)).GetAwaiter().GetResult();
            statePrompt=CompactState(snapshot,false);
        }catch(Exception){statePrompt="本轮身体即时感知暂时不可用，不能把旧状态当作当前事实；需要确认时调用 BodyState。";}
        UpdatePrompt();return text;
    }

    void EnsureStarted() { if (!started) throw new InvalidOperationException("3D 身体未启动，请先用 BodyConnect 或重新启用模块"); }

    public async Task ShowUsing(bool isUsing) { if (started && isUsing != usingState) { usingState = isUsing; await runtime.PostAsync("runtime", new { action = "using", enabled = isUsing }, DestroyCancellationToken); } }
    public async Task ShowSubtitle(string? subtitle) { if (started) await runtime.PostAsync("runtime", new { action = "subtitle", text = subtitle ?? "" }, DestroyCancellationToken); }
    public Task ShowExpression(string? expression) => BodyExpress(expression ?? "neutral");
    public async Task ShowMotion(string? motion) { if (!string.IsNullOrWhiteSpace(motion)) await BodyAct(action: motion); }
    public async Task<Vector2> GetPosition() { EnsureStarted(); var r = await runtime.PostAsync("runtime", new { action = "position" }, DestroyCancellationToken); return new Vector2((float)r.GetProperty("x").GetDouble(), (float)r.GetProperty("y").GetDouble()); }
    public async Task Move(Vector2 offset, float seconds) { EnsureStarted(); await runtime.PostAsync("runtime", new { action = "move", x = offset.X, y = offset.Y, seconds }, DestroyCancellationToken); }

    protected override async Task OnDestroy()
    {
        if (!active) return;
        caller.ChatCalledAsync -= OnReplyFinished; ChatBot.ChatSent -= OnChatSent; ChatBot.ChatSend -= OnBeforeChat;
        pollCts?.Cancel();
        if (polling != null) { try { await polling; } catch (OperationCanceledException) { } }
        if (ownsBody) {
            try { using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20)); await runtime.StopAsync(timeout.Token); }
            finally { lease.Release(); ownsBody = false; }
        }
        started = false; runtime.Dispose(); pollCts?.Dispose(); startGate.Dispose(); interactor.Dispose();
    }
}
