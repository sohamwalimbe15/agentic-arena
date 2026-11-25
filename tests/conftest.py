import json
import os

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # never used: tests inject a fake client

from arena.llm import Budget, Llm, Store


class Fake:
    """stands in for AsyncAnthropic. script(req) returns a list of content blocks."""

    def __init__(self, script):
        self.script, self.reqs = script, []
        self.messages = self

    async def create(self, **req):
        self.reqs.append(req)
        return {
            "content": self.script(req),
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }


def text(t):
    return [{"type": "text", "text": t}]


def use(name, id_="t1", **inp):
    return [{"type": "tool_use", "id": id_, "name": name, "input": inp}]


@pytest.fixture
def mk(tmp_path):
    def make(script, session=1.0, hard=15.0):
        store = Store(str(tmp_path / "db"))
        fake = Fake(script)
        return Llm(store, Budget(store, session, hard), client=fake), fake

    return make


def last_user(req):
    m = req["messages"][-1]["content"]
    return m if isinstance(m, str) else json.dumps(m)
