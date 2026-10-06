import asyncio

import reflex as rx

from reflex_ddns_livekit_english_chat import livekit_server, stt


class ServerStatusState(rx.State):
    """Public (non-secret) status of the built-in LiveKit server and speech model."""

    phase: str = "starting"
    message: str = ""
    version: str = ""
    node_ip: str = ""
    node_ip_source: str = ""
    tcp_port: str = ""
    udp_port: str = ""
    # The subtitles' speech model: "downloading" | "loading" | "ready" | "error".
    stt_phase: str = "loading"
    stt_message: str = ""
    stt_model: str = ""

    @rx.var
    def is_ready(self) -> bool:
        return self.phase == "running"

    def _apply(self, info: dict[str, str], speech: dict[str, str]) -> None:
        self.phase = info["phase"]
        self.message = info["message"]
        self.version = info["version"]
        self.node_ip = info["node_ip"]
        self.node_ip_source = info["node_ip_source"]
        self.tcp_port = info["tcp_port"]
        self.udp_port = info["udp_port"]
        self.stt_phase = speech["phase"]
        self.stt_message = speech["message"]
        self.stt_model = speech["model"]

    @rx.event(background=True)
    async def watch(self):
        """Poll until the server and the speech model are up (the first start downloads both)."""
        for _ in range(240):
            info = await asyncio.to_thread(livekit_server.status)
            speech = stt.status()
            async with self:
                self._apply(info, speech)
            if info["phase"] in ("running", "error") and speech["phase"] in ("ready", "error"):
                return
            await asyncio.sleep(2)
