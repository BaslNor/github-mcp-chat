import os
import json
import time
import asyncio
from fastapi import FastAPI
from dotenv import load_dotenv
from pydantic import BaseModel
from mcp.client.stdio import stdio_client
from llm import get_completion, get_stream
from fastapi.responses import StreamingResponse
from mcp import ClientSession, StdioServerParameters
from contextlib import asynccontextmanager, AsyncExitStack
from langfuse import observe, propagate_attributes, get_client


load_dotenv()


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


WRITE_TOOLS = {
    "issue_write", "add_issue_comment", "sub_issue_write", "create_repository",
    "create_branch", "create_or_update_file", "delete_file", "push_files",
    "fork_repository", "create_pull_request", "merge_pull_request",
    "update_pull_request", "update_pull_request_branch", "pull_request_review_write",
    "add_comment_to_pending_review", "add_reply_to_pull_request_comment",
    "request_copilot_review", "assign_copilot_to_issue",
}


class UserMCPSessionManager:
    def __init__(self, idle_timeout: int = 600):
        self.sessions: dict[str, dict] = {}
        self.idle_timeout = idle_timeout
        self._lock = asyncio.Lock()

    def _get_server_params(self, github_token: str | None) -> StdioServerParameters:
        token = github_token or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
        return StdioServerParameters(
            command="docker",
            args=[
                "run", "-i", "--rm",
                "-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={token}",
                "-e", "GITHUB_TOOLSETS=repos,issues",
                "ghcr.io/github/github-mcp-server"
            ],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": token}
        )

    async def get_or_create_session(self, session_id: str, github_token: str | None) -> tuple[ClientSession, list[dict]]:
        async with self._lock:
            token_to_use = github_token or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")

            if session_id in self.sessions:
                cached = self.sessions[session_id]
                if cached["token"] == token_to_use:
                    cached["last_active"] = time.time()
                    return cached["session"], cached["tools"]
                else:
                    await self._close_session_unlocked(session_id)

            stack = AsyncExitStack()
            try:
                server_params = self._get_server_params(token_to_use)
                read, write = await stack.enter_async_context(stdio_client(server_params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                tools_result = await session.list_tools()
                openai_tools = mcp_tools_to_openai_format(tools_result.tools)

                self.sessions[session_id] = {
                    "stack": stack,
                    "session": session,
                    "tools": openai_tools,
                    "token": token_to_use,
                    "last_active": time.time()
                }
                print(f"✅ [MCP Pool] Opened new session container for session_id: {session_id}")
                return session, openai_tools
            except Exception as e:
                await stack.aclose()
                raise e

    async def _close_session_unlocked(self, session_id: str):
        if session_id in self.sessions:
            cached = self.sessions.pop(session_id)
            try:
                await cached["stack"].aclose()
                print(f"🧹 [MCP Pool] Closed session container for session_id: {session_id}")
            except Exception as e:
                print(f"⚠️ Error closing MCP session {session_id}: {e}")

    async def close_session(self, session_id: str):
        async with self._lock:
            await self._close_session_unlocked(session_id)

    async def cleanup_idle_sessions(self):
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                now = time.time()
                to_delete = [
                    sid for sid, data in self.sessions.items()
                    if now - data["last_active"] > self.idle_timeout
                ]
                for sid in to_delete:
                    await self._close_session_unlocked(sid)

    async def close_all(self):
        async with self._lock:
            for sid in list(self.sessions.keys()):
                await self._close_session_unlocked(sid)


mcp_manager = UserMCPSessionManager(idle_timeout=600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(mcp_manager.cleanup_idle_sessions())
    app.state.mcp_manager = mcp_manager
    app.state.pending_approvals = {}

    print("🚀 App initialized with Dynamic User MCP Session Pool")

    yield

    print("🛑 Shutting down all active user MCP sessions...")
    cleanup_task.cancel()
    await mcp_manager.close_all()


app = FastAPI(lifespan=lifespan)


class ChatRequest(BaseModel):
    messages: list[dict]
    session_id: str
    github_token: str | None = None


class ApprovalRequest(BaseModel):
    session_id: str
    approved: bool
    github_token: str | None = None


async def call_mcp_tool(session: ClientSession, tool_name: str, args: dict):
    with get_client().start_as_current_observation(as_type="span", name=f"mcp_tool:{tool_name}") as span:
        span.update(input=args)
        try:
            result_tool = await session.call_tool(tool_name, args)
            result_text = result_tool.content[0].text
            span.update(output=result_text)
            return result_text
        except Exception as e:
            span.update(level="ERROR", status_message=str(e))
            return f"Error: the tool '{tool_name}' failed to execute. Details: {str(e)}"


@observe(name="chat_request", capture_input=False, capture_output=False)
async def generate_response(messages: list[dict], session: ClientSession, tools: list[dict], session_id: str, app_state, github_token: str | None = None):
    get_client().update_current_span(input={"messages": messages, "session_id": session_id})
    full_output = ""

    with propagate_attributes(session_id=session_id):
        try:
            result = await get_completion(messages, tools=tools)

            if result["source"] in ("gemini", "error"):
                event = {"type": "token", "content": result["text"]}
                yield json.dumps(event) + "\n"
                get_client().update_current_span(output=result["text"])
                return

            message = result["message"]

            if message.tool_calls:
                write_call = None
                for tool_call in message.tool_calls:
                    if tool_call.function.name in WRITE_TOOLS:
                        write_call = tool_call
                        break

                if write_call:
                    args = json.loads(write_call.function.arguments)
                    app_state.pending_approvals[session_id] = {
                        "tool_call_id": write_call.id,
                        "tool_name": write_call.function.name,
                        "args": args,
                        "messages": messages,
                        "assistant_message": message,
                        "github_token": github_token
                    }
                    event = {
                        "type": "approval_required",
                        "tool_name": write_call.function.name,
                        "args": args
                    }
                    yield json.dumps(event) + "\n"
                    get_client().update_current_span(output={"approval_required": write_call.function.name})
                    return

                messages.append(message)

                for tool_call in message.tool_calls:
                    args = json.loads(tool_call.function.arguments)
                    result_text = await call_mcp_tool(session, tool_call.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text
                    })

                async for chunk in get_stream(messages):
                    full_output += chunk
                    event = {"type": "token", "content": chunk}
                    yield json.dumps(event) + "\n"
            else:
                async for chunk in get_stream(messages):
                    full_output += chunk
                    event = {"type": "token", "content": chunk}
                    yield json.dumps(event) + "\n"

            get_client().update_current_span(output=full_output)

        except Exception as e:
            get_client().update_current_span(level="ERROR", status_message=str(e))
            event = {"type": "error", "content": "Something went wrong while processing your request. Please try again."}
            yield json.dumps(event) + "\n"


@app.post("/chat")
async def chat(request: ChatRequest):
    manager: UserMCPSessionManager = app.state.mcp_manager
    session, tools = await manager.get_or_create_session(request.session_id, request.github_token)

    return StreamingResponse(
        generate_response(
            request.messages, session, tools, request.session_id, app.state, request.github_token
        ),
        media_type="text/plain"
    )


@observe(name="approval_request", capture_input=False, capture_output=False)
async def handle_approval(session_id: str, approved: bool, session: ClientSession, app_state, github_token: str | None = None):
    get_client().update_current_span(input={"session_id": session_id, "approved": approved})

    with propagate_attributes(session_id=session_id):
        try:
            pending = app_state.pending_approvals.get(session_id)

            if not pending:
                event = {"type": "token", "content": "No pending action found."}
                yield json.dumps(event) + "\n"
                return

            del app_state.pending_approvals[session_id]

            if not approved:
                event = {"type": "token", "content": "Action cancelled."}
                yield json.dumps(event) + "\n"
                return

            messages = pending["messages"]
            messages.append(pending["assistant_message"])

            result_text = await call_mcp_tool(session, pending["tool_name"], pending["args"])

            messages.append({
                "role": "tool",
                "tool_call_id": pending["tool_call_id"],
                "content": result_text
            })

            async for chunk in get_stream(messages):
                event = {"type": "token", "content": chunk}
                yield json.dumps(event) + "\n"

        except Exception as e:
            get_client().update_current_span(level="ERROR", status_message=str(e))
            event = {"type": "error", "content": "Something went wrong while processing your approval. Please try again."}
            yield json.dumps(event) + "\n"


@app.post("/approve")
async def approve(request: ApprovalRequest):
    manager: UserMCPSessionManager = app.state.mcp_manager
    session, _ = await manager.get_or_create_session(request.session_id, request.github_token)

    return StreamingResponse(
        handle_approval(request.session_id, request.approved, session, app.state, request.github_token),
        media_type="text/plain"
    )