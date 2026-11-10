# 依赖、来源与许可证边界

## 本仓库没有整体开源授权

本项目是使用原创合成数据的本地 Demo／测试版，用于生产部署前的本地验证，不包含企业生产材料或既有模型权重。当前**没有 LICENSE 文件，也没有整体开源许可证声明**。不要由“源码可见”“使用 MIT 依赖”或“公开上游模型”推导出本仓库整体可再分发、商用或改许可。发布前需要作者明确授权，并分别复核依赖、模型、转换产物及数据的权利边界。

本文是依赖边界说明，不是完成的法律审查或完整 SBOM；不替代精确版本的上游 LICENSE、NOTICE、模型卡及模型分发仓库条款。

## Python 与前端依赖

- Python `>=3.12,<3.13`，构建后端 Hatchling `>=1.27`，wheel 包目录为 `quality_analysis`，入口点 `quality = quality_analysis.cli:main`。
- LangGraph 固定为 `1.2.5`；其他声明范围见 `pyproject.toml`，实际解析版本、公开下载 URL 和哈希见 `uv.lock`。
- 当前 lock 中 registry 指向公开阿里云 PyPI 镜像。原安装说明记录公开 PyPI 文件端点超时后改镜像；哈希约束提升复现能力，但不能替代供应链审查，也不表示验证脚本会联网。
- 浏览器工作台为仓库内原生 JS/CSS/HTML，无 CDN 或 npm 构建依赖。可选 `browser_smoke.mjs` 当前仅适用于 macOS，使用已安装在 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` 的 Chrome、可执行的 Node 和项目 `.venv/bin/python`，没有浏览器路径覆盖参数；不能将此脚本视为跨平台验收。浏览器自身许可证独立。

主要上游通常采用的许可如下；**此表不是对锁定发行物逐文件复核已完成的声明**：

| 依赖 | 用途 | 上游许可边界 |
| --- | --- | --- |
| FastAPI、Pydantic、Typer | API、输入约束、命令生态 | MIT |
| python-multipart | multipart 解析依赖 | Apache-2.0；本地锁定 0.0.32 的元数据及所附 LICENSE.txt 已核对，不属于 MIT 组 |
| Uvicorn、HTTPX | 本地 ASGI 与 HTTP | BSD-3-Clause |
| LangGraph | 单 Agent 有界图 | MIT；传递依赖独立复核 |
| NumPy、SciPy | 向量和统计 | BSD-3-Clause；二进制发行物内附加库声明独立保留 |
| SQLGlot | SQL AST 白名单校验 | MIT |
| pypdf | 有文本层 PDF 解析 | BSD-3-Clause |
| Hatchling、pytest、Ruff | 构建、测试、静态检查 | MIT |
| uv | 外部环境/构建工具，不打入应用 wheel | MIT 或 Apache-2.0，上游双许可 |

LangGraph 的传递依赖包含 tracing 相关组件；“依赖存在”不代表允许上传，本工程执行图时明确关闭 tracing。构建/运行所需依赖不会打包为模型或私有环境副本。最终安装环境与 wheel 的依赖解析、许可文本和 NOTICE 保留仍需发布者复核。

## 模型与推理引擎：单独取得、单独核验

以下记录上游身份与本次本地测试使用的公开 GGUF 发行物。三份权重均已下载并逐文件核对字节数和 SHA-256；模型及本机构建的引擎仍保留在仓库之外。文件完整性校验不替代许可证法律审查或转换算法验证。

| 组件 | 上游参考 | 指定格式/许可注意事项 |
| --- | --- | --- |
| llama.cpp | https://github.com/ggml-org/llama.cpp | MIT；固定 `b6500` / `a7a98e0fffed794396b3fbad4dcdbbc184963645`，本机构建二进制/库仍需哈希校验 |
| Qwen3-4B-Instruct-2507 | https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507 | 上游标示 Apache-2.0；本项目目标为 GGUF `Q4_K_M`，量化/转换分发者的版本和声明必须另核验 |
| BGE-M3 | https://huggingface.co/BAAI/bge-m3 | 上游标示 MIT；本项目使用 FP16 dense 1024 维，不宣称实现全部 BGE-M3 模式 |
| BGE-reranker-v2-m3 | https://huggingface.co/BAAI/bge-reranker-v2-m3 | 上游标示 Apache-2.0；FP16 转换产物来源、转换版本和原模型许可需分别保留 |

模型原仓库与 GGUF 发行仓库不是同一概念。仅有“Q4_K_M/FP16”或模型名不能确定转换代码、revision、文件内容和许可；`QA_RUNTIME_CONFIG` 的 SHA-256 可以验证指定文件一致性，不能证明文件来源合法或生成转换过程正确。公开发布时不附模型、运行二进制、依赖库或含本机绝对路径的配置，也不在脚本里自动下载这些资源。

### 实际使用的公开发行物

| 用途 | GGUF 发行仓库 | 固定 revision | 文件 / 字节数 |
| --- | --- | --- | --- |
| 生成 | `unsloth/Qwen3-4B-Instruct-2507-GGUF` | `0b0406b39725d752255ffeb48f102c66f98e14aa` | `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` / 2,497,281,120 |
| 嵌入 | `gpustack/bge-m3-GGUF` | `23ab3dbb7c04d72e859be33b01cb9c6c16dadbaf` | `bge-m3-FP16.gguf` / 1,157,671,200 |
| 重排 | `gpustack/bge-reranker-v2-m3-GGUF` | `d49f7d400a3f5fa38ebd1ffe116b2ef15ffefbc2` | `bge-reranker-v2-m3-FP16.gguf` / 1,159,776,896 |

按上表顺序，已核对的 SHA-256：

```text
3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597
daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c
5df93be121c09c43432102ad2b9569d369ccb85c209ca7583e8ccd28f0e41b88
```

本次下载采用公开路径 `https://modelscope.cn/models/{发行仓库}/resolve/{revision}/{文件名}`。操作者须自行确认当前来源与上游条款，再显式下载；应用、配置脚本和验收脚本不自动下载。不要更换量化文件后仍沿用旧哈希或旧验收结果。

llama.cpp 使用固定提交的 850 个源文件，逐项核对 Git blob 身份后本机构建；曾缺失的 18 个文件通过同一提交的公开 GitHub blob API 补齐。部分归档下载失败记录保留，未用损坏归档构建。引擎、库的 SHA-256 随具体构建产生，由配置工具写入本地 JSON，不将本机路径写入仓库。

## 原创 fixture 与效果陈述

业务数据、历史报告、英文/中文查询是本仓库原创合成 fixture，用来检验权限、统计、引用和图状态契约，不从生产案例反推效果。人写五摘要仅作为检索元数据，不是模型训练数据集；入库不训练 LLM。不宣称生产检索精度、根因命中率、节约工时、部署数量或企业收益。

真实模型测试如果完成，也只能说明指定版本/硬件/配置在这些 fixture 上的结果；不能外推到生产安全、其他精度模型、其他许可证版本或其他资料分布。
