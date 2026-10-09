# 第三方依赖与资源

- Alife：https://github.com/BDFFZI/Alife 。宿主与插件 API，不随本仓库复制。
- ARDY：NVIDIA ARDY（https://github.com/nv-tlabs/ardy），当前使用的源码在 `runtime/ardy/`，保留 `LICENSE` 和 `ATTRIBUTIONS.MD`。模型来源 https://huggingface.co/nvidia/ARDY-Core-RP-20FPS-Horizon40 。模型权重不包含在仓库。
- Kimodo / kimodo.cpp：外部文本编码后端。源码项目 https://github.com/localai-org/kimodo.cpp ，GGUF 来源 https://huggingface.co/LocalAI-io/Llama-3-Kimodo-GGML 。本仓库不包含其二进制或模型。
- EmoAva / CTEG / FLAME 配套资源：https://github.com/WalkerMitty/EmoAva 。通过环境清单获取，不随仓库分发。可下载不等于可任意重新授权；各资源使用及再分发须遵循其原始条款。
- BERT tokenizer：https://huggingface.co/google-bert/bert-base-cased 。未包含模型或 tokenizer 数据文件。
- Three.js、@pixiv/three-vrm、Vite 等前端依赖：版本见 `frontend/package.json` 和锁文件，通过 npm 获取，遵循各自许可证。
- Microsoft.Web.WebView2：通过 NuGet 获取，版本见 `desktop-host/DesktopPet.csproj`。
- Python、PyTorch、CUDA、TensorRT 等依赖未包含在本仓库，使用和分发遵循各自条款。

本清单描述源码依赖，不宣称拥有第三方模型或素材版权。用户导入的 VRM 应具有相应使用权限。
