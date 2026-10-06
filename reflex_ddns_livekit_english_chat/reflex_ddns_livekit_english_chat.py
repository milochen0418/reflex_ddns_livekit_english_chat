import reflex as rx
from reflex_ddns_auth.intent import IntentPage, intent

from reflex_ddns_livekit_english_chat import livekit_server, stt, translate
from reflex_ddns_livekit_english_chat.livekit_bridge import LiveKitBridgeState, bind_livekit
from reflex_ddns_livekit_english_chat.livekit_proxy import register_livekit_routes
from reflex_ddns_livekit_english_chat.states.server_state import ServerStatusState
from reflex_ddns_livekit_english_chat.states.settings_state import SettingsState

# Single source of truth for how LiveKit JS binds to this UI.
LIVEKIT_UI = bind_livekit(LiveKitBridgeState)

_CARD = "w-full max-w-md bg-white p-8 rounded-2xl border border-gray-100 shadow-sm"
_PRIMARY_BUTTON = (
    "w-full bg-violet-600 hover:bg-violet-700 text-white font-semibold py-2.5 "
    "rounded-lg transition-colors flex items-center justify-center mt-4 disabled:opacity-50"
)


def input_field(
    label: str, name: str, placeholder: str, type: str = "text", value: str = ""
) -> rx.Component:
    return rx.el.div(
        rx.el.label(label, class_name="block text-sm font-semibold text-gray-700 mb-1"),
        rx.el.input(
            name=name,
            type=type,
            placeholder=placeholder,
            default_value=value,
            key=value,
            class_name="w-full px-4 py-2 border border-gray-200 rounded-lg focus:ring-2 focus:ring-violet-500 focus:border-transparent outline-none transition-all",
        ),
        class_name="w-full",
    )


def error_box(message: rx.Var) -> rx.Component:
    return rx.el.div(
        rx.icon("circle-alert", class_name="h-5 w-5 text-red-500 shrink-0"),
        rx.el.p(message, class_name="text-red-700 text-sm break-words min-w-0"),
        class_name="bg-red-50 p-4 rounded-lg flex items-center gap-3 border border-red-100 mb-2",
    )


def server_status_pill() -> rx.Component:
    return rx.el.div(
        rx.el.div(
            class_name=rx.match(
                ServerStatusState.phase,
                ("running", "size-2 rounded-full bg-green-500"),
                ("error", "size-2 rounded-full bg-red-500"),
                "size-2 rounded-full bg-yellow-500 animate-pulse",
            )
        ),
        rx.match(
            ServerStatusState.phase,
            ("running", rx.el.span("Self-hosted LiveKit ", ServerStatusState.version)),
            ("downloading", rx.el.span("Downloading LiveKit server…")),
            ("error", rx.el.span("LiveKit server unavailable")),
            rx.el.span("Starting LiveKit server…"),
        ),
        id="server-status",
        data_phase=ServerStatusState.phase,
        class_name="flex items-center gap-2 bg-white px-3 py-1 rounded-full border border-gray-100 text-xs font-medium text-gray-600",
    )


def speech_status_pill() -> rx.Component:
    """The subtitles' speech model: downloaded once, then loaded at every start."""
    return rx.el.div(
        rx.el.div(
            class_name=rx.match(
                ServerStatusState.stt_phase,
                ("ready", "size-2 rounded-full bg-green-500"),
                ("error", "size-2 rounded-full bg-red-500"),
                "size-2 rounded-full bg-yellow-500 animate-pulse",
            )
        ),
        rx.match(
            ServerStatusState.stt_phase,
            ("ready", rx.el.span("Subtitles ready · Whisper ", ServerStatusState.stt_model)),
            (
                "downloading",
                rx.el.span(
                    "Downloading the speech model (first start only)… ",
                    ServerStatusState.stt_progress,
                ),
            ),
            ("error", rx.el.span("Subtitles unavailable", title=ServerStatusState.stt_message)),
            rx.el.span("Loading the speech model…"),
        ),
        id="speech-status",
        data_phase=ServerStatusState.stt_phase,
        class_name="flex items-center gap-2 bg-white px-3 py-1 rounded-full border border-gray-100 text-xs font-medium text-gray-600",
    )


