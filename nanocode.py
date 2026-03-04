#!/usr/bin/env python3
"""nanocode - minimal coding assistant"""

import glob as globlib, json, os, re, subprocess, urllib.error, urllib.request


def env_bool(name, tri_state=False):
    value = os.environ.get(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None if tri_state else False


DRY_RUN = env_bool("NANOCODE_DRY_RUN")
ZAI_CODING_PLAN = env_bool("NANOCODE_ZAI_CODING_PLAN")
try:
    REQUEST_TIMEOUT = max(15, int(os.environ.get("NANOCODE_HTTP_TIMEOUT", "30")))
except ValueError:
    REQUEST_TIMEOUT = 30
PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "api_url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-opus-4-5",
        "key_env": "ANTHROPIC_API_KEY",
        "style": "anthropic",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "system_message": False,
    },
    "openrouter": {
        "label": "OpenRouter",
        "api_url": "https://openrouter.ai/api/v1/messages",
        "default_model": "anthropic/claude-opus-4.5",
        "key_env": "OPENROUTER_API_KEY",
        "style": "anthropic",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "system_message": False,
    },
    "inception": {
        "label": "Inception",
        "api_url": "https://api.inceptionlabs.ai/v1/chat/completions",
        "default_model": "mercury-2",
        "key_env": "INCEPTION_API_KEY",
        "style": "openai",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "system_message": True,
    },
    "zai": {
        "label": "z.ai",
        "api_url": "https://api.z.ai/api/paas/v4/chat/completions",
        "default_model": "glm-5",
        "key_env": "ZAI_API_KEY",
        "style": "openai",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "system_message": True,
    },
}


def detect_provider():
    explicit = os.environ.get("NANOCODE_PROVIDER", "").strip().lower()
    explicit = "zai" if explicit == "z_ai" else explicit
    if explicit in PROVIDERS:
        return explicit
    for provider in ("inception", "zai", "openrouter"):
        if os.environ.get(PROVIDERS[provider]["key_env"]):
            return provider
    return "anthropic"


PROVIDER = detect_provider()
PROVIDER_CFG = PROVIDERS[PROVIDER]
PROVIDER_LABEL = PROVIDER_CFG["label"]
MODEL = os.environ.get("MODEL", PROVIDER_CFG["default_model"])
RESET, BOLD, DIM = ("\x1b[0m", "\x1b[1m", "\x1b[2m")
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\x1b[34m",
    "\x1b[36m",
    "\x1b[32m",
    "\x1b[33m",
    "\x1b[31m",
)


def read(args):
    lines = open(args["path"]).readlines()
    offset, limit = (args.get("offset", 0), args.get("limit", len(lines)))
    return "".join(
        (
            f"{offset + i + 1:4}| {line}"
            for i, line in enumerate(lines[offset : offset + limit])
        )
    )


def write(args):
    with open(args["path"], "w") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    text = open(args["path"]).read()
    old, new = (args["old"], args["new"])
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if count > 1 and (not args.get("all")):
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    with open(args["path"], "w") as f:
        f.write(
            text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
        )
    return "ok"


def glob(args):
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"


