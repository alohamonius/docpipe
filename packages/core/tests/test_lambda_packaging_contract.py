"""The seams a deploy crosses and no unit test does, read at the source level.

Four files have to agree for `POST /chat` to answer: the Makefile builds the zip,
infra/__main__.py ships it with an environment, infra/components/api.py names the
handler and the platform, and ChatService.from_env reads the environment. Each is
tested alone; this file holds them together. Parsed as text, not imported —
infra/ is quarantined from the packages (README, repo layout).
"""

from __future__ import annotations

import re
from pathlib import Path

import chat  # services/api/chat.py

ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = (ROOT / "Makefile").read_text()
MAIN = (ROOT / "infra/__main__.py").read_text()
API = (ROOT / "infra/components/api.py").read_text()
CORE_CHAT = (ROOT / "packages/core/src/docpipe_core/chat.py").read_text()

# What the Lambda's environment must carry: every name from_env requires or reads.
ENV_NAMES = (
    "KNOWLEDGE_BASE_ID",
    "CONVERSATIONS_TABLE",
    "CHAT_MODEL_ID",
    "GUARDRAIL_ID",
    "GUARDRAIL_VERSION",
)


def make_var(name: str) -> str:
    match = re.search(rf"^{name} [:?]?= (.+)$", MAKEFILE, re.M)
    assert match, f"{name} left the Makefile"
    return match.group(1).strip()


def test_the_zip_the_makefile_builds_is_the_zip_the_stack_ships() -> None:
    assert "lambda-zip:" in MAKEFILE
    assert make_var("LAMBDA_ZIP") == "build/lambda.zip"
    assert '"build" / "lambda.zip"' in MAIN
    assert "make lambda-zip" in MAIN  # the refusal names the fix


def test_the_zip_carries_the_handler_file_and_the_core_package() -> None:
    assert "cp services/api/chat.py $(LAMBDA_BUILD)/chat.py" in MAKEFILE
    assert "cp -R packages/core/src/docpipe_core $(LAMBDA_BUILD)/docpipe_core" in MAKEFILE
    assert "--package docpipe-core --no-dev" in MAKEFILE  # locked runtime deps, no dev group


def test_the_handler_string_names_the_shipped_function() -> None:
    assert 'HANDLER = "chat.handler"' in API
    assert chat.__name__ == "chat" and callable(chat.handler)


def test_the_wheels_are_built_for_the_platform_the_function_runs_on() -> None:
    platform = make_var("LAMBDA_PLATFORM")
    python = make_var("LAMBDA_PYTHON")
    assert platform.startswith("aarch64-") and 'ARCHITECTURE = "arm64"' in API
    assert f'RUNTIME = "python{python}"' in API


def test_the_environment_the_stack_sets_is_the_environment_the_service_reads() -> None:
    for name in ENV_NAMES:
        assert f'"{name}"' in MAIN, f"__main__.py does not set {name} on the function"
        assert f'"{name}"' in CORE_CHAT, f"ChatService.from_env does not read {name}"
    # The chat model is the STACK's, never a code default when the stack has spoken.
    assert '"CHAT_MODEL_ID": chat_model_id' in MAIN


def test_the_key_value_is_never_exported() -> None:
    assert "key.value" not in API
    assert re.search(r'pulumi\.export\("chat_api_key_id"', MAIN)
    assert not re.search(r'pulumi\.export\("chat_api_key(_value)?"', MAIN)