def language_select(text_class: str = "text-sm") -> rx.Component:
    """The language picked subtitle words are translated to (remembered in this browser)."""
    return rx.el.label(
        rx.icon("languages", class_name="h-4 w-4 text-violet-500 shrink-0"),
        rx.el.select(
            *[rx.el.option(label, value=code) for code, label, _name in translate.LANGUAGES],
            # Uncontrolled: the choice shows at once (subtitles.js reads it when
            # translating), instead of springing back until the backend confirms it.
            default_value=LiveKitBridgeState.translate_to,
            key=LiveKitBridgeState.translate_to,
            on_change=LiveKitBridgeState.set_translate_to,
            title="Translate selected subtitles to",
            aria_label="Translate to",
            custom_attrs={"data-translate-to": ""},
            class_name=f"{text_class} bg-white border border-gray-200 rounded-lg pl-2 pr-7 py-1 text-gray-700 focus:ring-2 focus:ring-violet-500 outline-none",
        ),
        class_name="flex items-center gap-1.5 shrink-0",
    )


def transcript_panel(log_height: str) -> rx.Component:
    """Everyone's subtitles as lines; select words in them and right-click to translate."""
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                rx.icon("captions", class_name="h-4 w-4 text-violet-500 shrink-0"),
                rx.el.span("Subtitles", class_name="text-sm font-semibold text-gray-800"),
                LIVEKIT_UI.subtitle_status("text-xs text-amber-600 truncate"),
                class_name="flex items-center gap-2 min-w-0",
            ),
            rx.el.div(
                rx.el.span(
                    "Select words, then right-click to translate to",
                    class_name="text-xs text-gray-400 hidden md:inline",
                ),
                language_select("text-xs"),
                class_name="flex items-center gap-2 shrink-0",
            ),
            class_name="flex items-center justify-between gap-3 mb-2",
        ),
        LIVEKIT_UI.transcript(f"{log_height} overflow-y-auto pr-1"),
        class_name="w-full mt-5 p-3 rounded-xl border border-gray-100 bg-gray-50/70 text-left",
    )


def info_row(label: str, value: rx.Var | str) -> rx.Component:
    return rx.el.div(
        rx.el.dt(label, class_name="text-gray-500"),
        rx.el.dd(value, class_name="font-mono text-gray-900 break-all text-right"),
        class_name="flex justify-between gap-4 py-1.5 border-b border-gray-50 text-sm",
    )


def readonly_field(label: str, value: rx.Var, field_id: str) -> rx.Component:
    return rx.el.div(
        rx.el.label(label, class_name="block text-xs font-semibold text-gray-500 mb-1"),
        rx.el.input(
            id=field_id,
            value=value,
            read_only=True,
            class_name="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-lg font-mono text-xs text-gray-800",
        ),
        class_name="w-full",
    )


def server_admin_panel() -> rx.Component:
    return rx.el.div(
        rx.cond(
            SettingsState.server["message"] != "",
            error_box(SettingsState.server["message"]),
            rx.fragment(),
        ),
        rx.el.h2("Connect other apps", class_name="text-sm font-bold text-gray-900"),
        rx.el.p(
            "Any LiveKit SDK can use this server with these values.",
            class_name="text-xs text-gray-500 mb-2",
        ),
        readonly_field("LiveKit URL (client SDKs)", SettingsState.livekit_url, "livekit-url"),
        readonly_field("Server API URL (server SDKs)", SettingsState.server_api_url, "server-api-url"),
        readonly_field("API Key", SettingsState.livekit_api_key, "api-key"),
        readonly_field("API Secret", SettingsState.livekit_api_secret, "api-secret"),
        rx.el.form(
            rx.el.div(
                input_field(
                    "Media IP override (node_ip)",
                    "node_ip",
                    "empty = " + SettingsState.server["node_ip"],
                    value=SettingsState.node_ip_override,
                ),
                rx.el.p(
                    "The IP browsers send audio and face data to (ports TCP ",
                    SettingsState.server["tcp_port"],
                    " / UDP ",
                    SettingsState.server["udp_port"],
                    "). Default: EXTERNAL_IP from re-ddns, i.e. the Docker host's LAN IP.",
                    class_name="text-xs text-gray-500 mt-1",
                ),
                rx.el.button(
                    rx.cond(
                        SettingsState.is_saving,
                        rx.el.span("Saving...", class_name="animate-pulse"),
                        rx.el.div(
                            rx.icon("save", class_name="h-4 w-4"),
                            "Save & Apply",
                            class_name="flex items-center gap-2",
                        ),
                    ),
                    type="submit",
                    disabled=SettingsState.is_saving,
                    class_name=_PRIMARY_BUTTON,
                ),
            ),
            on_submit=SettingsState.save_node_ip,
            reset_on_submit=False,
            class_name="pt-4",
        ),
        rx.el.div(
            rx.el.button(
                rx.icon("rotate-ccw", class_name="h-4 w-4"),
                rx.cond(SettingsState.is_restarting, "Restarting...", "Restart server"),
                on_click=SettingsState.restart_server,
                disabled=SettingsState.is_restarting,
                class_name="flex items-center gap-2 text-sm font-semibold text-gray-700 bg-gray-100 hover:bg-gray-200 px-4 py-2 rounded-lg disabled:opacity-50",
            ),
            rx.el.button(
                rx.icon("refresh-cw", class_name="h-4 w-4"),
                "Refresh",
                on_click=SettingsState.refresh,
                class_name="flex items-center gap-2 text-sm font-semibold text-gray-700 bg-gray-100 hover:bg-gray-200 px-4 py-2 rounded-lg",
            ),
            class_name="flex gap-3 pt-2",
        ),
        rx.el.div(
            rx.el.p(
                "Server log · pid ",
                SettingsState.server["pid"],
                " · started ",
                SettingsState.server["started_at"],
                class_name="text-xs font-semibold text-gray-500 mb-1",
            ),
            rx.el.pre(
                SettingsState.server_log,
                class_name="text-[10px] leading-4 bg-gray-900 text-gray-100 p-3 rounded-lg max-h-64 overflow-auto whitespace-pre-wrap break-all",
            ),
            class_name="pt-4",
        ),
        class_name="space-y-3",
    )


