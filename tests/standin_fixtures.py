"""Synthetic credential-shaped fixtures. Every value is assembled from obviously
fake parts (FAKE/TEST/0000 runs); none is a real or real-looking credential."""

from __future__ import annotations


def fill(n: int, unit: str = "FAKE0TEST0") -> str:
    return (unit * (n // len(unit) + 1))[:n]


PEM_BODY = "\n".join([fill(64, "QUJDREVGR0hJSktM")] * 3)
PEM_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + PEM_BODY + "\n-----END OPENSSH PRIVATE KEY-----"
TSKEY = "tskey-auth-kTEST000CNTRL-0123456789abcdef0123456789abcdef"
B64_LINE = fill(80, "Zm9vYmFy")

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
    ("b", 0): ("key:\n" + PEM_KEY + "\nend", PEM_BODY, "-----BEGIN PUBLIC KEY-----"),
    ("b", 1): ("data:\n  " + B64_LINE + "\n", B64_LINE, "notes:\n  " + B64_LINE + "\n"),
    ("b", 2): ("client-key-data: " + fill(48, "Zm9v"), fill(48, "Zm9v"), "token: short"),
    ("b", 3): ('"private_key_id": "' + "0f" * 20 + '"', "0f" * 20, '"private_key_id": "x"'),
    ("b", 4): ('"type": "service_account"', "service_account", '"type": "user"'),
}
