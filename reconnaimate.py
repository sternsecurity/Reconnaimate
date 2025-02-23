import os
import platform
import shlex
import asyncio
import datetime
import json
import signal
import psutil

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll, Horizontal
from textual.events import Key
from textual.widgets import Header, Footer, Button, Input, Static, RichLog, Label, DataTable, Tree, TextArea
from textual.screen import Screen
from textual.suggester import Suggester

# Ensure the workspaces directory exists.
WORKSPACES_DIR = "workspaces"
os.makedirs(WORKSPACES_DIR, exist_ok=True)

# ---------------- Helper Widgets ----------------

def validate_schedule(data: dict) -> list:
    """Validate the schedule data for circular dependencies and past dates.
       Returns a list of warning messages."""
    warnings = []
    commands = data.get("commands", [])
    # Build a dependency graph: command id -> list of dependency IDs.
    graph = {}
    for cmd in commands:
        cmd_id = cmd.get("id")
        deps = cmd.get("dependencies", [])
        graph[cmd_id] = deps
    # Detect circular dependencies using DFS.
    def dfs(node, visited, rec_stack):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor, visited, rec_stack):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.remove(node)
        return False
    visited = set()
    for cmd_id in graph:
        if cmd_id not in visited:
            if dfs(cmd_id, visited, set()):
                warnings.append(f"Circular dependency detected involving command '{cmd_id}'.")
                break  # Or collect all cycles if needed.
    # Validate scheduled dates.
    today = datetime.date.today()
    current_time = datetime.datetime.now().time()
    for cmd in commands:
        start_date_str = cmd.get("start_date")
        start_time_str = cmd.get("start_time")
        if start_date_str:
            try:
                scheduled_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
            except Exception as e:
                warnings.append(f"Invalid date format in command '{cmd.get('id')}': {e}")
                continue
            if scheduled_date < today:
                warnings.append(f"Command '{cmd.get('id')}' scheduled for {scheduled_date} is in the past.")
            elif scheduled_date == today and start_time_str:
                try:
                    scheduled_time = datetime.datetime.strptime(start_time_str, "%H:%M:%S").time()
                except Exception as e:
                    warnings.append(f"Invalid time format in command '{cmd.get('id')}': {e}")
                    continue
                if scheduled_time < current_time:
                    warnings.append(f"Command '{cmd.get('id')}' scheduled for today at {scheduled_time} is in the past.")
    return warnings

def validate_json(content: str) -> list:
    """Validate JSON content and return a list of error messages (usually one error)."""
    errors = []
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        errors.append(f"{e.msg} at line {e.lineno}, column {e.colno} (char {e.pos})")
    return errors

class MySuggester(Suggester):
    async def get_suggestion(self, text: str) -> str:
        if not self.requester:
            return ""
        completions = [cmd for cmd in self.requester.app.completion_list if cmd.startswith(text)]
        if completions and completions[0] != text:
            return completions[0]
        return ""

class SuggesterInput(Input):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.suggester = MySuggester()
        self.suggester.requester = self

class ProcessRow(Container):
    def __init__(self, pid: int, cmdline: str, status: str, attached: bool = False, open_log: bool = False) -> None:
        super().__init__()
        self.pid = pid
        self.cmdline = cmdline
        self.status = status
        self.attached = attached
        self.open_log = open_log
        self.can_focus = False
        self.styles.layout = "horizontal"
        self.styles.width = "100%"
        self.styles.height = "auto"

    def compose(self) -> ComposeResult:
        info = Static(f"PID {self.pid}: {self.cmdline} [{self.status}]")
        info.styles.width = "60%"
        yield info
        if self.status == "running":
            if self.attached:
                yield Button("Detach", id=f"detach_{self.pid}", classes="process-btn")
            else:
                yield Button("Attach", id=f"attach_{self.pid}", classes="process-btn")
            yield Button("Kill", id=f"kill_{self.pid}", classes="process-btn")
        else:
            button_label = "Close Log" if self.open_log else "Open Log"
            yield Button(button_label, id=f"attachlog_{self.pid}", classes="process-btn")
            yield Button("Clear", id=f"clear_{self.pid}", classes="process-btn")

# ---------------- Asynchronous Scheduler Classes ----------------

class ScheduledCommand:
    def __init__(self, id, description, command, dependencies, start_date, start_time, start_delay, kill_after):
        self.id = id
        self.description = description
        self.command = command
        self.dependencies = dependencies or []
        self.start_date = start_date  # "YYYY-MM-DD" or None
        self.start_time = start_time  # "HH:MM:SS" or None
        self.start_delay = start_delay or 0
        self.kill_after = kill_after
        self.status = "pending"  # pending, running, completed, failed
        self.pid = None