def settings_page() -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                rx.el.a(
                    rx.el.div(
                        rx.icon("arrow-left", class_name="h-4 w-4"),
                        rx.el.span("Back to Lobby"),
                        class_name="flex items-center gap-2 text-gray-600 hover:text-gray-900 transition-colors",
                    ),
                    href="/",
                    class_name="mb-6 inline-block",
                ),
                rx.el.div(
                    rx.icon("settings", class_name="text-violet-500 h-8 w-8"),
                    rx.el.h1(
                        "LiveKit Server",
                        class_name="text-2xl font-bold text-gray-900",
                    ),
                    class_name="flex items-center gap-3 mb-2",
                ),
                rx.el.p(
                    "This app runs its own open-source LiveKit server inside its "
                    "container. No LiveKit Cloud account or keys are needed.",
                    class_name="text-gray-500 text-sm mb-4",
                ),
                rx.el.dl(
                    info_row("Status", ServerStatusState.phase),
                    info_row("Version", ServerStatusState.version),
                    info_row("Media IP", ServerStatusState.node_ip + " (" + ServerStatusState.node_ip_source + ")"),
                    info_row("Media ports", "TCP " + ServerStatusState.tcp_port + " · UDP " + ServerStatusState.udp_port),
                    class_name="mb-6",
                ),
                rx.cond(
                    SettingsState.is_admin_authenticated,
                    server_admin_panel(),
                    rx.el.form(
                        rx.el.div(
                            rx.cond(
                                SettingsState.auth_error != "",
                                error_box(SettingsState.auth_error),
                                rx.fragment(),
                            ),
                            input_field(
                                "Admin Passcode",
                                "admin_passcode",
                                "Enter admin passcode",
                                type="password",
                            ),
                            rx.el.button(
                                rx.cond(
                                    SettingsState.is_authenticating,
                                    rx.el.span(
                                        "Verifying...", class_name="animate-pulse"
                                    ),
                                    rx.el.div(
                                        rx.icon("lock", class_name="h-4 w-4"),
                                        "Unlock Settings",
                                        class_name="flex items-center gap-2",
                                    ),
                                ),
                                type="submit",
                                disabled=SettingsState.is_authenticating,
                                class_name=_PRIMARY_BUTTON,
                            ),
                            class_name="space-y-5",
                        ),
                        on_submit=SettingsState.verify_admin,
                        reset_on_submit=True,
                    ),
                ),
                class_name=_CARD,
            ),
            class_name="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4 py-10",
        ),
        class_name="font-['Inter']",
    )


_STYLE_LABELS = {"human": "Human", "cat": "Cat", "panda": "Panda", "robot": "Robot"}


