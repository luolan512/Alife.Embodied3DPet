using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Marisa.Embodied3DPet;

internal sealed class Embodied3DPetRuntime : IDisposable
{
    public string DirectoryPath { get; private set; } = "";
    public int Port { get; private set; }
    readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(15) };
    readonly SemaphoreSlim connection = new(1, 1);
    string token = "";
    internal static string ResolveRuntimeDirectory(string pluginDirectory)
    {
        string expected = File.ReadAllText(Path.Combine(pluginDirectory, "Runtime.sha256")).Trim();
        string preferred=Path.GetFullPath(Path.Combine(pluginDirectory,"..","..","..","Runtime","Embodied3DPet","1.0.0-"+expected.Substring(0,12)));
        string legacy=Path.GetFullPath(Path.Combine(pluginDirectory,"..","..","..","Runtime","SoulPet3d","1.0.0-"+expected.Substring(0,12)));
        // Preserve installed assets when the old directory is held open by the host.
        return Directory.Exists(preferred)?preferred:Directory.Exists(legacy)?legacy:preferred;
    }
    public async Task StartAsync(string pluginDirectory, Embodied3DPetConfig config, CancellationToken ct)
    {
        await connection.WaitAsync(ct);
        try {
            if (config.Port < 1024 || config.Port > 65534) throw new ArgumentException("端口必须为 1024–65534");
            if (!System.Text.RegularExpressions.Regex.IsMatch(config.Model, "^[a-zA-Z0-9_-]+$")) throw new ArgumentException("模型名只能包含英文、数字、横线和下划线");
            string archive = Path.Combine(pluginDirectory, "Runtime.zip");
            string expected = File.ReadAllText(Path.Combine(pluginDirectory, "Runtime.sha256")).Trim();
            string cache = Path.GetFullPath(Path.Combine(pluginDirectory, "..", "..", "..", "Runtime", "Embodied3DPet"));
            Directory.CreateDirectory(cache);
            DirectoryPath = ResolveRuntimeDirectory(pluginDirectory);
            string marker = Path.Combine(DirectoryPath, ".installed");
            if (!File.Exists(marker) || File.ReadAllText(marker).Trim() != expected) {
                if (!File.Exists(archive)) {
                    string backup = FindArchiveBackup(pluginDirectory);
                    if (!File.Exists(backup)) throw new FileNotFoundException("运行缓存需要重建，请先从备份恢复 Runtime.zip", archive);
                    File.Copy(backup, archive, overwrite: false);
                }
                await Task.Run(() => {
                    using (var stream = File.OpenRead(archive)) {
                        string actual = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
                        if (actual != expected) throw new InvalidDataException("Runtime.zip 校验失败");
                    }
                    string temporary = DirectoryPath + ".partial-" + Guid.NewGuid().ToString("N");
                    Directory.CreateDirectory(temporary);
                    ZipFile.ExtractToDirectory(archive, temporary);
                    ct.ThrowIfCancellationRequested();
                    File.WriteAllText(Path.Combine(temporary, ".installed"), expected);
                    if (Directory.Exists(DirectoryPath)) throw new IOException("运行缓存不完整，请将该缓存目录改名后再试：" + DirectoryPath);
                    Directory.Move(temporary, DirectoryPath);
                }, ct);
            }
            ApplyRuntimePatch(pluginDirectory, DirectoryPath);
            File.WriteAllText(Path.Combine(DirectoryPath,"character-agent","config","desktop-control.json"),JsonSerializer.Serialize(new {enable_dragging=true}));
            Port = config.Port;
            // An existing listener must have both the adapter tag and this cache's process record.
            try {
                var health = await GetRawAsync("health", ct);
                if (health.GetProperty("adapter").GetString() != "Marisa.Embodied3DPet") throw new IOException("端口已被其他程序占用");
                var record = JsonDocument.Parse(File.ReadAllText(Path.Combine(DirectoryPath, "character-agent", ".run", "server.json"))).RootElement;
                if (record.GetProperty("pid").GetInt32() != health.GetProperty("pid").GetInt32() || record.GetProperty("port").GetInt32() != Port) throw new IOException("服务不属于本插件运行缓存");
            } catch (HttpRequestException) { }
            await RunScriptAsync("start.ps1", config, ct);
            var session = await GetRawAsync("session", ct);
            token = session.GetProperty("token").GetString()!;
            await RemoveInstalledArchiveAsync(pluginDirectory, DirectoryPath, ct);
        } finally { connection.Release(); }
    }

    internal static void ApplyRuntimePatch(string pluginDirectory,string cache)
    {
        string archive=Path.Combine(pluginDirectory,"Runtime-Patch.zip");
        if(!File.Exists(archive))return;
        using var stream=File.OpenRead(archive);
        string hash=Convert.ToHexString(SHA256.HashData(stream));
        if(hash!=File.ReadAllText(Path.Combine(pluginDirectory,"Runtime-Patch.sha256")).Trim().ToUpperInvariant())throw new InvalidDataException("运行补丁校验失败");
        string marker=Path.Combine(cache,".runtime-patch");
        if(File.Exists(marker)&&File.ReadAllText(marker)==hash)return;
        ZipFile.ExtractToDirectory(archive,cache,true);
        File.WriteAllText(marker,hash);
    }

    static string FindArchiveBackup(string pluginDirectory)
    {
        string record = Path.Combine(pluginDirectory, "Runtime-backup.json");
        if (!File.Exists(record)) return "";
        using var data = JsonDocument.Parse(File.ReadAllText(record));
        return data.RootElement.GetProperty("archive_path").GetString() ?? "";
    }

    internal static async Task<bool> RemoveInstalledArchiveAsync(string pluginDirectory, string cacheDirectory, CancellationToken ct)
    {
        string archive = Path.Combine(pluginDirectory, "Runtime.zip");
        if (!File.Exists(archive)) return false;
        string expected = File.ReadAllText(Path.Combine(pluginDirectory, "Runtime.sha256")).Trim();
        string marker = Path.Combine(cacheDirectory, ".installed");
        if (!File.Exists(marker) || File.ReadAllText(marker).Trim() != expected) return false;
        foreach (string required in new[] { "runtime/python/python.exe", "character-agent/dist/desktop/CharacterDesktopPet.exe", "character-agent/dist/web/pet.html", "character-agent/apps/agent-server/main.py", "kimodo.cpp/Llama-3-Kimodo-Q4_K.gguf", "kimodo.cpp/tokenizer.gguf", "ASSET-MANIFEST.json" })
            if (!File.Exists(Path.Combine(cacheDirectory, required))) return false;
        string backup = FindArchiveBackup(pluginDirectory);
        if (string.IsNullOrEmpty(backup) || Path.GetFullPath(backup).Equals(Path.GetFullPath(archive), StringComparison.OrdinalIgnoreCase) || !File.Exists(backup)) return false;
        using (var stream = File.OpenRead(backup)) {
            string actual = Convert.ToHexString(await SHA256.HashDataAsync(stream, ct)).ToLowerInvariant();
            if (actual != expected) throw new InvalidDataException("备份压缩包校验失败，已保留插件内 Runtime.zip");
        }
        ct.ThrowIfCancellationRequested();
        File.Delete(archive);
        return true;
    }

    public async Task<JsonElement> GetAsync(string endpoint, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{Port}/api/v1/alife/{endpoint}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return await SendAsync(request, ct);
    }
    public async Task<JsonElement> PostAsync(string endpoint, object payload, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, $"http://127.0.0.1:{Port}/api/v1/alife/{endpoint}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = new StringContent(JsonSerializer.Serialize(payload), System.Text.Encoding.UTF8, "application/json");
        return await SendAsync(request, ct);
    }
    internal async Task<JsonElement> ControlAsync(string endpoint, object? payload, CancellationToken ct)
    {
        if (endpoint != "motion-config" && endpoint != "text-encoder" && endpoint != "playback") throw new ArgumentException("Unsupported settings endpoint");
        using var request = new HttpRequestMessage(payload == null ? HttpMethod.Get : HttpMethod.Post, $"http://127.0.0.1:{Port}/api/v1/{endpoint}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (payload != null) request.Content = new StringContent(JsonSerializer.Serialize(payload), System.Text.Encoding.UTF8, "application/json");
        return await SendAsync(request, ct);
    }
    async Task<JsonElement> GetRawAsync(string endpoint, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{Port}/api/v1/{endpoint}");
        return await SendAsync(request, ct);
    }
    async Task<JsonElement> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        using var response = await http.SendAsync(request, ct);
        using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException(document.RootElement.TryGetProperty("error", out var error) ? error.GetString() : "身体服务请求失败");
        return document.RootElement.Clone();
    }
    async Task RunScriptAsync(string script, Embodied3DPetConfig? config, CancellationToken ct)
    {
        Directory.CreateDirectory(Path.Combine(DirectoryPath, ".run"));
        string log = Path.Combine(DirectoryPath, ".run", "alife-" + Path.GetFileNameWithoutExtension(script) + ".log");
        string runner = Path.Combine(DirectoryPath, "scripts", "alife-runner.ps1");
        // Never pipe a launcher whose detached GUI children can inherit its output handles.
        File.WriteAllText(runner, """
            param([string]$TaskScript,[string]$TaskLog,[int]$TaskPort=0,[string]$TaskModel='', [switch]$TaskNoFace)
            $ErrorActionPreference='Stop'
            try {
                if($TaskPort -gt 0){ & $TaskScript -Port $TaskPort -Model $TaskModel -NoFace:$TaskNoFace *> $TaskLog }
                else { & $TaskScript *> $TaskLog }
            } catch { $_ | Out-String | Add-Content -LiteralPath $TaskLog; exit 1 }
            """, System.Text.Encoding.UTF8);
        var info = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = DirectoryPath };
        foreach (string arg in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runner, "-TaskScript", Path.Combine(DirectoryPath, "scripts", script), "-TaskLog", log }) info.ArgumentList.Add(arg);
        if (config != null) {
            info.ArgumentList.Add("-TaskPort"); info.ArgumentList.Add(config.Port.ToString());
            info.ArgumentList.Add("-TaskModel"); info.ArgumentList.Add(config.Model);
            // Continuous facial animation is always enabled for Embodied3DPet. 
        }
        foreach (string key in new[] { "CHARACTER_PYTHON", "PYTHONHOME", "PYTHONPATH", "CHARACTER_FACE_DEVICE", "ARDY_LLM_API_KEY" }) info.Environment.Remove(key);
        using var process = Process.Start(info) ?? throw new IOException("无法启动桌宠脚本");
        try { await process.WaitForExitAsync(ct); }
        catch (OperationCanceledException) { if (!process.HasExited) process.Kill(entireProcessTree: true); throw; }
        if (process.ExitCode != 0) throw new IOException("桌宠脚本失败，查看 " + log);
    }
    public Task StopAsync(CancellationToken ct) => string.IsNullOrEmpty(DirectoryPath) ? Task.CompletedTask : RunScriptAsync("stop.ps1", null, ct);
    public void Dispose() { http.Dispose(); connection.Dispose(); }
}
