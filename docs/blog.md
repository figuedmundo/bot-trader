video: https://youtu.be/IqvnryFzZD4?si=-FePDn_rgKs0hVuk
blog: https://www.humbledtrader.com/blog/connect-claude-to-tradingview-mcp/

# How to Connect Claude to TradingView (Complete Setup Guide)
I connected Claude to TradingView and turned my charts into an AI-powered trading assistant.

In this guide, I’ll show you exactly how I built a workflow using Claude Code and TradingView MCP to:

*   Scan premarket gappers automatically
*   Analyze charts directly from TradingView
*   Build strategy scanners
*   Backtest trading setups with Pinescript
*   Send telegram alerts straight to your phone

Everything in this tutorial uses plain-English prompts. No coding experience required. You can also copy the prompts I used directly below.

This is part 1 of the guide, [in part 2](https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/), I’ll complete the trading pipeline with our strategy using [Interactive Brokers](https://linktw.in/IBKR-HT) API.

In This Guide
-------------

*   [How to Connect Claude to Tradingview?](#How-to-Connect-Claude-to-Tradingview)
*   [What Can Claude Do With TradingView?](#what-can-claude-do-inside-tradingview)
*   [Install Claude Code & Tradingview](#Install-Claude-Code-Tradingview)
*   [Build an AI Premarket Gap Scanner](#build-ai-premarket-gap-scanner)
*   [Automate Your Trading Workflow](#Automate-Your-Trading-Workflow)
*   [Backtest Trading Strategies with Claude](#Backtest-Trading-Strategies-with-Claude)
*   [Send Trading Alerts to Telegram](#Send-Trading-Alerts-to-Telegram)
*   [FAQ](#faq)

1.  Install Claude Code
2.  Install TradingView Desktop on Mac
3.  Install the TradingView MCP connection
4.  Enable remote debugging
5.  Connect Claude to TradingView
6.  Verify the connection
7.  Start building scanners and automations

We will go through each of these steps in detail later in the post.

What Can Claude Do Inside TradingView?
--------------------------------------

Here is the list of tasks Claude and [Tradingview](https://linktw.in/TV-htweb) connection can do for traders:

*   Read watchlists and tickers
*   Switch charts and timeframes
*   Add indicators
*   Analyze support and resistance levels
*   Build scanners
*   Create PineScript strategies
*   Backtest strategies
*   Send alerts to Telegram on the phone

Step 1 — Install Claude Code
----------------------------

If you’re new to Claude, install Claude Code (the command-line version) — it’s what the scanners run on top of. The Claude chat app on your phone or browser is great for back-and-forth conversations, but for automation, scheduling, and connecting to other apps, you need the command-line version called Claude Code.

1.  Open Terminal on your Mac and paste this:

```
curl -fsSL https://claude.ai/install.sh | bash
```


2.  Sign in with a paid Claude account. You’ll need a paid plan to use Claude Code (no free tier). The $20/month Pro plan is fine to follow this tutorial and run scanners manually. I personally run **Max 20x ($200/month)** because I’m using Claude for trading, content, and a bunch of other projects, and I needed the headroom.
3.  Open Terminal and confirm it’s working:

If you see a version number, you’re good.

> 💡 **Heads up on plans**: Pro $20 is enough to follow this tutorial step by step. If you want to run the FULL automated daily setup like I do (Scanner B firing 9 times a day from 10am to 2pm), you’ll hit Pro’s usage caps before lunch. For ongoing daily production use, you’ll need at least **Max 5x at $100/month**. Start on Pro to learn it, upgrade to Max when you’re ready to deploy.

* * *

Step 2 — Install TradingView Desktop
------------------------------------

1.  [Get $15 Bonus for TradingView](https://linktw.in/TV-htweb)
2.  Pick any paid plan. The lowest tier (**Essential, $12.95/month billed annually, or $14.95 month-to-month**) is enough for the scanner workflow. If you want PineScript backtesting with bar replay, you’ll need Pro+ or higher.
3.  Download the Desktop app from your TradingView account page. Don’t skip this step — the browser version will not work with the MCP.
4.  Sign in to the desktop app and pin a starter chart (any ticker, any timeframe).

* * *

Step 3 — Install the TradingView MCP
------------------------------------

This is the bridge that lets Claude read your live charts. Credit to the developer who built it: [@Tradesdontlie on X](https://x.com/Tradesdontlie/status/2039080409581891890) — full technical breakdown of how it works in his post.

The install needs **two prompts with a quick Claude restart in between**.

**Prompt 1** — paste this into Claude Code:

```
Install the TradingView MCP server from https://github.com/tradesdontlie/tradingview-mcp. Clone it to ~/tradingview-mcp, run npm install, and register it in my Claude MCP config at user scope. Then launch TradingView with the remote debugging port enabled (port 9222). Tell me when you're done.
```


Claude will ask permission to run a few shell commands (`git clone`, `npm install`, `claude mcp add`, the TradingView launch). Just hit Approve on each one.

**Then**: quit Claude Code (Ctrl+C or close the window) and re-launch it. This restart is what loads the new MCP into your session.

**Prompt 2** — paste this in the new session:

```
Run tv_health_check and tell me if TradingView is connected.
```


If you see `cdp_connected: true` and `api_available: true`, you’re done.

If something breaks, the most common fixes:

*   TradingView wasn’t fully closed before launch. Quit it completely, then re-run the prompt.
*   Port 9222 is in use. Tell Claude: `Change the MCP debug port to 9223. Update both the MCP config and re-launch TradingView with the new port.`
*   You’re on the browser version. Re-read Step 2.

> 💡 **Important**: TradingView Desktop has to be running with an active chart tab any time you use these scanners. The “New Tab” welcome screen doesn’t count — open a real chart first.

* * *

Step 4 — Try a few basic prompts
--------------------------------

Before building the scanners, get a feel for what the MCP can do. Paste these one at a time:

```
Switch the chart to NVDA on a 5-minute timeframe and add the 200 SMA and RSI.
```


First, make sure your watchlist panel is open in TradingView (click the watchlist icon on the right side of the chart). Then paste:

```
Read my TradingView watchlist and tell me which names are up the most today.

Look at NVDA on the daily and draw the most important support and resistance levels you see, plus today's premarket high.

Give me a quick end-of-day briefing on NVDA — what happened today, key levels respected or broken, and what to watch tomorrow.
```


If those all work, you’re ready for the real workflow.

* * *

This is the foundation. Every morning before the open, this scanner pulls the biggest premarket movers, filters out junk, and tells you the news catalyst behind each gap.

Paste into Claude:

```
Build me a premarket gappers scanner as a single shell script in this directory.

DATA SOURCE: WebFetch https://finance.yahoo.com/markets/stocks/gainers/
Parse out ticker, price, gap %, volume for each row.

FILTERS:
- gap_pct > 5
- price > $3
- premarket_volume > 50000
- Keep top 10 by gap_pct descending (cap at 10 for runtime)

NEWS CATALYST: For each of the top 10, WebFetch https://www.benzinga.com/quote/{TICKER} with this exact prompt: "What recent news or catalyst is driving {TICKER} stock today? Return a one-sentence summary, then up to 2 recent headlines verbatim. Just the data — no commentary."

IMPORTANT: Do NOT use https://finance.yahoo.com/quote/{TICKER}/news/ — that endpoint returns HTTP 503 reliably. Benzinga is the working source.

OUTPUT: Save to ./premarket_gappers_YYYY-MM-DD.json with this exact schema:
{
  "scanned_at": "",
  "gappers": [
    {"rank": 1, "symbol": "AAPL", "price": 175.20, "gap_pct": 7.5, "premarket_volume": 1200000, "catalyst": "Beat Q1 earnings, raised FY guidance", "headlines": ["Apple Reports Strong Q1", "Analysts Boost Price Targets"]}
  ]
}

If a single ticker's catalyst lookup fails, set catalyst=null and headlines=[] for that ticker — do not abort the whole scan.

After saving, print a one-line summary: "Premarket Gappers: N names. Top: TICKER1 (X%) — catalyst1, TICKER2 (Y%) — catalyst2, TICKER3 (Z%) — catalyst3"

Expected runtime: 60-90 seconds. Do not run it more than once unless I ask.
```


> 💡 **Volume filter tip**: I run mine at 50K shares because below that the names are too illiquid for my account size. Start at 50K, drop to 20K if you want a wider net, raise to 100K+ if you trade larger size.

Claude will build a scanner script, fetch the data from a free source (Yahoo Finance gainers feed or similar), apply your filters, pull the news headlines, and save the output. **First run takes about 1-2 minutes — most of that is fetching the news catalyst for each name.** You’ll get a file like `premarket_gappers_2026-05-15.json` with the full list.

What the output looks like (real example from a recent run):

```
#1  POET  $20.75  +44.34%
#2  ONDS  $11.21  +26.52%
#3  FRMI  $7.37   +22.83%
#4  RDW   $13.99  +22.12%
#5  PCT   $12.39  +21.47%
... (top 30)
```


Each ticker comes with the news catalyst (earnings, FDA approval, partnership, etc.) so you can decide in 30 seconds whether it’s worth charting.

* * *

Step 6 — Automate the premarket scanner
---------------------------------------

Now you don’t want to run that manually every morning. Paste this:

```
This works great. Now I don't want to run it manually every morning. Schedule it to fire on its own every weekday at 8:30am New York time.

My laptop is sometimes asleep at that time. Don't try to keep my Mac awake — instead, if I open the laptop later in the morning (anytime before market close), have it run a catch-up scan automatically. Skip weekends, don't run twice in the same day, and don't fire if the premarket data is already stale.
```


Claude sets up a launchd job (Mac’s version of a cron) and adds a smart catchup layer so the scanner fires whenever you open your Mac in the valid window. Better than keeping the laptop awake all night — saves your battery and works whether you’re at the desk or not.

Verify it’s scheduled:

```
launchctl list | grep -i premarket
```


You should see your scanner job listed.

* * *

Step 7 — Build your strategy scanner (Scanner B)
------------------------------------------------

Premarket Scanner gives you the candidates. Strategy Scanner tells you which ones actually meet your trading setup.

The strategy I demo in the video is my **Trend Join Long** setup — I’m looking for a stock that’s gapped up with positive news, broken above premarket high intraday, and showing strength above prior-day high.

Paste into Claude:

```
Build me a Trend Join Long strategy scanner that filters tickers down to which ones meet my day-trading entry criteria right now.

TEST UNIVERSE: 3 tickers — AMD, NVDA, MU. (Small universe so the demo finishes in a reasonable time.)

PREREQ: TradingView Desktop must be running with CDP port 9222 enabled. First call tv_health_check. If cdp_connected=false OR api_available=false, stop and tell me to launch TV with: `open -a TradingView --args --remote-debugging-port=9222`. Do not proceed until I confirm.

TIME GATE: Only proceed if current time is between 10:00am and 3:30pm New York time. Otherwise save an error JSON and exit cleanly.

FOR EACH TICKER (sequential, do not parallelize):
1. chart_set_symbol to the ticker (use bare ticker like "AMD")
2. chart_set_timeframe to "D" (daily), then data_get_ohlcv count=210 → compute:
   - prev_daily_high = high of the last bar
   - prev_daily_close = close of the last bar
   - sma200 = mean of close for the last 200 bars
3. quote_get for the current intraday price → curr_px
4. chart_set_timeframe to "1" (1-minute), then data_get_ohlcv count=400 → compute:
   - pmh = max(high) of bars where today 04:00 ET ≤ bar.time < today 09:30 ET
   - today_hod = max(high) of bars where today 09:30 ET ≤ bar.time < now (excluding current bar)
5. Evaluate:
   - daily_breakout = (curr_px > prev_daily_high) AND (prev_daily_close > sma200)
   - intraday_breakout = (curr_px > pmh) AND (curr_px > today_hod)
   - Result = "PASS" if both true, else "fail_daily" or "fail_intraday"

OUTPUT: Save to ./tjl_watchlist_YYYY-MM-DD_HHMMET.json with this schema:
{
  "scanned_at": "",
  "candidates_checked": 3,
  "hits": [{"symbol": "AMD", "curr_price": 415.20, "prev_daily_high": 412.55, "sma200": 285.50, "pmh": 414.10, "today_hod": 415.50}],
  "all_results": [{"symbol": "AMD", "result": "PASS"}, {"symbol": "NVDA", "result": "fail_intraday"}, {"symbol": "MU", "result": "fail_daily"}]
}

After saving, print one line per ticker: "TICKER: PASS | fail_daily | fail_intraday — reason"

Expected runtime: 3-5 minutes total (the per-ticker MCP loop is the bottleneck — sequential by design).
```


**Important**: For this scanner to run, you need TradingView Desktop running with an active chart tab. The MCP needs a live chart to read from.

> ⏱️ **Heads up — this one’s slow**. Each per-ticker check takes 3-5 minutes (Claude loads the chart, reads daily and 1-min data, computes the SMA200, checks intraday levels). For a 10-ticker list that’s 30+ minutes of waiting. You don’t want to run it by hand — that’s exactly why we automate it in the next step.

* * *

Step 8 — Automate the strategy scanner every 30 minutes
-------------------------------------------------------

The 10am snapshot misses setups that develop later in the day. Real Trend Join Long entries happen at 10:45, 11:30, sometimes after lunch. Fix it:

```
10am is too early to only run this once. Setups happen at 10:45, 11:30, sometimes after lunch.

Fire this strategy scanner every 30 minutes from 10am to 2pm New York time.

But don't blow up my phone with 9 messages a day. Only ping me if it's the first run of the day, or if there's a new hit. Otherwise stay quiet.
```


```

```


Claude updates your launchd schedule, adds a “first-run-of-day” tracker, and gates notifications so you only hear from the bot when something new actually happens.

* * *

Step 9 — Backtest your strategy with PineScript
-----------------------------------------------

Now the question every trader asks: does this actually make money?

Time to backtest in TradingView. Paste:

```
Build a Trend Join Long strategy backtest in TradingView end-to-end:

1. Call tv_health_check. Confirm cdp_connected=true and api_available=true. If either is false, stop and tell me to launch TV with: open -a TradingView --args --remote-debugging-port=9222
2. chart_set_symbol("AMD"), chart_set_timeframe("5"), and confirm Extended Hours is enabled (the strategy needs premarket data to compute PMH).
3. Open the existing Pine slot called "Demo TJL Strategy" using pine_open with that exact name. DO NOT use pine_new — that risks overwriting other Pine slots in the library.
4. Inject the PineScript code below using pine_set_source, then pine_smart_compile. If has_errors=true, show me the error and stop — do not auto-fix without confirming.
5. Once it compiles clean, use ui_find_element with query='[title="Add to chart"]' strategy='css' to locate the Add-to-Chart button, then ui_mouse_click on its coordinates to apply the strategy.
6. The Strategy Tester populates at the bottom. The MCP's data_get_strategy_results tool returns empty (known bug) — workaround is capture_screenshot region="strategy_tester" and read the numbers off the image.
7. Report back the win rate, total trades, total P&L, and profit factor.

PINESCRIPT CODE:

[paste full Pine code from pine/trend_join_breakout_v4_no_regime.pine]
```


```

```


Claude writes the PineScript, injects it into TradingView’s Pine editor, compiles it, fixes any errors, applies it to your chart, and reads the Strategy Tester results back to you.

> 🔁 **This is iterative — don’t panic**. Pine usually takes 2-3 compile cycles before it’s clean. Claude will write code, hit a syntax or type error, read the error back from TradingView, fix it, recompile. That back-and-forth is normal — let it run, don’t interrupt.
> 
> 💡 **Reading the results sometimes takes a couple of tries**. There’s a known quirk in the MCP where the dedicated “read strategy results” tool returns empty even when the numbers are clearly on screen. Claude usually screenshots the Strategy Tester panel and reads the numbers from the image instead. Either way you’ll get the metrics — just don’t be surprised if you see Claude take a screenshot mid-flow.

Real results from my own run (on MU 5-min, ~60-day window):

*   21 trades
*   66.67% win rate
*   +$49.24 P&L
*   Profit factor 1.57
*   Max drawdown 0.74%

That’s a tradeable edge on a single ticker. Multi-symbol sweep across **a watchlist of 32 momentum names** over 30 days: 280 trades, 54.6% win rate, +$1,167 P&L, profit factor 1.59.

> ⚠️ **About the watchlist**: those sweep numbers are on a curated list of momentum names — semis, AI, energy infrastructure, high-beta consumer. I stress-tested the strategy on utilities and staples and it loses money there. This setup is designed for stocks that move, not for defensives. Run the prompt above on momentum names and you’ll get results that look like mine. Run it on a utility like SO or a staple like PG and you’ll get bad results — that’s not the strategy being broken, it’s just the wrong universe.
> 
> 🎬 **Want to see the strategy fire bar-by-bar?** TradingView’s bar replay feature is incredible for this. Step through a historical day and watch each entry, partial exit, and trail trigger in real time.

* * *

Step 10 — Test an “obvious” filter (and learn from it)
------------------------------------------------------

Most traders’ instinct: “the strategy probably works better when SPY and QQQ are also up.” Let’s test that:

```
Now I want to test whether adding an SPY/QQQ regime filter improves the strategy. Modify the SAME "Demo TJL Strategy" slot — add the filter on top of the existing entry logic — then re-run and compare to the backtest numbers you just gave me.

REGIME FILTER LOGIC:
- At the first 5-minute bar at or after 10:00am ET each day, latch a `regime_ok` flag:
  regime_ok = (SPY current close > SPY's previous daily close) AND (QQQ current close > QQQ's previous daily close)
- Use request.security to pull SPY and QQQ data. Use lookahead=barmerge.lookahead_off to avoid lookahead bias.
- Once latched, hold `regime_ok` for the rest of the day. Reset to na on new_day.
- Add `regime_ok == true` to the existing can_enter condition (currently `entry_trigger and strategy.position_size == 0 and not at_force_close`).
- Visual: add a faint red background tint on regime-fail days so it's obvious when the filter is gating us out.

STEPS:
1. pine_get_source to read the current "Demo TJL Strategy" code
2. Splice in the regime filter logic (don't rewrite the whole strategy — just add the SPY/QQQ requests, the regime_ok latch, and modify can_enter)
3. pine_set_source with the modified version
4. pine_smart_compile. If has_errors=true, show me the error and stop.
5. Once clean, the strategy auto-reruns on the chart (no need to click Add to chart again — it's already applied).
6. capture_screenshot region="strategy_tester" to grab the new numbers.

REPORT BACK in this exact format:

| Metric | Without regime (v1) | With regime (v2) | Delta |
|---|---|---|---|
| Total trades | X | Y | ±N |
| Win rate | X% | Y% | ±N pp |
| Total P&L | $X | $Y | ±$N |
| Profit factor | X | Y | ±N |

Then tell me in plain English: did the regime filter help or hurt? By how much? Is the delta big enough to actually change my behavior?
```


```

```


My results (30-day backtest, 32 watchlist tickers):


|Metric       |With Regime Filter|Without Regime Filter|
|-------------|------------------|---------------------|
|Trades       |157               |280                  |
|Win rate     |54.1%             |54.6%                |
|Total P&L    |+$381             |+$1,167              |
|Profit factor|1.28              |1.59                 |


The regime filter actually hurt. April was a net LOSS with the filter on (-$123) vs +$593 without. The “obvious” addition kills more winners than losers, because momentum names that break out on red-tape days are exhibiting relative strength — exactly what we want.

This is the lesson: **test everything, even the obvious stuff**. Half the things you’d assume help your strategy are actually hurting it.

* * *

Step 11 — Send results straight to your phone with Telegram
-----------------------------------------------------------

You don’t want to sit at your laptop refreshing JSON files. Get the alerts on your phone.

**Pre-step — get your bot token and chat ID:**

1.  Open Telegram and message **@BotFather**. Send `/newbot`, follow the prompts, and copy the bot token it gives you (looks like `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).
2.  BotFather will give you a `t.me/<your_bot_name>` link. Click it to open your new bot in Telegram, then send `/start` so the bot is allowed to message you back.
3.  In your browser, visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` (paste your token in place of `<YOUR_TOKEN>`). Look for `"chat":{"id":` — that number is your chat ID.

> 🔒 **Security**: your bot token is a password — anyone who has it can send messages as your bot. Don’t paste it into shared Claude chats, don’t commit it to GitHub, don’t post it in screenshots. Store it in a password manager.

Then paste this into Claude:

```
Wire up Telegram notifications for both scanners.

CREDENTIALS: I have my bot token and chat ID in .env in this directory. Read them (they're TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). Do NOT print the token value to stdout or any file that gets committed.

MODIFY both scanner scripts (premarket gappers + tjl strategy) to send Telegram messages after saving their JSON output:

Format for Scanner A (premarket gappers):
📊 *Premarket Gappers* — YYYY-MM-DD
• TICKER $price +gap% — catalyst sentence
• TICKER $price +gap% — catalyst sentence
... (one bullet per gapper)
If catalyst is null, omit the dash and catalyst portion.

Format for Scanner B (TJL hits):
🎯 *TJL Watchlist* — HH:MM ET
• TICKER @ $price (PMH $X, prev_high $Y, SMA200 $Z)
... (one bullet per hit)
If 0 hits: body is "No TJL hits this run."

SEND VIA CURL:
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d chat_id="${TELEGRAM_CHAT_ID}" \
  --data-urlencode text="" \
  -d parse_mode=Markdown

GATING:
- Scanner A: send on every run (it fires once per day, low volume)
- Scanner B: send only on first run of day, new hit, or error (per the gating rules from before)
- On any curl failure, log to stdout but don't fail the scan

After modifying both scripts, run Scanner A once to verify the Telegram message lands on my phone.
```


```

```


You can also use email, Discord, Slack, or SMS instead of Telegram. The setup pattern is the same — give Claude the credentials, tell it the format, and it wires the rest. (WhatsApp is technically possible but requires the WhatsApp Business API, which means business verification through Meta and a paid carrier like Twilio. Way more friction than Telegram. Skip WhatsApp unless you really need it.)

> 💡 **Bonus**: Have Claude also send a one-line market summary at the same time so you can check the broader tape at a glance.

* * *

Step 12 — Connect to a paper trading account
--------------------------------------------

The next step is [connecting this to a broker](https://www.humbledtrader.com/blog/ai-trading-bot-claude-ibkr/) so the AI can actually execute paper trades — no manual order entry. I’ve been forward-testing with [Interactive Brokers (IBKR) paper](https://linktw.in/IBKR-HT) for the past few weeks, and it’s working but it’s far from plug-and-play. There’s a whole layer of safety checks, position sizing rules, and daily limits to wire up first.

Full walkthrough is in the next video — subscribe on YouTube so you don’t miss it.

> 🚨 **Don’t connect this to a live account until you’ve tested it on paper for at least a month.** Same rule applies to any automation — if something goes wrong, you want to lose pretend money, not real money.

* * *

FAQ
---

**Do I need to know how to code?**  
No. Every prompt above is plain English. Claude does the coding part. You’ll need to be comfortable copying commands into a terminal and approving permission prompts, but that’s it.

**Will this work on Windows?**  
The MCP itself works on Windows — [TradingView](https://linktw.in/TV-htweb) Desktop and the debug port are cross-platform. What’s Mac-specific in this setup is the auto-scheduling layer (I use macOS launchd; on Windows you’d use Task Scheduler instead). Manual runs would work fine on Windows. I’ll cover the Windows version in a follow-up video.

**Does this place trades for me?**  
No. Reading and analyzing only. Execution is a separate setup (covered in the next video).

**Is my data safe?**  
Mostly yes. The MCP runs locally — it talks to TradingView Desktop on your own machine over port 9222. Your TradingView account credentials are never shared with Claude or Anthropic. Chart screenshots, strategy code, and watchlists stay on your machine.

One thing to know: anything Claude reasons over (like “analyze this OHLCV data”) passes through Anthropic’s API, same as any normal Claude prompt. That’s how the AI works at all — your conversation with Claude is sent to Anthropic, not run locally. If you’re handling sensitive trade signals you don’t want a cloud LLM to see, that matters.

**How much does the whole stack cost?**  
Two costs to think about:

*   **To follow this tutorial and run scanners manually**: TradingView Essential ($12.95/mo annually) + Claude Pro ($20/mo) = ~$33/mo. Good for testing.
*   **To run the full automated daily setup like I do**: TradingView Essential ($12.95/mo) + Claude Max 5x ($100/mo) = ~$113/mo. The Max plan is needed because Scanner B fires 9 times a day and Pro hits its usage caps before lunch.

If you want deep historical PineScript backtesting (multi-year), TradingView Premium is ~$50/mo instead of Essential.

The MCP server itself is free and open source.

**Can I use ChatGPT or Gemini instead?**  
I built this with Claude Code. OpenAI’s Codex CLI also supports MCP servers and could probably run a similar setup, but I haven’t tested it for this exact stack. The consumer chat apps (ChatGPT.com, claude.ai, gemini.google.com) don’t support local MCP servers yet — you need a coding agent that runs on your machine.

**Will TradingView updates break this?**  
Possibly. The MCP uses TradingView’s internal debug interface, which isn’t an officially supported API. If you want stability, pin your TradingView Desktop version once it’s working.

**Can I use a different strategy than Trend Join Long?**  
Absolutely. Replace the criteria in the Scanner B prompt and the PineScript prompt with your own setup. The whole pipeline is the same.

**Is this allowed under TradingView’s terms of service?**  
Reading data from your own paid TradingView Desktop app on your own machine for personal use is fine — the MCP is doing what you’d be doing manually (looking at your charts, copying numbers), just automated. What’s NOT okay: redistributing the data, running a paid signals service off it, or scraping data you haven’t paid for. If you’re building anything commercial off this, check TradingView’s TOS directly or contact their support. (I’m not a lawyer, this isn’t legal advice, etc.)