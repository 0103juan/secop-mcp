"""Pregunta por la contratación pública en lenguaje natural: Claude como host MCP, server.py como servidor."""

import asyncio
import sys
import time
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import Client, StdioServerParameters
from model_gateway import AsyncGateway

LEDGER = Path(__file__).with_name(".gateway") / "ledger.jsonl"


async def ask(question: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py"))])
    async with Client(server) as mcp_client:
        tools = (await mcp_client.list_tools()).tools
        # model-gateway picks the model (no route, so the large tier, with its server-side refusal fallback)
        # and writes what every turn of the loop cost to the ledger.
        gateway = AsyncGateway(AsyncAnthropic(), key="chat", ledger=LEDGER)
        runner = gateway.tool_runner(
            task="chat",
            max_iterations=15,
            output_config={"effort": "medium"},
            system=(mcp_client.instructions or "") + " Responde en español.",
            tools=[async_mcp_tool(tool, mcp_client.session) for tool in tools],
            messages=[{"role": "user", "content": question}],
        )
        started, calls = time.monotonic(), 0
        async for message in runner:
            if message.stop_reason == "refusal":
                print("[el modelo rechazó la solicitud]")
            for block in message.content:
                if block.type == "tool_use":
                    calls += 1
                    print(f"  -> {block.name}({block.input})")
                elif block.type == "text":
                    print(block.text)
        # What the session cost, so a README can quote a measurement instead of an estimate.
        tokens_in, tokens_out, usd = (sum(turn[kind] for turn in gateway.calls)
                                      for kind in ("input_tokens", "output_tokens", "usd"))
        print(f"\n[{calls} llamadas a herramientas, {tokens_in} tokens de entrada, {tokens_out} de salida, "
              f"{usd:.4f} USD, {time.monotonic() - started:.0f} s]")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # piped output on Windows defaults to cp1252, which has no "≈"
    asyncio.run(ask(" ".join(sys.argv[1:]) or
                    "¿Cuáles fueron los cinco mayores contratistas de la Alcaldía de Medellín en 2024?"))
