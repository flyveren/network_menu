import { createServer as createHttpServer } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { Buffer } from "node:buffer";

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

const requestListener = async (req, res) => {
  try {
    const requestUrl = new URL(req.url ?? "/", "http://localhost");

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

    if (requestUrl.pathname === "/favicon.ico") {
      res.writeHead(204);
      res.end();
      return;
    }

    const sanitizedPath = normalize(requestUrl.pathname).replace(/^\.\.(\/|\\)/g, "");
    const filePath = sanitizedPath.endsWith("/")
      ? "index.html"
      : sanitizedPath.replace(/^\//, "");

    const absolutePath = join(publicRoot, filePath);
    const content = await readFile(absolutePath);
    const mimeType =
      mimeTypes[extname(absolutePath).toLowerCase()] ?? "application/octet-stream";

    res.writeHead(200, {
      "Content-Type": mimeType,
      "Cache-Control": "no-cache",
    });
    res.end(content);
  } catch (error) {
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

