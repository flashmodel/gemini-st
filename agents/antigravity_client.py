import subprocess
import threading
import queue
import json
import shutil
import sys
import logging
import os
import signal
import re
import datetime
try:
    import sublime
except ImportError:
    sublime = None

LOG = logging.getLogger(__package__)


def find_antigravity_cli(custom_command=None):
    """Search PATH and common default locations for the agy/antigravity CLI,
    or validate a custom command path.
    """
    if custom_command:
        cmd = os.path.expanduser(custom_command.strip())
        if cmd and cmd not in ("agy", "antigravity"):
            if os.path.isabs(cmd):
                return cmd if (os.path.isfile(cmd) and os.access(cmd, os.X_OK)) else None
            return shutil.which(cmd)

    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy

    which_antigravity = shutil.which("antigravity")
    if which_antigravity:
        return which_antigravity

    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "") or os.path.expanduser("~")

        if localappdata:
            candidates.append(os.path.join(localappdata, "agy", "bin", "agy.exe"))
            candidates.append(os.path.join(localappdata, "agy", "bin", "agy.cmd"))
            candidates.append(os.path.join(localappdata, "Programs", "antigravity", "bin", "agy.exe"))
            candidates.append(os.path.join(localappdata, "Programs", "antigravity", "bin", "agy.cmd"))
        if userprofile:
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy.exe"))
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy.cmd"))
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy"))
            candidates.append(os.path.join(userprofile, ".gemini", "antigravity-cli", "bin", "agy.exe"))
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "agy.cmd"),
                os.path.join(appdata, "npm", "agy"),
                os.path.join(appdata, "npm", "antigravity.cmd"),
                os.path.join(appdata, "npm", "antigravity"),
            ])
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "agy"),
            os.path.join(home, ".local", "bin", "antigravity"),
            os.path.join(home, ".gemini", "antigravity-cli", "bin", "agy"),
            os.path.join(home, ".npm-global", "bin", "agy"),
            os.path.join(home, ".npm-global", "bin", "antigravity"),
            "/usr/local/bin/agy",
            "/usr/bin/agy",
            "/opt/homebrew/bin/agy",
            "/home/linuxbrew/.linuxbrew/bin/agy",
            "/usr/local/bin/antigravity",
            "/usr/bin/antigravity",
            "/opt/homebrew/bin/antigravity",
            "/home/linuxbrew/.linuxbrew/bin/antigravity",
        ]

    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found Antigravity CLI at default location: {path_str}")
            return path_str

    return None


def _get_base_dir():
    """Return default ~/.gemini/antigravity-cli directory."""
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")


def _get_history_file():
    """Return default history.jsonl file path."""
    return os.path.join(_get_base_dir(), "history.jsonl")


def _get_transcript_paths(session_id):
    """
    Return candidate transcript log paths for a conversation ID (antigravity-cli only).
    Prefers transcript_full.jsonl (lossless full turns) over transcript.jsonl (compact).
    """
    base = _get_base_dir()
    return [
        os.path.join(base, "brain", session_id, ".system_generated", "logs", "transcript_full.jsonl"),
        os.path.join(base, "brain", session_id, ".system_generated", "logs", "transcript.jsonl"),
    ]


def _clean_antigravity_prompt(content):
    """Strip XML wrappers like <USER_REQUEST> and normalize whitespace."""
    if not content:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.DOTALL)
        text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
        text = re.sub(r"<user_information>.*?</user_information>", "", text, flags=re.DOTALL)
        text = text.strip()
    return " ".join(text.split())


def _parse_iso_timestamp(ts_str):
    """Parse ISO-8601 UTC timestamp to unix epoch float."""
    try:
        norm = ts_str.replace("Z", "+00:00")
        s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], norm)
        dt = datetime.datetime.fromisoformat(s)
        return float(dt.timestamp()) if dt.year > 1 else 0.0
    except Exception:
        return 0.0


def _is_workspace_match(ws_path, norm_cwd):
    """Check if a workspace path matches the target cwd (equal or parent/child)."""
    if not ws_path or not norm_cwd:
        return False
    norm_ws = os.path.normcase(os.path.realpath(ws_path))
    return (
        norm_ws == norm_cwd
        or norm_cwd.startswith(norm_ws + os.sep)
        or norm_ws.startswith(norm_cwd + os.sep)
    )


