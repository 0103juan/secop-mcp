"""Pregunta por la contratación pública en lenguaje natural: Claude como host MCP, server.py como servidor."""

import asyncio
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import Client, StdioServerParameters

MODEL = "claude-sonnet-5-5"


async def ask(question: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py"))])
    async with Client(server) as mcp_client:
        tools = (await mcp_client.list_tools()).tools
        runner = AsyncAnthropic().beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            max_iterations=15,
            output_config={"effort": "medium"},
            # a safety decline is re-run on Anthropic's recommended fallback model instead of failing
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=(mcp_client.instructions or "") + " Responde en español.",
            tools=[async_mcp_tool(tool, mcp_client.session) for tool in tools],
            messages=[{"role": "user", "content": question}],
        )
        async for message in runner:
            if message.stop_reason == "refusal":
                print("[el modelo rechazó la solicitud]")
            for block in message.content:
                if block.type == "tool_use":
                    print(f"  -> {block.name}({block.input})")
                elif block.type == "text":
                    print(block.text)


if __name__ == "__main__":
    asyncio.run(ask(" ".join(sys.argv[1:]) or
                    "¿Cuáles fueron los cinco mayores contratistas de la Alcaldía de Medellín en 2024?"))
