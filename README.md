# github-mcp-chat

A production-oriented AI chatbot that lets you operate GitHub through natural language. It combines an LLM-powered conversational interface with a **GitHub MCP agent**, and applies real production-engineering patterns — streaming, retries, model fallback, human-in-the-loop approvals, graceful error handling, and full observability.

The chatbot understands a request, decides on its own whether GitHub access is needed, picks the right MCP tool, and — for any action that **writes** to GitHub — pauses and asks you to approve before it runs.

---

## Key Features

- **Conversational chat** with maintained context and **streamed** responses (token-by-token).
- **GitHub MCP agent** — lists repos, searches issues, reads issue details, creates issues, adds comments, and more. The model chooses the tool based on your request rather than a fixed script.
- **Human-in-the-loop approval** — read operations run automatically; any **write** (create issue, comment, push, merge, etc.) is held until you click **Approve** or **Reject**.
- **LLM fallback** — primary model is OpenAI; on appropriate failures it falls back to Google Gemini instead of failing the request.
- **Retry with exponential backoff** — transient errors (timeouts, rate limits, connection/5xx errors) are retried before any fallback kicks in.
- **Graceful error handling** — the user sees a clear message, never a raw stack trace.
- **Observability with Langfuse** — traces capture the session, LLM calls (model, tokens, latency, cost), agent/tool decisions, MCP execution, retries, and fallback usage.
- **GitHub OAuth login** (Chainlit) — the signed-in user's own GitHub token is used for MCP calls.
- **Per-user isolated MCP sessions** — each session gets its own containerized GitHub MCP server, pooled and auto-cleaned after idle timeout.
- **One-command startup** via Docker Compose.

---

## Architecture

```
        ┌──────────────┐
        │  Chainlit UI │   GitHub OAuth login, streaming, approve/reject buttons
        └──────┬───────┘
               │  HTTP (stream)
               ▼
        ┌──────────────┐
        │   FastAPI    │   /chat  and  /approve  endpoints
        │   Backend    │   pending-approval store, MCP session pool
        └──────┬───────┘
               │
               ▼
        ┌──────────────┐
        │   AI Agent   │   decides: chat vs. tool, which tool, approval needed?
        └──────┬───────┘
               │
      ┌────────┴────────┐
      ▼                 ▼
 ┌─────────┐       ┌─────────┐
 │ OpenAI  │  ──▶  │ Gemini  │   fallback on appropriate failures
 │  (LLM)  │       │ (LLM)   │
 └─────────┘       └─────────┘
               │
               ▼
        ┌──────────────┐
        │  GitHub MCP  │   one isolated container per user session
        │    Server    │
        └──────┬───────┘
               │
               ▼
            GitHub

  Observability: Langfuse traces every layer
  (LLM calls · tool calls · MCP execution · retries · fallback · errors)
```

### Request flow

```
User request → LLM call → agent decision → (approval, if write) → MCP tool call → GitHub → streamed final response
```

---

## Project Structure

```
github-mcp-chat/
├── chainlit-ui/            # Frontend
│   ├── app.py              # Chainlit app: OAuth, streaming, approval actions
│   ├── chainlit.md         # Welcome screen
│   ├── requirements.txt
│   └── Dockerfile
├── fastapi-backend/        # Backend + agent
│   ├── main.py             # FastAPI app, MCP session pool, approval logic
│   ├── llm.py              # OpenAI call + retry + Gemini fallback
│   ├── requirements.txt
│   └── Dockerfile          # includes docker CLI (spawns MCP containers)
├── docker-compose.yml      # Runs UI + backend together
├── .env                    # Secrets — NOT committed
├── .env.example            # Template for required variables
└── README.md
```

---

## Prerequisites

- **Docker** and **Docker Compose**
- The backend spawns the GitHub MCP server as a Docker container, so it needs access to the Docker socket. This is already wired in `docker-compose.yml` (mounts `/var/run/docker.sock`).
- API keys for OpenAI and Gemini
- A **GitHub Personal Access Token** (fallback / default) — or use GitHub OAuth login so each user supplies their own
- A **Langfuse** account (cloud or self-hosted) for tracing

---

## Environment Variables

