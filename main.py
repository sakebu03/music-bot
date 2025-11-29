import os
import re
import asyncio
import discord
from discord.ext import commands
from yt_dlp import YoutubeDL

# ------------------ KONFIG ------------------

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Brak zmiennej środowiskowej DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# YDL z obejściem części blokad (udaje klienta Android)
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android"]
        }
    }
}

FFMPEG_OPTIONS = {
    "options": "-vn"
}

# Kolejki per serwer: guild_id -> {"queue": [song, ...], "history": [song, ...]}
guild_players = {}

# Prosty regex do sprawdzenia, czy query to URL YouTube
YOUTUBE_URL_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/")


def get_guild_player(guild_id: int):
    if guild_id not in guild_players:
        guild_players[guild_id] = {"queue": [], "history": []}
    return guild_players[guild_id]


async def play_song(ctx, song):
    """Odtwarza pojedynczy utwór."""
    voice_client = ctx.voice_client
    if not voice_client:
        return

    source = await discord.FFmpegOpusAudio.from_probe(song["url"], **FFMPEG_OPTIONS)
    voice_client.play(
        source,
        after=lambda e: asyncio.run_coroutine_threadsafe(
            play_next_in_queue(ctx), bot.loop
        ),
    )

    await ctx.send(f"▶️ Odtwarzam: **{song['title']}**")


async def play_next_in_queue(ctx):
    """Automatycznie odpala następny utwór z kolejki po zakończeniu poprzedniego."""
    await bot.wait_until_ready()

    if not ctx.guild or not ctx.guild.id:
        return

    player = get_guild_player(ctx.guild.id)
    voice_client = ctx.voice_client

    if not voice_client or not voice_client.is_connected():
        return

    if player["queue"]:
        next_song = player["queue"].pop(0)
        player["history"].append(next_song)
        try:
            await play_song(ctx, next_song)
        except Exception as e:
            await ctx.send(f"❌ Błąd przy odtwarzaniu: `{e}`")
            await play_next_in_queue(ctx)
    else:
        # brak kolejnych utworów
        pass


async def fetch_youtube_info(query: str):
    """
    Pobiera info o utworze z YouTube.
    Jeśli film jest zablokowany (Sign in to confirm you’re not a bot),
    a użytkownik podał sam tytuł, spróbujemy automatycznie innych wersji (lyrics / audio).
    """

    is_url = bool(YOUTUBE_URL_REGEX.search(query))

    def _extract(q: str):
        with YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(q, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = _extract(query)
    except Exception as e:
        msg = str(e)

        # Specjalny przypadek: YouTube chce logowania / captchy
        if "Sign in to confirm you’re not a bot" in msg:
            # jeśli to był link – nie kombinujemy, prosimy o inną wersję
            if is_url:
                raise RuntimeError(
                    "Ten konkretny film na YouTube wymaga zalogowania / potwierdzenia, "
                    "że nie jesteś botem. Spróbuj innej wersji tej piosenki "
                    "(np. lyrics / audio) albo wklej inny link."
                )

            # jeśli to nie był link – spróbujmy znaleźć inną wersję
            fallback_queries = [
                f"{query} lyrics",
                f"{query} audio",
                f"{query} official audio",
            ]

            last_error = e
            for fq in fallback_queries:
                try:
                    info = _extract(fq)
                    # jak się uda – przerywamy pętlę i lecimy dalej
                    break
                except Exception as fe:
                    last_error = fe
                    info = None

            if not info:
                # nic z fallbacków nie zadziałało
                raise RuntimeError(
                    "YouTube mocno blokuje ten utwór dla botów (wymaga logowania). "
                    "Spróbuj podać inną wersję, np. inny tytuł / inny link."
                ) from last_error
        else:
            # jakiś inny błąd yt-dlp
            raise RuntimeError("Nie udało się pobrać informacji z YouTube.") from e

    return {
        "url": info["url"],
        "title": info.get("title", "Nieznany tytuł"),
        "webpage_url": info.get("webpage_url", query),
    }


# ------------------ EVENTY ------------------


@bot.event
async def on_ready():
    print(f"Zalogowano jako {bot.user} (ID: {bot.user.id})")


# ------------------ KOMENDY PODSTAWOWE ------------------


@bot.command(name="join")
async def join(ctx):
    """Bot dołącza do Twojego kanału głosowego."""
    if ctx.author.voice is None:
        await ctx.send("Musisz być na kanale głosowym!")
        return

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)

    await ctx.send(f"Dołączono do: **{channel}**")


