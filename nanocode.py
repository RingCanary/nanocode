#!/usr/bin/env python3
"""nanocode - minimal coding assistant"""

import glob as globlib, json, os, re, subprocess, urllib.request

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
INCEPTION_KEY = os.environ.get("INCEPTION_API_KEY")
DRY_RUN = os.environ.get("NANOCODE_DRY_RUN", "").lower() in {"1", "true", "yes"}


def detect_provider():
    explicit_provider = os.environ.get("NANOCODE_PROVIDER", "").strip().lower()
    if explicit_provider in {"anthropic", "openrouter", "inception"}:
        return explicit_provider
    if INCEPTION_KEY:
        return "inception"
    if OPENROUTER_KEY:
        return "openrouter"
    return "anthropic"


PROVIDER = detect_provider()
PROVIDER_LABEL = {
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "inception": "Inception",
}[PROVIDER]
API_URL = {
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openrouter": "https://openrouter.ai/api/v1/messages",
    "inception": "https://api.inceptionlabs.ai/v1/chat/completions",
}[PROVIDER]
MODEL = os.environ.get(
    "MODEL",
    {
        "anthropic": "claude-opus-4-5",
        "openrouter": "anthropic/claude-opus-4.5",
        "inception": "mercury-2",
    }[PROVIDER],
)

# ANSI colors
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, YELLOW, RED = (
    "\033[34m",
    "\033[36m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
)


# --- Tool implementations ---


def read(args):
    lines = open(args["path"]).readlines()
    offset = args.get("offset", 0)
    limit = args.get("limit", len(lines))
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(args):
    with open(args["path"], "w") as f:
        f.write(args["content"])
    return "ok"


def edit(args):
    text = open(args["path"]).read()
    old, new = args["old"], args["new"]
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not args.get("all") and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = (
        text.replace(old, new) if args.get("all") else text.replace(old, new, 1)
    )
    with open(args["path"], "w") as f:
        f.write(replacement)
    return "ok"


def glob(args):
    pattern = (args.get("path", ".") + "/" + args["pat"]).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
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
    proc = subprocess.Popen(
        args["cmd"], shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )
    output_lines = []
    try:
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                print(f"  {DIM}│ {line.rstrip()}{RESET}", flush=True)
                output_lines.append(line)
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        output_lines.append("\n(timed out after 30s)")
    return "".join(output_lines).strip() or "(empty)"


# --- Tool definitions: (description, schema, function) ---

TOOLS = {
    "read": (
        "Read file with line numbers (file path, not directory)",
        {"path": "string", "offset": "number?", "limit": "number?"},
        read,
    ),
    "write": (
        "Write content to file",
        {"path": "string", "content": "string"},
        write,
    ),
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
    "bash": (
        "Run shell command",
        {"cmd": "string"},
        bash,
    ),
}


def run_tool(name, args):
    try:
        return TOOLS[name][2](args)
    except Exception as err:
        return f"error: {err}"


def make_schema(provider):
    result = []
    for name, (description, params, _fn) in TOOLS.items():
        properties = {}
        required = []
        for param_name, param_type in params.items():
            is_optional = param_type.endswith("?")
            base_type = param_type.rstrip("?")
            properties[param_name] = {
                "type": "integer" if base_type == "number" else base_type
            }
            if not is_optional:
                required.append(param_name)
        schema = {"type": "object", "properties": properties, "required": required}
        if provider == "inception":
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            )
        else:
            result.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": schema,
                }
            )
    return result


def call_api_anthropic(messages, system_prompt):
    if DRY_RUN:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[dry-run] {PROVIDER_LABEL} request prepared for {API_URL} with model {MODEL}",
                }
            ]
        }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            {
                "model": MODEL,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": messages,
                "tools": make_schema(PROVIDER),
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **(
                {"Authorization": f"Bearer {OPENROUTER_KEY}"}
                if PROVIDER == "openrouter"
                else {"x-api-key": ANTHROPIC_KEY or ""}
            ),
        },
    )
    response = urllib.request.urlopen(request)
    return json.loads(response.read())


def call_api_inception(messages):
    if DRY_RUN:
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"[dry-run] Inception request prepared for {API_URL} with model {MODEL}",
                    }
                }
            ]
        }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            {
                "model": MODEL,
                "max_tokens": 8192,
                "messages": messages,
                "tools": make_schema("inception"),
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INCEPTION_KEY or ''}",
        },
    )
    response = urllib.request.urlopen(request)
    return json.loads(response.read())


