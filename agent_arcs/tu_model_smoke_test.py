from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_arcs.tu_models import build_tu_chat_model, tu_model_name


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _usage_metadata(response: object) -> dict[str, object]:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return usage
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage")
        if isinstance(token_usage, dict):
            return token_usage
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test TU KI-Toolbox chat models.")
    parser.add_argument("--role", choices=["main", "sub", "auditor"], default="main")
    parser.add_argument("--prompt", default="Reply with exactly: ok")
    args = parser.parse_args()

    _load_env_file(PROJECT_ROOT / ".env")

    model_name = tu_model_name(args.role)
    model = build_tu_chat_model(role=args.role, temperature=0.0, max_retries=1, timeout=60)
    response = model.invoke([HumanMessage(content=args.prompt)])

    text = str(getattr(response, "content", "")).strip()
    payload = {
        "role": args.role,
        "model": model_name,
        "ok": bool(text),
        "response_preview": text[:300],
        "usage_metadata": _usage_metadata(response),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
