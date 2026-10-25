import json
from datetime import date
from types import SimpleNamespace as NS

from deal_desk.agent import assess
from deal_desk.audit import JsonlAuditStore
from deal_desk.config import Settings
from deal_desk.runner import run
from tests.test_guardrails import ACTIVITIES, OPP_ID, TODAY, at_risk, opportunity

SETTINGS = Settings(_env_file=None, model="claude-sonnet-5-5")


def tool_use(name, opp_id=OPP_ID, id_="tu1"):
    return NS(type="tool_use", id=id_, name=name, input={"opportunity_id": opp_id})


def reply(stop_reason, *content):
    return NS(stop_reason=stop_reason, content=list(content), model="claude-sonnet-5-5",
              usage=NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                       cache_read_input_tokens=500))


def text(s):
    return NS(type="text", text=s)


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"], default=lambda o: vars(o))))
        return self.responses.pop(0)


class FakeSF:
    def __init__(self, opps=None):
        self.opps = opps or [opportunity()]
        self.calls = []
        self.tasks = []

    def search_opportunities(self, limit=50):
        return [{"Id": o["Id"], "Name": o["Name"]} for o in self.opps]

    def get_opportunity_details(self, opp_id):
        self.calls.append(("details", opp_id))
        return next(o for o in self.opps if o["Id"] == opp_id)

    def get_activity_history(self, opp_id):
        self.calls.append(("activities", opp_id))
        return ACTIVITIES

    def has_open_agent_task(self, opp_id):
        return any(t[0] == opp_id for t in self.tasks)

    def create_task(self, opp_id, **fields):
        self.tasks.append((opp_id, fields))
        return "00TNEW"

    def api_usage(self):
        return {"used": 1, "total": 15000}


def good_run_llm():
    return FakeLLM(
        reply("tool_use", tool_use("get_opportunity_details", id_="a"),
              tool_use("get_activity_history", id_="b")),
        reply("end_turn", text(at_risk().model_dump_json())),
    )


def test_tool_loop_then_valid_output():
    sf = FakeSF()
    out = assess(good_run_llm(), sf, OPP_ID, SETTINGS, TODAY)
    assert out.assessment.status == "at_risk" and out.error is None
    assert out.tool_calls == 2 and out.validation_attempts == 1
    assert sf.calls == [("details", OPP_ID), ("activities", OPP_ID)]  # fetched once, reused by routing
    assert out.usage["input_tokens"] == 2000


def test_invalid_output_is_retried_once_with_the_error():
    llm = FakeLLM(reply("end_turn", text('{"status": "at_risk"}')),
                  reply("end_turn", text(at_risk().model_dump_json())))
    out = assess(llm, FakeSF(), OPP_ID, SETTINGS, TODAY)
    assert out.assessment and out.validation_attempts == 2
    assert "failed validation" in llm.requests[1][-1]["content"]


def test_second_invalid_output_falls_back_to_no_assessment():
    llm = FakeLLM(reply("end_turn", text("not json")), reply("end_turn", text("still not json")))
    out = assess(llm, FakeSF(), OPP_ID, SETTINGS, TODAY)
    assert out.assessment is None and out.error and out.validation_attempts == 2


def test_model_cannot_read_other_opportunities():
    llm = FakeLLM(reply("tool_use", tool_use("get_opportunity_details", opp_id="006OTHER000000000")),
                  reply("end_turn", text(at_risk().model_dump_json())))
    sf = FakeSF()
    assess(llm, sf, OPP_ID, SETTINGS, TODAY)
    tool_result = llm.requests[1][-1]["content"][0]
    assert tool_result["is_error"] and "Out of scope" in tool_result["content"]
    assert ("details", "006OTHER000000000") not in sf.calls


def test_refusal_becomes_an_error_not_an_assessment():
    out = assess(FakeLLM(reply("refusal")), FakeSF(), OPP_ID, SETTINGS, TODAY)
    assert out.assessment is None and out.error == "model stopped: refusal"
    assert out.opportunity["Id"] == OPP_ID  # routing still gets the real record


def test_dry_run_never_writes(tmp_path):
    sf = FakeSF()
    summary = run(SETTINGS, sf, good_run_llm(), JsonlAuditStore(tmp_path), today=TODAY)
    assert SETTINGS.dry_run and sf.tasks == []
    assert summary["auto_write"] == 1 and summary["cost_usd"] > 0
    row = json.loads((tmp_path / "decisions.jsonl").read_text())
    assert row["decision"] == "auto_write" and row["task_id"] is None and row["dry_run"]


def test_live_run_writes_one_task_and_audits_it(tmp_path):
    sf = FakeSF()
    run(SETTINGS.model_copy(update={"dry_run": False}), sf, good_run_llm(), JsonlAuditStore(tmp_path),
        today=TODAY)
    assert len(sf.tasks) == 1
    opp_id, fields = sf.tasks[0]
    assert opp_id == OPP_ID and fields["due_date"] == date(2026, 10, 28)
    assert "Draft email to Ibrahim Yusuf" in fields["description"]
    row = json.loads((tmp_path / "decisions.jsonl").read_text())
    assert row["task_id"] == "00TNEW" and row["approved_by"] == "guardrails"


def test_rerun_does_not_duplicate_tasks(tmp_path):
    sf = FakeSF()
    live = SETTINGS.model_copy(update={"dry_run": False})
    run(live, sf, good_run_llm(), JsonlAuditStore(tmp_path), today=TODAY)
    summary = run(live, sf, good_run_llm(), JsonlAuditStore(tmp_path), today=TODAY)
    assert len(sf.tasks) == 1
    assert summary["already_actioned"] == 1 and summary["auto_write"] == 0
    second = json.loads((tmp_path / "decisions.jsonl").read_text().splitlines()[1])
    assert second["decision"] == "already_actioned" and second["task_id"] is None
