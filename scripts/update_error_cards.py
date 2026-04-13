import glob


def determine_error_type(msg):
    msg = msg.lower()
    if any(
        x in msg
        for x in [
            "missing",
            "invalid key",
            "no auth",
            "no data — set claude",
            "no data — oauth failed",
            "no data — web api failed",
            "no logs/auth",
        ]
    ):
        return "missing_config"
    if any(x in msg for x in ["unauthorized", "expired", "invalid token"]):
        return "auth_failed"
    if any(x in msg for x in ["rate limited", "429"]):
        return "rate_limited"
    if any(x in msg for x in ["connection", "timeout", "conn fail"]):
        return "timeout"
    if any(
        x in msg
        for x in [
            "parse error",
            "invalid response",
            "empty log",
            "no data",
            "no limits",
            "no quota",
            "no usage",
        ]
    ):
        return "parse_error"
    if any(x in msg for x in ["api error", "http", "fail", "db error", "api unavailable"]):
        return "api_error"
    return "unknown"


for filepath in glob.glob("app/services/**/*.py", recursive=True):
    with open(filepath) as f:
        lines = f.readlines()

    modified = False

    # Simple line-by-line replacement for single-line error_card
    for i in range(len(lines)):
        line = lines[i]
        if "error_card(" in line and "error_type=" not in line:
            # extract string literal (arg3)
            # basic parsing: error_card("Service", "icon", "Message")
            import re

            match = re.search(
                r'(error_card\([^,]+,\s*"[^"]+",\s*)(f?"[^"]*?"|f?\'[^\']*?\')(.*?)\)',
                line,
            )
            if match:
                prefix = match.group(1)
                msg_val = match.group(2)
                suffix = match.group(3)

                # Check if there is already a 4th argument
                if suffix.strip() == "":
                    err_type = determine_error_type(msg_val)
                    new_line = (
                        line[: match.start()]
                        + f'{prefix}{msg_val}, error_type="{err_type}"{suffix})'
                        + line[match.end() :]
                    )
                    lines[i] = new_line
                    modified = True
            else:
                # Might be multi-line in smart_collector.py
                pass

    if modified:
        with open(filepath, "w") as f:
            f.writelines(lines)
        print(f"Updated {filepath}")