def call_api(messages, system_prompt):
    if PROVIDER == "inception":
        return call_api_inception(messages)
    return call_api_anthropic(messages, system_prompt)


def separator():
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80
    return f"{DIM}{'─' * min(width, 80)}{RESET}"


def render_markdown(text):
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


def preview_result(result):
    result_lines = result.split("\n")
    preview = result_lines[0][:60]
    if len(result_lines) > 1:
        preview += f" ... +{len(result_lines) - 1} lines"
    elif len(result_lines[0]) > 60:
        preview += "..."
    return preview


def execute_tool(tool_name, tool_args):
    arg_preview = str(next(iter(tool_args.values()), ""))[:50]
    print(f"\n{GREEN}⏺ {tool_name.capitalize()}{RESET}({DIM}{arg_preview}{RESET})")
    result = run_tool(tool_name, tool_args)
    print(f"  {DIM}⎿  {preview_result(result)}{RESET}")
    return result


def run_anthropic_turn(messages, system_prompt):
    while True:
        response = call_api(messages, system_prompt)
        # Display token usage if available
        usage = response.get("usage")
        if usage:
            total = usage.get("total_tokens")
            if total is None:
                total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            print(f"{YELLOW}🔢 Tokens used: {total}{RESET}")
        content_blocks = response.get("content", [])
        tool_results = []

        for block in content_blocks:
            if block["type"] == "text":
                print(f"\n{CYAN}⏺{RESET} {render_markdown(block['text'])}")

            if block["type"] == "tool_use":
                tool_name = block["name"]
                tool_args = block["input"]
                result = execute_tool(tool_name, tool_args)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    }
                )

        messages.append({"role": "assistant", "content": content_blocks})
        if not tool_results:
            break
        messages.append({"role": "user", "content": tool_results})


def normalize_openai_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_chunks = []
        for item in content:
            if isinstance(item, str):
                text_chunks.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text_chunks.append(item.get("text", ""))
        return "".join(text_chunks)
    return ""


def parse_tool_args(raw_args):
    if isinstance(raw_args, dict):
        return raw_args
    if not raw_args:
        return {}
    try:
        return json.loads(raw_args)
    except json.JSONDecodeError:
        return {}


def run_inception_turn(messages, system_prompt):
    while True:
        response = call_api(messages, system_prompt)
        # Display token usage if available
        usage = response.get("usage")
        if usage:
            total = usage.get("total_tokens")
            if total is None:
                total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            print(f"{YELLOW}🔢 Tokens used: {total}{RESET}")
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message", {})

        text = normalize_openai_content(message.get("content"))
        if text:
            print(f"\n{CYAN}⏺{RESET} {render_markdown(text)}")

        tool_calls = message.get("tool_calls") or []
        assistant_message = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        if not tool_calls:
            break

        for tool_call in tool_calls:
            function_call = tool_call.get("function", {})
            tool_name = function_call.get("name", "")
            tool_args = parse_tool_args(function_call.get("arguments"))
            result = execute_tool(tool_name, tool_args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "content": result,
                }
            )


def check_api_key():
    if DRY_RUN:
        return
    if PROVIDER == "inception" and not INCEPTION_KEY:
        raise RuntimeError("INCEPTION_API_KEY is required for provider=inception")
    if PROVIDER == "openrouter" and not OPENROUTER_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required for provider=openrouter")
    if PROVIDER == "anthropic" and not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is required for provider=anthropic")


def initial_messages(system_prompt):
    if PROVIDER == "inception":
        return [{"role": "system", "content": system_prompt}]
    return []


def main():
    try:
        check_api_key()
    except RuntimeError as err:
        print(f"{RED}⏺ Error: {err}{RESET}")
        return

    print(
        f"{BOLD}nanocode{RESET} | {DIM}{MODEL} ({PROVIDER_LABEL}) | {os.getcwd()}{RESET}\n"
    )
    system_prompt = f"Concise coding assistant. cwd: {os.getcwd()}"
    messages = initial_messages(system_prompt)

    while True:
        try:
            print(separator())
            user_input = input(f"{BOLD}{BLUE}❯{RESET} ").strip()
            print(separator())
            if not user_input:
                continue
            if user_input in ("/q", "exit"):
                break
            if user_input == "/c":
                messages = initial_messages(system_prompt)
                print(f"{GREEN}⏺ Cleared conversation{RESET}")
                continue

            messages.append({"role": "user", "content": user_input})

            if PROVIDER == "inception":
                run_inception_turn(messages, system_prompt)
            else:
                run_anthropic_turn(messages, system_prompt)

            print()

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")


if __name__ == "__main__":
    main()