class AsyncScheduler:
    def __init__(self, filename, process_manager):
        """
        filename: Path to a .recon JSON file.
        process_manager: An instance of Reconnaimate.
        """
        self.filename = filename
        self.process_manager = process_manager
        self.commands = {}  # Maps command id -> ScheduledCommand
        self.load_schedule()

    def load_schedule(self):
        try:
            with open(self.filename, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.process_manager.write_output(f"Error reading scheduler file: {e}")
            data = {"commands": []}
        warnings = validate_schedule(data)
        for w in warnings:
            self.process_manager.write_output(f"[bold red]{w}[/bold red]")
        for cmd_data in data.get("commands", []):
            cmd = ScheduledCommand(
                id=cmd_data["id"],
                description=cmd_data.get("description", ""),
                command=cmd_data["command"],
                dependencies=cmd_data.get("dependencies", []),
                start_date=cmd_data.get("start_date"),
                start_time=cmd_data.get("start_time"),
                start_delay=cmd_data.get("start_delay", 0),
                kill_after=cmd_data.get("kill_after")
            )
            self.commands[cmd.id] = cmd

    def dependencies_met(self, cmd: ScheduledCommand) -> bool:
        return all(self.commands.get(dep) and self.commands[dep].status == "completed" for dep in cmd.dependencies)

    def time_to_start(self, cmd: ScheduledCommand) -> bool:
        now = datetime.datetime.now()
        if cmd.start_date:
            try:
                scheduled_date = datetime.datetime.strptime(cmd.start_date, "%Y-%m-%d").date()
            except ValueError:
                return False
            if now.date() < scheduled_date:
                return False
        if cmd.start_time:
            try:
                scheduled_time = datetime.datetime.strptime(cmd.start_time, "%H:%M:%S").time()
            except ValueError:
                return False
            scheduled_datetime = datetime.datetime.combine(now.date(), scheduled_time)
            if now < scheduled_datetime:
                return False
        return True

    async def kill_after_delay(self, pid, delay):
        await asyncio.sleep(delay)
        await self.process_manager._kill_pid(pid)

    async def run_command(self, cmd: ScheduledCommand):
        if cmd.start_delay:
            await asyncio.sleep(cmd.start_delay)
        # Ensure that the command string is passed so it gets stored.
        pid = await self.process_manager.execute_command(cmd.command, command_str=cmd.command)
        if pid is None:
            self.process_manager.write_output(f"Scheduled command {cmd.id} failed to start.", log=True)
            cmd.status = "failed"
            return
        cmd.pid = pid
        cmd.status = "running"
        self.process_manager.write_output(f"Scheduled command {cmd.id} started (PID {pid}).", log=True)
        if cmd.kill_after:
            asyncio.create_task(self.kill_after_delay(pid, cmd.kill_after))
        while not await self.process_manager.check_process_complete(pid):
            await asyncio.sleep(1)
        cmd.status = "completed"
        self.process_manager.write_output(f"Scheduled command {cmd.id} completed.", log=True)

    async def run(self):
        while True:
            tasks = []
            for cmd in self.commands.values():
                if cmd.status == "pending" and self.dependencies_met(cmd) and self.time_to_start(cmd):
                    tasks.append(asyncio.create_task(self.run_command(cmd)))
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.sleep(1)

# ---------------- Scheduler View Screen ----------------

class SchedulerScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header("Scheduler View")
        with Container(id="scheduler_container"):
            with Container(id="table_container"):
                yield DataTable(id="scheduler_table")
            with Horizontal(id="bottom_container"):
                with Container(id="editor_container"):
                    yield TextArea.code_editor(id="scheduler_editor", language="json")
                    with Horizontal(id="save_controls"):
                        yield Button("Save", id="save_scheduler_btn", classes="footer-btn")
                        yield Static("", id="save_error")
                with Container(id="tree_container"):
                    yield Tree("Dependencies", id="scheduler_tree")
        with Container(id="scheduler_footer"):
            # Toggle button that shows "Activate Schedule" or "Deactivate Schedule"
            yield Button("Activate Schedule", id="activate_schedule_btn", classes="footer-btn")
            yield Button("Close Scheduler", id="close_scheduler_btn", classes="footer-btn")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2, self.refresh_table)
        self.update_static_widgets()
        table_container = self.query_one("#table_container", Container)
        editor_container = self.query_one("#editor_container", Container)
        tree_container = self.query_one("#tree_container", Container)
        table_container.border_title = "Scheduler Table"
        if getattr(self, "scheduler", None):
            editor_container.border_title = f"Scheduler Editor - {self.scheduler.filename}"
        else:
            editor_container.border_title = "Scheduler Editor"
        tree_container.border_title = "Dependencies"
        footer = self.query_one("#scheduler_footer", Container)
        footer.styles.layout = "horizontal"
        # Set the toggle button label based on whether the scheduler is active.
        button = self.query_one("#activate_schedule_btn", Button)
        if self.app.scheduler_task is not None:
            button.label = "Deactivate Schedule"
        else:
            button.label = "Activate Schedule"
        # Hide the Save button while the scheduler is active.
        save_button = self.query_one("#save_scheduler_btn", Button)
        if self.app.scheduler_task is not None:
            save_button.styles.display = "none"
        else:
            save_button.styles.display = "block"

    def refresh_table(self):
        table: DataTable = self.query_one("#scheduler_table", DataTable)
        if not table.columns:
            table.clear()
            table.add_column("ID")
            table.add_column("Description")
            table.add_column("Command")
            table.add_column("Status")
            table.add_column("Dependencies")
            table.add_column("Start Date")
            table.add_column("Start Time")
            table.add_column("Delay")
            table.add_column("Kill After")
        else:
            table.clear()
        if getattr(self, "scheduler", None):
            for cmd in self.scheduler.commands.values():
                deps = ", ".join(cmd.dependencies)
                table.add_row(
                    cmd.id,
                    cmd.description,
                    cmd.command,
                    cmd.status,
                    deps,
                    str(cmd.start_date) if cmd.start_date else "",
                    str(cmd.start_time) if cmd.start_time else "",
                    str(cmd.start_delay),
                    str(cmd.kill_after) if cmd.kill_after is not None else ""
                )
        table.refresh()

    def update_static_widgets(self):
        editor: TextArea = self.query_one("#scheduler_editor", TextArea)
        if getattr(self, "scheduler", None):
            try:
                with open(self.scheduler.filename, "r") as f:
                    file_content = f.read()
            except Exception as e:
                file_content = f"Error reading scheduler file: {e}"
            editor.text = file_content
            editor.refresh()
        else:
            editor.text = "No scheduler loaded. Please load a scheduler file first."
            editor.refresh()
        tree: Tree = self.query_one("#scheduler_tree", Tree)
        for child in list(tree.root.children):
            child.remove()
        tree.root.label = "Scheduled Commands Dependencies"
        if getattr(self, "scheduler", None):
            for cmd in self.scheduler.commands.values():
                cmd_node = tree.root.add(f"{cmd.id}: {cmd.description}")
                if cmd.dependencies:
                    for dep in cmd.dependencies:
                        cmd_node.add(f"Depends on: {dep}")
        tree.root.expand_all()
        tree.refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "activate_schedule_btn":
            await self.toggle_schedule()
        elif btn_id == "close_scheduler_btn":
            self.app.pop_screen()
        elif btn_id == "save_scheduler_btn":
            self.save_schedule()

    async def toggle_schedule(self):
        button = self.query_one("#activate_schedule_btn", Button)
        # Use the scheduler task stored in the main app.
        if self.app.scheduler_task is not None:
            # Scheduler is active; deactivate it.
            self.app.scheduler_task.cancel()
            self.app.scheduler_task = None
            # Kill running scheduler processes.
            for cmd in self.scheduler.commands.values():
                if cmd.status == "running" and cmd.pid is not None:
                    await self.app._kill_pid(cmd.pid)
                    cmd.status = "terminated"
            button.label = "Activate Schedule"
            self.app.write_output("Scheduler deactivated.", log=True)
            # Show the Save button when scheduler is not active.
            self.query_one("#save_scheduler_btn", Button).styles.display = "block"
        else:
            # Activate scheduler.
            self.app.scheduler_task = asyncio.create_task(self.scheduler.run())
            button.label = "Deactivate Schedule"
            self.app.write_output("Scheduler activated.", log=True)
            # Hide the Save button when scheduler is active.
            self.query_one("#save_scheduler_btn", Button).styles.display = "none"

    def save_schedule(self):
        editor: TextArea = self.query_one("#scheduler_editor", TextArea)
        error_label = self.query_one("#save_error", Static)
        content = editor.text
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            error_label.update(
                f"[bold red]JSON Error: {e.msg} at line {e.lineno}, col {e.colno} (char {e.pos})[/bold red]")
            return
        warnings = validate_schedule(parsed)
        if warnings:
            error_label.update("[bold red]" + "\n".join(warnings) + "[/bold red]")
            return
        try:
            with open(self.scheduler.filename, "w") as f:
                f.write(content)
            error_label.update("")
            self.app.write_output("Scheduler file saved.", log=True)
            self.scheduler.load_schedule()
        except Exception as e:
            error_label.update(f"[bold red]Error saving scheduler file: {e}[/bold red]")
            return
        self.update_static_widgets()

