import { createServer as createHttpServer } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { Buffer } from "node:buffer";
import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const publicRoot = __dirname;
const port = Number.parseInt(process.env.PORT ?? "6666", 10);

await loadEnv();

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml; charset=utf-8",
  ".ico": "image/x-icon",
  ".mp4": "video/mp4",
};

const spotifyStateStore = new Map();
const SPOTIFY_STATE_TTL = 15 * 60 * 1000;

function normalizeSpotifyPlaylistId(raw) {
  if (!raw) return null;
  const trimmed = String(raw).trim();
  if (!trimmed) return null;

  // Matches URLs like https://open.spotify.com/playlist/{id}
  const urlMatch = trimmed.match(/playlist\/([a-zA-Z0-9]+)(?:\?|$)/i);
  if (urlMatch?.[1]) {
    return urlMatch[1];
  }

  // Matches URIs like spotify:playlist:{id}
  const uriMatch = trimmed.match(/spotify:playlist:([a-zA-Z0-9]+)/i);
  if (uriMatch?.[1]) {
    return uriMatch[1];
  }

  // Fallback: if it looks like a bare ID (alphanumeric, at least 10 chars)
  if (/^[a-zA-Z0-9]{10,}$/.test(trimmed)) {
    return trimmed;
  }

  return null;
}

const requestListener = async (req, res) => {
  try {
    console.log(`[SERVER] ${req.method} ${req.url}`);
    const protocol = req.socket?.encrypted ? "https" : "http";
    const host = req.headers.host ?? "localhost";
    const baseUrl = `${protocol}://${host}`;
    const requestUrl = new URL(req.url ?? "/", baseUrl);
    const normalizedPathname = (() => {
      const path = requestUrl.pathname || "/";
      const stripped = path.replace(/\/+$/, "");
      return stripped === "" ? "/" : stripped;
    })();
    console.log(`[SERVER] Normalized path: ${normalizedPathname}, Raw pathname: ${requestUrl.pathname}`);

    if (requestUrl.pathname === "/api/contributions") {
      const username = requestUrl.searchParams.get("user") || "flyveren";

      try {
        const payload = await fetchGitHubContributions(username);
        res.writeHead(200, {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-cache",
        });
        res.end(JSON.stringify(payload));
      } catch (primaryError) {
        try {
          const fallbackPayload = await fetchFallbackContributions(username);
          res.writeHead(200, {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-cache",
          });
          res.end(JSON.stringify(fallbackPayload));
        } catch (fallbackError) {
          res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
          res.end(
            JSON.stringify({
              error: "Failed to load contributions",
              details: `${primaryError.message}; fallback: ${fallbackError.message}`,
            })
          );
        }
      }
      return;
    }

    if (requestUrl.pathname === "/api/transcribe" && req.method === "POST") {
      await handleTranscription(req, res);
      return;
    }

    if (requestUrl.pathname === "/api/respond" && req.method === "POST") {
      await handleAiResponse(req, res);
      return;
    }

    if (requestUrl.pathname === "/api/speech" && req.method === "POST") {
      await handleTextToSpeech(req, res);
      return;
    }

    if (normalizedPathname === "/connect" && req.method === "GET") {
      console.log("Handling /connect");
      await handleSpotifyConnect(requestUrl, res);
      return;
    }

    if (
      (normalizedPathname === "/spotify-auth-callback" ||
        normalizedPathname === "/spotify-auth-callback.html" ||
        normalizedPathname === "/api/spotify-auth-callback") &&
      req.method === "GET"
    ) {
      console.log("Handling /spotify-auth-callback");
      await handleSpotifyCallback(requestUrl, res);
      return;
    }

    if (normalizedPathname === "/api/news/proxy" && req.method === "GET") {
      await handleNewsProxy(requestUrl, res);
      return;
    }

    if (normalizedPathname === "/api/spotify/config" && req.method === "GET") {
      handleSpotifyConfig(res);
      return;
    }

    if (normalizedPathname === "/api/spotify/refresh" && req.method === "POST") {
      await handleSpotifyRefresh(req, res);
      return;
    }

    // Facebook routes - check both normalized and raw pathname
    if (
      (requestUrl.pathname === "/api/facebook/scrape" || normalizedPathname === "/api/facebook/scrape") &&
      req.method === "POST"
    ) {
      console.log("[SERVER] Handling /api/facebook/scrape");
      await handleFacebookScrape(req, res);
      return;
    }

    if (
      (requestUrl.pathname === "/api/facebook/posts" || normalizedPathname === "/api/facebook/posts") &&
      req.method === "GET"
    ) {
      console.log("[SERVER] Handling /api/facebook/posts");
      await handleFacebookPosts(req, res);
      return;
    }

    if (requestUrl.pathname === "/favicon.ico") {
      res.writeHead(204);
      res.end();
      return;
    }

    const sanitizedPath = normalize(
      normalizedPathname === "/"
        ? "index.html"
        : normalizedPathname.replace(/^\//, "")
    ).replace(/^\.\.(\/|\\)/g, "");
    const filePath = sanitizedPath.endsWith("/")
      ? `${sanitizedPath}index.html`
      : sanitizedPath;

    const absolutePath = join(publicRoot, filePath);
    console.log(`[SERVER] Static file lookup: ${absolutePath}`);
    const content = await readFile(absolutePath);
    const mimeType =
      mimeTypes[extname(absolutePath).toLowerCase()] ?? "application/octet-stream";

    res.writeHead(200, {
      "Content-Type": mimeType,
      "Cache-Control": "no-cache",
    });
    res.end(content);
  } catch (error) {
    console.error(`[SERVER] Request error for ${req.method} ${req.url}:`, error);
    if (error.code === "ENOENT") {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Not found");
    } else {
      res.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Internal Server Error");
    }
  }
};

async function fetchGitHubContributions(username) {
  const contributionsUrl = `https://github.com/users/${encodeURIComponent(
    username
  )}/contributions`;
  const response = await fetch(contributionsUrl, {
    headers: {
      "User-Agent": "navigation-dashboard/1.0",
      Accept: "text/html,application/xhtml+xml",
    },
  });

  if (!response.ok) {
    throw new Error(`GitHub responded ${response.status}`);
  }

  const html = await response.text();
  const regex =
    /data-date="(?<date>[^"]+)"[^>]*data-count="(?<count>\d+)"[^>]*data-level="(?<level>\d+)"/g;
  const contributions = [];
  let match;
  while ((match = regex.exec(html)) !== null) {
    const { date, count, level } = match.groups ?? {};
    if (!date) continue;
    contributions.push({
      date,
      count: Number.parseInt(count ?? "0", 10),
      level: Number.parseInt(level ?? "0", 10),
    });
  }

  if (contributions.length === 0) {
    throw new Error("Unable to parse GitHub contributions markup");
  }

  let mergedContributions = contributions;

  try {
    const fallback = await fetchFallbackContributions(username);
    if (Array.isArray(fallback?.contributions) && fallback.contributions.length > 0) {
      const merged = new Map();
      fallback.contributions.forEach((entry) => {
        if (!entry?.date) return;
        const count = Number.isFinite(entry.count) ? entry.count : Number(entry.count ?? 0) || 0;
        const level =
          Number.isFinite(entry.level) && entry.level !== undefined
            ? Number(entry.level)
            : Number(entry.intensity ?? entry.count ?? 0) || 0;
        merged.set(entry.date, {
          date: entry.date,
          count,
          level,
        });
      });

      mergedContributions.forEach((entry) => {
        if (!entry?.date) return;
        merged.set(entry.date, {
          date: entry.date,
          count: Number.isFinite(entry.count) ? entry.count : Number(entry.count ?? 0) || 0,
          level: Number.isFinite(entry.level) ? entry.level : Number(entry.level ?? 0) || 0,
        });
      });

      mergedContributions = Array.from(merged.values()).sort(
        (a, b) => new Date(a.date).getTime() - new Date(b.date).getTime()
      );
    }
  } catch (mergeError) {
    console.warn("Failed to merge fallback contributions:", mergeError);
  }

  return {
    source: contributionsUrl,
    fetchedAt: new Date().toISOString(),
    contributions: mergedContributions,
  };
}

