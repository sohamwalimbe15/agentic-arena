import asyncio
import hashlib
import json
import os
import random
import sqlite3
import time
from pathlib import Path

import anthropic
import httpx

# USD per million tokens (input, output). Only Anthropic models cost money.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# free OpenAI-compatible hosts: base url, key env var, default requests/min.
# The RPM defaults are conservative guesses, not published limits; set <PROVIDER>_RPM.
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", 20),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", 5),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", 10),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", 10),
}
RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)
MAX_WAIT = 900  # a longer Retry-After means a daily quota is gone; stop instead of sleeping


class BudgetExceeded(Exception):
    pass


class QuotaExhausted(BudgetExceeded):
    """free tier says to wait too long; runs abort cleanly and resume later"""


class ContextTruncated(Exception):
    pass


def load_env(path: str = ".env"):
    for line in Path(path).read_text().splitlines() if Path(path).exists() else []:
        k, _, v = line.partition("=")
        if v.strip() and not line.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())


load_env()


def split_model(m: str) -> tuple[str | None, str]:
    """'groq:llama-3.3-70b-versatile' -> ('groq', 'llama-3.3-70b-versatile'); no prefix -> Anthropic"""
    head, _, rest = m.partition(":")
    return (head, rest) if rest and (head in PROVIDERS or head == "ollama") else (None, m)


def model(role: str) -> str:
    if env := os.environ.get(f"ARENA_{role.upper()}_MODEL"):
        return env
    strong = role in ("target", "attacker")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "claude-sonnet-5-5" if role == "attacker" else "claude-haiku-5-5"
    for p, (_, key, _) in PROVIDERS.items():
        if os.environ.get(key):
            if p == "groq":
                return "groq:llama-3.3-70b-versatile" if strong else "groq:llama-3.1-8b-instant"
            if p == "gemini":  # id taken from Google's OpenAI-compat docs examples
                return "gemini:gemini-3.8-flash"
            raise RuntimeError(
                f"{key} is set but I don't know a {p} model; set ARENA_{role.upper()}_MODEL"
            )
    return "ollama:qwen2.5:7b" if strong else "ollama:qwen2.5:3b"


