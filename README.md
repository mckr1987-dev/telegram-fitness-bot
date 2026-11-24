# Telegram Fitness Bot

A private, single-user bot for your weekly workouts, diet versions, and daily weight. Requires Python 3.10 or newer. There are no additional packages.

## Set up locally

1. Open the official [@BotFather](https://t.me/BotFather) chat in Telegram, send /newbot, and follow the prompts.
2. From the bot folder in PowerShell, enter your token at the prompt and start the bot.

       $env:TELEGRAM_BOT_TOKEN = Read-Host "BotFather token" -MaskInput
       python bot.py

3. Open your bot in Telegram, send /id, and copy the number it returns. Stop the local process with Ctrl+C. Set your owner ID and restart in the same PowerShell session.

       $env:TELEGRAM_OWNER_ID = Read-Host "Your Telegram user ID"
       python bot.py

   If your PowerShell does not support -MaskInput, open PowerShell 7. Keep the token private. The bot only accepts fitness requests in a private chat from the configured numeric owner ID. Run one copy per token. Keep the process running while you want the bot online.

## Save your plans and log a weight

| Send | What the bot does |
|---|---|
| /setworkout Monday: Treadmill 30 min; mobility 10 min | Saves Monday's recurring workout. Repeat for each weekday you want to schedule. |
| Today workout pls | Shows the schedule for the current weekday in India. |
| /week | Shows the whole weekly schedule. |
| Update my diet from today | Asks you to paste your full plan in one message, with meal names and quantities. The next free-text message is saved as the diet (even if it mentions workouts or weight), unless it is very short. The request expires after 30 minutes. |
| Give me present diet chart | Shows your latest effective diet. |
| /diet_history | Lists the last 10 revisions. Use the displayed version command to open a specific chart. |
| Today weight is 74.5 | Saves your weight in kg for that day. Repeat to correct the day's entry. |
| Yesterday weight was 74.8 | Saves the reading for yesterday. |
| /weight 2026-11-20 74.2 | Saves a reading for an earlier date. Future dates are rejected. |
| /weight_history | Shows up to 30 recent daily weights. |
| /progress | Shows latest weight, change since your starting weigh-in, and up to six weekly averages with counts. |
| How am I progressing? | Shows the overall progress view. The bot also understands requests such as 'give me last month progress' and 'show my progress for the past 10 weeks'. |
| Give my progress for last 3 weeks | Includes this week and the prior two Monday-based weeks, with weekly averages and the change between the first and latest readings. Use 1 to 52 weeks. |
| /progress start YYYY-MM-DD | Changes the progress starting point to an existing weigh-in. |
| *(send a Dr Trust results screenshot)* | Reads all body composition values with Gemini and shows them for checking. Reply `yes` to save, `no` to discard, or correct values such as `fat 19.5, bmr 1665`. Add a caption like `yesterday` or `2026-11-20` for an earlier day. The weight is also logged for /progress. |
| /body | Shows the latest body composition reading. Add a date (`/body 2026-11-20`) for an earlier one. |
| /body trend | Compares your first and latest readings over the last 4 weeks. Use `/body trend 8` for another period. |
| /body_history | Lists recent readings with weight and body fat. |
| /trend | Shows your smoothed weight trend, weekly rate over the last 4 weeks, and goal forecast. The trend also appears after each weigh-in and in /progress. |
| /goal 70 | Sets a goal weight and forecasts when you'll reach it at the current rate. `/goal clear` removes it. Losing faster than about 1% of body weight per week is flagged. |
| /done | Logs today's workout as done. Also understands "workout done", "did my workout", and `/done yesterday` or `/done 2026-11-20`. |
| /skip knee sore | Logs a skipped workout with an optional reason. Also understands "skipped workout, knee sore". |
| /adherence | Shows this week's workouts against your plan. `/adherence 4` summarises the last 4 weeks. Days whose plan starts with "Rest" don't count. |
| /coach | Asks Gemini for a short review of your week: weight trend, body composition changes, workouts and diet, with up to 3 actions. It is also sent automatically on Sundays at 7 pm IST; `/coach off` stops that. |
| /cancel | Clears a pending diet update or screenshot reading. |

The bot starts with empty plans. Paste the diet and workouts you currently follow. Dates and weekdays use India Standard Time. SQLite data survives a restart in fitness.sqlite3 beside bot.py. Stop the process before copying that file as a backup. Set FITNESS_DB_PATH to use a different existing directory.

The supported phrases and commands run locally. The bot exchanges messages with Telegram and stores the tracking data in SQLite.

## Body composition screenshots

Screenshots are read by Google Gemini, so the bot needs a Gemini API key from https://aistudio.google.com/apikey. Add `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`) to the environment file. Use a key with billing turned on: on Gemini's free tier, Google may use submitted images to improve its products. Nothing is saved until you reply `yes`, and the bot cross-checks related values (fat mass against body fat %, water weight against body water %) to catch misread digits.

To check what Gemini reads before deploying:

    $env:GEMINI_API_KEY = Read-Host "Gemini API key" -MaskInput
    python body_composition.py screenshot.jpg

## Weekly coach

The coach sends Gemini a plain-text summary of your last few weeks (weights, trend, body composition, workout log, weekly plan and current diet). It is told to use only that data, not to give medical advice, and never to suggest losing more than about 1% of body weight per week. Treat it as encouragement and a second look at your numbers, not as medical or dietetic advice.

## Run the tests

    python -m unittest discover -s tests -v

The tests use an in-memory database and never contact Telegram. GitHub Actions runs them on every push.

## Deploy on Ubuntu EC2

Copy `bot.py`, `body_composition.py`, `gemini.py`, `insights.py`, `fitness-bot.service`, and your existing `fitness.sqlite3` to the instance. Create the data directory at `/opt/fitness-bot/data`, keep the database there, and install the service file as `/etc/systemd/system/fitness-bot.service`.

Copy `fitness-bot.env.example` to `/etc/fitness-bot.env`, then edit that server-side file to enter your new Telegram token and Telegram owner ID. Keep the environment file private with root-only read permissions (`sudo chmod 600 /etc/fitness-bot.env`). The example file contains placeholders only.

Create the `fitnessbot` Linux user and grant it access to `/opt/fitness-bot` and its data directory. Then run `sudo systemctl daemon-reload`, `sudo systemctl enable --now fitness-bot`, and check `sudo systemctl status fitness-bot`. View logs with `sudo journalctl -u fitness-bot -f`.

After confirming the EC2 bot replies, stop the Windows copy so only one process polls Telegram.