def grep(args):
    pattern = re.compile(args["pat"])
    hits = []
    for filepath in globlib.glob(args.get("path", ".") + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"


def bash(args):
    try:
        proc = subprocess.run(
            args["cmd"],
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        return (proc.stdout or "").strip() or "(empty)"
    except subprocess.TimeoutExpired as err:
        output = err.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if not isinstance(output, str):
            output = ""
        return (output + "\n(timed out after 30s)").strip()


TOOLS = {
    "read": (
        "Read file with line numbers (file path, not directory)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        read,
    ),
    "write": ("Write content to file", {"path": "string", "content": "string"}, write),
    "edit": (
        "Replace old with new in file (old must be unique unless all=true)",
        {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
        edit,
    ),
    "glob": (
        "Find files by pattern, sorted by mtime",
        {"pat": "string", "path": "string?"},
        glob,
    ),
    "grep": (
        "Search files for regex pattern",
        {"pat": "string", "path": "string?"},
        grep,
    ),
    "bash": ("Run shell command", {"cmd": "string"}, bash),
}


def run_tool(name, args):
    try:
        return TOOLS[name][2](args)
    except Exception as err:
        return f"error: {err}"


def parse_tool_args(raw_args):
    if isinstance(raw_args, dict):
        return raw_args
    if not raw_args:
        return {}
    try:
        return json.loads(raw_args)
    except json.JSONDecodeError:
        return {}


def normalize_openai_content(content):
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    chunks = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            chunks.append(item.get("text", ""))
    text = "".join(chunks)
    return [text] if text else []


def extract_usage_tuple(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return (None, None, None)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        total_tokens is None
        and isinstance(input_tokens, int)
        and isinstance(output_tokens, int)
    ):
        total_tokens = input_tokens + output_tokens
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
        total_tokens if isinstance(total_tokens, int) else None,
    )


def extract_zai_plan(choice, message):
    if not ZAI_CODING_PLAN:
        return ""
    for source in (message, choice):
        if not isinstance(source, dict):
            continue
        for key in ("coding_plan", "plan", "reasoning", "analysis"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                text = value.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return ""


def parse_response(provider, response):
    if PROVIDERS[provider]["style"] == "anthropic":
        texts, tool_calls = ([], [])
        for block in response.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            if block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "name": block.get("name", ""),
                        "args": block.get("input", {}),
                        "id": block.get("id", ""),
                    }
                )
        payload = {"role": "assistant", "content": response.get("content", [])}
        return {
            "assistant_text": texts,
            "tool_calls": tool_calls,
            "usage": extract_usage_tuple(response),
            "assistant_payload": payload,
        }
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    raw_tool_calls = message.get("tool_calls") or []
    tool_calls = []
    for tool_call in raw_tool_calls:
        function_call = tool_call.get("function", {})
        tool_calls.append(
            {
                "name": function_call.get("name", ""),
                "args": parse_tool_args(function_call.get("arguments")),
                "id": tool_call.get("id", ""),
            }
        )
    message_texts = normalize_openai_content(message.get("content"))
    plan_text = extract_zai_plan(choice, message) if provider == "zai" else ""
    display_texts = [plan_text, *message_texts] if plan_text else message_texts
    payload = {
        "role": "assistant",
        "content": "".join(message_texts),
        **({"tool_calls": raw_tool_calls} if raw_tool_calls else {}),
    }
    return {
        "assistant_text": display_texts,
        "tool_calls": tool_calls,
        "usage": extract_usage_tuple(response),
        "assistant_payload": payload,
    }


def make_schema(provider):
    openai_style = PROVIDERS[provider]["style"] == "openai"
    schemas = []
    for name, (description, params, _fn) in TOOLS.items():
        properties = {}
        required = []
        for param_name, param_type in params.items():
            base_type = param_type.rstrip("?")
            properties[param_name] = {
                "type": "integer" if base_type == "number" else base_type
            }
            if not param_type.endswith("?"):
                required.append(param_name)
        schema = {"type": "object", "properties": properties, "required": required}
        item = (
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            }
            if openai_style
            else {"name": name, "description": description, "input_schema": schema}
        )
        schemas.append(item)
    return schemas


def inception_optional_params():
    params = {}
    effort = os.environ.get("NANOCODE_REASONING_EFFORT", "").strip().lower()
    if effort in {"instant", "low", "medium", "high"}:
        params["reasoning_effort"] = effort
    summary = env_bool("NANOCODE_REASONING_SUMMARY", tri_state=True)
    if summary is not None:
        params["reasoning_summary"] = summary
    temp = os.environ.get("NANOCODE_TEMPERATURE", "").strip()
    if temp:
        try:
            params["temperature"] = float(temp)
        except ValueError:
            pass
    stop = os.environ.get("NANOCODE_STOP", "").strip()
    if stop:
        stops = [item for item in stop.split("||") if item]
        if stops:
            params["stop"] = stops[:4]
    return params


def build_request(messages, system_prompt, provider):
    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "messages": messages,
        "tools": make_schema(provider),
    }
    style = PROVIDERS[provider]["style"]
    if style == "anthropic":
        payload["system"] = system_prompt
    elif provider == "inception":
        payload.update(inception_optional_params())
    elif provider == "zai" and ZAI_CODING_PLAN:
        payload["coding_plan"] = True
    return payload


