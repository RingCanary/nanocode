#!/usr/bin/env python3
"""nanocode - minimal coding assistant"""

import glob as globlib, json, os, queue, re, select, subprocess, sys, termios, threading, time, tty, urllib.request

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


class EscKeyWatcher:
    def __init__(self):
        self.enabled = os.name == "posix" and sys.stdin.isatty()
        self.fd = None
        self.old_settings = None

    def __enter__(self):
        if self.enabled:
            self.fd = sys.stdin.fileno()
            self.old_settings = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self.enabled and self.fd is not None and self.old_settings is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)

    def esc_pressed(self):
        if not self.enabled or self.fd is None:
            return False
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return False
        key = os.read(self.fd, 1)
        return key == b"\x1b"


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
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_lines = []
    output_queue = queue.Queue()
    done = object()

    def stream_output():
        for line in proc.stdout:
            output_queue.put(line)
        output_queue.put(done)

    reader = threading.Thread(target=stream_output, daemon=True)
    reader.start()

    interrupted = False
    started_at = time.monotonic()

    try:
        with EscKeyWatcher() as key_watcher:
            while True:
                try:
                    line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    line = None

                if line is done:
                    if proc.poll() is not None:
                        break
                elif line:
                    print(f"  {DIM}│ {line.rstrip()}{RESET}", flush=True)
                    output_lines.append(line)

                if not interrupted and key_watcher.esc_pressed():
                    interrupted = True
                    proc.terminate()
                    output_lines.append("\n(interrupted by Esc)")

                if not interrupted and time.monotonic() - started_at > 30:
                    proc.kill()
                    output_lines.append("\n(timed out after 30s)")

                if proc.poll() is not None and output_queue.empty():
                    break
    finally:
        if proc.poll() is None:
            proc.kill()

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
            ],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 8,
                "total_tokens": 20,
            },
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
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
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


def new_usage_bucket():
    return {
        "calls": 0,
        "input": 0,
        "output": 0,
        "total": 0,
        "has_input": 0,
        "has_output": 0,
        "has_total": 0,
    }


def extract_usage(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        input_tokens = usage.get("prompt_tokens")

    output_tokens = usage.get("output_tokens")
    if output_tokens is None:
        output_tokens = usage.get("completion_tokens")

    total_tokens = usage.get("total_tokens")
    if (
        total_tokens is None
        and isinstance(input_tokens, int)
        and isinstance(output_tokens, int)
    ):
        total_tokens = input_tokens + output_tokens

    if not any(isinstance(v, int) for v in (input_tokens, output_tokens, total_tokens)):
        return None

    return {
        "input": input_tokens if isinstance(input_tokens, int) else None,
        "output": output_tokens if isinstance(output_tokens, int) else None,
        "total": total_tokens if isinstance(total_tokens, int) else None,
    }


def add_usage(bucket, usage):
    if not usage:
        return
    bucket["calls"] += 1
    if usage["input"] is not None:
        bucket["input"] += usage["input"]
        bucket["has_input"] += 1
    if usage["output"] is not None:
        bucket["output"] += usage["output"]
        bucket["has_output"] += 1
    if usage["total"] is not None:
        bucket["total"] += usage["total"]
        bucket["has_total"] += 1


def usage_parts(bucket):
    parts = []
    if bucket["has_input"]:
        parts.append(f"in {bucket['input']}")
    if bucket["has_output"]:
        parts.append(f"out {bucket['output']}")
    if bucket["has_total"]:
        parts.append(f"total {bucket['total']}")
    return parts


def print_usage_summary(turn_usage, session_usage):
    if not turn_usage["calls"]:
        return
    turn_parts = usage_parts(turn_usage)
    session_parts = usage_parts(session_usage)
    text = f"🔢 Turn tokens: {' | '.join(turn_parts) or 'unknown'}"
    if session_parts:
        text += f" | session {' | '.join(session_parts)}"
    print(f"{YELLOW}{text}{RESET}")


def print_help():
    print(f"{DIM}Commands: /h /help, /stats, /c, /q, exit{RESET}")


def print_stats(session_usage):
    if not session_usage["calls"]:
        print(f"{DIM}No token usage recorded yet.{RESET}")
        return
    parts = usage_parts(session_usage)
    print(f"{YELLOW}🔢 Session tokens: {' | '.join(parts)}{RESET}")
    print(f"{DIM}API calls: {session_usage['calls']}{RESET}")


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
    if tool_name == "bash":
        print(f"  {DIM}⎿  Press Esc to interrupt{RESET}")
    result = run_tool(tool_name, tool_args)
    print(f"  {DIM}⎿  {preview_result(result)}{RESET}")
    return result


def run_anthropic_turn(messages, system_prompt, session_usage):
    turn_usage = new_usage_bucket()
    while True:
        response = call_api(messages, system_prompt)
        usage = extract_usage(response)
        add_usage(turn_usage, usage)
        add_usage(session_usage, usage)
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
    return turn_usage


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


def run_inception_turn(messages, system_prompt, session_usage):
    turn_usage = new_usage_bucket()
    while True:
        response = call_api(messages, system_prompt)
        usage = extract_usage(response)
        add_usage(turn_usage, usage)
        add_usage(session_usage, usage)
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
    return turn_usage


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
            if user_input in ("/q", "exit"):
                break
            if user_input in ("/h", "/help"):
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

            if PROVIDER == "inception":
                turn_usage = run_inception_turn(messages, system_prompt, session_usage)
            else:
                turn_usage = run_anthropic_turn(messages, system_prompt, session_usage)

            print_usage_summary(turn_usage, session_usage)
            print()

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as err:
            print(f"{RED}⏺ Error: {err}{RESET}")


if __name__ == "__main__":
    main()
