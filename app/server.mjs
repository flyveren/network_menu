import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const publicRoot = __dirname;
const port = Number.parseInt(process.env.PORT ?? "6666", 10);

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml; charset=utf-8",
  ".ico": "image/x-icon",
  ".mp4": "video/mp4",
};

const server = createServer(async (req, res) => {
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
});

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

  return {
    source: contributionsUrl,
    fetchedAt: new Date().toISOString(),
    contributions,
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
  return fallbackPayload;
}

server.listen(port, "0.0.0.0", () => {
  console.log(`Navigation demo running on http://0.0.0.0:${port}`);
});