async function fetchFallbackContributions(username) {
  const upstreamUrl = `https://github-contributions-api.jogruber.de/v4/${encodeURIComponent(
    username
  )}?y=all`;
  const upstreamResponse = await fetch(upstreamUrl, {
    headers: { "User-Agent": "navigation-dashboard-fallback/1.0" },
  });

  if (!upstreamResponse.ok) {
    throw new Error(`Fallback API responded ${upstreamResponse.status}`);
  }

  const fallbackPayload = await upstreamResponse.json();
  const contributions = normalizeFallbackContributions(fallbackPayload);
  return {
    source: upstreamUrl,
    fetchedAt: new Date().toISOString(),
    contributions,
  };
}

function normalizeFallbackContributions(payload) {
  if (!payload) {
    return [];
  }

  if (Array.isArray(payload.contributions)) {
    return payload.contributions
      .filter((entry) => Boolean(entry?.date))
      .map((entry) => ({
        date: entry.date,
        count: Number(entry.count ?? 0) || 0,
        level: Number(entry.level ?? entry.intensity ?? 0) || 0,
      }));
  }

  if (Array.isArray(payload.years)) {
    const flattened = [];
    payload.years.forEach((year) => {
      year?.contributions?.forEach((entry) => {
        if (!entry?.date) return;
        flattened.push({
          date: entry.date,
          count: Number(entry.count ?? entry.value ?? 0) || 0,
          level: Number(entry.level ?? entry.intensity ?? entry.count ?? 0) || 0,
        });
      });
    });
    return flattened;
  }

  return [];
}