Create a `.env` file in the project root. See `.env.example` for the template.

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key (primary LLM). Remove it to demo the retry/fallback path. |
| `GEMINI_API_KEY` | Yes | Google Gemini API key (fallback LLM). |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Yes* | Default GitHub token used when a user isn't logged in via OAuth. |
| `LANGFUSE_PUBLIC_KEY` | Yes | Langfuse public key for tracing. |
| `LANGFUSE_SECRET_KEY` | Yes | Langfuse secret key for tracing. |
| `LANGFUSE_HOST` | Yes | Langfuse host, e.g. `https://cloud.langfuse.com`. |
| `OAUTH_GITHUB_CLIENT_ID` | Optional | GitHub OAuth app client ID (for Chainlit login). |
| `OAUTH_GITHUB_CLIENT_SECRET` | Optional | GitHub OAuth app client secret. |
| `CHAINLIT_AUTH_SECRET` | Optional | Secret for Chainlit auth sessions (generate with `chainlit create-secret`). |
| `FASTAPI_URL` | Optional | Backend chat endpoint. Defaults to `http://localhost:8000/chat`; set by Compose. |
| `APPROVE_URL` | Optional | Backend approve endpoint. Defaults to `http://localhost:8000/approve`; set by Compose. |

\* Required unless every user logs in with GitHub OAuth and supplies their own token.

> **Never commit `.env`.** It contains live secrets that will be scraped and revoked if pushed publicly. Keep it in `.gitignore`.

---

## Running with Docker Compose (recommended)

```bash
docker compose up --build
```

- **Chainlit UI** → http://localhost:8001
- **FastAPI backend** → http://localhost:8000

Stop with `Ctrl+C`, or run detached with `docker compose up -d`.

---

## Running Locally (without Compose)

**Backend:**
```bash
cd fastapi-backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

**Frontend (in a second terminal):**
```bash
cd chainlit-ui
pip install -r requirements.txt
chainlit run app.py --port 8001
```

Make sure `.env` is present and Docker is running (the backend launches the GitHub MCP server as a container).

---

## How It Works

### Tool selection & approval
The model receives the GitHub MCP tools and decides which (if any) to call. Tool names in the backend's `WRITE_TOOLS` set — creating issues, comments, branches, files, pull requests, merges, etc. — are treated as **write actions**: the request is paused, the proposed tool and arguments are shown in the UI, and it only executes after you approve. All other (read) tools run immediately.

### Retry & fallback
`llm.py` retries transient OpenAI errors (timeout, connection, rate limit, 5xx) up to 3 times with exponential backoff. If OpenAI still fails — or on auth/permission errors — it falls back to Gemini. Fallback is triggered only for those specific error classes, not blindly for every error.

### Observability
Every request is wrapped in Langfuse spans: the chat request, each LLM call (with model, tokens, latency, cost), the agent's tool decision, MCP tool execution, retries, fallback usage, and errors. Open your Langfuse dashboard to trace a full workflow end to end.

---

## Example Prompts

- *"List my repositories."* → read, runs automatically.
- *"Find open issues related to authentication in owner/repo."* → searches issues, runs automatically.
- *"Create an issue about the login bug in owner/repo."* → prepares the issue, **asks for approval** before creating.
- *"Add a comment to issue #42 in owner/repo."* → reads the issue, **asks for approval** before commenting.

---

## Demo Checklist

1. **Normal conversation** — a non-GitHub question, streamed.
2. **GitHub read** — list repos or search issues.
3. **Retry** — trigger a transient failure and watch the backoff in logs/Langfuse.
4. **Fallback** — remove `OPENAI_API_KEY` from `.env` and confirm Gemini answers.
5. **Full trace** — open the Langfuse trace for a GitHub workflow.
6. **Failure handling** — cause a tool/API error and confirm the user gets a clean message.

---

## Notes

- **Models:** the code currently runs `gpt-4o` (primary) and `gemini-3.7-flash` (fallback), set in `fastapi-backend/llm.py`. Adjust `OPENAI_MODEL` / `GEMINI_MODEL` there if you want different models. (The task brief mentions GPT-5.5 / Gemini 3.7 Flash — align these if required.)
- **MCP toolsets:** enabled toolsets are `repos,issues` (see `GITHUB_TOOLSETS` in `main.py`). Extend as needed.
- **Session cleanup:** idle MCP session containers are closed automatically after 10 minutes of inactivity.

---

## Tech Stack

Chainlit · FastAPI · Python · OpenAI · Google Gemini · Model Context Protocol (GitHub MCP Server) · Langfuse · Docker & Docker Compose
