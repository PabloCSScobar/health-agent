from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import json
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from health_agent.mcp import server

TOKEN = "synthetic-test-token"
HEADERS = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

EXPECTED_TOOLS = {
    "get_running_profile", "get_weekly_running_load", "get_intensity_distribution", "get_efficiency_trend",
    "get_fitness_form", "analyze_run", "find_comparable_runs", "get_latest_workout", "get_workouts_on_date",
    "get_workouts", "get_correlations", "get_strength_sessions", "get_exercise_progress",
    "get_strength_weekly_volume", "get_nutrition_summary", "get_energy_balance", "find_foods",
    "get_nutrition_day", "get_nutrition_range", "list_supplements", "get_body_trend",
    "get_body_composition_latest", "get_body_composition_trend", "get_recovery_baseline", "get_recovery_day",
    "get_recovery_range", "get_wellbeing_history", "estimate_daily_expenditure", "get_knowledge",
    "read_document", "list_documents", "log_manual_entries", "set_user_profile_facts", "remember_fact",
    "forget_knowledge", "restore_knowledge", "add_supplement", "propose_reminder_rule",
    "record_supplement_intake", "activate_reminder_rule", "list_reminder_rules", "get_user_profile",
    "get_data_freshness", "save_document",
}


class McpAuthTest(unittest.TestCase):
    def post(self, token_setting: str, authorization: str | None):
        headers = dict(HEADERS)
        if authorization is not None:
            headers["Authorization"] = authorization
        with patch.object(server.settings, "mcp_token", token_setting), TestClient(server.build_app()) as client:
            return client.post("/mcp", json=LIST_BODY, headers=headers)

    def test_missing_token_is_rejected(self) -> None:
        self.assertEqual(self.post(TOKEN, None).status_code, 401)

    def test_wrong_token_is_rejected(self) -> None:
        self.assertEqual(self.post(TOKEN, "Bearer wrong").status_code, 401)
        self.assertEqual(self.post(TOKEN, f"Basic {TOKEN}").status_code, 401)

    def test_empty_server_token_refuses_everything(self) -> None:
        with self.assertLogs("health_agent.mcp", level="WARNING"):
            self.assertEqual(self.post("", "Bearer ").status_code, 401)
        with self.assertLogs("health_agent.mcp", level="WARNING"):
            self.assertEqual(self.post("", "Bearer anything").status_code, 401)

    def test_correct_token_lists_tools(self) -> None:
        response = self.post(TOKEN, f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, 200)
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_unknown_host_is_rejected(self) -> None:
        headers = {**HEADERS, "Host": "evil.example", "Authorization": f"Bearer {TOKEN}"}
        with patch.object(server.settings, "mcp_token", TOKEN), TestClient(server.build_app()) as client:
            self.assertEqual(client.post("/mcp", json=LIST_BODY, headers=headers).status_code, 421)

    def test_tailscale_host_is_accepted(self) -> None:
        headers = {**HEADERS, "Host": "health-agent-vps.tail8242a2.ts.net", "Authorization": f"Bearer {TOKEN}"}
        with patch.object(server.settings, "mcp_token", TOKEN), TestClient(server.build_app()) as client:
            self.assertEqual(client.post("/mcp", json=LIST_BODY, headers=headers).status_code, 200)

    def test_health_is_public_even_without_token(self) -> None:
        with patch.object(server.settings, "mcp_token", ""), TestClient(server.build_app()) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class McpToolsTest(unittest.TestCase):
    def test_tool_names_exclude_routing(self) -> None:
        tools = asyncio.run(server.build_mcp().list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, EXPECTED_TOOLS)
        for routing in ("delegate", "ask_agent", "_delegate"):
            self.assertNotIn(routing, names)

    def test_activate_description_requires_confirmation(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.build_mcp().list_tools())}
        self.assertIn("jawnie potwierdził", tools["activate_reminder_rule"].description)

    def test_call_path_uses_underlying_function_and_serialises(self) -> None:
        calls: list[dt.date] = []

        def get_recovery_day(date: dt.date) -> dict:
            calls.append(date)
            return {"date": date, "steps": decimal.Decimal("1234.5"), "when": dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc)}

        with patch.dict(server.ALL_TOOLS, {"get_recovery_day": get_recovery_day}):
            mcp = server.build_mcp()
        result = asyncio.run(mcp.call_tool("get_recovery_day", {"date": "2026-01-01"}))
        self.assertEqual(calls, [dt.date(2026, 1, 1)])
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload, {"date": "2026-01-01", "steps": 1234.5, "when": "2026-01-02T03:04:00+00:00"})

    def test_tool_with_name_and_fn_parameters_is_callable(self) -> None:
        # add_supplement(name=...) zderzało się z parametrem `name` w run_tool -> TypeError
        def add_supplement(name: str, fn: str = "x") -> dict:
            return {"name": name, "fn": fn}

        with patch.dict(server.ALL_TOOLS, {"add_supplement": add_supplement}):
            mcp = server.build_mcp()
        result = asyncio.run(mcp.call_tool("add_supplement", {"name": "kreatyna", "fn": "y"}))
        self.assertEqual(json.loads(result.content[0].text), {"name": "kreatyna", "fn": "y"})

    def test_value_error_becomes_polish_tool_error(self) -> None:
        def get_recovery_day(date: dt.date) -> dict:
            raise ValueError("Nieprawidłowa data")

        with patch.dict(server.ALL_TOOLS, {"get_recovery_day": get_recovery_day}):
            mcp = server.build_mcp()
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(mcp.call_tool("get_recovery_day", {"date": "2026-01-01"}))
        self.assertIn("Nieprawidłowa data", str(ctx.exception))

    def test_propose_reminder_rule_returns_rule_id_without_button_text(self) -> None:
        text = "Propozycja przypomnienia #7: Woda o 20:00. Potwierdź ją przyciskiem. [REMINDER_DRAFT:7]"
        with patch.object(server, "_propose_reminder_rule", return_value=text):
            result = server.propose_reminder_rule("Woda")
        self.assertEqual(result["rule_id"], 7)
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("REMINDER_DRAFT", result["opis"])
        self.assertNotIn("przyciskiem", result["opis"])

    def test_to_jsonable_handles_nan_and_nested_values(self) -> None:
        self.assertEqual(server.to_jsonable({"a": [float("nan"), decimal.Decimal("1.5"), dt.date(2026, 1, 1)]}),
                         {"a": [None, 1.5, "2026-01-01"]})


if __name__ == "__main__":
    unittest.main()