async function handleTranscription(req, res) {
  console.log("/api/transcribe request received");
  try {
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Missing OPENAI_API_KEY in environment" }));
      return;
    }

    const chunks = [];
    for await (const chunk of req) {
      chunks.push(chunk);
    }
    const audioBuffer = Buffer.concat(chunks);
    if (audioBuffer.length === 0) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Request body was empty" }));
      return;
    }

    const contentType = req.headers["content-type"] || "audio/webm";
    const extension = guessExtension(contentType);
    const form = new FormData();
    const blob = new Blob([audioBuffer], { type: contentType });
    form.append("file", blob, `input.${extension}`);
    form.append("model", process.env.OPENAI_TRANSCRIBE_MODEL ?? "gpt-4o-mini-transcribe");

    if (process.env.OPENAI_TRANSCRIBE_LANGUAGE) {
      form.append("language", process.env.OPENAI_TRANSCRIBE_LANGUAGE);
    }

    const fetchUrl = `${process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1"}/audio/transcriptions`;
    console.log("Calling OpenAI transcription with model", form.get("model"));
    const response = await fetch(fetchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
      },
      body: form,
    });

    const responseText = await response.text();
    console.log("/api/transcribe response status", response.status);
    res.writeHead(response.status, { "Content-Type": "application/json; charset=utf-8" });
    res.end(responseText);
  } catch (error) {
    console.error("Transcription proxy failed:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to transcribe audio", details: error.message }));
  }
}

function guessExtension(contentType) {
  if (!contentType) return "webm";
  if (contentType.includes("wav")) return "wav";
  if (contentType.includes("mp3") || contentType.includes("mpeg")) return "mp3";
  if (contentType.includes("ogg")) return "ogg";
  if (contentType.includes("m4a")) return "m4a";
  return "webm";
}

async function handleAiResponse(req, res) {
  console.log("/api/respond request received");
  try {
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Missing OPENAI_API_KEY in environment" }));
      return;
    }

    const chunks = [];
    let totalLength = 0;
    const limit = Number.parseInt(process.env.AI_REQUEST_LIMIT ?? "1048576", 10); // 1 MB default
    for await (const chunk of req) {
      totalLength += chunk.length;
      if (totalLength > limit) {
        res.writeHead(413, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ error: "Request body too large" }));
        return;
      }
      chunks.push(chunk);
    }
    const bodyText = Buffer.concat(chunks).toString("utf8");

    let payload;
    try {
      payload = JSON.parse(bodyText);
    } catch (error) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Invalid JSON body" }));
      return;
    }

    const prompt = String(payload?.prompt ?? "").trim();
    const languageHint = String(payload?.language ?? "").trim();
    if (!prompt) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Prompt is required" }));
      return;
    }

    const model = process.env.OPENAI_RESPONSE_MODEL ?? "gpt-4o-mini";
    const systemPrompt = process.env.OPENAI_RESPONSE_SYSTEM_PROMPT?.trim();
    const fetchUrl = `${process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1"}/responses`;

    const requestBody = {
      model,
      input: [
        ...(systemPrompt
          ? [{ role: "system", content: systemPrompt }]
          : []),
        ...(languageHint
          ? [{ role: "user", content: `The user is speaking ${languageHint}.` }]
          : []),
        { role: "user", content: prompt },
      ],
    };

    console.log("Calling OpenAI response model", model);
    const response = await fetch(fetchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(requestBody),
    });

    const responseText = await response.text();
    console.log("/api/respond status", response.status);
    res.writeHead(response.status, { "Content-Type": "application/json; charset=utf-8" });
    res.end(responseText);
  } catch (error) {
    console.error("AI response proxy failed:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to fetch AI response", details: error.message }));
  }
}

async function loadEnv() {
  const envPath = join(__dirname, ".env");
  try {
    const envText = await readFile(envPath, "utf8");
    envText
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"))
      .forEach((line) => {
        const eqIndex = line.indexOf("=");
        if (eqIndex === -1) return;
        const key = line.slice(0, eqIndex).trim();
        const value = line.slice(eqIndex + 1).trim();
        if (key && !(key in process.env)) {
          process.env[key] = value;
        }
      });
  } catch (error) {
    if (error.code !== "ENOENT") {
      console.warn("Failed to load .env file:", error);
    }
  }
}

