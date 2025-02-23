# Reconnaimate

**Reconnaimate** is a terminal-based process manager and scheduler built with Python and [Textual](https://github.com/Textualize/textual). It allows you to run and manage system commands interactively, view live command output, attach to running processes, and even schedule commands with dependencies—all within a rich TUI (Terminal User Interface).

## Features

- **Process Management**  
  - Run system commands in the background.
  - Attach/detach from running processes for live output.
  - Kill processes (with support for terminating entire process groups on Unix or using psutil on Windows).
  - List running and terminated processes in separate panels.
  
- **Workspace Support**  
  - Organize logs and command history into workspaces.
  - Create, list, and select workspaces (each workspace is stored in the `workspaces/` directory).
  - Logs are stored in `command_output.log` and command history in `history.txt` within each workspace.

- **Asynchronous Scheduler**  
  - Load a scheduling file (with a `.recon` extension) containing a JSON array of scheduled commands.
  - Schedule commands with options for start date, time, delays, and automatic termination (`kill_after`).
  - Validate schedule data to detect circular dependencies and ensure scheduled times are valid.
  - View and edit the scheduler in a dedicated Scheduler View, which includes:
    - A data table of scheduled commands.
    - A JSON editor for modifying the scheduler file.
    - A dependency tree to visualize command relationships.

- **User Interface Enhancements**  
  - Interactive command input with suggestion support, use right to complete.
  - A split-panel layout showing running processes, command output, and pop-out logs.
  - Command history navigation using the up/down arrow keys.

## Requirements

- **Python** 3.7+
- **Textual** – For building the terminal UI  
- **psutil** – For process management on Windows and Unix

Install dependencies via pip:

```bash
pip install textual psutil
```

Install dependencies via apt:
```bash
apt install python3-textual python3-psutil
```

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/yourusername/reconnaimate.git
   cd reconnaimate
   ```

2. **Run the Application:**

   ```bash
   python reconnaimate.py
   ```

## Usage

When you run the application, you will be presented with a TUI that includes multiple panels:
- **Left Panel:** Displays running and terminated processes.
- **Center Panel:** Shows the command output (also stored in the active workspace).
- **Popout Panels:** For attached process output or viewing detailed logs.

![Screenshot 2025-02-23 153118](https://github.com/user-attachments/assets/12c7a43f-c90c-46d5-886f-dd798f3394a9)



### Available Commands

- **help**  
  Show the help message with a list of available commands.

- **run `<command>` [--logfile filename]**  
  Run a system command. The command’s output is logged in the active workspace.

- **attach `<PID>`**  
  Attach to a running process to view its live output.

- **kill `<PID>`**  
  Terminate a running process (this action is logged).

- **list**  
  List all currently running processes.

- **detach**  
  Detach from any attached process or log popout.

- **exit**  
  Quit the application.

- **workspace `<create|list|select>` [name]**  
  Manage workspaces:
  - `workspace create <name>` – Create a new workspace.
  - `workspace list` – List all available workspaces.
  - `workspace select <name>` – Select an existing workspace as active.

- **scheduler load `<file.recon>`**  
  Load a scheduler JSON file to enable scheduling functionality and access the Scheduler View.
  - A default workspace is automatically created.
  - Using the `workspace load` command loads the .recon from the workspace or a path can be specified. 


## File Structure

- **reconnaimate.py**  
  The main application file that contains the TUI implementation, process management logic, and asynchronous scheduler.
  
- **workspaces/**  
  A directory automatically created in the project root to store workspaces.
  - Each workspace (e.g., `default/`) contains:
    - `command_output.log` – Log file with the output of executed commands.
    - `history.txt` – File recording command history.

- **\<scheduler_file\>.recon**  
  Scheduler files (in JSON format) that define scheduled commands. These files can be loaded via the `scheduler load` command.

### Scheduler JSON Format

The scheduler file is a JSON document that should contain an array of command definitions under the key `"commands"`. Each command may include the following fields:
- **id** – Unique identifier for the command.
- **description** – Brief description of the command.
- **command** – The actual system command to execute.
- **dependencies** – List of command IDs that must complete before this command runs.
- **start_date** – Scheduled start date in `YYYY-MM-DD` format.
- **start_time** – Scheduled start time in `HH:MM:SS` format.
- **start_delay** – Delay (in seconds) before executing the command.
- **kill_after** – Time (in seconds) after which the command will be terminated if still running.

The application validates the scheduler file to detect issues like circular dependencies or scheduling commands in the past.

![Screenshot 2025-02-23 153721](https://github.com/user-attachments/assets/48f34020-ef29-4506-91d9-0d58cec6fe69)


## Contributing

Contributions are welcome! Feel free to open issues or submit pull requests if you have improvements or new features to add.

## License

[MIT License](LICENSE)
```
