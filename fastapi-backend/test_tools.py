import os
import json
import asyncio
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "يرجع حالة الطقس الحالية لمدينة معينة",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "اسم المدينة، مثلاً 'Cairo'"
                    }
                },
                "required": ["city"]
            }
        }
    }
]


def get_weather(city: str) -> str:
    return f"الطقس في {city} حاليًا: 32 درجة مئوية، سماء صافية."


async def main():
    messages = [{"role": "user", "content": "إيه حالة الطقس في القاهرة؟"}]

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=tools
    )

    message = response.choices[0].message

    if message.tool_calls:
        messages.append(message)

        for tool_call in message.tool_calls:
            args = json.loads(tool_call.function.arguments)
            result = get_weather(city=args["city"])

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })

        final_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )

        print("الرد النهائي:", final_response.choices[0].message.content)
    else:
        print("الرد النهائي:", message.content)


asyncio.run(main())