async function handleTextToSpeech(req, res) {
  console.log("/api/speech request received");
  try {
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Missing OPENAI_API_KEY in environment" }));
      return;
    }

    const chunks = [];
    let size = 0;
    const limit = Number.parseInt(process.env.AI_REQUEST_LIMIT ?? "1048576", 10);
    for await (const chunk of req) {
      size += chunk.length;
      if (size > limit) {
        res.writeHead(413, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ error: "Request body too large" }));
        return;
      }
      chunks.push(chunk);
    }

    let payload;
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch (error) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Invalid JSON body" }));
      return;
    }

    const text = String(payload?.text ?? "").trim();
    if (!text) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Text is required" }));
      return;
    }

    const model = process.env.OPENAI_TTS_MODEL ?? "gpt-4o-mini-tts";
    const voice = payload?.voice || process.env.OPENAI_TTS_VOICE || "alloy";
    const responseFormat = payload?.response_format || process.env.OPENAI_TTS_FORMAT || "mp3";
    const speed = Number.parseFloat(payload?.speed ?? process.env.OPENAI_TTS_SPEED ?? "1.0");
    const pitch = Number.parseFloat(payload?.pitch ?? process.env.OPENAI_TTS_PITCH ?? "0");

    const fetchUrl = `${process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1"}/audio/speech`;
    console.log("Calling OpenAI speech model", model, "voice", voice);
    const response = await fetch(fetchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        voice,
        input: text,
        response_format: responseFormat,
        speed: Number.isFinite(speed) ? speed : 1.0,
        pitch: Number.isFinite(pitch) ? pitch : 0,
      }),
    });

    console.log("/api/speech status", response.status);
    const arrayBuffer = await response.arrayBuffer();
    res.writeHead(response.status, {
      "Content-Type": response.headers.get("content-type") ?? `audio/${responseFormat}`,
    });
    res.end(Buffer.from(arrayBuffer));
  } catch (error) {
    console.error("Text-to-speech proxy failed:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to synthesize speech", details: error.message }));
  }
}

async function handleSpotifyConnect(requestUrl, res) {
  try {
    cleanupSpotifyState();
    const clientId = process.env.SPOTIFY_CLIENT_ID;
    if (!clientId) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Missing SPOTIFY_CLIENT_ID in environment" }));
      return;
    }

    const envRedirect = process.env.SPOTIFY_REDIRECT_URI;
    const defaultRedirect = new URL("/spotify-auth-callback", requestUrl.origin).toString();
    const redirectUri = envRedirect
      ? new URL(envRedirect, requestUrl.origin).toString()
      : defaultRedirect;
    const scopes =
      process.env.SPOTIFY_SCOPES ??
      [
        "user-read-email",
        "user-read-private",
        "user-read-playback-state",
        "user-modify-playback-state",
        "streaming",
      ].join(" ");

    const state = generateRandomBase64Url(24);
    const codeVerifier = generateRandomBase64Url(64);
    const codeChallenge = base64UrlEncode(
      createHash("sha256").update(codeVerifier, "utf8").digest()
    );

    spotifyStateStore.set(state, {
      codeVerifier,
      redirectUri,
      createdAt: Date.now(),
    });

    const authorizeUrl = new URL("https://accounts.spotify.com/authorize");
    authorizeUrl.search = new URLSearchParams({
      response_type: "code",
      client_id: clientId,
      redirect_uri: redirectUri,
      scope: scopes,
      state,
      code_challenge: codeChallenge,
      code_challenge_method: "S256",
    }).toString();

    res.writeHead(302, {
      Location: authorizeUrl.toString(),
      "Cache-Control": "no-store",
    });
    res.end();
  } catch (error) {
    console.error("Spotify connect failed:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to initiate Spotify auth" }));
  }
}

async function handleSpotifyCallback(requestUrl, res) {
  const errorParam = requestUrl.searchParams.get("error");
  const state = requestUrl.searchParams.get("state") ?? "";
  const stored = spotifyStateStore.get(state);
  spotifyStateStore.delete(state);

  if (errorParam) {
    console.error("Spotify authorization error:", errorParam);
    return respondWithSpotifyResult(
      res,
      requestUrl.origin,
      { error: errorParam },
      400
    );
  }

  if (!stored) {
    return respondWithSpotifyResult(
      res,
      requestUrl.origin,
      { error: "Invalid or expired state" },
      400
    );
  }

  const code = requestUrl.searchParams.get("code");
  if (!code) {
    return respondWithSpotifyResult(
      res,
      requestUrl.origin,
      { error: "Missing authorization code" },
      400
    );
  }

  try {
    const clientId = process.env.SPOTIFY_CLIENT_ID;
    if (!clientId) {
      throw new Error("Missing SPOTIFY_CLIENT_ID");
    }
    const clientSecret = process.env.SPOTIFY_CLIENT_SECRET;
    const tokenUrl = "https://accounts.spotify.com/api/token";
    const bodyParams = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: stored.redirectUri,
      code_verifier: stored.codeVerifier,
    });
    if (!clientSecret) {
      bodyParams.set("client_id", clientId);
    }

    const headers = {
      "Content-Type": "application/x-www-form-urlencoded",
    };
    if (clientSecret) {
      headers.Authorization = `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString(
        "base64"
      )}`;
    }

    const response = await fetch(tokenUrl, {
      method: "POST",
      headers,
      body: bodyParams.toString(),
    });

    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const error = payload?.error || `Spotify token request failed (${response.status})`;
      console.error("Spotify token exchange failed:", error, payload);
      return respondWithSpotifyResult(
        res,
        requestUrl.origin,
        { error, details: payload },
        response.status
      );
    }

    respondWithSpotifyResult(res, requestUrl.origin, { tokens: payload }, 200);
  } catch (error) {
    console.error("Spotify callback handling failed:", error);
    respondWithSpotifyResult(
      res,
      requestUrl.origin,
      { error: "Failed to complete Spotify authentication", details: error.message },
      500
    );
  }
}

