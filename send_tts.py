import httpx
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

def send_tts(
    user_input: str,
    tts_address: str,
    tts_secret: str) -> tuple[bool, str]:

    payload = {
            "text": user_input,
        }

    headers = {
        "X-TTS-Secret": tts_secret,
    }

    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.post(
                tts_address,
                json=payload,
                headers=headers,
            )

        response.raise_for_status()

        return True, "TTS message sent successfully."
    except httpx.HTTPStatusError as e:
        return False, f"TTS service rejected request: {e.response.status_code} {e.response.text}"

    except httpx.RequestError as e:
        return False, f"Could not reach TTS service: {e}"

if __name__ == "__main__":
    user_input = "Hello, this is a test message."
    tts_address = "http://example.com/tts"
    tts_secret = "your_tts_secret"

    success, message = send_tts(user_input, tts_address, tts_secret)
    if success:
        panel = Panel(
            Text(message, style="bold green"),
            title="TTS Success",
            border_style="green"
        )
        console.print(panel)
    else:
        panel = Panel(
            Text(message, style="bold red"),
            title="TTS Error",
            border_style="red"
        )
        console.print(panel)