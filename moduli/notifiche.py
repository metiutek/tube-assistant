"""
Simple Telegram notifications via direct HTTP — no async needed.
Used by agent.py to push status updates.
"""
import html
import os
import requests


def _esc(value) -> str:
    """Escape testo dinamico per parse_mode=HTML (titoli/errori con < & >)."""
    return html.escape(str(value), quote=True)

def _send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def _send_photo(image_path: str, caption: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        with open(image_path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=20,
            )
    except Exception:
        pass


def notify_start(topic: str) -> None:
    _send(f"🎬 <b>Nuovo video in produzione</b>\n\n📌 Topic: <i>{_esc(topic)}</i>\n\nInizio pipeline...")


def notify_step(step: str, detail: str = "") -> None:
    icons = {
        "audio": "🎙️",
        "clips": "🎞️",
        "rendering": "⚙️",
        "thumbnail": "🖼️",
        "upload": "📤",
        "strategy": "🧠",
    }
    icon = icons.get(step, "▶️")
    msg = f"{icon} <b>{step.capitalize()}</b>"
    if detail:
        msg += f"\n{detail}"
    _send(msg)


def notify_done(title: str, video_id: str, publish_time: str, thumb_path: str) -> None:
    caption = (
        f"✅ <b>Video schedulato!</b>\n\n"
        f"📹 <b>{_esc(title)}</b>\n"
        f"🔗 https://youtu.be/{video_id}\n"
        f"⏰ Pubblicazione: {_esc(publish_time)}"
    )
    if thumb_path and os.path.exists(thumb_path):
        _send_photo(thumb_path, caption)
    else:
        _send(caption)


def notify_error(error: str) -> None:
    _send(f"❌ <b>Errore pipeline</b>\n\n<code>{_esc(error[:500])}</code>")


def notify_config(problemi: list) -> None:
    """Problemi di configurazione trovati all'avvio.

    Separato da `notify_error`: non e' la pipeline ad essere fallita, e
    intestare "Errore pipeline" un'API da abilitare fa cercare il guasto nel
    posto sbagliato.
    """
    if not problemi:
        return
    righe = "\n".join(f"• {_esc(p)}" for p in problemi[:6])
    _send("⚠️ <b>Configurazione da sistemare</b>\n\n" + righe)


def notify_analytics(stats: list) -> None:
    if not stats:
        _send("📊 <b>Recap analytics</b>\n\nNessun video ancora.")
        return
    lines = ["📊 <b>Recap ultimi video</b>\n"]
    for v in stats:
        retention = int(v.get("avg_view_duration_seconds", 0) / max(v.get("duration_seconds", 480), 1) * 100)
        lines.append(
            f"▪️ <i>{_esc(v['title'][:40])}</i>\n"
            f"   👁 {v['views']} views | ❤️ {v['likes']} likes | 🔁 {retention}% retention\n"
        )
    _send("\n".join(lines))