function handleSpotifyConfig(res) {
  const rawPlaylistId = process.env.SPOTIFY_PLAYLIST_ID ?? null;
  const playlistId = normalizeSpotifyPlaylistId(rawPlaylistId);
  const config = {
    playlistId,
    playlistIdRaw: rawPlaylistId,
    baseUrl: process.env.PUBLIC_BASE_URL ?? null,
  };
  res.writeHead(200, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(config));
}

async function handleSpotifyRefresh(req, res) {
  try {
    const clientId = process.env.SPOTIFY_CLIENT_ID;
    const clientSecret = process.env.SPOTIFY_CLIENT_SECRET;
    if (!clientId) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Missing SPOTIFY_CLIENT_ID" }));
      return;
    }

    let bodyText = "";
    for await (const chunk of req) {
      bodyText += chunk;
    }
    let payload = null;
    try {
      payload = JSON.parse(bodyText || "{}");
    } catch (error) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Invalid JSON body" }));
      return;
    }

    const refreshToken =
      payload?.refresh_token ??
      payload?.refreshToken ??
      payload?.token ??
      payload?.refresh;
    if (!refreshToken) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "refresh_token is required" }));
      return;
    }

    const form = new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
    });

    if (!clientSecret) {
      form.set("client_id", clientId);
    }

    const headers = {
      "Content-Type": "application/x-www-form-urlencoded",
    };
    if (clientSecret) {
      headers.Authorization = `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString(
        "base64"
      )}`;
    }

    const response = await fetch("https://accounts.spotify.com/api/token", {
      method: "POST",
      headers,
      body: form.toString(),
    });

    const text = await response.text();
    res.writeHead(response.status, { "Content-Type": "application/json; charset=utf-8" });
    res.end(text);
  } catch (error) {
    console.error("Spotify refresh proxy failed:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to refresh Spotify token" }));
  }
}

