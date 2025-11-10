# Navigation Dashboard

An ambient navigation hub featuring reactive shaders, rotating quotes, weather, news, and quick links. The app now includes an experimental voice transcription input powered by OpenAI's Speech-to-Text API.

## Features

- Music-reactive shader visual embedded in the main dashboard.
- Fixed weather and news feeds with configurable categories.
- Circular navigation menu with primary app shortcuts.
- Voice capture via the brain icon that records audio and sends it to OpenAI for transcription.

> **Browser support:** Audio capture requires `MediaRecorder`, microphone access, **and a secure context**. Use Chrome/Edge on desktop and load the app via `https://...` or `http://localhost:6666` (accessing via raw LAN IP like `http://192.x.x.x` is blocked by browsers).

## Getting Started

1. **Install dependencies** (Node 18+ recommended):

   ```bash
   cd app
   npm install
   ```

2. **Environment variables**:

   - Copy the template and fill in your OpenAI key:

     ```bash
     cp app/env.template app/.env
     ```

   - Edit `app/.env`:

     ```
     OPENAI_API_KEY=sk-your-key
     # Optional overrides
     # OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
     # OPENAI_TRANSCRIBE_LANGUAGE=da
     # OPENAI_RESPONSE_MODEL=gpt-4o-mini
     # OPENAI_RESPONSE_SYSTEM_PROMPT=You are a helpful assistant. Reply concisely.
     # OPENAI_TTS_MODEL=gpt-4o-mini-tts
     # OPENAI_TTS_VOICE=alloy
     # OPENAI_TTS_FORMAT=mp3
     ```

3. **Run the dev server**:

   ```bash
   cd app
   npm start
   ```

   The site serves on [http://0.0.0.0:6666](http://0.0.0.0:6666).

4. **(Optional) Enable HTTPS with a self-signed certificate (bare metal)**:

   Browsers require HTTPS (or `localhost`) for microphone access. To serve the dashboard securely from another machine:

   ```bash
   cd app
   mkdir -p certs
   openssl req -x509 -newkey rsa:2048 -nodes \
     -keyout certs/localhost-key.pem \
     -out certs/localhost-cert.pem \
     -days 365 \
     -subj "/CN=localhost"
   ```

   Then update `app/.env`:

   ```
   SSL_KEY_PATH=./certs/localhost-key.pem
   SSL_CERT_PATH=./certs/localhost-cert.pem
   ```

   Restart `npm start`. Access via `https://<server>:6666` (browsers will warn about the self-signed cert; trust it to continue).

5. **Using transcription + AI reply**:

   - Open the page in a browser that supports `MediaRecorder` (Chrome/Edge recommended).
   - Visit via `https://` or `http://localhost:6666`, then allow microphone access when prompted.
   - Press and hold the brain icon to record; release to upload and transcribe.
   - Transcribed text appears in the input field beneath the brain menu. Once ready, the AI response is requested automatically with the configured system prompt (press Enter in the field to resend or adjust the text). The spoken reply plays back through the dashboard audio and drives the visualizer.

## Notes & Troubleshooting

- The transcription endpoint proxies requests to OpenAI via `/api/transcribe`. Make sure your server runs in a secure environment; never expose `OPENAI_API_KEY` to the client.
- If the placeholder shows "Tillad mikrofonadgang for at optage", enable mic access in the browser.
- After each AI reply, the server automatically synthesizes speech via `/api/speech`; customize the voice/model via env vars above. The audio also feeds the music visualizer.
- If you receive "Tillad mikrofonadgang for at optage" when clicking the brain button, check that the page is served via HTTPS or localhost and that the browser permitted microphone access.
- Logging for GitHub contribution fallback requests is available via `server.mjs`.
- Static assets sit directly in `app/`. Adjust or extend the shader, news parser, or weather integrations as needed.

## Running in Docker

This repo ships with a `Dockerfile` and `docker-compose.yml` for production-style deployment.

```bash
docker compose up --build -d
```

- The compose file binds `./app` into the container (so live edits are reflected).
- Ports `8081:8081` are mapped; when self-signed cert generation is enabled (default), visit `https://localhost:8081` and accept the browser warning.
- Environment variables:
  - `ENABLE_SELF_SIGNED_CERT=true` creates a cert at `app/certs/` on container start (override `SELF_SIGNED_CERT_SUBJECT` or `SELF_SIGNED_CERT_DAYS` as needed).
  - Provide `OPENAI_API_KEY` via `.env` or compose overrides to enable transcription.

Stop the stack with:

```bash
docker compose down
```

## License

This project is provided as-is for demo purposes. Adapt to your own use cases as needed.

