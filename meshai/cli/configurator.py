"""Rich-based TUI configurator for MeshAI."""

import time
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from ..config import Config, MeshSourceConfig, load_config, save_config

console = Console()


class Configurator:
    """Interactive configuration tool for MeshAI."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or Path("config.yaml")
        self.config: Config = load_config(self.config_path)
        self.modified = False

    def run(self) -> None:
        """Run the configurator."""
        try:
            self._show_welcome()
            self._main_menu()
        except KeyboardInterrupt:
            self._handle_exit()

    def _clear(self) -> None:
        """Clear the screen."""
        console.clear()

    def _show_welcome(self) -> None:
        """Display welcome header."""
        self._clear()
        header = Panel(
            Text(
                "MeshAI Configuration Tool\n"
                "Configure your Meshtastic LLM assistant",
                justify="center",
                style="cyan",
            ),
            title="[yellow]Welcome[/yellow]",
            border_style="blue",
        )
        console.print(header)
        console.print()

    def _status_icon(self, value: bool) -> str:
        """Return colored status icon."""
        return "[green]✓[/green]" if value else "[red]✗[/red]"

    def _main_menu(self) -> None:
        """Display and handle main menu."""
        while True:
            self._clear()
            self._show_header()

            table = Table(box=box.ROUNDED, show_header=False)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Description", style="white")
            table.add_column("Status", style="dim")

            disabled_count = len(self.config.commands.disabled_commands)
            cmd_status = f"{disabled_count} disabled" if disabled_count else "all enabled"

            table.add_row("1", "Bot Settings", self.config.bot.name)
            table.add_row("2", "Connection", f"{self.config.connection.type}")
            table.add_row("3", "LLM Backend", f"{self.config.llm.backend}/{self.config.llm.model}")
            table.add_row("4", "Response Settings", f"{self.config.response.max_length}ch max")
            table.add_row("5", "History & Memory", f"{self.config.history.max_messages_per_user} msgs")
            table.add_row("6", "Commands", cmd_status)
            ctx_status = self._status_icon(self.config.context.enabled)
            table.add_row("7", "Context", f"{ctx_status} {self.config.context.max_context_items} items")
            table.add_row("8", "Weather", f"{self.config.weather.primary}")
            mm_status = self._status_icon(self.config.meshmonitor.enabled)
            mm_url = self.config.meshmonitor.url or "[dim]not set[/dim]"
            table.add_row("9", "MeshMonitor Sync", f"{mm_status} {mm_url}")
            kb_status = self._status_icon(self.config.knowledge.enabled)
            kb_path = self.config.knowledge.db_path or "[dim]not set[/dim]"
            table.add_row("10", "Knowledge Base", f"{kb_status} {kb_path}")

            # Mesh Sources
            total_sources = len(self.config.mesh_sources)
            enabled_sources = sum(1 for s in self.config.mesh_sources if s.enabled)
            src_status = f"{enabled_sources}/{total_sources} enabled" if total_sources else "[dim]none[/dim]"
            table.add_row("11", "Mesh Sources", src_status)

            table.add_row("12", "Setup Wizard", "[dim]First-time setup[/dim]")

            console.print(table)
            console.print()

            # Exit options
            if self.modified:
                console.print("[yellow]* Unsaved changes[/yellow]")
                console.print()
            console.print("[white]13. Save[/white]                  [dim]Save config, stay in menu[/dim]")
            console.print("[green]14. Save & Restart Bot[/green]   [dim]Apply changes now[/dim]")
            console.print("[white]15. Save & Exit[/white]          [dim]Save, restart bot, exit[/dim]")
            console.print("[white]16. Exit without Saving[/white]")
            console.print()

            choice = IntPrompt.ask("Select option", default=14)

            if choice == 1:
                self._bot_settings()
            elif choice == 2:
                self._connection_settings()
            elif choice == 3:
                self._llm_settings()
            elif choice == 4:
                self._response_settings()
            elif choice == 5:
                self._history_settings()
            elif choice == 6:
                self._command_settings()
            elif choice == 7:
                self._context_settings()
            elif choice == 8:
                self._weather_settings()
            elif choice == 9:
                self._meshmonitor_settings()
            elif choice == 10:
                self._knowledge_settings()
            elif choice == 11:
                self._mesh_sources_settings()
            elif choice == 12:
                self._setup_wizard()
            elif choice == 13:
                self._save_only()
            elif choice == 14:
                self._save_and_restart()
            elif choice == 15:
                self._save_restart_exit()
                break
            elif choice == 16:
                break

    def _show_header(self) -> None:
        """Show compact header with modified indicator."""
        title = "[bold cyan]MeshAI Configuration[/bold cyan]"
        if self.modified:
            title += " [yellow]*[/yellow]"
        console.print(Panel(title, box=box.MINIMAL))

    def _handle_exit(self) -> None:
        """Handle exit (keyboard interrupt)."""
        if self.modified:
            if Confirm.ask("\nSave changes before exiting?", default=True):
                save_config(self.config, self.config_path)
                console.print("[green]Saved.[/green]")
        console.print("\nGoodbye!")

    def _bot_settings(self) -> None:
        """Bot settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Bot Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Bot Name", self.config.bot.name)
            table.add_row("2", "Owner", self.config.bot.owner or "[dim]not set[/dim]")
            table.add_row(
                "3", "Respond to DMs", self._status_icon(self.config.bot.respond_to_dms)
            )
            table.add_row(
                "4", "Filter BBS Protocols", self._status_icon(self.config.bot.filter_bbs_protocols)
            )
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = Prompt.ask("Bot name", default=self.config.bot.name)
                if value != self.config.bot.name:
                    self.config.bot.name = value
                    self.modified = True
            elif choice == 2:
                value = Prompt.ask("Owner", default=self.config.bot.owner)
                if value != self.config.bot.owner:
                    self.config.bot.owner = value
                    self.modified = True
            elif choice == 3:
                value = Confirm.ask("Respond to DMs?", default=self.config.bot.respond_to_dms)
                if value != self.config.bot.respond_to_dms:
                    self.config.bot.respond_to_dms = value
                    self.modified = True
            elif choice == 4:
                value = Confirm.ask("Filter BBS protocols?", default=self.config.bot.filter_bbs_protocols)
                if value != self.config.bot.filter_bbs_protocols:
                    self.config.bot.filter_bbs_protocols = value
                    self.modified = True

    def _connection_settings(self) -> None:
        """Connection settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Connection Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Connection Type", self.config.connection.type)
            table.add_row("2", "Serial Port", self.config.connection.serial_port)
            table.add_row("3", "TCP Host", self.config.connection.tcp_host)
            table.add_row("4", "TCP Port", str(self.config.connection.tcp_port))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                console.print("\n[cyan]1.[/cyan] serial - USB Serial connection")
                console.print("[cyan]2.[/cyan] tcp - TCP Network connection")
                sel = IntPrompt.ask("Select", default=1 if self.config.connection.type == "serial" else 2)
                value = "serial" if sel == 1 else "tcp"
                if value != self.config.connection.type:
                    self.config.connection.type = value
                    self.modified = True
            elif choice == 2:
                value = Prompt.ask("Serial port", default=self.config.connection.serial_port)
                if value != self.config.connection.serial_port:
                    self.config.connection.serial_port = value
                    self.modified = True
            elif choice == 3:
                value = Prompt.ask("TCP host", default=self.config.connection.tcp_host)
                if value != self.config.connection.tcp_host:
                    self.config.connection.tcp_host = value
                    self.modified = True
            elif choice == 4:
                value = IntPrompt.ask("TCP port", default=self.config.connection.tcp_port)
                if value != self.config.connection.tcp_port:
                    self.config.connection.tcp_port = value
                    self.modified = True

    def _llm_settings(self) -> None:
        """LLM backend settings submenu."""
        while True:
            self._clear()
            console.print("[bold]LLM Backend Settings[/bold]\n")

            # Mask API key for display
            api_key_display = "****" + self.config.llm.api_key[-4:] if len(self.config.llm.api_key) > 4 else "[dim]not set[/dim]"

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Backend", self.config.llm.backend)
            table.add_row("2", "API Key", api_key_display)
            table.add_row("3", "Base URL", self.config.llm.base_url)
            table.add_row("4", "Model", self.config.llm.model)
            table.add_row("5", "System Prompt", f"[dim]{len(self.config.llm.system_prompt)} chars[/dim]")
            table.add_row("6", "Use System Prompt", self._status_icon(self.config.llm.use_system_prompt))
            table.add_row("7", "Web Search", self._status_icon(self.config.llm.web_search))
            table.add_row("8", "Google Grounding", self._status_icon(self.config.llm.google_grounding))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                console.print("\n[cyan]1.[/cyan] openai - OpenAI / OpenAI-compatible (LiteLLM, etc)")
                console.print("[cyan]2.[/cyan] anthropic - Anthropic Claude")
                console.print("[cyan]3.[/cyan] google - Google Gemini")
                sel = IntPrompt.ask("Select", default=1)
                backends = {1: "openai", 2: "anthropic", 3: "google"}
                value = backends.get(sel, "openai")
                if value != self.config.llm.backend:
                    self.config.llm.backend = value
                    self.modified = True
            elif choice == 2:
                value = Prompt.ask("API Key", password=True)
                if value:
                    self.config.llm.api_key = value
                    self.modified = True
            elif choice == 3:
                value = Prompt.ask("Base URL", default=self.config.llm.base_url)
                if value != self.config.llm.base_url:
                    self.config.llm.base_url = value
                    self.modified = True
            elif choice == 4:
                value = Prompt.ask("Model", default=self.config.llm.model)
                if value != self.config.llm.model:
                    self.config.llm.model = value
                    self.modified = True
            elif choice == 5:
                console.print("\n[dim]Current prompt:[/dim]")
                console.print(self.config.llm.system_prompt or "(empty)")
                console.print()
                if Confirm.ask("Edit system prompt?", default=False):
                    console.print("[dim]Enter new prompt, or leave empty to clear[/dim]")
                    value = Prompt.ask("New system prompt", default="")
                    if value != self.config.llm.system_prompt:
                        self.config.llm.system_prompt = value
                        self.modified = True
            elif choice == 6:
                self.config.llm.use_system_prompt = not self.config.llm.use_system_prompt
                self.modified = True
            elif choice == 7:
                self.config.llm.web_search = not self.config.llm.web_search
                self.modified = True
            elif choice == 8:
                if self.config.llm.backend == "google":
                    self.config.llm.google_grounding = not self.config.llm.google_grounding
                    self.modified = True
                else:
                    console.print("[yellow]Google grounding is only available with the google backend.[/yellow]")
                    input("Press Enter to continue...")

    def _command_settings(self) -> None:
        """Command settings submenu."""
        # All built-in commands
        builtin = ["help", "ping", "status", "weather", "reset", "clear"]

        while True:
            self._clear()
            console.print("[bold]Command Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Command", style="white")
            table.add_column("Status", style="green")

            disabled = set(c.lower() for c in self.config.commands.disabled_commands)
            for i, cmd in enumerate(builtin, 1):
                status = "[red]disabled[/red]" if cmd in disabled else "[green]enabled[/green]"
                table.add_row(str(i), f"!{cmd}", status)

            table.add_row("", "", "")
            table.add_row("7", "Command Prefix", self.config.commands.prefix)
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif 1 <= choice <= len(builtin):
                cmd = builtin[choice - 1]
                if cmd in disabled:
                    self.config.commands.disabled_commands.remove(cmd)
                    console.print(f"[green]!{cmd} enabled[/green]")
                else:
                    self.config.commands.disabled_commands.append(cmd)
                    console.print(f"[red]!{cmd} disabled[/red]")
                self.modified = True
            elif choice == 7:
                value = Prompt.ask("Command prefix", default=self.config.commands.prefix)
                if value != self.config.commands.prefix:
                    self.config.commands.prefix = value
                    self.modified = True

    def _context_settings(self) -> None:
        """Mesh context settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Mesh Context Settings[/bold]\n")
            console.print("[dim]Passively observes channel traffic to give the LLM situational awareness.[/dim]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            channels = self.config.context.observe_channels
            ch_display = ", ".join(str(c) for c in channels) if channels else "[dim]all[/dim]"
            nodes = self.config.context.ignore_nodes
            node_display = ", ".join(nodes) if nodes else "[dim]none[/dim]"
            age_days = self.config.context.max_age // 86400

            table.add_row("1", "Enabled", self._status_icon(self.config.context.enabled))
            table.add_row("2", "Observe Channels", ch_display)
            table.add_row("3", "Ignore Nodes", node_display)
            table.add_row("4", "Max Age", f"{age_days}d")
            table.add_row("5", "Max Context Items", str(self.config.context.max_context_items))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                self.config.context.enabled = not self.config.context.enabled
                self.modified = True
            elif choice == 2:
                console.print("\n[dim]Enter channel indices separated by commas, or leave empty for all.[/dim]")
                value = Prompt.ask("Channels", default=", ".join(str(c) for c in channels))
                parsed = [int(x.strip()) for x in value.split(",") if x.strip().isdigit()] if value.strip() else []
                if parsed != self.config.context.observe_channels:
                    self.config.context.observe_channels = parsed
                    self.modified = True
            elif choice == 3:
                console.print("\n[dim]Enter node IDs separated by commas, or leave empty for none.[/dim]")
                value = Prompt.ask("Node IDs", default=", ".join(nodes))
                parsed = [x.strip() for x in value.split(",") if x.strip()] if value.strip() else []
                if parsed != self.config.context.ignore_nodes:
                    self.config.context.ignore_nodes = parsed
                    self.modified = True
            elif choice == 4:
                value = IntPrompt.ask("Max age (days)", default=age_days)
                seconds = value * 86400
                if seconds != self.config.context.max_age:
                    self.config.context.max_age = seconds
                    self.modified = True
            elif choice == 5:
                value = IntPrompt.ask("Max context items", default=self.config.context.max_context_items)
                if value != self.config.context.max_context_items:
                    self.config.context.max_context_items = value
                    self.modified = True

    def _weather_settings(self) -> None:
        """Weather settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Weather Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Primary Provider", self.config.weather.primary)
            table.add_row("2", "Fallback Provider", self.config.weather.fallback)
            table.add_row("3", "Default Location", self.config.weather.default_location or "[dim]not set[/dim]")
            table.add_row("4", "Open-Meteo URL", self.config.weather.openmeteo.url)
            table.add_row("5", "wttr.in URL", self.config.weather.wttr.url)
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                console.print("\n[cyan]1.[/cyan] openmeteo - Open-Meteo API (free, no key)")
                console.print("[cyan]2.[/cyan] wttr - wttr.in (free, simple)")
                console.print("[cyan]3.[/cyan] llm - Use LLM with web search")
                sel = IntPrompt.ask("Select", default=1)
                providers = {1: "openmeteo", 2: "wttr", 3: "llm"}
                value = providers.get(sel, "openmeteo")
                if value != self.config.weather.primary:
                    self.config.weather.primary = value
                    self.modified = True
            elif choice == 2:
                console.print("\n[cyan]1.[/cyan] openmeteo")
                console.print("[cyan]2.[/cyan] wttr")
                console.print("[cyan]3.[/cyan] llm")
                console.print("[cyan]4.[/cyan] none - No fallback")
                sel = IntPrompt.ask("Select", default=3)
                providers = {1: "openmeteo", 2: "wttr", 3: "llm", 4: "none"}
                value = providers.get(sel, "llm")
                if value != self.config.weather.fallback:
                    self.config.weather.fallback = value
                    self.modified = True
            elif choice == 3:
                value = Prompt.ask("Default location", default=self.config.weather.default_location)
                if value != self.config.weather.default_location:
                    self.config.weather.default_location = value
                    self.modified = True
            elif choice == 4:
                value = Prompt.ask("Open-Meteo URL", default=self.config.weather.openmeteo.url)
                if value != self.config.weather.openmeteo.url:
                    self.config.weather.openmeteo.url = value
                    self.modified = True
            elif choice == 5:
                value = Prompt.ask("wttr.in URL", default=self.config.weather.wttr.url)
                if value != self.config.weather.wttr.url:
                    self.config.weather.wttr.url = value
                    self.modified = True

    def _response_settings(self) -> None:
        """Response settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Response Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Min Delay (seconds)", str(self.config.response.delay_min))
            table.add_row("2", "Max Delay (seconds)", str(self.config.response.delay_max))
            table.add_row("3", "Max Length (chars)", str(self.config.response.max_length))
            table.add_row("4", "Max Messages", str(self.config.response.max_messages))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = float(Prompt.ask("Min delay", default=str(self.config.response.delay_min)))
                if value != self.config.response.delay_min:
                    self.config.response.delay_min = value
                    self.modified = True
            elif choice == 2:
                value = float(Prompt.ask("Max delay", default=str(self.config.response.delay_max)))
                if value != self.config.response.delay_max:
                    self.config.response.delay_max = value
                    self.modified = True
            elif choice == 3:
                value = IntPrompt.ask("Max length", default=self.config.response.max_length)
                if value != self.config.response.max_length:
                    self.config.response.max_length = value
                    self.modified = True
            elif choice == 4:
                value = IntPrompt.ask("Max messages", default=self.config.response.max_messages)
                if value != self.config.response.max_messages:
                    self.config.response.max_messages = value
                    self.modified = True

    def _history_settings(self) -> None:
        """History settings submenu."""
        while True:
            self._clear()
            console.print("[bold]History & Memory Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            timeout_hours = self.config.history.conversation_timeout // 3600
            table.add_row("1", "Database File", self.config.history.database)
            table.add_row("2", "Max Messages Per User", str(self.config.history.max_messages_per_user))
            table.add_row("3", "Conversation Timeout", f"{timeout_hours}h")
            table.add_row("4", "Auto Cleanup", self._status_icon(self.config.history.auto_cleanup))
            table.add_row("5", "Max Age (days)", str(self.config.history.max_age_days))
            table.add_row("", "[bold]Memory[/bold]", "")
            table.add_row("6", "Memory Enabled", self._status_icon(self.config.memory.enabled))
            table.add_row("7", "Window Size", str(self.config.memory.window_size))
            table.add_row("8", "Summarize Threshold", str(self.config.memory.summarize_threshold))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = Prompt.ask("Database file", default=self.config.history.database)
                if value != self.config.history.database:
                    self.config.history.database = value
                    self.modified = True
            elif choice == 2:
                value = IntPrompt.ask(
                    "Max messages per user", default=self.config.history.max_messages_per_user
                )
                if value != self.config.history.max_messages_per_user:
                    self.config.history.max_messages_per_user = value
                    self.modified = True
            elif choice == 3:
                value = IntPrompt.ask("Timeout (hours)", default=timeout_hours)
                seconds = value * 3600
                if seconds != self.config.history.conversation_timeout:
                    self.config.history.conversation_timeout = seconds
                    self.modified = True
            elif choice == 4:
                value = Confirm.ask("Enable auto cleanup?", default=self.config.history.auto_cleanup)
                if value != self.config.history.auto_cleanup:
                    self.config.history.auto_cleanup = value
                    self.modified = True
            elif choice == 5:
                value = IntPrompt.ask("Max age (days)", default=self.config.history.max_age_days)
                if value != self.config.history.max_age_days:
                    self.config.history.max_age_days = value
                    self.modified = True
            elif choice == 6:
                value = Confirm.ask("Enable memory?", default=self.config.memory.enabled)
                if value != self.config.memory.enabled:
                    self.config.memory.enabled = value
                    self.modified = True
            elif choice == 7:
                value = IntPrompt.ask("Window size", default=self.config.memory.window_size)
                if value != self.config.memory.window_size:
                    self.config.memory.window_size = value
                    self.modified = True
            elif choice == 8:
                value = IntPrompt.ask("Summarize threshold", default=self.config.memory.summarize_threshold)
                if value != self.config.memory.summarize_threshold:
                    self.config.memory.summarize_threshold = value
                    self.modified = True

    def _meshmonitor_settings(self) -> None:
        """MeshMonitor sync settings submenu."""
        while True:
            self._clear()
            console.print("[bold]MeshMonitor Sync Settings[/bold]\n")
            console.print("[dim]Sync auto-responder triggers from MeshMonitor to avoid duplicate responses.[/dim]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Enabled", self._status_icon(self.config.meshmonitor.enabled))
            table.add_row("2", "MeshMonitor URL", self.config.meshmonitor.url or "[dim]not set[/dim]")
            table.add_row("3", "Inject into Prompt", self._status_icon(self.config.meshmonitor.inject_into_prompt))
            table.add_row("4", "Refresh Interval", f"{self.config.meshmonitor.refresh_interval}s")
            table.add_row("5", "View Triggers", "[dim]Fetch and display[/dim]")
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                self.config.meshmonitor.enabled = not self.config.meshmonitor.enabled
                self.modified = True
            elif choice == 2:
                value = Prompt.ask("MeshMonitor URL (e.g., http://100.64.0.11:3333)",
                                   default=self.config.meshmonitor.url)
                if value != self.config.meshmonitor.url:
                    self.config.meshmonitor.url = value
                    self.modified = True
            elif choice == 3:
                self.config.meshmonitor.inject_into_prompt = not self.config.meshmonitor.inject_into_prompt
                self.modified = True
            elif choice == 4:
                value = IntPrompt.ask("Refresh interval (seconds)", default=self.config.meshmonitor.refresh_interval)
                if value != self.config.meshmonitor.refresh_interval:
                    self.config.meshmonitor.refresh_interval = value
                    self.modified = True
            elif choice == 5:
                self._view_meshmonitor_triggers()

    def _view_meshmonitor_triggers(self) -> None:
        """Fetch and display MeshMonitor triggers."""
        self._clear()
        console.print("[bold]MeshMonitor Triggers[/bold]\n")

        if not self.config.meshmonitor.url:
            console.print("[yellow]MeshMonitor URL not configured.[/yellow]")
            input("\nPress Enter to continue...")
            return

        console.print(f"[dim]Fetching from {self.config.meshmonitor.url}...[/dim]\n")

        try:
            from ..meshmonitor import MeshMonitorSync
            sync = MeshMonitorSync(self.config.meshmonitor.url)
            count = sync.load()

            if count == 0:
                if sync.last_error:
                    console.print(f"[red]Error: {sync.last_error}[/red]")
                else:
                    console.print("[yellow]No triggers configured in MeshMonitor.[/yellow]")
            else:
                console.print(f"[green]Loaded {count} triggers:[/green]\n")
                for trigger in sync.raw_triggers:
                    console.print(f"  [cyan]{trigger}[/cyan]")
        except Exception as e:
            console.print(f"[red]Failed to fetch triggers: {e}[/red]")

        input("\nPress Enter to continue...")


    def _knowledge_settings(self) -> None:
        """Knowledge base settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Knowledge Base Settings[/bold]\n")
            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Enabled", self._status_icon(self.config.knowledge.enabled))
            table.add_row("2", "Database Path", self.config.knowledge.db_path or "[dim]not set[/dim]")
            table.add_row("3", "Results Count", str(self.config.knowledge.top_k))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = Confirm.ask("Enable knowledge base?", default=self.config.knowledge.enabled)
                if value != self.config.knowledge.enabled:
                    self.config.knowledge.enabled = value
                    self.modified = True
            elif choice == 2:
                value = Prompt.ask("Database path", default=self.config.knowledge.db_path)
                if value != self.config.knowledge.db_path:
                    self.config.knowledge.db_path = value
                    self.modified = True
            elif choice == 3:
                value = IntPrompt.ask("Results count (top_k)", default=self.config.knowledge.top_k)
                if value != self.config.knowledge.top_k:
                    self.config.knowledge.top_k = value
                    self.modified = True

    def _mesh_sources_settings(self) -> None:
        """Mesh data sources settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Mesh Data Sources[/bold]\n")
            console.print("[dim]Connect to Meshview and/or MeshMonitor instances for live mesh data.[/dim]\n")

            # Display configured sources
            if self.config.mesh_sources:
                table = Table(box=box.ROUNDED)
                table.add_column("#", style="cyan", width=3)
                table.add_column("Name", style="white")
                table.add_column("Type", style="blue")
                table.add_column("URL", style="dim")
                table.add_column("Enabled", style="green")

                for i, src in enumerate(self.config.mesh_sources, 1):
                    table.add_row(
                        str(i),
                        src.name,
                        src.type,
                        src.url[:40] + "..." if len(src.url) > 40 else src.url,
                        self._status_icon(src.enabled),
                    )
                console.print(table)
            else:
                console.print("[dim]No sources configured.[/dim]")

            console.print()
            console.print("[cyan]1.[/cyan] Add source")
            console.print("[cyan]2.[/cyan] Edit source")
            console.print("[cyan]3.[/cyan] Remove source")
            console.print("[cyan]4.[/cyan] Test source")
            console.print("[cyan]0.[/cyan] Back")
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                self._add_mesh_source()
            elif choice == 2:
                self._edit_mesh_source()
            elif choice == 3:
                self._remove_mesh_source()
            elif choice == 4:
                self._test_mesh_source()

    def _add_mesh_source(self) -> None:
        """Add a new mesh data source."""
        self._clear()
        console.print("[bold]Add Mesh Source[/bold]\n")

        # Get name
        existing_names = {s.name for s in self.config.mesh_sources}
        while True:
            name = Prompt.ask("Source name (unique identifier)")
            if not name:
                console.print("[yellow]Name is required.[/yellow]")
                continue
            if name in existing_names:
                console.print(f"[yellow]Name '{name}' already exists. Choose another.[/yellow]")
                continue
            break

        # Get type
        console.print("\n[cyan]1.[/cyan] meshview - Meshview instance")
        console.print("[cyan]2.[/cyan] meshmonitor - MeshMonitor instance")
        type_choice = IntPrompt.ask("Source type", default=1)
        source_type = "meshview" if type_choice == 1 else "meshmonitor"

        # Get URL
        url = Prompt.ask("URL (e.g., https://meshview.example.com or http://192.168.1.100:3333)")

        # Get API token (MeshMonitor only)
        api_token = ""
        if source_type == "meshmonitor":
            console.print("\n[dim]API token is required for MeshMonitor. Use ${ENV_VAR} for env vars.[/dim]")
            api_token = Prompt.ask("API token", default="")

        # Get refresh interval
        refresh_interval = IntPrompt.ask("Refresh interval (seconds)", default=300)

        # Create and add source
        source = MeshSourceConfig(
            name=name,
            type=source_type,
            url=url,
            api_token=api_token,
            refresh_interval=refresh_interval,
            enabled=True,
        )
        self.config.mesh_sources.append(source)
        self.modified = True

        console.print(f"\n[green]Source '{name}' added.[/green]")
        input("Press Enter to continue...")

    def _edit_mesh_source(self) -> None:
        """Edit an existing mesh data source."""
        if not self.config.mesh_sources:
            console.print("[yellow]No sources to edit.[/yellow]")
            input("\nPress Enter to continue...")
            return

        self._clear()
        console.print("[bold]Edit Mesh Source[/bold]\n")

        # Show list
        for i, src in enumerate(self.config.mesh_sources, 1):
            status = "[green]enabled[/green]" if src.enabled else "[red]disabled[/red]"
            console.print(f"[cyan]{i}.[/cyan] {src.name} ({src.type}) - {status}")

        console.print("[cyan]0.[/cyan] Cancel")
        console.print()

        choice = IntPrompt.ask("Select source to edit", default=0)
        if choice == 0 or choice > len(self.config.mesh_sources):
            return

        src = self.config.mesh_sources[choice - 1]

        while True:
            self._clear()
            console.print(f"[bold]Edit Source: {src.name}[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Name", src.name)
            table.add_row("2", "Type", src.type)
            table.add_row("3", "URL", src.url)
            if src.type == "meshmonitor":
                token_display = "****" + src.api_token[-4:] if len(src.api_token) > 4 else src.api_token or "[dim]not set[/dim]"
                table.add_row("4", "API Token", token_display)
            table.add_row("5", "Refresh Interval", f"{src.refresh_interval}s")
            table.add_row("6", "Enabled", self._status_icon(src.enabled))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            opt = IntPrompt.ask("Select option", default=0)

            if opt == 0:
                return
            elif opt == 1:
                existing_names = {s.name for s in self.config.mesh_sources if s != src}
                value = Prompt.ask("Name", default=src.name)
                if value and value not in existing_names:
                    src.name = value
                    self.modified = True
                elif value in existing_names:
                    console.print("[yellow]Name already exists.[/yellow]")
            elif opt == 2:
                console.print("\n[cyan]1.[/cyan] meshview")
                console.print("[cyan]2.[/cyan] meshmonitor")
                t = IntPrompt.ask("Type", default=1 if src.type == "meshview" else 2)
                new_type = "meshview" if t == 1 else "meshmonitor"
                if new_type != src.type:
                    src.type = new_type
                    self.modified = True
            elif opt == 3:
                value = Prompt.ask("URL", default=src.url)
                if value != src.url:
                    src.url = value
                    self.modified = True
            elif opt == 4 and src.type == "meshmonitor":
                value = Prompt.ask("API Token", default=src.api_token)
                if value != src.api_token:
                    src.api_token = value
                    self.modified = True
            elif opt == 5:
                value = IntPrompt.ask("Refresh interval (seconds)", default=src.refresh_interval)
                if value != src.refresh_interval:
                    src.refresh_interval = value
                    self.modified = True
            elif opt == 6:
                src.enabled = not src.enabled
                self.modified = True

    def _remove_mesh_source(self) -> None:
        """Remove a mesh data source."""
        if not self.config.mesh_sources:
            console.print("[yellow]No sources to remove.[/yellow]")
            input("\nPress Enter to continue...")
            return

        self._clear()
        console.print("[bold]Remove Mesh Source[/bold]\n")

        # Show list
        for i, src in enumerate(self.config.mesh_sources, 1):
            console.print(f"[cyan]{i}.[/cyan] {src.name} ({src.type})")

        console.print("[cyan]0.[/cyan] Cancel")
        console.print()

        choice = IntPrompt.ask("Select source to remove", default=0)
        if choice == 0 or choice > len(self.config.mesh_sources):
            return

        src = self.config.mesh_sources[choice - 1]
        if Confirm.ask(f"Remove source '{src.name}'?", default=False):
            self.config.mesh_sources.pop(choice - 1)
            self.modified = True
            console.print(f"[green]Source '{src.name}' removed.[/green]")
            input("Press Enter to continue...")

    def _test_mesh_source(self) -> None:
        """Test a mesh data source connection."""
        if not self.config.mesh_sources:
            console.print("[yellow]No sources to test.[/yellow]")
            input("\nPress Enter to continue...")
            return

        self._clear()
        console.print("[bold]Test Mesh Source[/bold]\n")

        # Show list
        for i, src in enumerate(self.config.mesh_sources, 1):
            console.print(f"[cyan]{i}.[/cyan] {src.name} ({src.type})")

        console.print("[cyan]0.[/cyan] Cancel")
        console.print()

        choice = IntPrompt.ask("Select source to test", default=0)
        if choice == 0 or choice > len(self.config.mesh_sources):
            return

        src = self.config.mesh_sources[choice - 1]
        console.print(f"\n[dim]Testing {src.name} ({src.url})...[/dim]\n")

        try:
            if src.type == "meshview":
                from ..sources.meshview import MeshviewSource
                source = MeshviewSource(url=src.url, refresh_interval=src.refresh_interval)
            else:
                from ..sources.meshmonitor_data import MeshMonitorDataSource
                source = MeshMonitorDataSource(
                    url=src.url,
                    api_token=src.api_token,
                    refresh_interval=src.refresh_interval,
                )

            success = source.fetch_all()

            if success:
                console.print("[green]Connection successful![/green]\n")
                console.print(f"  Nodes: {len(source.nodes)}")
                if src.type == "meshview":
                    console.print(f"  Edges: {len(source.edges)}")
                    console.print(f"  Stats: {'loaded' if source.stats else 'none'}")
                    console.print(f"  Counts: {'loaded' if source.counts else 'none'}")
                else:
                    console.print(f"  Channels: {len(source.channels)}")
                    console.print(f"  Telemetry: {len(source.telemetry)}")
                    console.print(f"  Traceroutes: {len(source.traceroutes)}")
                    console.print(f"  Packets: {len(source.packets)}")
            else:
                console.print(f"[red]Connection failed: {source.last_error}[/red]")

        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

        input("\nPress Enter to continue...")

    def _setup_wizard(self) -> None:
        """First-time setup wizard."""
        self._clear()
        console.print(Panel("[bold]MeshAI Setup Wizard[/bold]", style="cyan"))
        console.print("\nThis wizard will help you configure MeshAI.\n")

        # Step 1: Bot identity
        console.print("[bold cyan]Step 1: Bot Identity[/bold cyan]")
        self.config.bot.name = Prompt.ask("Bot name", default="ai")
        self.config.bot.owner = Prompt.ask("Your name/callsign", default="")
        console.print()

        # Step 2: Connection
        console.print("[bold cyan]Step 2: Meshtastic Connection[/bold cyan]")
        console.print("[cyan]1.[/cyan] serial - USB Serial")
        console.print("[cyan]2.[/cyan] tcp - Network TCP")
        sel = IntPrompt.ask("Connection type", default=1)
        self.config.connection.type = "serial" if sel == 1 else "tcp"

        if self.config.connection.type == "serial":
            self.config.connection.serial_port = Prompt.ask(
                "Serial port", default="/dev/ttyUSB0"
            )
        else:
            self.config.connection.tcp_host = Prompt.ask(
                "TCP host", default="192.168.1.100"
            )
            self.config.connection.tcp_port = IntPrompt.ask("TCP port", default=4403)
        console.print()

        # Step 3: LLM
        console.print("[bold cyan]Step 3: LLM Backend[/bold cyan]")
        console.print("[cyan]1.[/cyan] openai - OpenAI / OpenAI-compatible")
        console.print("[cyan]2.[/cyan] anthropic - Anthropic Claude")
        console.print("[cyan]3.[/cyan] google - Google Gemini")
        sel = IntPrompt.ask("Backend", default=1)
        backends = {1: "openai", 2: "anthropic", 3: "google"}
        self.config.llm.backend = backends.get(sel, "openai")

        self.config.llm.api_key = Prompt.ask("API Key", password=True)

        if self.config.llm.backend == "openai":
            if Confirm.ask("Using local/self-hosted API?", default=False):
                self.config.llm.base_url = Prompt.ask(
                    "Base URL", default="http://localhost:4000/v1"
                )

        self.config.llm.model = Prompt.ask("Model", default="gpt-4o-mini")
        console.print()

        # Step 4: Weather (optional)
        console.print("[bold cyan]Step 4: Weather (optional)[/bold cyan]")
        self.config.weather.default_location = Prompt.ask(
            "Default location (for !weather)", default=""
        )
        console.print()

        self.modified = True
        console.print("[green]Setup complete![/green]")
        console.print("Press Enter to return to main menu...")
        input()

    def _save_only(self) -> None:
        """Save config and stay in menu."""
        save_config(self.config, self.config_path)
        console.print(f"[green]Configuration saved to {self.config_path}[/green]")
        self.modified = False
        input("Press Enter to continue...")

    def _save_and_restart(self) -> None:
        """Save config and signal bot to restart, stay in menu."""
        self._clear()
        console.print("[cyan]Saving configuration...[/cyan]")
        save_config(self.config, self.config_path)
        console.print("[green]Configuration saved![/green]")
        self.modified = False
        console.print()

        # Write restart signal file (docker-entrypoint watches for this)
        restart_file = Path("/tmp/meshai_restart")
        try:
            restart_file.touch()
            console.print("[cyan]Bot restart signal sent.[/cyan]")
            console.print()
            console.print("The bot will restart momentarily to apply changes.")
        except Exception as e:
            console.print(f"[yellow]Could not signal restart: {e}[/yellow]")

        input("\nPress Enter to continue...")

    def _save_restart_exit(self) -> None:
        """Save config, signal bot restart, and exit config tool."""
        console.print("[cyan]Saving configuration...[/cyan]")
        save_config(self.config, self.config_path)
        console.print("[green]Configuration saved![/green]")
        self.modified = False

        # Write restart signal file
        restart_file = Path("/tmp/meshai_restart")
        try:
            restart_file.touch()
            console.print("[cyan]Bot restart signal sent.[/cyan]")
        except Exception as e:
            console.print(f"[yellow]Could not signal restart: {e}[/yellow]")

        console.print("\nGoodbye!")


def run_configurator(config_path: Optional[Path] = None) -> None:
    """Entry point for configurator."""
    configurator = Configurator(config_path)
    configurator.run()
