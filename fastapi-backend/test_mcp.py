import os
import json
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters


load_dotenv()

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

server_params = StdioServerParameters(
    command="docker",
    args=[
        "run", "-i", "--rm",
        "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
        "-e", "GITHUB_TOOLSETS=repos,issues",
        "ghcr.io/github/github-mcp-server"
    ],
    env={"GITHUB_PERSONAL_ACCESS_TOKEN": os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")}
)


def mcp_tools_to_openai_format(mcp_tools):
    openai_tools = []
    for tool in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema
            }
        })
    return openai_tools


async def main():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools_result = await session.list_tools()
            openai_tools = mcp_tools_to_openai_format(mcp_tools_result.tools)

            messages = [
                {"role": "user", "content": "ابحث عن الـ issues المفتوحة في مستودع BaslNor/alx-zero_day"}
            ]

            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=openai_tools
            )

            message = response.choices[0].message

            if message.tool_calls:
                messages.append(message)

                for tool_call in message.tool_calls:
                    print(f"الموديل عايز يستخدم: {tool_call.function.name}")
                    args = json.loads(tool_call.function.arguments)
                    print(f"بالـ arguments: {args}")

                    result = await session.call_tool(tool_call.function.name, args)
                    result_text = result.content[0].text

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text
                    })

                final_response = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=messages
                )
                print("\nالرد النهائي:", final_response.choices[0].message.content)
            else:
                print("الرد النهائي:", message.content)


asyncio.run(main())