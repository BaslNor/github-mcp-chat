import os
import httpx
import json
import chainlit as cl
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    load_dotenv(dotenv_path=env_path)

FASTAPI_URL = os.getenv("FASTAPI_URL", "http://localhost:8000/chat")
APPROVE_URL = os.getenv("APPROVE_URL", "http://localhost:8000/approve")

@cl.oauth_callback
def oauth_callback(
    provider_id: str,
    token: str,
    raw_user_data: dict,
    default_user: cl.User,
):
    if provider_id == "github":
        username = raw_user_data.get("login")
        return cl.User(
            identifier=username,
            metadata={
                "role": "user",
                "github_token": token,
                "name": raw_user_data.get("name")
            }
        )
    return None


@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])
    user = cl.user_session.get("user")
    if user and user.metadata:
        token = user.metadata.get("github_token")
        cl.user_session.set("github_token", token)


async def stream_response(client: httpx.AsyncClient, url: str, payload: dict):
    full_response = ""
    msg = cl.Message(content="")
    await msg.send()

    async with client.stream("POST", url, json=payload) as response:
        async for line in response.aiter_lines():
            if not line:
                continue

            event = json.loads(line)

            if event["type"] == "token":
                full_response += event["content"]
                await msg.stream_token(event["content"])

            elif event["type"] == "error":
                await msg.update()
                await cl.Message(content=f"⚠️ {event['content']}").send()
                return None

            elif event["type"] == "approval_required":
                await msg.update()
                await ask_for_approval(event)
                return None

    await msg.update()
    return full_response


async def ask_for_approval(event: dict):
    tool_name = event["tool_name"]
    args = event["args"]

    actions = [
        cl.Action(name="approve_yes", payload={"approved": True}, label="✅ Approve"),
        cl.Action(name="approve_no", payload={"approved": False}, label="❌ Reject")
    ]

    await cl.Message(
        content=f"The assistant wants to run **{tool_name}** with these arguments:\n```json\n{json.dumps(args, indent=2)}\n```",
        actions=actions
    ).send()


@cl.on_message
async def main(message: cl.Message):
    history = cl.user_session.get("history")
    history.append({"role": "user", "content": message.content})
    github_token = cl.user_session.get("github_token")

    async with httpx.AsyncClient(timeout=60.0) as client:
        full_response = await stream_response(
            client,
            FASTAPI_URL,
            {
                "messages": history,
                "session_id": cl.context.session.id,
                "github_token": github_token
            }
        )

    if full_response is not None:
        history.append({"role": "assistant", "content": full_response})
        cl.user_session.set("history", history)


@cl.action_callback("approve_yes")
@cl.action_callback("approve_no")
async def on_approve(action: cl.Action):
    approved = action.payload["approved"]
    history = cl.user_session.get("history")
    github_token = cl.user_session.get("github_token")

    await action.remove()

    async with httpx.AsyncClient(timeout=60.0) as client:
        full_response = await stream_response(
            client,
            APPROVE_URL,
            {
                "session_id": cl.context.session.id,
                "approved": approved,
                "github_token": github_token
            }
        )

    if full_response:
        history.append({"role": "assistant", "content": full_response})
        cl.user_session.set("history", history)