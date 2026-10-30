"""Synthetic credential-shaped fixtures. Every value is assembled from obviously
fake parts (FAKE/TEST/0000 runs); none is a real or real-looking credential."""

from __future__ import annotations


def fill(n: int, unit: str = "FAKE0TEST0") -> str:
    return (unit * (n // len(unit) + 1))[:n]


PEM_BODY = "\n".join([fill(64, "QUJDREVGR0hJSktM")] * 3)
PEM_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + PEM_BODY + "\n-----END OPENSSH PRIVATE KEY-----"
TSKEY = "tskey-auth-kTEST000CNTRL-0123456789abcdef0123456789abcdef"
B64_LINE = fill(80, "Zm9vYmFy")
HEX40 = "0f" * 20
HEX64 = "0f" * 32
NPM_UUID = "0000000f-0000-0000-0000-0000000000ff"

# (tier, index) -> (positive text, the secret part that must disappear, negative text)
POSITIVE: dict[tuple[str, int], tuple[str, str, str]] = {
    ("a", 0): ("x ghp_" + fill(24) + " y", "ghp_" + fill(24), "ghp_short"),
    ("a", 1): ("github_pat_" + fill(30, "FAKE_TEST_"), fill(30, "FAKE_TEST_"), "github_pat_x"),
    ("a", 2): ("sk-ant-api03-" + fill(24, "FAKE-TEST-"), fill(24, "FAKE-TEST-"), "sk-short"),
    ("a", 3): ("AKIA" + fill(16, "FAKE0TEST0ZZZZ"), "AKIA" + fill(16, "FAKE0TEST0ZZZZ"), "AKIAlow"),
    ("a", 4): (
        "eyJ" + fill(12) + "." + fill(12) + "." + fill(12),
        "." + fill(12) + ".",
        "eyJab.cd.ef",
    ),
    ("a", 5): ("xoxb-" + fill(14, "0000-FAKE-"), fill(14, "0000-FAKE-"), "xoxz-00000000000000"),
    ("a", 6): ("glpat-" + fill(22, "FAKE_TEST-"), fill(22, "FAKE_TEST-"), "glpat-short"),
    ("a", 7): ("TS_AUTHKEY=" + TSKEY, TSKEY, "tskey-auth-only"),
    ("a", 8): ("AIza" + fill(35, "FAKE_TEST-"), fill(35, "FAKE_TEST-"), "AIzaShort"),
    ("a", 9): ("GOCSPX-" + fill(24), fill(24), "GOCSPX-x"),
    ("a", 10): ("ya29." + fill(32), fill(32), "ya29.short"),
    ("a", 11): ("1//0" + fill(32), fill(32), "1//0short"),
    ("a", 12): ("gsk_" + fill(40), fill(40), "gsk_short"),
    ("a", 13): ("hf_" + fill(34), fill(34), "hf_short"),
    ("a", 14): ("npm_" + fill(36), fill(36), "npm_short"),
    ("a", 15): ("pypi-AgEIcHlwaS5vcmc" + fill(52), fill(52), "pypi-AgEIcHlwaS5vcmcShort"),
    ("a", 16): ("dckr_pat_" + fill(24), fill(24), "dckr_pat_x"),
    ("a", 17): ("ATATT3" + fill(104), fill(104), "ATATT3short"),
    ("a", 18): ("bot 123456789:AA" + fill(33), fill(33), "123456789:AAshort"),
    ("a", 19): (
        "https://discord.com/api/webhooks/000000000000000/" + fill(52),
        fill(52),
        "discord.com/api/webhooks/1/x",
    ),
    ("a", 20): (
        "https://hooks.slack.com/services/T0000/B0000/" + fill(24),
        fill(24),
        "hooks.slack.com/services/x",
    ),
    ("a", 21): ("cfut_" + fill(40) + "0f0f0f0f", fill(40), "cfut_short"),
    ("a", 22): (
        "AGE-SECRET-KEY-1" + fill(56, "FAKE0TEST0ZZ"),
        fill(56, "FAKE0TEST0ZZ"),
        "AGE-SECRET-KEY-1X",
    ),
    ("a", 23): (
        "$argon2id$v=19$m=65536,t=3,p=4$" + fill(22) + "$" + fill(43),
        fill(43),
        "$argon2id$v=19$m=x",
    ),
    ("a", 24): ("sk_test_" + fill(24), fill(24), "sk_test_short"),
    ("a", 25): ("SG." + fill(22) + "." + fill(43), fill(43), "SG.short.x"),
    ("a", 26): ("tvly-" + fill(24), fill(24), "tvly-short"),
    ("a", 27): ("lsv2_pt_" + fill(24), fill(24), "lsv2_pt_short"),
    ("a", 28): ('BW_CLIENTSECRET="' + fill(30) + '"', fill(30), "BW_CLIENTSECRET=short"),
    ("a", 29): (
        "ASIA" + fill(16, "FAKE0TEST0ZZZZ"),
        "ASIA" + fill(16, "FAKE0TEST0ZZZZ"),
        "ASIAlow",
    ),
    ("a", 30): ("whsec_" + fill(32), fill(32), "whsec_short"),
    ("a", 31): (
        "xapp-1-A0FAKE0TEST-0000000000000-" + HEX64,
        HEX64,
        "xapp-1-A0FAKE0TEST-0000000000000-short",
    ),
    ("a", 32): ("hvs." + fill(26, "FAKE_TEST-"), fill(26, "FAKE_TEST-"), "hvs.short"),
    ("a", 33): ("glrt-" + fill(22, "FAKE_TEST-"), fill(22, "FAKE_TEST-"), "glrt-short"),
    ("a", 34): (
        "//registry.npmjs.org/:_authToken=" + NPM_UUID,
        NPM_UUID,
        "//registry.npmjs.org/:_authToken=${NPM_TOKEN}",
    ),
    ("a", 35): ("dapi" + HEX40[:32], HEX40[:32], "dapi" + "0f" * 8),
    ("a", 36): ("dop_v1_" + HEX64, HEX64, "dop_v1_" + HEX40),
    ("a", 37): ("sbp_" + HEX40, HEX40, "sbp_" + HEX40[:32]),
    ("a", 38): ("pscale_tkn_" + fill(34, "FAKE_TEST."), fill(34, "FAKE_TEST."), "pscale_tkn_short"),
    ("b", 0): ("key:\n" + PEM_KEY + "\nend", PEM_BODY, "-----BEGIN PUBLIC KEY-----"),
    ("b", 1): ("data:\n  " + B64_LINE + "\n", B64_LINE, "notes:\n  " + B64_LINE + "\n"),
    ("b", 2): (
        "client-key-data: " + fill(48, "Zm9v"),
        fill(48, "Zm9v"),
        '"token": "' + HEX40 + '"',
    ),
    ("b", 3): ('"private_key_id": "' + "0f" * 20 + '"', "0f" * 20, '"private_key_id": "x"'),
    ("b", 4): ('"type": "service_account"', "service_account", '"type": "user"'),
    ("b", 5): ("x-api-key: " + HEX64, HEX64, "x-api-key: short"),
}

# More cases for rules with several prefixes or a value filter:
# (tier, index), text, the secret part that must disappear.
EXTRA_POSITIVE: list[tuple[tuple[str, int], str, str]] = [
    (("a", 2), "sk-ant-oat01-" + fill(24, "FAKE-TEST-"), fill(24, "FAKE-TEST-")),
    (("a", 32), "hvb." + fill(26, "FAKE_TEST-"), fill(26, "FAKE_TEST-")),
    (("a", 33), "glcbt-" + fill(22, "FAKE_TEST-"), fill(22, "FAKE_TEST-")),
    (("a", 36), "doo_v1_" + HEX64, HEX64),
    (
        ("b", 2),
        "users:\n- name: u\n  user:\n    token: " + fill(48, "Zm9v") + "\n",
        fill(48, "Zm9v"),
    ),
    (("b", 2), '"accessToken": "' + fill(48, "Zm9v") + '"', fill(48, "Zm9v")),
    (("b", 2), "token: " + HEX40 + "x" + fill(8, "Zm9v"), HEX40),
    (("b", 5), '"X-Api-Key": "' + fill(32) + '"', fill(32)),
    (
        ("b", 5),
        "curl -H 'x-gitlab-token: " + fill(20, "FAKE_TEST-") + "' https://h",
        fill(20, "FAKE_TEST-"),
    ),
    (("b", 5), "Private-Token:" + HEX40, HEX40),
]
# (tier, index), text that rule must leave alone.
EXTRA_NEGATIVE: list[tuple[tuple[str, int], str]] = [
    (("b", 2), '"token": "' + HEX64 + '"'),
    (("b", 2), "token: " + HEX40),
    (("b", 2), '"token": "sha512-' + fill(64, "Zm9v") + '=="'),
    (("b", 2), '"token": "sha256-' + fill(43, "Zm9v") + '="'),
    (("b", 5), "x-api-key: $PATCHMON_API_KEY"),
    (("b", 5), '"x-api-key": "<your-api-key>"'),
    (("b", 5), "the x-api-key header carries the key"),
]

# Count-only rules (Tier C/D, never tokenized): (tier, index) -> (text with one
# match, text with none).
COUNT_ONLY: dict[tuple[str, int], tuple[str, str]] = {
    ("c", 0): ("my_service_token=" + fill(30), "api_key=short"),
    ("c", 1): ("SONARR_API_KEY=" + HEX40[:32], "SONARR_API_KEY=<your-api-key>"),
    ("c", 2): ("GITEA_TOKEN=" + HEX40, "GITEA_TOKEN=" + HEX40[:20]),
    ("c", 3): ("x-api-key: " + fill(20), "x-api-key: short"),
    ("c", 4): ("Authorization: Bearer " + fill(24), "Authorization: Bearer short"),
    ("c", 5): ("postgres://app:" + fill(12) + "@db.example", "postgres://app:${DB_PASS}@db"),
    ("c", 6): ("aws_secret_access_key = " + fill(40), "aws_secret_access_key = " + fill(20)),
    ("d", 0): ("password=" + fill(12), "password=<redacted>"),
}
# More count-only cases: (tier, index), text with exactly one match.
COUNT_ONLY_EXTRA: list[tuple[tuple[str, int], str]] = [
    (("c", 0), '"apiKey": "' + fill(30) + '"'),
    (("c", 0), "Client-Secret=" + fill(30)),
    (("c", 0), "ENCRYPTION_KEY: " + fill(30, "FAKE/TEST+")),
    (("c", 1), "jellyfin_api_key: " + HEX40[:32]),
    (("c", 4), "authorization: token " + fill(24)),
    (("c", 5), "mongodb+srv://app:" + fill(12) + "@cluster0.example"),
    (("c", 6), "SECRET_KEY=" + fill(40, "FAKE/TEST0")),
    (("d", 0), "PASSWD: " + fill(12)),
]

# Newest-turn fixtures, one per wire format. OLD only ever sits in history;
# NEW_A and NEW_D only in the newest turn; SYS_C only in system/instructions.
OLD = "ghp_" + fill(24)
NEW_A = "glpat-" + fill(22, "FAKE_TEST-")
NEW_D = "password=" + fill(12)
SYS_C = "Authorization: Bearer " + fill(24)
IMAGE_B64 = "ghp_" + fill(40)  # would be Tier A if base64 payloads were scanned
_TOOLS = [{"name": "bash", "description": "run a command", "input_schema": {"type": "object"}}]
_TOOL_USE = {"type": "tool_use", "id": "toolu_01", "name": "bash", "input": {"command": "env"}}
_CALL = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}

# name -> (request body, scope, new-turn counts per tier)
TURN_CASES: dict[str, tuple[dict[str, object], str, dict[str, int]]] = {
    "anthropic_first_turn": (
        {
            "model": "m",
            "system": [{"type": "text", "text": "ops notes: " + SYS_C}],
            "tools": _TOOLS,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "use " + OLD}]}],
        },
        "first",
        {"a": 1, "b": 0, "c": 1, "d": 0},
    ),
    "anthropic_tool_continuation": (
        {
            "model": "m",
            "system": [{"type": "text", "text": "ops notes: " + SYS_C}],
            "tools": _TOOLS,
            "messages": [
                {"role": "user", "content": "use " + OLD},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}, _TOOL_USE]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": [
                                {"type": "text", "text": NEW_D + "\n" + NEW_A},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": IMAGE_B64,
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
        },
        "tail",
        {"a": 1, "b": 0, "c": 0, "d": 1},
    ),
    "anthropic_prefill": (
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "use " + OLD},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "now " + NEW_D},
                {"role": "assistant", "content": "{"},
            ],
        },
        "tail",
        {"a": 0, "b": 0, "c": 0, "d": 1},
    ),
    "anthropic_prefill_first_turn": (
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "use " + OLD},
                {"role": "assistant", "content": "{"},
            ],
        },
        "first",
        {"a": 1, "b": 0, "c": 0, "d": 0},
    ),
    "openai_chat_tool_continuation": (
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": SYS_C},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "use " + OLD},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                    ],
                },
                {"role": "assistant", "content": None, "tool_calls": [_CALL]},
                {"role": "tool", "tool_call_id": "call_1", "content": NEW_A},
                {"role": "tool", "tool_call_id": "call_2", "content": NEW_D},
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": IMAGE_B64}}],
                },
            ],
        },
        "tail",
        {"a": 1, "b": 0, "c": 0, "d": 1},
    ),
    "responses_first_turn": (
        {"model": "m", "instructions": SYS_C, "input": [{"role": "user", "content": OLD}]},
        "first",
        {"a": 1, "b": 0, "c": 1, "d": 0},
    ),
    "responses_continuation": (
        {
            "model": "m",
            "instructions": SYS_C,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "use " + OLD}]},
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": OLD}],
                },
                {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": NEW_A + " " + NEW_D},
                {"type": "input_image", "image_url": IMAGE_B64},
            ],
        },
        "tail",
        {"a": 1, "b": 0, "c": 0, "d": 1},
    ),
    "responses_previous_response_id": (
        {
            "model": "m",
            "previous_response_id": "resp_0001",
            "instructions": SYS_C,
            "input": [{"type": "function_call_output", "call_id": "c1", "output": NEW_D}],
        },
        "input_only",
        {"a": 0, "b": 0, "c": 0, "d": 1},
    ),
    "responses_string_input": (
        {"model": "m", "instructions": SYS_C, "input": "use " + OLD},
        "first",
        {"a": 1, "b": 0, "c": 1, "d": 0},
    ),
    "compaction": (
        {
            "model": "m",
            "system": "ops notes: " + SYS_C,
            "messages": [
                {"role": "user", "content": "Summary of the session so far: deployed with " + OLD},
                {"role": "user", "content": "Continue."},
            ],
        },
        "first",
        {"a": 1, "b": 0, "c": 1, "d": 0},
    ),
}