async function handleNewsProxy(requestUrl, res) {
  const targetUrl = requestUrl.searchParams.get("url");
  if (!targetUrl) {
    res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Missing url parameter" }));
    return;
  }

  let remote;
  try {
    remote = new URL(targetUrl);
  } catch (error) {
    res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Invalid URL" }));
    return;
  }

  if (remote.protocol !== "http:" && remote.protocol !== "https:") {
    res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Only http and https protocols are allowed" }));
    return;
  }

  try {
    const response = await fetch(remote.toString(), {
      headers: {
        "User-Agent": "navigation-dashboard/1.0 (+https://github.com/)",
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
      redirect: "follow",
    });

    const contentType = response.headers.get("content-type") || "text/html; charset=utf-8";
    const status = response.status || 200;
    let body = await response.text();

    if (contentType.includes("text/html")) {
      const baseHref = new URL(".", remote).toString();
      const baseTag = `<base href="${baseHref}">`;
      if (/<head[^>]*>/i.test(body)) {
        body = body.replace(/<head([^>]*)>/i, `<head$1>\n${baseTag}`);
      } else {
        body = `${baseTag}\n${body}`;
      }
    }

    res.writeHead(status, {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    });
    res.end(body);
  } catch (error) {
    console.error("News proxy failed:", error);
    res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to load news article" }));
  }
}

async function handleFacebookScrape(req, res) {
  console.log("[FACEBOOK] /api/facebook/scrape request received");
  try {
    // Read request body to get URL if provided
    let requestBody = "";
    for await (const chunk of req) {
      requestBody += chunk.toString();
    }
    
    let groupUrl = process.env.FACEBOOK_GROUP_URL || "https://www.facebook.com/socialdemokratiet";
    try {
      const body = JSON.parse(requestBody);
      if (body.url) {
        groupUrl = body.url;
        console.log(`[FACEBOOK] Using URL from request: ${groupUrl}`);
      }
    } catch (e) {
      // If body parsing fails, use default URL
      console.log(`[FACEBOOK] Could not parse request body, using default URL`);
    }
    
    const email = process.env.FACEBOOK_EMAIL || null;
    const password = process.env.FACEBOOK_PASSWORD || null;
    const maxPosts = Number.parseInt(process.env.FACEBOOK_MAX_POSTS ?? "20", 10);
    
    console.log(`[FACEBOOK] Configuration:`);
    console.log(`[FACEBOOK]   Group URL: ${groupUrl}`);
    console.log(`[FACEBOOK]   Email: ${email ? email.substring(0, 3) + "***" : "not provided"}`);
    console.log(`[FACEBOOK]   Password: ${password ? "***" : "not provided"}`);
    console.log(`[FACEBOOK]   Max Posts: ${maxPosts}`);

    const scriptPath = join(__dirname, "scripts", "fetch_facebook_group.py");
    if (!existsSync(scriptPath)) {
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "Facebook scraping script not found" }));
      return;
    }

    const args = [
      scriptPath,
      "--group-url", groupUrl,
      "--max-posts", String(maxPosts),
      "--headless",
    ];

    if (email && password) {
      args.push("--email", email);
      args.push("--password", password);
    }

    console.log(`[FACEBOOK] Running scraper: python3 ${args.join(" ")}`);
    console.log(`[FACEBOOK] Working directory: ${__dirname}`);
    console.log(`[FACEBOOK] Script path: ${scriptPath}`);

    const pythonProcess = spawn("python3", args, {
      cwd: __dirname,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    pythonProcess.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    pythonProcess.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    const scrapePromise = new Promise((resolve, reject) => {
      pythonProcess.on("close", (code) => {
        console.log(`[FACEBOOK] Scraper exited with code ${code}`);
        console.log(`[FACEBOOK] stdout: ${stdout}`);
        console.log(`[FACEBOOK] stderr: ${stderr}`);
        if (code === 0) {
          resolve({ stdout, stderr });
        } else {
          const errorMsg = stderr || stdout || `Process exited with code ${code}`;
          reject(new Error(`Scraper exited with code ${code}: ${errorMsg}`));
        }
      });

      pythonProcess.on("error", (error) => {
        console.error(`[FACEBOOK] Failed to start scraper:`, error);
        reject(new Error(`Failed to start scraper: ${error.message}`));
      });
    });

    // Set a timeout of 5 minutes for scraping
    const timeoutPromise = new Promise((_, reject) => {
      setTimeout(() => {
        pythonProcess.kill("SIGTERM");
        reject(new Error("Scraping timeout after 5 minutes"));
      }, 5 * 60 * 1000);
    });

    try {
      await Promise.race([scrapePromise, timeoutPromise]);
    } catch (scrapeError) {
      console.error(`[FACEBOOK] Scraping process failed:`, scrapeError);
      res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ 
        error: "Scraping failed", 
        details: scrapeError.message,
        stdout: stdout.substring(0, 1000), // Limit output
        stderr: stderr.substring(0, 1000)
      }));
      return;
    }

    // After scraping, fetch and return the posts
    let groupId = "unknown";
    if (groupUrl.includes("/groups/")) {
      groupId = groupUrl.split("/groups/")[1].split("/")[0].split("?")[0];
    } else if (groupUrl.includes("profile.php?id=")) {
      // Extract ID from profile.php?id=XXXXX
      const match = groupUrl.match(/profile\.php\?id=(\d+)/);
      if (match) {
        groupId = match[1];
      } else {
        const parts = groupUrl.split("facebook.com/")[1]?.split("/");
        if (parts && parts.length > 0) {
          groupId = parts[0].split("?")[0];
        }
      }
    } else if (groupUrl.includes("facebook.com/")) {
      // Try to extract from page URL
      const parts = groupUrl.split("facebook.com/")[1].split("/");
      if (parts.length > 0) {
        groupId = parts[0].split("?")[0];
      }
    }

    const dataPath = join(__dirname, "data", `facebook_group_${groupId}.json`);
    console.log(`[FACEBOOK] Looking for data file at: ${dataPath}`);
    console.log(`[FACEBOOK] Group URL: ${groupUrl}, Group ID: ${groupId}`);
    console.log(`[FACEBOOK] __dirname: ${__dirname}`);

    // Check if data directory exists
    const dataDir = join(__dirname, "data");
    console.log(`[FACEBOOK] Data directory path: ${dataDir}`);
    console.log(`[FACEBOOK] Data directory exists: ${existsSync(dataDir)}`);
    
    if (existsSync(dataDir)) {
      // List files in data directory for debugging
      try {
        const { readdirSync } = await import("node:fs");
        const allFiles = readdirSync(dataDir);
        const facebookFiles = allFiles.filter(f => f.includes("facebook"));
        console.log(`[FACEBOOK] All files in data dir: ${allFiles.join(", ")}`);
        console.log(`[FACEBOOK] Facebook-related files: ${facebookFiles.join(", ")}`);
      } catch (e) {
        console.error(`[FACEBOOK] Could not list data directory: ${e.message}`);
      }
    }

    // Also try alternative group ID formats
    const alternativePaths = [
      dataPath,
      join(__dirname, "data", `facebook_group_${groupId.toLowerCase()}.json`),
      join(__dirname, "data", `facebook_group_${groupId.toUpperCase()}.json`),
    ];
    
    let foundPath = null;
    for (const altPath of alternativePaths) {
      if (existsSync(altPath)) {
        foundPath = altPath;
        console.log(`[FACEBOOK] Found file at: ${foundPath}`);
        break;
      }
    }

    if (!foundPath) {
      console.error(`[FACEBOOK] Data file not found. Tried paths: ${alternativePaths.join(", ")}`);
      console.error(`[FACEBOOK] Scraper stdout: ${stdout}`);
      console.error(`[FACEBOOK] Scraper stderr: ${stderr}`);
      res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ 
        error: "Scraping completed but no posts file found", 
        posts: [],
        details: `Expected file at: ${dataPath}. Tried: ${alternativePaths.join(", ")}`,
        groupId: groupId,
        groupUrl: groupUrl,
        stdout: stdout.substring(0, 2000),
        stderr: stderr.substring(0, 2000)
      }));
      return;
    }
    
    const fileContent = await readFile(foundPath, "utf8");
    const data = JSON.parse(fileContent);

    // Rebuild the all posts list from all party files
    // This ensures we don't lose posts when a party file is overwritten
    await rebuildAllPostsList();

    res.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(JSON.stringify(data));
  } catch (error) {
    console.error("[FACEBOOK] Scraping failed:", error);
    console.error("[FACEBOOK] Error stack:", error.stack);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ 
      error: "Failed to scrape Facebook group", 
      details: error.message,
      message: "Make sure Python dependencies are installed: pip install beautifulsoup4 selenium webdriver-manager"
    }));
  }
}