def style_choice(style: str, label: str) -> rx.Component:
    selected = LiveKitBridgeState.avatar_style == style
    return rx.el.button(
        rx.el.div(LIVEKIT_UI.style_preview(style), class_name="w-full aspect-square rounded-lg overflow-hidden"),
        rx.el.span(label, class_name="text-xs font-medium text-gray-700"),
        type="button",
        on_click=LiveKitBridgeState.set_avatar_style(style),
        title=label,
        custom_attrs={"aria-pressed": rx.cond(selected, "true", "false")},
        class_name=rx.cond(
            selected,
            "flex flex-col items-center gap-1 p-1.5 rounded-xl ring-2 ring-violet-500 bg-violet-50",
            "flex flex-col items-center gap-1 p-1.5 rounded-xl ring-1 ring-gray-200 hover:ring-violet-300 transition-shadow",
        ),
    )


def style_picker() -> rx.Component:
    return rx.el.div(
        rx.el.label("Avatar", class_name="block text-sm font-semibold text-gray-700 mb-1"),
        rx.el.div(
            *[style_choice(style, label) for style, label in _STYLE_LABELS.items()],
            class_name="grid grid-cols-4 gap-2",
        ),
        class_name="w-full",
    )


def lobby_view() -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                rx.icon("captions", class_name="h-12 w-12 text-violet-600"),
                rx.el.h1(
                    "English Rooms",
                    class_name="text-4xl font-extrabold text-gray-900 tracking-tight",
                ),
                rx.el.p(
                    "Speak English as a cartoon avatar. What everyone says appears as live "
                    "subtitles; select any words in them and right-click to translate them "
                    "into your own language.",
                    class_name="text-sm text-gray-500 text-center max-w-sm",
                ),
                rx.el.div(
                    server_status_pill(),
                    speech_status_pill(),
                    class_name="flex flex-wrap justify-center gap-2",
                ),
                class_name="flex flex-col items-center gap-4 mb-10",
            ),
            rx.el.div(
                rx.el.form(
                    rx.el.div(
                        rx.cond(
                            LiveKitBridgeState.error_message != "",
                            error_box(LiveKitBridgeState.error_message),
                            rx.cond(
                                ServerStatusState.phase == "error",
                                error_box(ServerStatusState.message),
                                rx.fragment(),
                            ),
                        ),
                        input_field(
                            "Display Name",
                            "username",
                            "Enter your name",
                            value=LiveKitBridgeState.username,
                        ),
                        input_field(
                            "Room Name",
                            "room_name",
                            "Enter room to join",
                            value=LiveKitBridgeState.room_name,
                        ),
                        style_picker(),
                        rx.el.div(
                            rx.el.span(
                                "Translate subtitles to",
                                class_name="block text-sm font-semibold text-gray-700 mb-1",
                            ),
                            language_select(),
                            class_name="w-full",
                        ),
                        rx.el.button(
                            rx.cond(
                                LiveKitBridgeState.loading,
                                rx.el.div(
                                    rx.spinner(size="1"),
                                    rx.el.span("Joining..."),
                                    class_name="flex items-center gap-2 justify-center",
                                ),
                                "Join Room",
                            ),
                            type="submit",
                            disabled=LiveKitBridgeState.loading,
                            class_name="w-full bg-violet-600 hover:bg-violet-700 text-white font-bold py-3 rounded-xl transition-all shadow-lg shadow-violet-200 mt-2 disabled:opacity-70",
                        ),
                        class_name="space-y-6",
                    ),
                    on_submit=LiveKitBridgeState.join_room,
                    reset_on_submit=False,
                ),
                class_name="w-full max-w-md bg-white p-8 rounded-3xl border border-gray-100 shadow-xl shadow-gray-200/50",
            ),
            rx.el.div(
                rx.el.a(
                    "Server Settings",
                    href="/settings",
                    class_name="text-sm text-gray-500 hover:text-violet-600 transition-colors",
                ),
                class_name="mt-8 text-center",
            ),
            class_name="w-full flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4",
        ),
        class_name="font-['Inter']",
    )


def connection_pill(text_class: str) -> rx.Component:
    return rx.el.div(
        rx.el.div(
            class_name=rx.cond(
                LiveKitBridgeState.connection_status == "Connected",
                "size-2 rounded-full bg-green-500",
                "size-2 rounded-full bg-yellow-500 animate-pulse",
            )
        ),
        rx.el.span(
            LiveKitBridgeState.connection_status,
            id="connection-status",
            class_name=f"{text_class} font-medium text-gray-600",
        ),
        class_name="flex items-center gap-2 bg-white px-3 py-1 rounded-full border border-gray-100 shrink-0",
    )


