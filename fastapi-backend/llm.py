import os
from dotenv import load_dotenv
from openai import (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
    AuthenticationError,
    PermissionDeniedError,
)
from langfuse.openai import AsyncOpenAI
from langfuse import observe, get_client
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from google import genai

load_dotenv()

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

OPENAI_MODEL = "gpt-4o"
GEMINI_MODEL = "gemini-3.7-flash"

RETRYABLE_ERRORS = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)
FALLBACK_ERRORS = RETRYABLE_ERRORS + (AuthenticationError, PermissionDeniedError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    reraise=True
)
async def _call_openai(messages, tools=None, stream=False):
    return await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        tools=tools,
        stream=stream
    )


def _messages_to_gemini_contents(messages):
    contents = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
        elif role == "assistant" and m.get("content"):
            contents.append({"role": "model", "parts": [{"text": m["content"]}]})
    return contents


@observe(as_type="generation", name="gemini-fallback")
async def _call_gemini(messages):
    contents = _messages_to_gemini_contents(messages)
    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents
    )
    return response.text


async def get_completion(messages, tools=None):
    try:
        response = await _call_openai(messages, tools=tools, stream=False)
        return {"source": "openai", "message": response.choices[0].message, "text": None}
    except FALLBACK_ERRORS as e:
        get_client().update_current_span(
            level="WARNING",
            status_message=f"OpenAI failed ({type(e).__name__}), falling back to Gemini"
        )
        try:
            text = await _call_gemini(messages)
            return {"source": "gemini", "message": None, "text": text}
        except Exception as gemini_error:
            get_client().update_current_span(level="ERROR", status_message=str(gemini_error))
            return {
                "source": "error",
                "message": None,
                "text": "Sorry, I'm having trouble reaching the AI service right now. Please try again in a moment."
            }


async def get_stream(messages):
    try:
        stream = await _call_openai(messages, tools=None, stream=True)
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except FALLBACK_ERRORS as e:
        get_client().update_current_span(
            level="WARNING",
            status_message=f"OpenAI streaming failed ({type(e).__name__}), falling back to Gemini"
        )
        try:
            text = await _call_gemini(messages)
            yield text
        except Exception as gemini_error:
            get_client().update_current_span(level="ERROR", status_message=str(gemini_error))
            yield "Sorry, I'm having trouble reaching the AI service right now. Please try again in a moment."