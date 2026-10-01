import argparse
import asyncio
import json

from knowgrain.config import Settings
from knowgrain.lightrag_runtime import LightRAGRuntime, QueryMode

DEMO_SOURCE_ID = "knowgrain-quickstart-v1"
DEMO_FILE_PATH = "Sources/Files/knowgrain-quickstart.md"
DEMO_TEXT = """# Knowgrain 本地方案

Knowgrain 将 LightRAG Core 嵌入本地 Python 后端进程，不单独运行 LightRAG Server。
本地 Ollama 默认提供语言模型和文本嵌入；用户也可以将模型配置切换到第三方服务。
原始资料和已审阅的 Wiki 页面保存在 Obsidian 兼容的 Markdown Vault 文件夹中。
LightRAG 使用 PostgreSQL 保存文本块、实体关系图、向量和文档处理状态。
"""


async def run_demo(question: str, mode: QueryMode) -> None:
    runtime = LightRAGRuntime(Settings())
    await runtime.start()
    try:
        await runtime.index_text(
            source_id=DEMO_SOURCE_ID,
            text=DEMO_TEXT,
            file_path=DEMO_FILE_PATH,
        )
        result = await runtime.retrieve(question, mode=mode)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the embedded LightRAG local demo")
    parser.add_argument(
        "--question",
        default="Knowgrain 如何在本地结合 LightRAG 和 Obsidian Vault？",
        help="Question to retrieve evidence for",
    )
    parser.add_argument(
        "--mode",
        choices=("local", "global", "hybrid", "mix", "naive"),
        default="mix",
        help="LightRAG retrieval mode",
    )
    args = parser.parse_args()
    asyncio.run(run_demo(args.question, args.mode))


if __name__ == "__main__":
    main()