def enable_audio_button() -> rx.Component:
    return rx.cond(
        LiveKitBridgeState.audio_blocked,
        rx.el.button(
            rx.icon("volume-2", class_name="h-5 w-5"),
            "Click to enable audio",
            on_click=LiveKitBridgeState.enable_audio,
            class_name="w-full mb-4 flex items-center justify-center gap-2 bg-amber-50 text-amber-800 border border-amber-200 rounded-xl py-2.5 font-semibold hover:bg-amber-100 transition-colors",
        ),
        rx.fragment(),
    )


# Space between tiles (gap-3), and the window height the room page needs
# around the grid (header, subtitles, controls, paddings).
_TILE_GAP = "0.75rem"
_ROOM_CHROME = "520px"


def tile_width(fit_window: bool) -> rx.Var | str:
    """CSS width of a tile: one column of the grid (minus its share of the gaps).

    With ``fit_window`` the tiles also shrink until all rows fit the window
    height, so nobody is scrolled out of view. Not inside the intent dialog:
    there 100vh is the iframe's own height, which follows the content.
    """
    cols, rows = LiveKitBridgeState.grid_cols, LiveKitBridgeState.grid_rows
    by_width = f"calc((100% - {_TILE_GAP} * ({cols} - 1)) / {cols})"
    if not fit_window:
        return by_width
    by_height = f"calc((100vh - {_ROOM_CHROME} - {_TILE_GAP} * ({rows} - 1)) / {rows} * 16 / 9)"
    return f"min({by_width}, {by_height})"