def price(m: str, usage: dict) -> float:
    if split_model(m)[0]:
        return 0.0
    pin, pout = PRICES[m]  # unknown paid model: KeyError, so spend can never go untracked
    return (usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6


def thinking_for(m: str) -> dict:
    if "haiku" in m:
        return {"thinking": {"type": "disabled"}}
    if "sonnet" in m:
        return {"thinking": {"type": "between_tools"}}
    return {}


class Store:
    """sqlite file holding the response cache and the spend ledger."""

    def __init__(self, path: str | None = None):
        path = path or os.environ.get("ARENA_DB", ".arena/cache.db")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("create table if not exists cache(k text primary key, v text)")
        self.db.execute("create table if not exists spend(ts real, model text, usd real)")

    def get(self, k: str):
        row = self.db.execute("select v from cache where k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, k: str, v: dict):
        self.db.execute("insert or replace into cache values(?,?)", (k, json.dumps(v)))
        self.db.commit()

    def charge(self, m: str, usd: float):
        self.db.execute("insert into spend values(?,?,?)", (time.time(), m, usd))
        self.db.commit()

    def total(self) -> float:
        return self.db.execute("select coalesce(sum(usd),0) from spend").fetchone()[0]


class Budget:
    """session cap plus a hard cap over everything ever recorded in the ledger."""

    def __init__(self, store: Store, session_usd: float, hard_usd: float | None = None):
        self.store, self.session = store, session_usd
        self.hard = (
            hard_usd if hard_usd is not None else float(os.environ.get("ARENA_HARD_CAP_USD", "15"))
        )
        self.spent = 0.0
        self.inflight = 0.0

    def reserve(self, est: float):
        if (
            self.spent + self.inflight + est > self.session
            or self.store.total() + self.inflight + est > self.hard
        ):
            raise BudgetExceeded(f"spent ${self.spent:.2f} of ${self.session:.2f}")
        self.inflight += est

    def settle(self, est: float, m: str, actual: float):
        self.inflight -= est
        self.spent += actual
        self.store.charge(m, actual)


class Llm:
    """the one place every model call goes through: cache, throttle, retries, concurrency, spend."""

    def __init__(
        self,
        store: Store,
        budget: Budget,
        client=None,
        conc: int | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self.store, self.budget = store, budget
        self._anthropic = client
        self.http = http or httpx.AsyncClient(timeout=600)
        self.sem = asyncio.Semaphore(conc or int(os.environ.get("ARENA_CONC", "12")))
        self.local_sem = asyncio.Semaphore(int(os.environ.get("ARENA_OLLAMA_CONC", "2")))
        self.locks: dict[str, asyncio.Lock] = {}
        self.next_ok: dict[str, float] = {}
        self.hits = self.calls = 0

    async def call(self, m, system, messages, tools=None, max_tokens=400, salt=""):
        """salt separates repeats of an identical prompt so the cache doesn't collapse them."""
        req = dict(model=m, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            req["tools"] = tools
        key = hashlib.sha256(json.dumps([req, str(salt)], sort_keys=True).encode()).hexdigest()
        if (hit := self.store.get(key)) is not None:
            self.hits += 1
            return hit
        prov, name = split_model(m)
        if prov:  # free backends cost $0, so no budget check
            out = await self._free(prov, name, system, messages, tools, max_tokens)
        else:
            out = await self._paid(req)
        self.calls += 1
        self.store.put(key, out)
        return out

    async def _paid(self, req):
        m = req["model"]
        pin, pout = PRICES[m]
        est = (len(json.dumps(req)) / 3 * pin + req["max_tokens"] * pout) / 1e6  # worst case
        self.budget.reserve(est)
        try:
            async with self.sem:
                r = await self._send(req | thinking_for(m))
        except BaseException:
            self.budget.inflight -= est
            raise
        out = clean(r)
        self.budget.settle(est, m, price(m, out["usage"]))
        return out

    async def _send(self, req):
        if self._anthropic is None:
            self._anthropic = anthropic.AsyncAnthropic(max_retries=0)
        for i in range(6):
            try:
                return await self._anthropic.messages.create(**req)
            except RETRYABLE:
                if i == 5:
                    raise
                await asyncio.sleep(min(30, 2**i) * (0.5 + random.random()))

    async def _free(self, prov, name, system, messages, tools, max_tokens):
        ollama = prov == "ollama"
        temp = float(os.environ.get("ARENA_TEMP", "0"))
        body = {"model": name, "messages": to_chat(system, messages, ollama)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        if ollama:
            ctx = int(os.environ.get("ARENA_NUM_CTX", "8192"))
            body |= {
                "stream": False,
                "options": {"num_ctx": ctx, "num_predict": max_tokens, "temperature": temp},
            }
            url, headers = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat", {}
        else:
            base, keyvar, _ = PROVIDERS[prov]
            if not os.environ.get(keyvar):
                raise RuntimeError(f"{keyvar} is not set")
            body |= {"max_tokens": max_tokens, "temperature": temp}
            url, headers = (
                base + "/chat/completions",
                {"Authorization": f"Bearer {os.environ[keyvar]}"},
            )
        async with self.local_sem if ollama else self.sem:
            j = (await self._post(prov, url, headers, body)).json()
        if ollama:
            msg, pin, pout = j["message"], j.get("prompt_eval_count", 0), j.get("eval_count", 0)
            if pin >= ctx - 8:  # Ollama truncates silently, which would corrupt results
                raise ContextTruncated(f"prompt used {pin} of num_ctx={ctx}; raise ARENA_NUM_CTX")
            calls = msg.get("tool_calls") or []
        else:
            msg = j["choices"][0]["message"]
            pin, pout = (
                j.get("usage", {}).get("prompt_tokens", 0),
                j.get("usage", {}).get("completion_tokens", 0),
            )
            calls = msg.get("tool_calls") or []
        blocks = [{"type": "text", "text": msg["content"]}] if msg.get("content") else []
        for n, c in enumerate(calls):
            args = c["function"].get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": c.get("id") or f"call_{n}_{abs(hash(str(c))) % 10**6}",
                    "name": c["function"]["name"],
                    "input": args,
                }
            )
        return {
            "content": blocks,
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {"input_tokens": pin, "output_tokens": pout},
        }

    async def _throttle(self, prov):
        rpm = float(os.environ.get(f"{prov.upper()}_RPM", PROVIDERS[prov][2]))
        async with self.locks.setdefault(prov, asyncio.Lock()):
            now = time.monotonic()
            wait = self.next_ok.get(prov, 0) - now
            self.next_ok[prov] = max(now, self.next_ok.get(prov, 0)) + 60 / rpm
            if wait > 0:
                await asyncio.sleep(wait)

    async def _post(self, prov, url, headers, body):
        for i in range(6):
            if prov != "ollama":
                await self._throttle(prov)
            try:
                r = await self.http.post(url, headers=headers, json=body)
            except httpx.TransportError:
                if i == 5:
                    raise
                await asyncio.sleep(min(30, 2**i) * (0.5 + random.random()))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers["retry-after"])
                except (KeyError, ValueError):
                    wait = min(60, 2**i) * (0.5 + random.random())
                if wait > MAX_WAIT:
                    raise QuotaExhausted(f"{prov} asked for a {wait:.0f}s wait")
                if i == 5:
                    r.raise_for_status()
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r


def to_chat(system: str, messages: list, ollama: bool) -> list:
    """Anthropic-style messages -> OpenAI/Ollama chat messages"""
    out = [{"role": "system", "content": system}]
    names: dict[str, str] = {}
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            calls = []
            for b in (b for b in c if b["type"] == "tool_use"):
                names[b["id"]] = b["name"]
                args = b["input"] if ollama else json.dumps(b["input"])
                calls.append(
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": args},
                    }
                )
            msg = {
                "role": "assistant",
                "content": "".join(b["text"] for b in c if b["type"] == "text"),
            }
            out.append(msg | ({"tool_calls": calls} if calls else {}))
        else:
            for b in (b for b in c if b["type"] == "tool_result"):
                t = {"role": "tool", "content": b["content"]}
                out.append(
                    t
                    | (
                        {"tool_name": names.get(b["tool_use_id"], "")}
                        if ollama
                        else {"tool_call_id": b["tool_use_id"]}
                    )
                )
            if txt := "".join(b["text"] for b in c if b["type"] == "text"):
                out.append({"role": "user", "content": txt})
    return out


def clean(r) -> dict:
    r = r if isinstance(r, dict) else r.model_dump()
    blocks = []
    for b in r["content"]:
        if b["type"] == "text":
            blocks.append({"type": "text", "text": b["text"]})
        elif b["type"] == "tool_use":
            blocks.append({k: b[k] for k in ("type", "id", "name", "input")})
    u = r["usage"]
    return {
        "content": blocks,
        "stop_reason": r.get("stop_reason"),
        "usage": {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]},
    }


def text_of(r: dict) -> str:
    return "".join(b["text"] for b in r["content"] if b["type"] == "text")