@bot.command(name="leave")
async def leave(ctx):
    """Bot wychodzi z kanału głosowego."""
    if ctx.voice_client is not None:
        await ctx.voice_client.disconnect()
        # czyścimy kolejkę dla tego serwera
        player = get_guild_player(ctx.guild.id)
        player["queue"].clear()
        player["history"].clear()
        await ctx.send("Wyszedłem z kanału i wyczyściłem kolejkę 👋")
    else:
        await ctx.send("Nie jestem na żadnym kanale.")


# ------------------ MUZYKA: PLAY / QUEUE / NEXT / BACK / STOP ------------------


@bot.command(name="play")
async def play(ctx, *, query: str):
    """
    Dodaje utwór do kolejki i jeśli nic nie gra – od razu odtwarza.
    Przykłady:
    !play https://youtu.be/...
    !play never gonna give you up
    """
    if ctx.author.voice is None:
        await ctx.send("Najpierw wejdź na kanał głosowy.")
        return

    if ctx.voice_client is None:
        await ctx.author.voice.channel.connect()

    voice_client = ctx.voice_client
    player = get_guild_player(ctx.guild.id)

    await ctx.send(f"🔎 Szukam: `{query}`")

    try:
        song = await fetch_youtube_info(query)
    except RuntimeError as e:
        await ctx.send(f"❌ {e}")
        return
    except Exception:
        await ctx.send("❌ Wystąpił nieznany błąd podczas pobierania utworu.")
        return

    # Jeśli nic nie gra i kolejka jest pusta -> odpal od razu
    if not voice_client.is_playing() and not player["queue"] and not player["history"]:
        player["history"].append(song)
        await play_song(ctx, song)
    else:
        # dodajemy do kolejki
        player["queue"].append(song)
        await ctx.send(f"➕ Dodano do kolejki: **{song['title']}**")


@bot.command(name="queue")
async def queue_cmd(ctx):
    """Pokazuje aktualną kolejkę utworów."""
    player = get_guild_player(ctx.guild.id)
    if not player["queue"]:
        await ctx.send("📭 Kolejka jest pusta.")
        return

    msg_lines = ["🎶 **Kolejka:**"]
    for i, song in enumerate(player["queue"], start=1):
        msg_lines.append(f"`{i}.` {song['title']}")
    await ctx.send("\n".join(msg_lines))


@bot.command(name="next", aliases=["skip"])
async def next_cmd(ctx):
    """Przechodzi do następnego utworu w kolejce."""
    voice_client = ctx.voice_client
    if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
        await ctx.send("Nic teraz nie gram.")
        return

    player = get_guild_player(ctx.guild.id)
    if not player["queue"]:
        await ctx.send("Brak następnego utworu w kolejce.")
        return

    voice_client.stop()  # after callback odpali play_next_in_queue
    await ctx.send("⏭ Pomijam do następnego utworu...")


@bot.command(name="back")
async def back_cmd(ctx):
    """Wraca do poprzedniego utworu (jeśli jest w historii)."""
    voice_client = ctx.voice_client
    if not voice_client:
        await ctx.send("Nie jestem na kanale głosowym.")
        return

    player = get_guild_player(ctx.guild.id)

    if len(player["history"]) < 2:
        await ctx.send("Brak poprzedniego utworu w historii.")
        return

    # aktualny (ostatni) -> wrzucamy na początek kolejki
    current = player["history"].pop()
    player["queue"].insert(0, current)

    # poprzedni z historii gramy teraz
    previous = player["history"].pop()

    # zatrzymujemy obecne audio i gramy poprzedni
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    player["history"].append(previous)
    await play_song(ctx, previous)
    await ctx.send("⏮ Cofnięto do poprzedniego utworu.")


@bot.command(name="stop")
async def stop_cmd(ctx):
    """
    Zatrzymuje odtwarzanie i czyści kolejkę.
    """
    voice_client = ctx.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()

    player = get_guild_player(ctx.guild.id)
    player["queue"].clear()
    player["history"].clear()

    await ctx.send("⏹ Zatrzymano muzykę i wyczyszczono kolejkę.")


@bot.command(name="pause")
async def pause(ctx):
    """Pauzuje muzykę."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸ Zapauzowano.")
    else:
        await ctx.send("Nie ma czego pauzować.")


@bot.command(name="resume")
async def resume(ctx):
    """Wznawia muzykę."""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Wznowiono.")
    else:
        await ctx.send("Nic nie jest zapauzowane.")


# ------------------ START BOTA ------------------

if __name__ == "__main__":
    bot.run(TOKEN)