def participant_tile(participant: dict, width: rx.Var | str) -> rx.Component:
    """One person in the room: their avatar, drawn from the face values they send."""
    return rx.el.div(
        LIVEKIT_UI.avatar(participant["identity"], seed=participant["name"], mirrored=participant["is_local"]),
        rx.cond(
            participant["is_local"],
            # Our own tile also shows the camera picture (it never leaves this
            # device) and what the face tracker is doing.
            rx.fragment(
                rx.cond(
                    participant["camera_on"],
                    rx.el.div(
                        LIVEKIT_UI.camera_preview(),
                        title="Only on this device, never sent",
                        class_name="absolute right-2 bottom-2.5 w-[24%] aspect-[4/3] rounded-md overflow-hidden ring-2 ring-white shadow-md bg-gray-900",
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    LiveKitBridgeState.face_status != "",
                    rx.el.div(
                        LiveKitBridgeState.face_status,
                        id="face-status",
                        class_name="absolute left-2 top-2 max-w-[70%] truncate bg-black/55 text-white text-xs font-medium px-2 py-1 rounded-md",
                    ),
                    rx.fragment(),
                ),
            ),
            rx.fragment(),
        ),
        rx.el.div(
            rx.cond(
                participant["is_muted"],
                rx.icon("mic-off", class_name="h-3.5 w-3.5 text-red-400 shrink-0"),
                rx.fragment(),
            ),
            rx.cond(
                participant["camera_on"],
                rx.fragment(),
                rx.icon("video-off", class_name="h-3.5 w-3.5 text-amber-300 shrink-0"),
            ),
            rx.el.span(participant["name"], class_name="truncate"),
            rx.cond(
                participant["is_local"],
                rx.el.span("(You)", class_name="text-white/70 shrink-0"),
                rx.fragment(),
            ),
            class_name="absolute left-2 bottom-2.5 max-w-[70%] flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-2 py-1 rounded-md",
        ),
        # What they are saying, as a subtitle above their name.
        LIVEKIT_UI.caption(participant["identity"]),
        # Microphone level along the bottom edge.
        rx.el.div(
            LIVEKIT_UI.volume_bar(participant["identity"]),
            class_name="absolute inset-x-0 bottom-0 h-1",
        ),
        rx.cond(
            participant["is_speaking"],
            rx.el.div(
                class_name="absolute inset-0 rounded-xl border-[3px] border-green-500 pointer-events-none"
            ),
            rx.fragment(),
        ),
        # Keyed by identity: React never hands one person's canvas to another.
        key=participant["identity"],
        custom_attrs={
            "data-camera": rx.cond(participant["camera_on"], "on", "off"),
            "data-local": rx.cond(participant["is_local"], "1", "0"),
        },
        class_name="participant-tile relative aspect-video rounded-xl overflow-hidden bg-gray-100 shrink-0",
        style={"width": width},
    )


def avatar_grid(single_width: str, *, fit_window: bool) -> rx.Component:
    """Everyone's tile, rows centered (3 people: two above, one centered below).

    Someone alone in the room gets one tile of ``single_width``.
    """
    width = tile_width(fit_window)
    return rx.el.div(
        rx.foreach(LiveKitBridgeState.participants, lambda p: participant_tile(p, width)),
        id="avatar-grid",
        class_name=rx.cond(
            LiveKitBridgeState.grid_cols == 1,
            f"flex justify-center w-full {single_width} mx-auto",
            "flex flex-wrap justify-center gap-3 w-full",
        ),
    )


def media_toggles(large: bool) -> rx.Component:
    """Microphone and camera on/off, and the next avatar.

    Each toggle holds two static icons: a Var icon name makes Reflex use
    lucide's DynamicIcon, which loads every icon module.
    """
    pad, icon = ("p-4", "h-6 w-6") if large else ("p-3", "h-5 w-5")
    on = f"{pad} rounded-full bg-violet-100 text-violet-600 hover:bg-violet-200 transition-colors"
    off = f"{pad} rounded-full bg-red-100 text-red-600 hover:bg-red-200 transition-colors"
    return rx.fragment(
        rx.el.button(
            rx.cond(
                LiveKitBridgeState.is_muted,
                rx.icon("mic-off", class_name=icon),
                rx.icon("mic", class_name=icon),
            ),
            on_click=LiveKitBridgeState.toggle_mute,
            title=rx.cond(LiveKitBridgeState.is_muted, "Unmute", "Mute"),
            class_name=rx.cond(LiveKitBridgeState.is_muted, off, on),
        ),
        rx.el.button(
            rx.cond(
                LiveKitBridgeState.is_camera_off,
                rx.icon("video-off", class_name=icon),
                rx.icon("video", class_name=icon),
            ),
            on_click=LiveKitBridgeState.toggle_camera,
            title=rx.cond(LiveKitBridgeState.is_camera_off, "Turn on camera", "Turn off camera"),
            class_name=rx.cond(LiveKitBridgeState.is_camera_off, off, on),
        ),
        rx.el.button(
            rx.icon("smile", class_name=icon),
            on_click=LiveKitBridgeState.next_avatar_style,
            title="Change avatar",
            class_name=on,
        ),
    )


def privacy_note() -> rx.Component:
    return rx.el.p(
        rx.icon("shield-check", class_name="h-4 w-4 text-green-600 shrink-0"),
        "Subtitles and translations are made by this app's own server. Only your voice, your "
        "expression values and your subtitles are sent; your camera picture stays on this device.",
        class_name="flex items-center justify-center gap-1.5 text-xs text-gray-500 mt-3 text-center",
    )


def room_view() -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                rx.el.div(
                    rx.el.div(
                        rx.icon("hash", class_name="h-5 w-5 text-violet-500 shrink-0"),
                        rx.el.h2(
                            LiveKitBridgeState.room_name,
                            class_name="text-xl font-bold text-gray-900 truncate",
                        ),
                        rx.el.button(
                            rx.icon("link", class_name="h-4 w-4"),
                            on_click=LiveKitBridgeState.copy_invite_link,
                            title="Copy invite link",
                            class_name="p-1.5 rounded-lg text-gray-400 hover:text-violet-600 hover:bg-violet-50 transition-colors",
                        ),
                        class_name="flex items-center gap-2 min-w-0",
                    ),
                    connection_pill("text-sm"),
                    class_name="flex items-center justify-between gap-3 mb-6",
                ),
                enable_audio_button(),
                avatar_grid("max-w-3xl", fit_window=True),
                transcript_panel("h-36"),
                rx.el.div(
                    media_toggles(large=True),
                    rx.el.button(
                        rx.icon("phone-off", class_name="h-6 w-6"),
                        on_click=LiveKitBridgeState.leave_room,
                        title="Leave room",
                        class_name="p-4 rounded-full bg-gray-900 text-white hover:bg-gray-800 transition-colors",
                    ),
                    class_name="flex items-center justify-center gap-6 mt-6 pt-6 border-t border-gray-100",
                ),
                privacy_note(),
                class_name="w-full max-w-5xl bg-white p-6 sm:p-8 rounded-3xl border border-gray-100 shadow-xl",
            ),
            class_name="flex flex-col items-center justify-center min-h-screen bg-gray-50 px-4 py-6",
        ),
        class_name="font-['Inter']",
    )


