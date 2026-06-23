const fs = require('node:fs/promises');
const path = require('node:path');

const rootDir = path.resolve(__dirname, '..');
const yahooGainersPageUrl = 'https://finance.yahoo.com/markets/stocks/gainers/';
const yahooScreenerUrls = [
  'https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?count=100&formatted=false&scrIds=day_gainers&lang=en-US&region=US&corsDomain=finance.yahoo.com',
  'https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved?count=100&formatted=false&scrIds=day_gainers&lang=en-US&region=US&corsDomain=finance.yahoo.com',
];
const maxResults = 10;
const requestTimeoutMs = 15000;

const headers = {
  'accept': 'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'accept-language': 'en-US,en;q=0.9',
  'cache-control': 'no-cache',
  'pragma': 'no-cache',
  'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36',
};

function todayIsoDate() {
  return new Date().toISOString().slice(0, 10);
}

function outputPathForToday() {
  return path.join(rootDir, `premarket_gappers_${todayIsoDate()}.json`);
}

function asNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }

  if (typeof value !== 'string') {
    return null;
  }

  const normalized = value.replace(/[$,%+\s]/g, '').replace(/,/g, '');
  const parsed = Number.parseFloat(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function asVolume(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.trunc(value);
  }

  if (typeof value !== 'string') {
    return null;
  }

  const trimmed = value.trim().toUpperCase().replace(/,/g, '');
  const match = trimmed.match(/^([0-9]*\.?[0-9]+)\s*([KMB])?$/);
  if (!match) {
    return null;
  }

  const [, rawNumber, suffix] = match;
  const multiplier = suffix === 'B' ? 1_000_000_000 : suffix === 'M' ? 1_000_000 : suffix === 'K' ? 1_000 : 1;
  return Math.trunc(Number.parseFloat(rawNumber) * multiplier);
}

function decodeHtml(value) {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
}

function stripHtml(value) {
  return decodeHtml(value.replace(/<script[\s\S]*?<\/script>/gi, ' ').replace(/<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
}

async function fetchText(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), requestTimeoutMs);

  try {
    const response = await fetch(url, {
      headers,
      signal: controller.signal,
      ...options,
    });

    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }

    return await response.text();
  } finally {
    clearTimeout(timeout);
  }
}

function parseYahooQuote(rawQuote) {
  const symbol = rawQuote.symbol;
  const price = asNumber(rawQuote.regularMarketPrice?.raw ?? rawQuote.regularMarketPrice);
  const gapPct = asNumber(rawQuote.regularMarketChangePercent?.raw ?? rawQuote.regularMarketChangePercent);
  const premarketVolume = asVolume(rawQuote.regularMarketVolume?.raw ?? rawQuote.regularMarketVolume);

  if (!symbol || price === null || gapPct === null || premarketVolume === null) {
    return null;
  }

  return {
    symbol,
    price,
    gap_pct: gapPct,
    premarket_volume: premarketVolume,
  };
}

async function fetchYahooGainers() {
  const errors = [];

  for (const url of yahooScreenerUrls) {
    try {
      const body = await fetchText(url, {
        headers: {
          ...headers,
          'accept': 'application/json',
          'referer': yahooGainersPageUrl,
        },
      });
      const payload = JSON.parse(body);
      const quotes = payload.finance?.result?.[0]?.quotes;

      if (!Array.isArray(quotes)) {
        throw new Error('Yahoo response did not include finance.result[0].quotes');
      }

      return quotes
        .map(parseYahooQuote)
        .filter(Boolean)
        .filter((quote) => quote.gap_pct > 5 && quote.price > 3 && quote.premarket_volume > 50000)
        .sort((left, right) => right.gap_pct - left.gap_pct)
        .slice(0, maxResults);
    } catch (error) {
      errors.push(`${url}: ${error.message}`);
    }
  }

  throw new Error(`Yahoo gainers source failed (${errors.join('; ')})`);
}

function extractHeadlines(html, symbol) {
  const text = stripHtml(html);
  const sentences = text
    .split(/(?<=[.!?])\s+/)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence.length > 20 && sentence.length < 220);

  const symbolRegex = new RegExp(`\\b${symbol.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i');
  const marketKeywords = /stock|shares|trading|price|analyst|earnings|revenue|guidance|upgrade|downgrade|FDA|merger|acquisition|offering|contract|partnership|approval|results/i;

  const candidates = sentences.filter((sentence) => symbolRegex.test(sentence) || marketKeywords.test(sentence));
  return [...new Set(candidates)].slice(0, 2);
}

function extractCatalyst(headlines, symbol) {
  if (headlines.length === 0) {
    return null;
  }

  const first = headlines[0].replace(/\s+/g, ' ').trim();
  if (!first) {
    return null;
  }

  return first.includes(symbol) ? first : `${symbol}: ${first}`;
}

async function fetchCatalyst(symbol) {
  try {
    const url = `https://www.benzinga.com/quote/${encodeURIComponent(symbol)}`;
    const html = await fetchText(url, {
      headers: {
        ...headers,
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      },
    });
    const headlines = extractHeadlines(html, symbol);

    return {
      catalyst: extractCatalyst(headlines, symbol),
      headlines,
    };
  } catch (error) {
    return {
      catalyst: null,
      headlines: [],
    };
  }
}

function formatPercent(value) {
  return `${Number(value).toFixed(2).replace(/\.00$/, '')}%`;
}

function formatSummary(gappers) {
  const top = gappers.slice(0, 3).map((gapper) => `${gapper.symbol} (${formatPercent(gapper.gap_pct)}) — ${gapper.catalyst ?? 'no catalyst found'}`);
  return `Premarket Gappers: ${gappers.length} names. Top: ${top.join(', ')}`;
}

async function main() {
  const filtered = await fetchYahooGainers();
  const enriched = [];

  for (const [index, gapper] of filtered.entries()) {
    const catalyst = await fetchCatalyst(gapper.symbol);
    enriched.push({
      rank: index + 1,
      symbol: gapper.symbol,
      price: gapper.price,
      gap_pct: gapper.gap_pct,
      premarket_volume: gapper.premarket_volume,
      catalyst: catalyst.catalyst,
      headlines: catalyst.headlines,
    });
  }

  const output = {
    scanned_at: new Date().toISOString(),
    gappers: enriched,
  };

  await fs.writeFile(outputPathForToday(), `${JSON.stringify(output, null, 2)}\n`);
  console.log(formatSummary(enriched));
}

main().catch(async (error) => {
  const output = {
    scanned_at: new Date().toISOString(),
    gappers: [],
  };

  await fs.writeFile(outputPathForToday(), `${JSON.stringify(output, null, 2)}\n`);
  console.error(`Premarket gappers scan failed: ${error.message}`);
  console.log('Premarket Gappers: 0 names. Top:');
  process.exitCode = 1;
});