/**
 * Creates a unique key for a post to detect duplicates
 */
function createPostKey(post) {
  if (post.post_link) {
    return `link:${post.post_link}`;
  }
  if (post.post_text) {
    const textNormalized = post.post_text.toLowerCase().trim().replace(/[^\w\s]/g, '').replace(/\s+/g, ' ');
    const textWords = textNormalized.split(' ').slice(0, 50).join(' ');
    const timeKey = (post.post_time || '').toLowerCase().trim();
    return `text:${textWords}|time:${timeKey}`;
  }
  return null;
}

/**
 * Rebuilds the all posts list from all individual party files
 * This ensures we don't lose posts when a party file is overwritten
 */
async function rebuildAllPostsList() {
  const dataDir = join(__dirname, "data");
  const allPostsPath = join(dataDir, "facebook_all_posts.json");
  
  try {
    if (!existsSync(dataDir)) {
      console.log(`[FACEBOOK] Data directory doesn't exist, skipping rebuild`);
      return;
    }
    
    const fs = await import("node:fs/promises");
    const files = await fs.readdir(dataDir);
    const facebookFiles = files.filter(f => 
      f.startsWith("facebook_group_") && 
      f.endsWith(".json") && 
      f !== "facebook_all_posts.json"
    );
    
    if (facebookFiles.length === 0) {
      console.log(`[FACEBOOK] No party files found, skipping rebuild`);
      return;
    }
    
    console.log(`[FACEBOOK] Rebuilding all posts list from ${facebookFiles.length} party files`);
    
    const allPosts = [];
    const seenPostKeys = new Set();
    
    // Read all posts from all party files
    for (const file of facebookFiles) {
      const filePath = join(dataDir, file);
      try {
        const fileContent = await readFile(filePath, "utf8");
        const data = JSON.parse(fileContent);
        
        if (data.posts && Array.isArray(data.posts)) {
          console.log(`[FACEBOOK] Loading ${data.posts.length} posts from ${file}`);
          
          for (const post of data.posts) {
            const key = createPostKey(post);
            
            // Skip if we've seen this post before
            if (key && seenPostKeys.has(key)) {
              continue;
            }
            
            if (key) {
              seenPostKeys.add(key);
            }
            
            allPosts.push(post);
          }
        }
      } catch (err) {
        console.error(`[FACEBOOK] Error reading file ${file}: ${err.message}`);
      }
    }
    
    // Sort by scraped_at date (newest first)
    allPosts.sort((a, b) => {
      const dateA = new Date(a.scraped_at || 0);
      const dateB = new Date(b.scraped_at || 0);
      return dateB - dateA;
    });
    
    const allPostsData = {
      source: "all_parties",
      scrapedAt: new Date().toISOString(),
      totalPosts: allPosts.length,
      posts: allPosts,
    };
    
    await writeFile(allPostsPath, JSON.stringify(allPostsData, null, 2), "utf8");
    console.log(`[FACEBOOK] Rebuilt all posts list: ${allPosts.length} unique posts from ${facebookFiles.length} party files`);
  } catch (error) {
    console.error(`[FACEBOOK] Error rebuilding all posts list: ${error.message}`);
  }
}