def call_panel() -> rx.Component:
    """The call itself, sized for the caller's dialog (no full-screen layout)."""
    return rx.el.div(
        rx.el.div(
            rx.el.div(
                rx.icon("captions", class_name="h-5 w-5 text-violet-500 shrink-0"),
                rx.el.h2(
                    LiveKitBridgeState.room_title,
                    class_name="text-lg font-bold text-gray-900 truncate",
                ),
                class_name="flex items-center gap-2 min-w-0",
            ),
            connection_pill("text-xs"),
            # Room for the dialog's minimize and close buttons (top right).
            class_name="flex items-center justify-between gap-3 mb-4 pr-20",
        ),
        enable_audio_button(),
        avatar_grid("max-w-xl", fit_window=False),
        transcript_panel("h-28"),
        rx.el.div(
            media_toggles(large=False),
            rx.el.button(
                rx.icon("phone-off", class_name="h-5 w-5"),
                on_click=LiveKitBridgeState.hang_up,
                title="Hang up",
                class_name="p-3 rounded-full bg-red-600 text-white hover:bg-red-700 transition-colors",
            ),
            class_name="flex items-center justify-center gap-6 pt-5 mt-5 border-t border-gray-100",
        ),
        privacy_note(),
    )


def call_intent_view() -> rx.Component:
    """Page of the ``call.join`` intent: joins on open, hang up answers the caller."""
    return rx.el.div(
        LIVEKIT_UI.bridge_input(),
        rx.cond(
            LiveKitBridgeState.error_message != "",
            rx.el.div(
                error_box(LiveKitBridgeState.error_message),
                rx.el.button(
                    "Close",
                    on_click=IntentPage.cancel,
                    class_name="w-full mt-2 py-2.5 rounded-lg bg-gray-100 text-gray-700 font-semibold hover:bg-gray-200",
                ),
                class_name="pr-10",
            ),
            rx.cond(
                LiveKitBridgeState.is_connected,
                call_panel(),
                rx.el.div(
                    rx.spinner(size="2"),
                    rx.el.span("Joining the call...", class_name="text-sm text-gray-600"),
                    class_name="flex items-center justify-center gap-3 py-10",
                ),
            ),
        ),
        class_name="p-6 bg-white font-['Inter']",
    )


def index() -> rx.Component:
    return rx.el.div(
        LIVEKIT_UI.bridge_input(),
        rx.cond(LiveKitBridgeState.is_connected, room_view(), lobby_view()),
    )


app = rx.App(
    theme=rx.theme(appearance="light"),
    head_components=[
        rx.el.link(rel="preconnect", href="https://fonts.googleapis.com"),
        rx.el.link(rel="preconnect", href="https://fonts.gstatic.com", cross_origin=""),
        rx.el.link(
            href="https://fonts.googleapis.com/css2?family=Inter:wght@400..700&display=swap",
            rel="stylesheet",
        ),
        *LIVEKIT_UI.head_components(),
    ],
)
app.add_page(
    index,
    route="/",
    title="English Rooms",
    on_load=[LiveKitBridgeState.on_lobby_load, ServerStatusState.watch],
)
app.add_page(
    settings_page,
    route="/settings",
    title="LiveKit Server",
    on_load=[SettingsState.on_settings_load, ServerStatusState.watch],
)



# Other *.reflex-ddns.com apps open calls in a dialog (DDNS Intent), e.g. relack:
#   Intent.start(None, "call.join", private={"room": <call id>}, title=..., user=..., name=...)
@intent(
    app,
    action="call.join",
    on_open=LiveKitBridgeState.open_call_intent,
    title="English Call",
    params={"room": str, "title": str, "user": str, "name": str},
    required=("room",),
)
def call_intent() -> rx.Component:
    return call_intent_view()


# The embedded LiveKit server + the /rtc and /twirp relays to it.
app.register_lifespan_task(livekit_server.lifespan)
register_livekit_routes(app)
# Subtitles (Whisper, /stt) and translations (Ollama, /translate).
app.register_lifespan_task(stt.lifespan)
stt.register_routes(app)
translate.register_routes(app)
