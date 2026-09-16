<p align="center">
  <img src="app/static/img/apple-touch-icon.png" width="96" alt="Spotea icon">
</p>

<h1 align="center">Spotea</h1>

<p align="center">
  <b>Your own music streaming app.</b><br>
  Built on YouTube Music. Self-hosted, free, and it keeps playing offline.
</p>

<p align="center">
  <a href="https://github.com/sertacartun/spotea/actions/workflows/tests.yml"><img src="https://github.com/sertacartun/spotea/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white" alt="Docker ready">
</p>

<p align="center">
  <img src="docs/images/hero.png" alt="Spotea's player with synced lyrics">
</p>

## Why Spotea?

<img src="docs/images/demo.gif" align="right" width="280" alt="Browsing Spotea on a phone and opening the player with synced lyrics">

- 🎤 **Follow artists.** New releases from the artists you follow show up on Home.
- 🔎 **Explore the whole catalogue.** Search YouTube Music for songs, artists, albums, charts and moods.
- ✨ **Recommendations based on who you follow.** Explore suggests songs and artists from your library.
- 📝 **Synced lyrics.** Lyrics scroll along with the song.
- 🎶 **Your own playlists.** Plus Favorites and Recently Played.
- 📥 **Downloads you own.** Audio is saved to your disk and can be exported as one zip.
- ✈️ **Offline playback.** Keep songs on your phone and play them with no connection.
- 📱 **Installs like an app.** Add it to your home screen on iOS or Android.

<br clear="right">

<p align="center">
  <img src="docs/images/phones.png" alt="Spotea on a phone: Home, Explore, Player and Library">
</p>

## Get started

You only need [Docker](https://docs.docker.com/get-docker/).

```bash
git clone https://github.com/sertacartun/spotea.git && cd spotea
cp .env.example .env        # then set SECRET_KEY in .env
docker compose up -d
```

Open **http://localhost:8000** and create your account. That's it.

To update later: `git pull && docker compose up -d --build`. Your music in `./data` stays.

## More

- [Self-hosting guide](docs/SELF_HOSTING.md): configuration, HTTPS, running without Docker
- [Architecture](ARCHITECTURE.md): how it works inside
- [Contributing](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