async function handleFacebookPosts(req, res) {
  console.log("/api/facebook/posts request received");
  try {
    const dataDir = join(__dirname, "data");
    if (!existsSync(dataDir)) {
      res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ error: "No posts found. Please scrape first.", posts: [] }));
      return;
    }

    const allPostsPath = join(dataDir, "facebook_all_posts.json");
    let allPosts = [];
    
    // Try to use the all posts file first
    if (existsSync(allPostsPath)) {
      try {
        const fileContent = await readFile(allPostsPath, "utf8");
        const data = JSON.parse(fileContent);
        if (data.posts && Array.isArray(data.posts)) {
          allPosts = data.posts;
          console.log(`[FACEBOOK] Loaded ${allPosts.length} posts from all posts list`);
        }
      } catch (err) {
        console.error(`[FACEBOOK] Error reading all posts file: ${err.message}, falling back to individual files`);
      }
    }
    
    // If all posts file doesn't exist or is empty, combine from individual files
    if (allPosts.length === 0) {
      const fs = await import("node:fs/promises");
      const files = await fs.readdir(dataDir);
      const facebookFiles = files.filter(f => f.startsWith("facebook_group_") && f.endsWith(".json") && f !== "facebook_all_posts.json");
      
      console.log(`[FACEBOOK] Found ${facebookFiles.length} individual Facebook files: ${facebookFiles.join(", ")}`);

      if (facebookFiles.length === 0) {
        res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ error: "No posts found. Please scrape first.", posts: [] }));
        return;
      }

      // Combine all posts from all files
      const seenPostKeys = new Set(); // Track duplicates across all parties
      
      for (const file of facebookFiles) {
        const filePath = join(dataDir, file);
        try {
          const fileContent = await readFile(filePath, "utf8");
          const data = JSON.parse(fileContent);
          
          if (data.posts && Array.isArray(data.posts)) {
            console.log(`[FACEBOOK] Loading ${data.posts.length} posts from ${file}`);
            
            // Add posts, checking for duplicates across parties
            for (const post of data.posts) {
              const postKey = createPostKey(post);
              
              // Skip if we've seen this post before
              if (postKey && seenPostKeys.has(postKey)) {
                continue;
              }
              
              if (postKey) {
                seenPostKeys.add(postKey);
              }
              
              allPosts.push(post);
            }
          }
        } catch (err) {
          console.error(`[FACEBOOK] Error reading file ${file}: ${err.message}`);
        }
      }

      console.log(`[FACEBOOK] Combined ${allPosts.length} unique posts from ${facebookFiles.length} files`);
      
      // Sort by scraped_at date (newest first)
      allPosts.sort((a, b) => {
        const dateA = new Date(a.scraped_at || 0);
        const dateB = new Date(b.scraped_at || 0);
        return dateB - dateA;
      });
      
      // Save the combined list for future use
      if (allPosts.length > 0) {
        await rebuildAllPostsList();
      }
    }

    const combinedData = {
      source: "all_parties",
      scrapedAt: new Date().toISOString(),
      totalPosts: allPosts.length,
      posts: allPosts,
    };

    res.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(JSON.stringify(combinedData));
  } catch (error) {
    console.error("Failed to load Facebook posts:", error);
    res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "Failed to load Facebook posts", details: error.message }));
  }
}

function respondWithSpotifyResult(res, origin, data, status = 200) {
  const allowedOrigin = process.env.SPOTIFY_ALLOWED_ORIGIN ?? origin;
  const safePayload = JSON.stringify(data ?? {});
  const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Spotify Authentication</title>
  </head>
  <body>
    <p>Authentication flow completed. You may close this window.</p>
    <script>
      (function() {
        try {
          const payload = ${safePayload};
          if (window.opener && !window.opener.closed) {
            window.opener.postMessage(
              { type: "spotify-auth-complete", payload },
              ${JSON.stringify(allowedOrigin)}
            );
          }
        } catch (err) {
          console.error("Failed to post message to opener", err);
        }
        setTimeout(function () { window.close(); }, 1500);
      })();
    </script>
  </body>
</html>`;
  res.writeHead(status, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
  res.end(html);
}

function generateRandomBase64Url(bytes = 32) {
  return base64UrlEncode(randomBytes(bytes));
}

function base64UrlEncode(buffer) {
  return Buffer.from(buffer)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function cleanupSpotifyState() {
  const now = Date.now();
  for (const [state, entry] of spotifyStateStore.entries()) {
    if (!entry?.createdAt || now - entry.createdAt > SPOTIFY_STATE_TTL) {
      spotifyStateStore.delete(state);
    }
  }
}

async function loadTlsCredentials() {
  const keyPath = process.env.SSL_KEY_PATH;
  const certPath = process.env.SSL_CERT_PATH;
  if (!keyPath || !certPath) {
    return null;
  }

  if (!existsSync(keyPath) || !existsSync(certPath)) {
    console.warn("SSL key or cert file not found, falling back to HTTP.");
    return null;
  }

  try {
    const [key, cert] = await Promise.all([readFile(keyPath), readFile(certPath)]);
    return { key, cert };
  } catch (error) {
    console.warn("Failed to read SSL credentials, falling back to HTTP:", error);
    return null;
  }
}

const tlsCredentials = await loadTlsCredentials();
const server = tlsCredentials
  ? createHttpsServer(tlsCredentials, requestListener)
  : createHttpServer(requestListener);

server.listen(port, "0.0.0.0", () => {
  const protocol = tlsCredentials ? "https" : "http";
  console.log(`Navigation demo running on ${protocol}://0.0.0.0:${port}`);
});