# ---------------- Main Process Manager with Scheduler Command and Dynamic Scheduler View ----------------

class Reconnaimate(App):
    CSS = """
    App {
        layout: grid;
        grid-rows: auto 1fr auto auto;
        height: 100%;
        width: 100vw;
    }
    #panels {
        layout: horizontal;
    }
    #left_panel {
        width: 30%;
        height: 100%;
        border: round #aaaaaa;
        margin-right: 1;
    }
    #running_label, #terminated_label {
        width: 100%;
        background: #34495e;
        color: white;
        text-align: center;
    }
    #running_container, #terminated_container {
        width: 100%;
        height: 45%;
    }
    #center_panel {
        width: 70%;
        height: 100%;
        border: round #aaaaaa;
    }
    #popout_panel, #logpopout_panel {
        width: 0%;
        height: 100%;
        border: none;
    }
    #popout_panel.active, #logpopout_panel.active {
        border: round #aaaaaa;
    }
    #output_panel, #attached_output_panel, #log_output_panel {
        width: 100%;
        height: 100%;
    }
    #command_area {
        padding: 1;
        height: 5;
        align-vertical: bottom;
    }
    .command-input {
        border: round #aaaaaa;
        width: 100%;
    }
    #footer_controls {
        dock: bottom;
        height: 2;
        content-align: center middle;
        background: #222;
    }
    Button.footer-btn {
        margin: 0 1;
        padding: 0 1;
        background: #555;
        color: white;
        border: none;
    }
    Button.process-btn {
        background: #34495e;
        color: white;
        border: none;
        padding: 0 1;
        min-width: 7;
        height: 1;
    }
    /* Scheduler screen extra styling for individual elements */
    #table_container{
        height: 10;
        border: round #aaaaaa;
    }
    #editor_container {
        border: round #aaaaaa;
        height: 100%;
    }
    #scheduler_editor {
        height: 100%;
    }
    #save_controls {
        dock: bottom;
        height: auto;
        min-height: 1;
    }
    #tree_container {
        border: round #aaaaaa;
        height: 100%;
    }
    /* Ensure the bottom container lays out its children horizontally */
    #bottom_container {
        layout: horizontal;
    }
    #scheduler_footer {
        layout: horizontal;
        dock: bottom;
        height: 2;
    }
    """
    def __init__(self):
        super().__init__()
        self.running_processes = {}  # Maps PID -> (proc, status, log_filename, command)
        self.is_windows = (platform.system() == "Windows")
        self.attached_process = None
        self.proc_lock = asyncio.Lock()
        self.current_workspace = None  # Active workspace name
        self.completion_list = ["help", "run", "attach", "kill", "list", "detach", "exit", "workspace", "scheduler"]
        self.command_history = []
        self.history_index = 0
        self.open_log_pid = None
        self.scheduler = None
        # Store the scheduler task here so its state persists.
        self.scheduler_task = None

    def get_command_str(self, pid) -> str:
        entry = self.running_processes.get(pid)
        if entry and len(entry) >= 4:
            return entry[3]
        return "Unknown"

    async def close_popouts(self):
        if self.attached_process:
            self.attached_process = None
            try:
                self.query_one("#attached_output_panel", RichLog).clear()
            except Exception:
                pass
            popout = self.query_one("#popout_panel", Container)
            popout.remove_class("active")
            popout.styles.width = "0%"
        if self.open_log_pid is not None:
            self.open_log_pid = None
            log_container = self.query_one("#logpopout_panel", Container)
            log_container.remove_class("active")
            log_container.styles.width = "0%"
            try:
                self.query_one("#log_output_panel", RichLog).clear()
            except Exception:
                pass
            center = self.query_one("#center_panel", Container)
            center.styles.width = "70%"

    def parse_command_params(self, args):
        custom_log_filename = None
        new_args = []
        i = 0
        while i < len(args):
            if args[i].startswith("--logfile="):
                custom_log_filename = args[i].split("=", 1)[1]
                i += 1
            elif args[i] == "--logfile":
                if i + 1 < len(args):
                    custom_log_filename = args[i + 1]
                    i += 2
                else:
                    raise ValueError("Error: --logfile flag provided without filename.")
            else:
                new_args.append(args[i])
                i += 1
        return new_args, custom_log_filename

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Container(
                Label("Running Processes", id="running_label"),
                VerticalScroll(id="running_container", can_focus=False),
                Label("Terminated Processes", id="terminated_label"),
                VerticalScroll(id="terminated_container", can_focus=False),
                id="left_panel",
            ),
            Container(
                RichLog(id="output_panel", highlight=False, wrap=True),
                id="center_panel"
            ),
            Container(
                RichLog(id="attached_output_panel", highlight=False, wrap=True),
                id="popout_panel"
            ),
            Container(
                RichLog(id="log_output_panel", highlight=False, wrap=True),
                id="logpopout_panel"
            ),
            id="panels"
        )
        yield Container(
            SuggesterInput(id="command_input", placeholder="Enter command..."),
            id="command_area"
        )
        with Container(id="footer_controls"):
            yield Button("Scheduler View", id="scheduler_view_btn", classes="footer-btn")
        yield Footer()

    async def on_mount(self):
        self.output_panel: RichLog = self.query_one("#output_panel", RichLog)
        self.attached_panel: RichLog = self.query_one("#attached_output_panel", RichLog)
        self.log_panel: RichLog = self.query_one("#log_output_panel", RichLog)
        self.command_input: SuggesterInput = self.query_one("#command_input", SuggesterInput)
        self.command_input.add_class("command-input")
        self.set_focus(self.command_input)
        left_panel = self.query_one("#left_panel", Container)
        left_panel.border_title = "Processes"
        center_panel = self.query_one("#center_panel", Container)
        default_workspace_path = os.path.join(WORKSPACES_DIR, "default")
        if not os.path.isdir(default_workspace_path):
            os.makedirs(default_workspace_path, exist_ok=True)
            self.write_output("Workspace 'default' created and set as active workspace.", log=False)
        else:
            self.write_output("Workspace 'default' exists and set as active workspace.", log=False)
        self.current_workspace = "default"
        self.load_workspace_data()
        center_panel.border_title = f"Command Output (Workspace: {self.current_workspace})"
        self.write_output("Command Output:\nWaiting for input...", log=True)
        self.set_interval(2, self.update_process_panel)
        try:
            scheduler_btn = self.query_one("#scheduler_view_btn", Button)
            if self.scheduler is None:
                scheduler_btn.styles.display = "none"
            else:
                scheduler_btn.styles.display = "block"
        except Exception:
            pass

    def update_scheduler_table(self):
        table: DataTable = self.query_one("#scheduler_table", DataTable)
        table.clear(rows=True)
        for cmd in self.scheduler.commands.values():
            deps = ", ".join(cmd.dependencies)
            table.add_row(
                cmd.id,
                cmd.description,
                cmd.command,
                cmd.status,
                deps,
                str(cmd.start_date) if cmd.start_date else "",
                str(cmd.start_time) if cmd.start_time else "",
                str(cmd.start_delay),
                str(cmd.kill_after) if cmd.kill_after is not None else ""
            )

    def write_output(self, message: str, log: bool = True):
        self.output_panel.write(message + "\n")
        if log and self.current_workspace:
            self.log_to_workspace(message + "\n")

    def log_to_workspace(self, message: str):
        path = os.path.join(WORKSPACES_DIR, self.current_workspace, "command_output.log")
        try:
            timestamp = datetime.datetime.now().isoformat()
            with open(path, "a") as f:
                f.write(f"{timestamp} - {message}")
        except Exception:
            pass

    async def on_key(self, event: Key) -> None:
        if self.focused is self.command_input:
            if event.key == "up":
                if self.command_history:
                    self.history_index = max(0, self.history_index - 1)
                    self.command_input.value = self.command_history[self.history_index]
                event.stop()
            elif event.key == "down":
                if self.command_history:
                    if self.history_index < len(self.command_history) - 1:
                        self.history_index += 1
                        self.command_input.value = self.command_history[self.history_index]
                    else:
                        self.history_index = len(self.command_history)
                        self.command_input.value = ""
                event.stop()

    def append_history(self, command: str):
        path = os.path.join(WORKSPACES_DIR, self.current_workspace, "history.txt")
        try:
            with open(path, "a") as f:
                f.write(command + "\n")
        except Exception:
            pass

    def load_workspace_data(self):
        if not self.current_workspace:
            return
        center_panel = self.query_one("#center_panel", Container)
        center_panel.border_title = f"Command Output (Workspace: {self.current_workspace})"
        self.output_panel.clear()
        path = os.path.join(WORKSPACES_DIR, self.current_workspace)
        command_output_log = os.path.join(path, "command_output.log")
        history_path = os.path.join(path, "history.txt")
        if os.path.isfile(command_output_log):
            try:
                with open(command_output_log, "r") as f:
                    logs = f.read()
                self.write_output(f"Workspace '{self.current_workspace}' Command Output Log loaded.", log=False)
                self.write_output(logs, log=False)
            except Exception:
                pass
        else:
            self.write_output(f"No command output log found for workspace '{self.current_workspace}'.", log=False)
        if os.path.isfile(history_path):
            try:
                with open(history_path, "r") as f:
                    lines = f.readlines()
                self.command_history = [line.strip() for line in lines if line.strip()][-50:]
                self.history_index = len(self.command_history)
            except Exception:
                pass
        else:
            self.write_output(f"No command history found for workspace '{self.current_workspace}'.", log=False)

    async def update_process_panel(self):
        async with self.proc_lock:
            for pid, entry in list(self.running_processes.items()):
                if len(entry) == 3:
                    proc, status, log_filename = entry
                    command = "Unknown"
                    self.running_processes[pid] = (proc, status, log_filename, command)
                proc, status, log_filename, command = self.running_processes[pid]
                status = "terminated" if proc.returncode is not None else "running"
                self.running_processes[pid] = (proc, status, log_filename, command)
        try:
            running_container = self.query_one("#running_container", VerticalScroll)
            terminated_container = self.query_one("#terminated_container", VerticalScroll)
        except Exception:
            return
        for child in list(running_container.children):
            child.remove()
        for child in list(terminated_container.children):
            child.remove()
        async with self.proc_lock:
            for pid, (proc, status, log_filename, command) in self.running_processes.items():
                status = "terminated" if proc.returncode is not None else "running"
                self.running_processes[pid] = (proc, status, log_filename, command)
                open_log = (status != "running" and self.open_log_pid == pid)
                row = ProcessRow(pid, self.get_command_str(pid), status, attached=(self.attached_process == proc), open_log=open_log)
                if status == "running":
                    running_container.mount(row)
                else:
                    terminated_container.mount(row)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "command_input":
            command = event.value.strip()
            if command:
                self.append_history(command)
                self.command_history.append(command)
                self.history_index = len(self.command_history)
                await self.execute_command(command)
            event.input.value = ""

    async def execute_command(self, command: str, command_str: str = None):
        args = shlex.split(command)
        if not args:
            self.write_output("Error: no command given.", log=True)
            return
        cmd_name = args[0].lower()
        if cmd_name == "workspace":
            self.handle_workspace_command(args[1:])
            return
        if cmd_name == "scheduler":
            self.handle_scheduler_command(args[1:])
            return
        if cmd_name in {"help", "run", "attach", "kill", "list", "detach", "exit"}:
            await self.run_internal(cmd_name, args[1:])
            return
        try:
            new_args, custom_log_filename = self.parse_command_params(args)
        except ValueError as e:
            self.write_output(str(e), log=True)
            return
        if not new_args:
            self.write_output("Error: no command given after removing logfile parameter", log=True)
            return
        exec_command = command_str if command_str is not None else " ".join(new_args)
        if custom_log_filename is None:
            executable = new_args[0]
            tail = exec_command.partition(" ")[2][:10].strip().replace(" ", "_")
            if not tail:
                tail = "default"
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            custom_log_filename = f"{executable}_{tail}_{timestamp}.log"
        if not os.path.isabs(custom_log_filename):
            custom_log_filename = os.path.join(WORKSPACES_DIR, self.current_workspace, custom_log_filename)
        log_filename = custom_log_filename
        try:
            if self.is_windows and cmd_name in ["cmd", "powershell"]:
                proc = await asyncio.create_subprocess_shell(
                    exec_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    shell=True,
                    creationflags=0x00000010
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    exec_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    shell=True,
                    preexec_fn=os.setsid if not self.is_windows else None
                )
            pid = proc.pid
            async with self.proc_lock:
                self.running_processes[pid] = (proc, "running", log_filename, exec_command)
            self.write_output(f"> {command}\nProcess {pid} started in background. Logging to {log_filename}", log=True)
            asyncio.create_task(self._stream_process_output_async(proc, log_filename, exec_command))
            return pid
        except Exception as e:
            self.write_output(f"Error: {e}", log=True)

    async def _stream_process_output_async(self, proc, log_filename, command_str):
        try:
            with open(log_filename, "a") as logfile:
                timestamp = datetime.datetime.now().isoformat()
                logfile.write(f"{timestamp} - Executing: {command_str}\n")
                logfile.flush()
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    decoded_line = line.decode().rstrip("\n")
                    logfile.write(decoded_line + "\n")
                    logfile.flush()
                    if self.attached_process == proc:
                        self.query_one("#attached_output_panel", RichLog).write(decoded_line)
        except Exception as e:
            self.write_output(f"Error in streaming output: {e}", log=True)

    async def check_process_complete(self, pid):
        async with self.proc_lock:
            entry = self.running_processes.get(pid)
        if entry:
            proc, _, _, _ = entry
            return proc.returncode is not None
        return True

    async def run_internal(self, cmd_name, args):
        mapper = {
            "help": self.show_help,
            "run": self.run_process,
            "attach": self.attach_process,
            "kill": self.kill_process,
            "list": self.list_processes,
            "detach": self.detach_process,
            "exit": self.exit_app,
            "scheduler": self.handle_scheduler_command
        }
        fn = mapper.get(cmd_name)
        if fn:
            await fn(args)

    def handle_workspace_command(self, args):
        if not args:
            self.write_output("Usage: workspace <create|list|select> [name]", log=False)
            return
        subcmd = args[0].lower()
        if subcmd == "create":
            if len(args) < 2:
                self.write_output("Usage: workspace create <name>", log=False)
            else:
                name = args[1]
                path = os.path.join(WORKSPACES_DIR, name)
                try:
                    os.makedirs(path, exist_ok=False)
                    self.write_output(f"Workspace '{name}' created.", log=False)
                except FileExistsError:
                    self.write_output(f"Workspace '{name}' already exists.", log=False)
        elif subcmd == "list":
            workspaces = [d for d in os.listdir(WORKSPACES_DIR) if os.path.isdir(os.path.join(WORKSPACES_DIR, d))]
            if workspaces:
                self.write_output("Workspaces:\n" + "\n".join(workspaces), log=False)
            else:
                self.write_output("No workspaces found.", log=False)
        elif subcmd == "select":
            if len(args) < 2:
                self.write_output("Usage: workspace select <name>", log=False)
            else:
                name = args[1]
                path = os.path.join(WORKSPACES_DIR, name)
                if os.path.isdir(path):
                    self.current_workspace = name
                    self.load_workspace_data()
                    self.write_output(f"Workspace '{name}' selected.", log=False)
                else:
                    self.write_output(f"Workspace '{name}' does not exist.", log=False)
        else:
            self.write_output("Unknown workspace command. Use create, list, or select.", log=False)

    def handle_scheduler_command(self, args):
        if not args or args[0].lower() != "load" or len(args) < 2:
            self.write_output("Usage: scheduler load <file.recon>", log=False)
            return
        file_path = args[1]
        if not os.path.isabs(file_path):
            workspace_file_path = os.path.join(WORKSPACES_DIR, self.current_workspace, file_path)
            if os.path.exists(workspace_file_path):
                file_path = workspace_file_path
        if not os.path.exists(file_path):
            self.write_output(f"File {file_path} not found.", log=True)
            return
        self.scheduler = AsyncScheduler(file_path, self)
        self.write_output(f"Scheduler loaded from {file_path}.", log=True)
        try:
            scheduler_btn = self.query_one("#scheduler_view_btn", Button)
            scheduler_btn.styles.display = "block"
        except Exception:
            pass

    async def show_help(self, _args):
        help_text = """
Available Commands:
- help: Show this help message
- run <command> [--logfile filename]: Run a system command (command input and its output will be logged)
- attach <PID>: Attach to a running process (display only, not logged)
- kill <PID>: Terminate a process (this action is logged)
- list: Show all running processes
- detach: Detach from any popout (live attach or log popout) that is open
- exit: Quit the application
- workspace <create|list|select> [name]: Manage workspaces
- scheduler load <file.recon>: Load a scheduling file and enable the Scheduler View button
"""
        self.write_output(help_text.strip(), log=False)

    async def run_process(self, args):
        if not args:
            self.write_output("Usage: run <command> [--logfile filename]", log=True)
            return
        await self.execute_command(" ".join(args))

    async def attach_process(self, args):
        if not args or not args[0].isdigit():
            self.write_output("Usage: attach <PID>", log=False)
            return
        pid = int(args[0])
        await self._attach_pid(pid)

    async def _attach_pid(self, pid: int):
        async with self.proc_lock:
            entry = self.running_processes.get(pid)
            proc = entry[0] if entry else None
        if self.attached_process and self.attached_process != proc:
            await self.detach_process([])
        if proc and proc.returncode is None:
            self.attached_process = proc
            self.write_output(f"Attached to process {pid}. Press 'detach' to return.", log=False)
            popout = self.query_one("#popout_panel", Container)
            popout.add_class("active")
            popout.styles.width = "40%"
            center = self.query_one("#center_panel", Container)
            center.styles.width = "30%"
        else:
            self.write_output(f"Could not attach to process {pid}, not running.", log=False)

    async def kill_process(self, args):
        if not args or not args[0].isdigit():
            self.write_output("Usage: kill <PID>", log=True)
            return
        pid = int(args[0])
        await self._kill_pid(pid)

    async def _kill_pid(self, pid: int):
        async with self.proc_lock:
            entry = self.running_processes.get(pid)
            proc, status, log_filename, command = (entry if entry else (None, None, None, None))
        if proc:
            if proc == self.attached_process:
                self.write_output(f"Detaching from PID {pid} because it is being killed.", log=True)
                await self.detach_process([])
                self.attached_process = None
            if self.is_windows:
                try:
                    proc_ps = psutil.Process(pid)
                    children = proc_ps.children(recursive=True)
                    for child in children:
                        child.kill()
                    proc_ps.kill()
                    proc_ps.wait(timeout=3)
                    self.write_output(f"Killed PID {pid} and its {len(children)} child process(es).", log=True)
                except Exception as e:
                    self.write_output(f"Failed to kill PID {pid} with psutil: {e}", log=True)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except TypeError:
                        proc.wait()
                    self.write_output(f"Killed process group for PID {pid}", log=True)
                except Exception as e:
                    self.write_output(f"Error terminating PID {pid}: {e}", log=True)
            async with self.proc_lock:
                self.running_processes[pid] = (proc, "terminated", log_filename, command)
        else:
            self.write_output(f"PID {pid} not found.", log=True)

    async def list_processes(self, _args):
        async with self.proc_lock:
            process_list = "\n".join(
                f"{pid}: {self.get_command_str(pid)}"
                for pid, (proc, _, _, _) in self.running_processes.items()
                if proc.returncode is None
            )
        self.write_output(f"Running Processes:\n{process_list if process_list else 'No active processes.'}", log=True)

    async def detach_process(self, _args):
        if self.attached_process or self.open_log_pid is not None:
            self.write_output("Detached from popout(s).", log=False)
            await self.close_popouts()
        else:
            self.write_output("No popout is currently open.", log=False)

    async def exit_app(self, _args):
        self.write_output("Exiting application...", log=True)
        self.exit()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "scheduler_view_btn":
            if self.scheduler is None:
                self.write_output("No scheduler file loaded. Please load a scheduler file first.", log=True)
            else:
                scheduler_screen = SchedulerScreen()
                scheduler_screen.scheduler = self.scheduler
                await self.push_screen(scheduler_screen)
            return
        if not btn_id or "_" not in btn_id:
            return
        parts = btn_id.split("_", 1)
        action = parts[0]
        pid_str = parts[1]
        try:
            pid = int(pid_str)
        except ValueError:
            return
        if action == "attach":
            await self._attach_pid(pid)
        elif action == "detach":
            await self.detach_process([])
        elif action == "kill":
            await self._kill_pid(pid)
        elif action == "clear":
            if self.open_log_pid == pid:
                self.open_log_pid = None
                log_container = self.query_one("#logpopout_panel", Container)
                log_container.remove_class("active")
                log_container.styles.width = "0%"
                self.query_one("#log_output_panel", RichLog).clear()
                self.write_output(f"Closed log for PID {pid}.", log=False)
                center = self.query_one("#center_panel", Container)
                center.styles.width = "70%"
            async with self.proc_lock:
                if pid in self.running_processes:
                    self.running_processes.pop(pid)
            await self.update_process_panel()
        elif action == "attachlog":
            await self._toggle_log(pid)
        elif btn_id == "save_scheduler_btn":
            self.save_schedule()

    async def _toggle_log(self, pid: int):
        async with self.proc_lock:
            entry = self.running_processes.get(pid)
            if not entry:
                self.write_output(f"No record found for PID {pid}.", log=False)
                return
            proc, status, log_filename, command = entry
        if status != "terminated":
            self.write_output(f"Process {pid} is still running; use the regular attach.", log=False)
            return
        log_container = self.query_one("#logpopout_panel", Container)
        center = self.query_one("#center_panel", Container)
        if self.open_log_pid == pid:
            self.open_log_pid = None
            log_container.remove_class("active")
            log_container.styles.width = "0%"
            center.styles.width = "70%"
            self.write_output(f"Closed log for PID {pid}.", log=False)
        else:
            try:
                with open(log_filename, "r") as logfile:
                    log_content = logfile.read()
                logpopout = self.query_one("#log_output_panel", RichLog)
                logpopout.clear()
                logpopout.write(log_content)
                log_container.add_class("active")
                log_container.styles.width = "40%"
                center.styles.width = "30%"
                self.open_log_pid = pid
                self.write_output(f"Opened log for PID {pid}.", log=False)
            except Exception as e:
                self.write_output(f"Error loading log file for PID {pid}: {e}", log=True)
        await self.update_process_panel()
        self.refresh(layout=True)

if __name__ == "__main__":
    app = Reconnaimate()
    app.run()
