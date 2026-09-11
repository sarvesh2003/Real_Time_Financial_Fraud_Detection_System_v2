import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"


async def main():
    tool_name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)], env=dict(os.environ))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            for item in result.content:
                if hasattr(item, "text"):
                    print(item.text)
                else:
                    print(item)


if __name__ == "__main__":
    asyncio.run(main())