def _read_transcript_summary_and_mtime(session_id):
    """
    Read first valid USER_INPUT prompt and file mtime from brain transcript logs.
    Returns: (summary: str, mtime: float)
    """
    for t_path in _get_transcript_paths(session_id):
        if not os.path.isfile(t_path):
            continue
        try:
            mtime = os.path.getmtime(t_path)
        except OSError:
            mtime = 0.0

        summary = ""
        try:
            with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if idx > 15:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "USER_INPUT":
                        cleaned = _clean_antigravity_prompt(rec.get("content") or "")
                        if cleaned and not cleaned.startswith("/"):
                            summary = cleaned
                            break
        except Exception:
            pass
        return summary, mtime
    return "", 0.0


def list_antigravity_sessions(cwd=None):
    """
    List past Antigravity conversations from ~/.gemini/antigravity-cli cache, brain, and history.
    Returns list of dicts: {'session_id': str, 'summary': str, 'mtime': float, 'steps': int,
    'workspace': str} sorted newest-first.
    """
    from urllib.parse import urlparse, unquote

    base_dir = _get_base_dir()
    history_file = _get_history_file()
    last_conv_file = os.path.join(base_dir, "cache", "last_conversations.json")
    meta_file = os.path.join(base_dir, "cache", "conversation_metadata.json")

    sessions_by_id = {}

    # 1. Main metadata: bulk load structured sessions (Title, Steps, UpdatedAt, WorkspaceURIs)
    if os.path.isfile(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for cid, c_data in data.get("conversations", {}).items():
                        if not isinstance(c_data, dict):
                            continue
                        summary = c_data.get("summary") or {}
                        num_steps = summary.get("NumSteps", 0)
                        if num_steps <= 0:
                            continue

                        uris = summary.get("WorkspaceURIs") or []
                        ws_set = set()
                        for u in uris:
                            if isinstance(u, str) and u.startswith("file://"):
                                path = unquote(urlparse(u).path)
                                if path:
                                    ws_set.add(path)

                        title = (summary.get("Title") or summary.get("Preview") or "").strip()
                        mtime = _parse_iso_timestamp(summary.get("UpdatedAt") or "")
                        if mtime <= 0:
                            mtime = _parse_iso_timestamp(c_data.get("last_modified_time") or "")

                        sessions_by_id[cid] = {
                            "session_id": cid,
                            "summary": title,
                            "mtime": mtime,
                            "workspaces": ws_set,
                            "steps": num_steps,
                        }
        except Exception as e:
            LOG.debug(f"Error reading conversation metadata: {e}")

    # 2. Single-pass merge: history.jsonl (augments workspaces and missing active sessions)
    if os.path.isfile(history_file):
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    cid = entry.get("conversationId")
                    if not cid:
                        continue

                    ws = entry.get("workspace")
                    timestamp_ms = entry.get("timestamp") or 0
                    mtime = float(timestamp_ms) / 1000.0 if timestamp_ms else 0.0
                    display = entry.get("display") or ""
                    cleaned = _clean_antigravity_prompt(display)

                    if cid in sessions_by_id:
                        curr = sessions_by_id[cid]
                        if ws:
                            curr["workspaces"].add(ws)
                        if not curr.get("summary") and cleaned and not cleaned.startswith("/"):
                            curr["summary"] = cleaned
                        if mtime > curr.get("mtime", 0.0):
                            curr["mtime"] = mtime
                    else:
                        is_slash = cleaned.startswith("/")
                        ws_set = {ws} if ws else set()
                        sessions_by_id[cid] = {
                            "session_id": cid,
                            "summary": "" if is_slash else cleaned,
                            "mtime": mtime,
                            "workspaces": ws_set,
                            "steps": 0,
                        }
        except Exception as e:
            LOG.warning(f"Failed to read antigravity history: {e}")

    # 3. Active mappings: last_conversations.json (augments active workspace mapping per directory)
    if os.path.isfile(last_conv_file):
        try:
            with open(last_conv_file, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for ws, cid in data.items():
                        if isinstance(cid, str) and isinstance(ws, str) and ws:
                            if cid in sessions_by_id:
                                sessions_by_id[cid]["workspaces"].add(ws)
                            else:
                                sessions_by_id[cid] = {
                                    "session_id": cid,
                                    "summary": "",
                                    "mtime": 0.0,
                                    "workspaces": {ws},
                                    "steps": 0,
                                }
        except Exception as e:
            LOG.debug(f"Error reading last conversations: {e}")

    # 4. Fast targeted fallback & transcript mtime calibration
    for cid, s in sessions_by_id.items():
        if not s.get("summary"):
            fb_summary, fb_mtime = _read_transcript_summary_and_mtime(cid)
            if fb_summary:
                s["summary"] = fb_summary
            if fb_mtime > s.get("mtime", 0.0):
                s["mtime"] = fb_mtime
        else:
            for t_path in _get_transcript_paths(cid):
                if os.path.isfile(t_path):
                    try:
                        mt = os.path.getmtime(t_path)
                        if mt > s.get("mtime", 0.0):
                            s["mtime"] = mt
                    except OSError:
                        pass
                    break

    # 5. Filter by cwd if requested
    results = []
    norm_cwd = os.path.normcase(os.path.realpath(cwd)) if cwd else None

    for s in sessions_by_id.values():
        ws_set = s.get("workspaces") or set()
        matched_ws = None
        if norm_cwd:
            if not ws_set:
                continue
            for ws in ws_set:
                if _is_workspace_match(ws, norm_cwd):
                    matched_ws = ws
                    break
            if not matched_ws:
                continue
        else:
            matched_ws = next(iter(ws_set), None) if ws_set else None

        results.append({
            "session_id": s["session_id"],
            "summary": s["summary"] or "(empty)",
            "mtime": s["mtime"],
            "steps": s.get("steps", 0),
            "workspace": matched_ws,
        })

    # 6. Sort newest first (reverse chronological order)
    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results


def get_antigravity_session_tail(session_id, cwd=None, history_limit=50):
    """Retrieve tail information for an Antigravity conversation."""
    sessions = list_antigravity_sessions(cwd)
    meta = next((s for s in sessions if s["session_id"] == session_id), None)
    if not meta and cwd:
        all_sessions = list_antigravity_sessions(None)
        meta = next((s for s in all_sessions if s["session_id"] == session_id), None)

    transcript_paths = _get_transcript_paths(session_id)

    turns = []
    current_prompt = None
    current_response = ""
    file_mtime = 0.0

    for path in transcript_paths:
        if os.path.isfile(path):
            try:
                file_mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        rec_type = record.get("type")
                        content = record.get("content") or ""
                        if rec_type == "USER_INPUT":
                            if current_prompt is not None:
                                turns.append({"prompt": current_prompt, "response": current_response.strip()})
                            current_prompt = _clean_antigravity_prompt(content)
                            current_response = ""
                        elif rec_type in ("PLANNER_RESPONSE", "AGENT_RESPONSE"):
                            if content:
                                current_response += "\n" + content
                if current_prompt is not None:
                    turns.append({"prompt": current_prompt, "response": current_response.strip()})
                break
            except Exception as e:
                LOG.warning(f"Error reading transcript for session {session_id}: {e}")

    # Filter out completely empty turns
    turns = [t for t in turns if (t.get("prompt") and t["prompt"].strip()) or (t.get("response") and t["response"].strip())]

    if not meta and not turns:
        return None

    # Determine summary and mtime before slicing turns
    first_prompt = turns[0]["prompt"] if turns else ""
    summary = (meta.get("summary") if meta else "") or (first_prompt[:60] if first_prompt else "")
    mtime = (meta.get("mtime") if meta else None) or file_mtime

    if history_limit > 0 and len(turns) > history_limit:
        turns = turns[-history_limit:]

    return {
        "summary": summary,
        "mtime": mtime,
        "turns": turns,
    }


class AntigravityClient:
    """
    Handles Antigravity CLI (agy) protocol communication in headless stream-json mode,
    providing compatibility with GeminiCLI's ACP interface.
    """

    @classmethod
    def _parse_models_output(cls, text):
        """Parse raw output from `agy models` command."""
        if not text:
            return []

        model_dict = {}
        order = []

        lines = re.split(r'[\r\n]+', text)
        for line in lines:
            line = re.sub(r'^.*Fetching available models\.\.\.', '', line).strip()
            if not line or line.startswith("ERROR:") or line.startswith("Failed to"):
                continue
            match = re.match(r'^([a-zA-Z0-9._-]+)\s+(.+)$', line)
            if not match:
                continue

            raw_id = match.group(1).strip()
            display_name = match.group(2).strip()

            effort_match = re.search(r'-(high|medium|low)$', raw_id)
            if effort_match and raw_id.startswith("gemini-"):
                effort = effort_match.group(1)
                base_id = raw_id[:-len(effort)-1]
                clean_name = re.sub(r'\s*\((High|Medium|Low)\)$', '', display_name)
                if base_id not in model_dict:
                    model_dict[base_id] = {
                        "modelId": base_id,
                        "value": base_id,
                        "name": clean_name,
                        "displayName": clean_name,
                        "description": "",
                        "supportsEffort": True,
                        "supportedReasoningEfforts": [],
                    }
                    order.append(base_id)
                if effort not in model_dict[base_id]["supportedReasoningEfforts"]:
                    model_dict[base_id]["supportedReasoningEfforts"].append(effort)
            else:
                if raw_id not in model_dict:
                    model_dict[raw_id] = {
                        "modelId": raw_id,
                        "value": raw_id,
                        "name": display_name,
                        "displayName": display_name,
                        "description": "",
                        "supportsEffort": False,
                    }
                    order.append(raw_id)

        return [model_dict[mid] for mid in order]

    def _model_supports_effort(self, model_id):
        """Check if the given model supports reasoning effort flag."""
        if not model_id or model_id == "default":
            return False
        for m in self.available_models:
            mid = m.get("modelId") or m.get("value")
            if mid == model_id:
                return bool(m.get("supportsEffort", False))
        return model_id.startswith("gemini-")

    def __init__(self, callbacks, cwd=None, session_id=None, ignore_history=False, model=None, approve_mode=None, effort=None, skip_permissions=True):
        self.callbacks = callbacks
        self.cwd = cwd if cwd else os.path.expanduser("~")
        self.session_id = session_id if session_id else ""
        self.ignore_messages = ignore_history
        self.process = None
        self.input_queue = queue.Queue()
        self.message_id = 0
        self.current_msg_id = 0
        self.inited = False
        self.init_event = threading.Event()
        self.session_event = threading.Event()
        self._interrupted = False
        self._restarting = False
        self._stderr_lines = []

        self.current_model_id = model if model and model != "default" else None
        self.current_effort = effort
        self.approve_mode = approve_mode or "allow-edit"
        self.skip_permissions = skip_permissions

        self.available_models = []

        self.agent_capabilities = {
            "loadSession": True
        }
        self.agent_version = "1.0.0"
        self._start_args = {}

    def _fetch_models_in_background(self, agent_command=None, extra_env=None):
        """Launch background thread to fetch and update available models from CLI."""
        threading.Thread(
            target=self._fetch_models,
            args=(agent_command, extra_env),
            daemon=True
        ).start()

    def _fetch_models(self, agent_command=None, extra_env=None):
        cli = agent_command or find_antigravity_cli() or "agy"

        resolved_cli = shutil.which(cli) or cli
        if not os.path.isabs(resolved_cli) and not shutil.which(resolved_cli):
            return

        env = os.environ.copy()
        cli_dir = os.path.dirname(resolved_cli) if os.path.isabs(resolved_cli) else ""
        if cli_dir:
            env["PATH"] = cli_dir + os.pathsep + env.get("PATH", "")
        local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        if extra_env:
            env.update(extra_env)

        v_args = {}
        is_win = (sublime.platform() == 'windows') if sublime and hasattr(sublime, 'platform') else (sys.platform == 'win32')
        if is_win:
            v_args['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

        try:
            cmd = [resolved_cli, "models"]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                universal_newlines=True,
                **v_args
            )
            stdout, _ = proc.communicate(timeout=5)
            if proc.returncode == 0 and stdout:
                parsed = self._parse_models_output(stdout)
                if parsed:
                    self.available_models = parsed
                    LOG.info("Loaded %d models from '%s models'", len(parsed), cli)
        except Exception as e:
            LOG.debug("Failed to fetch models from %s: %s", cli, e)

    def start(self, api_key=None, agent_command=None, extra_env=None):
        """Start the Antigravity CLI process and communication threads."""
        self._start_args = {
            "api_key": api_key,
            "agent_command": agent_command,
            "extra_env": extra_env
        }
        self._fetch_models_in_background(agent_command, extra_env)
        threading.Thread(
            target=self._start_thread,
            args=(api_key, agent_command, extra_env),
            daemon=True
        ).start()

    def _build_cmd(self, agent_command=None, extra_env=None):
        cli = agent_command or find_antigravity_cli() or "agy"

        cmd = [
            cli,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]

        if self.current_model_id and self.current_model_id != "default":
            model = self.current_model_id
            if "/" in model:
                model = model.split("/")[-1]
            cmd.extend(["--model", model])

            if self._model_supports_effort(model):
                effort = self.current_effort
                if not effort or effort not in ("low", "medium", "high"):
                    effort = "high" if "pro" in model.lower() else "medium"
                cmd.extend(["--effort", effort])

        if self.session_id:
            cmd.extend(["--conversation", self.session_id])

        if self.approve_mode == "accept-all":
            cmd.append("--dangerously-skip-permissions")
        elif self.approve_mode == "allow-edit":
            cmd.extend(["--mode", "accept-edits"])
            if self.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

        # Check for multi-root workspace directories
        if extra_env and extra_env.get("GEMINI_CLI_IDE_WORKSPACE_PATH"):
            for d in extra_env["GEMINI_CLI_IDE_WORKSPACE_PATH"].split(os.pathsep):
                if d and d != self.cwd and os.path.isdir(d):
                    cmd.extend(["--add-dir", d])

        return cli, cmd

    def _spawn_process(self, api_key, agent_command, extra_env):
        cli, cmd = self._build_cmd(agent_command, extra_env)

        resolved_cli = shutil.which(cli) or cli
        if not os.path.isabs(resolved_cli) and not shutil.which(resolved_cli):
            self.callbacks['on_error'](
                f"Antigravity CLI ('{cli}') not found in PATH or standard installation locations.\n"
                "Please install it or configure 'antigravity_command' in GeminiCLI settings."
            )
            return False

        env = os.environ.copy()
        cli_dir = os.path.dirname(resolved_cli) if os.path.isabs(resolved_cli) else ""
        if cli_dir:
            env["PATH"] = cli_dir + os.pathsep + env.get("PATH", "")
        local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        if api_key:
            env["GOOGLE_API_KEY"] = api_key

        if extra_env:
            env.update(extra_env)

        LOG.info("Antigravity CLI start: %s (cwd: %s)", cmd, self.cwd)

        popen_args = {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'shell': False,
            'env': env,
            'cwd': self.cwd,
            'encoding': 'utf-8',
            'universal_newlines': True,
            'bufsize': 1
        }
        is_win = (sublime.platform() == 'windows') if sublime and hasattr(sublime, 'platform') else (sys.platform == 'win32')
        if is_win:
            popen_args['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

        self.process = subprocess.Popen(cmd, **popen_args)
        return True

    def _start_thread(self, api_key, agent_command, extra_env):
        try:
            if not self._spawn_process(api_key, agent_command, extra_env):
                return

            threading.Thread(target=self._read_loop, daemon=True).start()
            threading.Thread(target=self._read_stderr_loop, daemon=True).start()
            threading.Thread(target=self._write_loop, daemon=True).start()
        except FileNotFoundError:
            self.callbacks['on_error']("Antigravity CLI executable not found.")
        except Exception as e:
            self.callbacks['on_error']("Antigravity execution error: %s" % e)

    def _write_loop(self):
        while True:
            item = self.input_queue.get()
            if item is None:
                if self.process and self.process.stdin:
                    try:
                        self.process.stdin.close()
                    except Exception:
                        pass
                break

            # If restart was deferred while busy, terminate old process now
            if self._restarting and self.process and self.process.poll() is None:
                self.process._restarting = True
                try:
                    self.process.terminate()
                except Exception:
                    pass

            msg_id, user_input = item
            self.current_msg_id = msg_id

            # If process has exited (e.g. after interrupt or settings restart),
            # respawn with existing session_id
            process_dead = not self.process or self.process.poll() is not None
            if self._interrupted or self._restarting or process_dead:
                self._interrupted = False
                self._restarting = False
                try:
                    ok = self._spawn_process(
                        self._start_args.get("api_key"),
                        self._start_args.get("agent_command"),
                        self._start_args.get("extra_env")
                    )
                    if not ok:
                        continue
                    threading.Thread(target=self._read_loop, daemon=True).start()
                    threading.Thread(target=self._read_stderr_loop, daemon=True).start()
                except Exception as e:
                    self.callbacks['on_error']("Failed to reconnect Antigravity: %s" % e)
                    continue

            payload = {
                "event": "user",
                "message": {
                    "content": user_input
                }
            }
            try:
                self._write_json(payload)
            except Exception as e:
                self.callbacks['on_error']("Error writing to Antigravity process: %s" % e)

    def _write_json(self, data):
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(data) + "\n"
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def _read_loop(self):
        proc = self.process
        try:
            for line in iter(proc.stdout.readline, ""):
                if line:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        LOG.debug("Antigravity read raw: %s", data)
                        self._handle_event(data)
                    except Exception as e:
                        LOG.error("Antigravity parse json error: %s (line: %s)", e, line_str[:200])
            LOG.info("Antigravity stdio closed")
        except Exception as e:
            if not self._interrupted:
                LOG.error("Antigravity read stdout error: %s", e)
        finally:
            LOG.info("Antigravity session ended")
            is_restarting = getattr(proc, "_restarting", False) or (
                self._restarting and not self.current_msg_id
            )
            if (
                proc
                and proc.poll() is not None
                and proc.returncode != 0
                and not self._interrupted
                and not is_restarting
            ):
                err_msg = self._extract_exit_error()
                if "context canceled" not in err_msg.lower():
                    self.callbacks['on_error'](err_msg)
                if self.current_msg_id:
                    self.callbacks['on_stop'](self.current_msg_id, "end_turn")
                    self.current_msg_id = 0
            elif self._interrupted and self.current_msg_id:
                self.callbacks['on_stop'](self.current_msg_id, "cancelled")
                self.current_msg_id = 0
            if not is_restarting:
                self.callbacks['on_exit']()

    def _read_stderr_loop(self):
        proc = self.process
        try:
            for line in iter(proc.stderr.readline, ""):
                if line:
                    line_str = line.strip()
                    if line_str:
                        self._stderr_lines.append(line_str)
                        if len(self._stderr_lines) > 50:
                            self._stderr_lines.pop(0)
                        LOG.debug("Antigravity stderr: %s", line_str)
        except Exception:
            pass

    def _extract_exit_error(self):
        for line in reversed(self._stderr_lines):
            low = line.lower()
            if "error:" in low or "please sign in" in low or "authentication" in low or "failed" in low:
                return line
        if self._stderr_lines:
            return self._stderr_lines[-1]
        rc = self.process.returncode if self.process else "unknown"
        return f"Antigravity process exited unexpectedly (code {rc})."

    def _handle_event(self, data):
        event_type = data.get("event")

        if event_type == "init":
            self.session_id = data.get("conversation_id") or self.session_id
            self.inited = True
            self.init_event.set()
            self.session_event.set()
            self.callbacks['on_session_ready']()

        elif event_type == "step_update":
            step_update = data.get("step_update") or data
            step_type = step_update.get("step_type")
            state = step_update.get("state")
            step_index = step_update.get("step_index")

            if step_type == "agent_response":
                text = step_update.get("text_delta") or step_update.get("content")
                if text:
                    self.callbacks['on_message'](text)

            elif step_type == "thinking":
                thinking_text = (
                    step_update.get("text_delta")
                    or step_update.get("thinking")
                    or step_update.get("content")
                )
                if thinking_text:
                    self.callbacks['on_thought'](thinking_text)

            elif step_type == "tool":
                tool_call = step_update.get("tool_call") or {}
                tool_info = step_update.get("tool_info") or {}
                tool_name = (
                    step_update.get("tool_name")
                    or tool_call.get("name")
                    or tool_info.get("name")
                    or "tool"
                )
                params = (
                    tool_call.get("args")
                    or tool_call.get("parameters")
                    or tool_info.get("parameters")
                    or {}
                )
                if isinstance(params, str):
                    try:
                        params = json.loads(params)
                    except Exception:
                        pass

                tool_kind = "tool"
                tool_title = tool_name
                if tool_name in ("run_command", "bash", "shell", "exec"):
                    tool_kind = "execute"
                    cmd_val = params.get("CommandLine") or params.get("command") or ""
                    tool_title = cmd_val
                elif tool_name in (
                    "write_to_file", "write_file", "replace_file_content",
                    "multi_replace_file_content", "edit_file"
                ):
                    tool_kind = "edit"
                    tool_title = params.get("TargetFile") or params.get("Path") or ""
                elif tool_name in ("view_file", "read_file"):
                    tool_kind = "read"
                    file_path = params.get("Path") or params.get("AbsolutePath") or ""
                    start_line = params.get("StartLine")
                    end_line = params.get("EndLine")
                    if file_path and start_line is not None and end_line is not None:
                        tool_title = f"{file_path}#L{start_line}-L{end_line}"
                    elif file_path and start_line is not None:
                        tool_title = f"{file_path}#L{start_line}"
                    else:
                        tool_title = file_path
                elif tool_name in ("grep_search", "find_by_name", "list_dir"):
                    tool_kind = "search"
                    query_val = params.get("Query") or params.get("Pattern") or params.get("DirectoryPath") or ""
                    tool_title = f"{tool_name} {query_val}".strip()

                tool_id = str(tool_call.get("id") or step_index or self._next_message_id())
                tool_update = {
                    "status": "in_progress" if state == "ACTIVE" else "completed",
                    "kind": tool_kind,
                    "title": tool_title,
                    "toolCallId": tool_id,
                    "name": tool_name,
                    "parameters": params,
                }
                self.callbacks['on_tool_call'](tool_update)

        elif event_type == "result":
            result = data.get("result") or data
            status = result.get("status")
            err_msg = result.get("error") or ""

            if status == "ERROR" and not self._interrupted:
                err_lower = err_msg.lower()
                is_ignorable = (
                    "context canceled" in err_lower
                    or "stream was interrupted" in err_lower
                    or bool(
                        result.get("response")
                        and any(
                            term in err_lower
                            for term in (
                                "eof",
                                "closed network connection",
                                "connection reset",
                                "broken pipe",
                            )
                        )
                    )
                )
                if err_msg and not is_ignorable:
                    self.callbacks['on_error'](err_msg)

            stop_reason = "cancelled" if self._interrupted else "end_turn"
            self.callbacks['on_stop'](self.current_msg_id, stop_reason)
            self.current_msg_id = 0

    def _next_message_id(self):
        self.message_id += 1
        return self.message_id

    def send_input(self, text):
        self.ignore_messages = False
        msgid = self._next_message_id()
        self.input_queue.put((msgid, text))
        return msgid

    def send_permission_response(self, msg_id, option_id):
        behavior = "allow" if ("allow" in option_id.lower() or "proceed" in option_id.lower()) else "deny"
        payload = {
            "event": "approval_response",
            "request_id": str(msg_id),
            "response": {
                "behavior": behavior
            }
        }
        self._write_json(payload)

    def agent_session_cancel(self):
        self._interrupted = True
        self._restarting = False
        proc = self.process
        if proc and proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
            except Exception as e:
                LOG.error("Failed to interrupt Antigravity: %s", e)

        while not self.input_queue.empty():
            try:
                self.input_queue.get_nowait()
            except Exception:
                break

    def _restart_process_for_settings(self):
        self._restarting = True

    def agent_session_set_model(self, model_id=None, effort=None):
        if not model_id or model_id == "default":
            self.current_model_id = None
        else:
            self.current_model_id = model_id
        if effort is not None:
            self.current_effort = effort if effort in ("low", "medium", "high") else None
        LOG.info("Antigravity model set to: %s (effort: %s)",
                 self.current_model_id, self.current_effort)
        self._restart_process_for_settings()

    def agent_session_set_effort(self, effort=None):
        self.current_effort = effort if effort in ("low", "medium", "high") else None
        LOG.info("Antigravity effort set to: %s", self.current_effort)
        self._restart_process_for_settings()

    def agent_session_set_approve_mode(self, mode, skip_permissions=None):
        self.approve_mode = mode or "allow-edit"
        if skip_permissions is not None:
            self.skip_permissions = skip_permissions
        LOG.info("Antigravity approve_mode set to: %s (skip_permissions: %s)",
                 self.approve_mode, self.skip_permissions)
        self._restart_process_for_settings()

    def stop(self):
        self._interrupted = True
        self._restarting = False
        self.input_queue.put(None)
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.process = None
