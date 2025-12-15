"""Rich-based TUI configurator for MeshAI."""

from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from ..config import Config, get_default_config, load_config, save_config

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

            table.add_row("1", "Bot Settings", f"@{self.config.bot.name}")
            table.add_row("2", "Connection", f"{self.config.connection.type}")
            table.add_row("3", "LLM Backend", f"{self.config.llm.backend}/{self.config.llm.model}")
            table.add_row("4", "Response Settings", f"{self.config.response.max_length}ch max")
            table.add_row("5", "Channels", f"{self.config.channels.mode}")
            table.add_row("6", "History & Memory", f"{self.config.history.max_messages_per_user} msgs")
            table.add_row("7", "Rate Limits", f"{self.config.rate_limits.messages_per_minute}/min")
            table.add_row("8", "Web Status Page", self._status_icon(self.config.web_status.enabled))
            table.add_row("9", "Announcements", self._status_icon(self.config.announcements.enabled))
            table.add_row("10", "Setup Wizard", "[dim]First-time setup[/dim]")

            console.print(table)
            console.print()

            # Exit options
            if self.modified:
                console.print("[yellow]* Unsaved changes[/yellow]")
                console.print()
            console.print("[white]11. Save[/white]                 [dim]Save config, stay in menu[/dim]")
            console.print("[green]12. Save & Restart Bot[/green]   [dim]Apply changes now[/dim]")
            console.print("[white]13. Save & Exit[/white]          [dim]Save, restart bot, exit[/dim]")
            console.print("[white]14. Exit without Saving[/white]")
            console.print()

            choice = IntPrompt.ask("Select option", default=12)

            if choice == 1:
                self._bot_settings()
            elif choice == 2:
                self._connection_settings()
            elif choice == 3:
                self._llm_settings()
            elif choice == 4:
                self._response_settings()
            elif choice == 5:
                self._channel_settings()
            elif choice == 6:
                self._history_settings()
            elif choice == 7:
                self._rate_limits_settings()
            elif choice == 8:
                self._web_status_settings()
            elif choice == 9:
                self._announcements_settings()
            elif choice == 10:
                self._setup_wizard()
            elif choice == 11:
                self._save_only()
            elif choice == 12:
                self._save_and_restart()
            elif choice == 13:
                self._save_restart_exit()
                break
            elif choice == 14:
                break

    def _show_header(self) -> None:
        """Show compact header with modified indicator."""
        title = "[bold cyan]MeshAI Configuration[/bold cyan]"
        if self.modified:
            title += " [yellow]*[/yellow]"
        console.print(Panel(title, box=box.MINIMAL))

    def _get_modified_indicator(self) -> str:
        """Return modified indicator string."""
        return "[yellow]* Unsaved changes[/yellow]" if self.modified else ""

    def _bot_settings(self) -> None:
        """Bot settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Bot Settings[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Bot Name (@mention)", self.config.bot.name)
            table.add_row("2", "Owner", self.config.bot.owner or "[dim]not set[/dim]")
            table.add_row(
                "3",
                "Respond to @mentions",
                self._status_icon(self.config.bot.respond_to_mentions),
            )
            table.add_row(
                "4", "Respond to DMs", self._status_icon(self.config.bot.respond_to_dms)
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
                value = Confirm.ask(
                    "Respond to @mentions?", default=self.config.bot.respond_to_mentions
                )
                if value != self.config.bot.respond_to_mentions:
                    self.config.bot.respond_to_mentions = value
                    self.modified = True
            elif choice == 4:
                value = Confirm.ask("Respond to DMs?", default=self.config.bot.respond_to_dms)
                if value != self.config.bot.respond_to_dms:
                    self.config.bot.respond_to_dms = value
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
                console.print(self.config.llm.system_prompt)
                console.print()
                if Confirm.ask("Edit system prompt?", default=False):
                    value = Prompt.ask("New system prompt")
                    if value:
                        self.config.llm.system_prompt = value
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

    def _channel_settings(self) -> None:
        """Channel filtering settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Channel Filtering[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            whitelist_str = ", ".join(str(c) for c in self.config.channels.whitelist)
            table.add_row("1", "Mode", self.config.channels.mode)
            table.add_row("2", "Whitelist Channels", whitelist_str or "[dim]none[/dim]")
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                console.print("\n[cyan]1.[/cyan] all - Respond on all channels")
                console.print("[cyan]2.[/cyan] whitelist - Only respond on specific channels")
                sel = IntPrompt.ask("Select", default=1 if self.config.channels.mode == "all" else 2)
                value = "all" if sel == 1 else "whitelist"
                if value != self.config.channels.mode:
                    self.config.channels.mode = value
                    self.modified = True
            elif choice == 2:
                value = Prompt.ask(
                    "Whitelist (comma-separated)", default=whitelist_str
                )
                try:
                    channels = [int(c.strip()) for c in value.split(",") if c.strip()]
                    if channels != self.config.channels.whitelist:
                        self.config.channels.whitelist = channels
                        self.modified = True
                except ValueError:
                    console.print("[red]Invalid input. Use comma-separated numbers.[/red]")

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

    def _rate_limits_settings(self) -> None:
        """Rate limits settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Rate Limits[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Messages Per Minute (per user)", str(self.config.rate_limits.messages_per_minute))
            table.add_row("2", "Global Messages Per Minute", str(self.config.rate_limits.global_messages_per_minute))
            table.add_row("3", "Cooldown (seconds)", str(self.config.rate_limits.cooldown_seconds))
            table.add_row("4", "Burst Allowance", str(self.config.rate_limits.burst_allowance))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = IntPrompt.ask("Messages per minute", default=self.config.rate_limits.messages_per_minute)
                if value != self.config.rate_limits.messages_per_minute:
                    self.config.rate_limits.messages_per_minute = value
                    self.modified = True
            elif choice == 2:
                value = IntPrompt.ask("Global messages per minute", default=self.config.rate_limits.global_messages_per_minute)
                if value != self.config.rate_limits.global_messages_per_minute:
                    self.config.rate_limits.global_messages_per_minute = value
                    self.modified = True
            elif choice == 3:
                value = float(Prompt.ask("Cooldown (seconds)", default=str(self.config.rate_limits.cooldown_seconds)))
                if value != self.config.rate_limits.cooldown_seconds:
                    self.config.rate_limits.cooldown_seconds = value
                    self.modified = True
            elif choice == 4:
                value = IntPrompt.ask("Burst allowance", default=self.config.rate_limits.burst_allowance)
                if value != self.config.rate_limits.burst_allowance:
                    self.config.rate_limits.burst_allowance = value
                    self.modified = True

    def _web_status_settings(self) -> None:
        """Web status page settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Web Status Page[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Enabled", self._status_icon(self.config.web_status.enabled))
            table.add_row("2", "Port", str(self.config.web_status.port))
            table.add_row("3", "Show Uptime", self._status_icon(self.config.web_status.show_uptime))
            table.add_row("4", "Show Message Count", self._status_icon(self.config.web_status.show_message_count))
            table.add_row("5", "Show Connected Nodes", self._status_icon(self.config.web_status.show_connected_nodes))
            table.add_row("6", "Show Recent Activity", self._status_icon(self.config.web_status.show_recent_activity))
            table.add_row("7", "Require Auth", self._status_icon(self.config.web_status.require_auth))
            table.add_row("8", "Auth Password", "****" if self.config.web_status.auth_password else "[dim]not set[/dim]")
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = Confirm.ask("Enable web status?", default=self.config.web_status.enabled)
                if value != self.config.web_status.enabled:
                    self.config.web_status.enabled = value
                    self.modified = True
            elif choice == 2:
                value = IntPrompt.ask("Port", default=self.config.web_status.port)
                if value != self.config.web_status.port:
                    self.config.web_status.port = value
                    self.modified = True
            elif choice == 3:
                value = Confirm.ask("Show uptime?", default=self.config.web_status.show_uptime)
                if value != self.config.web_status.show_uptime:
                    self.config.web_status.show_uptime = value
                    self.modified = True
            elif choice == 4:
                value = Confirm.ask("Show message count?", default=self.config.web_status.show_message_count)
                if value != self.config.web_status.show_message_count:
                    self.config.web_status.show_message_count = value
                    self.modified = True
            elif choice == 5:
                value = Confirm.ask("Show connected nodes?", default=self.config.web_status.show_connected_nodes)
                if value != self.config.web_status.show_connected_nodes:
                    self.config.web_status.show_connected_nodes = value
                    self.modified = True
            elif choice == 6:
                value = Confirm.ask("Show recent activity?", default=self.config.web_status.show_recent_activity)
                if value != self.config.web_status.show_recent_activity:
                    self.config.web_status.show_recent_activity = value
                    self.modified = True
            elif choice == 7:
                value = Confirm.ask("Require authentication?", default=self.config.web_status.require_auth)
                if value != self.config.web_status.require_auth:
                    self.config.web_status.require_auth = value
                    self.modified = True
            elif choice == 8:
                value = Prompt.ask("Password", password=True)
                if value:
                    self.config.web_status.auth_password = value
                    self.modified = True

    def _announcements_settings(self) -> None:
        """Announcements settings submenu."""
        while True:
            self._clear()
            console.print("[bold]Announcements[/bold]\n")

            table = Table(box=box.ROUNDED)
            table.add_column("Option", style="cyan", width=4)
            table.add_column("Setting", style="white")
            table.add_column("Value", style="green")

            table.add_row("1", "Enabled", self._status_icon(self.config.announcements.enabled))
            table.add_row("2", "Interval (hours)", str(self.config.announcements.interval_hours))
            table.add_row("3", "Channel", str(self.config.announcements.channel))
            table.add_row("4", "Messages", f"{len(self.config.announcements.messages)} defined")
            table.add_row("5", "Random Order", self._status_icon(self.config.announcements.random_order))
            table.add_row("0", "Back", "")

            console.print(table)
            console.print()

            choice = IntPrompt.ask("Select option", default=0)

            if choice == 0:
                return
            elif choice == 1:
                value = Confirm.ask("Enable announcements?", default=self.config.announcements.enabled)
                if value != self.config.announcements.enabled:
                    self.config.announcements.enabled = value
                    self.modified = True
            elif choice == 2:
                value = IntPrompt.ask("Interval (hours)", default=self.config.announcements.interval_hours)
                if value != self.config.announcements.interval_hours:
                    self.config.announcements.interval_hours = value
                    self.modified = True
            elif choice == 3:
                value = IntPrompt.ask("Channel", default=self.config.announcements.channel)
                if value != self.config.announcements.channel:
                    self.config.announcements.channel = value
                    self.modified = True
            elif choice == 4:
                self._announcements_messages_editor()
            elif choice == 5:
                value = Confirm.ask("Random order?", default=self.config.announcements.random_order)
                if value != self.config.announcements.random_order:
                    self.config.announcements.random_order = value
                    self.modified = True

    def _announcements_messages_editor(self) -> None:
        """Edit announcement messages."""
        while True:
            self._clear()
            console.print("[bold]Announcement Messages[/bold]\n")

            if self.config.announcements.messages:
                for i, msg in enumerate(self.config.announcements.messages, 1):
                    console.print(f"  {i}. {msg[:60]}...")
            else:
                console.print("  [dim]No messages[/dim]")

            console.print("\n[cyan]a[/cyan] Add message")
            console.print("[cyan]r[/cyan] Remove message")
            console.print("[cyan]0[/cyan] Back")
            console.print()

            choice = Prompt.ask("Select", default="0")

            if choice == "0":
                return
            elif choice.lower() == "a":
                value = Prompt.ask("Message text")
                if value:
                    self.config.announcements.messages.append(value)
                    self.modified = True
            elif choice.lower() == "r":
                if self.config.announcements.messages:
                    idx = IntPrompt.ask("Remove which number", default=1)
                    if 1 <= idx <= len(self.config.announcements.messages):
                        self.config.announcements.messages.pop(idx - 1)
                        self.modified = True

    def _setup_wizard(self) -> None:
        """First-time setup wizard."""
        self._clear()
        console.print(Panel("[bold]MeshAI Setup Wizard[/bold]", style="cyan"))
        console.print("\nThis wizard will help you configure MeshAI.\n")

        # Step 1: Bot identity
        console.print("[bold cyan]Step 1: Bot Identity[/bold cyan]")
        self.config.bot.name = Prompt.ask("Bot name (for @mentions)", default="ai")
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