def provider_headers(provider):
    cfg = PROVIDERS[provider]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg["style"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    headers[cfg["auth_header"]] = (
        f"{cfg['auth_prefix']}{os.environ.get(cfg['key_env'], '')}"
    )
    return headers


def decode_json_bytes(data):
    text = data.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        snippet = text[:300].strip().replace("\n", " ")
        raise RuntimeError(
            f"Non-JSON response from {PROVIDER_LABEL}: {snippet or '<empty>'}"
        )


def request_json(request):
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return decode_json_bytes(response.read())
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace").strip()
        detail = body[:300] if body else str(err.reason)
        raise RuntimeError(f"HTTP {err.code} from {PROVIDER_LABEL}: {detail}")
    except urllib.error.URLError as err:
        raise RuntimeError(f"Network error from {PROVIDER_LABEL}: {err.reason}")


def dry_run_response(provider):
    cfg = PROVIDERS[provider]
    text = f"[dry-run] {cfg['label']} request prepared for {cfg['api_url']} with model {MODEL}"
    if cfg["style"] == "openai":
        message = {"role": "assistant", "content": text}
        if provider == "zai" and ZAI_CODING_PLAN:
            message["coding_plan"] = "[dry-run] coding plan enabled"
        return {
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    }


def call_api(messages, system_prompt, provider):
    if DRY_RUN:
        return dry_run_response(provider)
    request = urllib.request.Request(
        PROVIDERS[provider]["api_url"],
        data=json.dumps(build_request(messages, system_prompt, provider)).encode(),
        headers=provider_headers(provider),
    )
    return request_json(request)


def new_usage_bucket():
    return {"calls": 0, "input": None, "output": None, "total": None}


def usage_dict(usage_tuple):
    if not isinstance(usage_tuple, tuple) or len(usage_tuple) != 3:
        return None
    input_tokens, output_tokens, total_tokens = usage_tuple
    if not any((v is not None for v in (input_tokens, output_tokens, total_tokens))):
        return None
    return {"input": input_tokens, "output": output_tokens, "total": total_tokens}


def add_usage(bucket, usage):
    if not usage:
        return
    bucket["calls"] += 1
    for key in ("input", "output", "total"):
        value = usage[key]
        if value is None:
            continue
        bucket[key] = value if bucket[key] is None else bucket[key] + value


def usage_parts(bucket):
    parts = []
    for key, label in (("input", "in"), ("output", "out"), ("total", "total")):
        if bucket[key] is not None:
            parts.append(f"{label} {bucket[key]}")
    return parts


def separator():
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80
    return f"{DIM}{'─' * min(width, 80)}{RESET}"


def render_markdown(text):
    return re.sub("\\*\\*(.+?)\\*\\*", f"{BOLD}\\1{RESET}", text)


def print_help():
    print(f"{DIM}Commands: /h /help, /stats, /c, /q, exit{RESET}")


def print_stats(session_usage):
    if not session_usage["calls"]:
        print(f"{DIM}No token usage recorded yet.{RESET}")
        return
    print(f"{YELLOW}🔢 Session tokens: {' | '.join(usage_parts(session_usage))}{RESET}")
    print(f"{DIM}API calls: {session_usage['calls']}{RESET}")


def print_usage_summary(turn_usage, session_usage):
    if not turn_usage["calls"]:
        return
    text = f"🔢 Turn tokens: {' | '.join(usage_parts(turn_usage)) or 'unknown'}"
    session_parts = usage_parts(session_usage)
    if session_parts:
        text += f" | session {' | '.join(session_parts)}"
    print(f"{YELLOW}{text}{RESET}")


def preview_result(result):
    lines = result.split("\n")
    first = lines[0][:60]
    if len(lines) > 1:
        return first + f" ... +{len(lines) - 1} lines"
    if len(lines[0]) > 60:
        return first + "..."
    return first


def execute_tool(tool_name, tool_args):
    arg_preview = str(next(iter(tool_args.values()), ""))[:50]
    print(f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})")
    result = run_tool(tool_name, tool_args)
    print(f"  {DIM}⎿  {preview_result(result)}{RESET}")
    return result


def tool_result_message(provider, tool_call, result):
    if PROVIDERS[provider]["style"] == "anthropic":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": result,
                }
            ],
        }
    return {"role": "tool", "tool_call_id": tool_call["id"], "content": result}


def check_api_key():
    if DRY_RUN:
        return
    key_env = PROVIDER_CFG["key_env"]
    if not os.environ.get(key_env):
        raise RuntimeError(f"{key_env} is required for provider={PROVIDER}")


def initial_messages(system_prompt):
    return (
        [{"role": "system", "content": system_prompt}]
        if PROVIDER_CFG["system_message"]
        else []
    )


def run_turn(messages, system_prompt, session_usage):
    turn_usage = new_usage_bucket()
    while True:
        parsed = parse_response(PROVIDER, call_api(messages, system_prompt, PROVIDER))
        usage = usage_dict(parsed["usage"])
        add_usage(turn_usage, usage)
        add_usage(session_usage, usage)
        for text in parsed["assistant_text"]:
            if text:
                print(f"\n{CYAN}⏺{RESET} {render_markdown(text)}")
        messages.append(parsed["assistant_payload"])
        if not parsed["tool_calls"]:
            return turn_usage
        for tool_call in parsed["tool_calls"]:
            result = execute_tool(
                tool_call.get("name", ""), tool_call.get("args") or {}
            )
            messages.append(tool_result_message(PROVIDER, tool_call, result))


def main():
    try:
        check_api_key()
    except RuntimeError as err:
        print(f"{RED}⏺ Error: {err}{RESET}")
        return
    print(
        f"{BOLD}nanocode{RESET} | {DIM}{MODEL} ({PROVIDER_LABEL}) | {os.getcwd()}{RESET}\n"
    )
    print_help()
    system_prompt = f"Concise coding assistant. cwd: {os.getcwd()}"
    messages = initial_messages(system_prompt)
    session_usage = new_usage_bucket()
    while True:
        try:
            print(separator())
            user_input = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
            print(separator())
            if not user_input:
                continue
            if user_input in {"/q", "exit"}:
                break
            if user_input in {"/h", "/help"}:
                print_help()
                continue
            if user_input == "/stats":
                print_stats(session_usage)
                continue
            if user_input == "/c":
                messages = initial_messages(system_prompt)
                print(f"{GREEN}⏺ Cleared conversation{RESET}")
                continue
            messages.append({"role": "user", "content": user_input})
            print_usage_summary(
                run_turn(messages, system_prompt, session_usage), session_usage
            )
            print()
        except (KeyboardInterrupt, EOFError):
            break
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")


if __name__ == "__main__":
    main()
