# Phone alerts and control (Telegram)

Get every new clip on your phone as a video with **Approve** and **Reject** buttons, plus a message when a streamer
goes live, a new upload is clipped, a video is posted, or something fails. You can also check on the app and restart it
from anywhere.

It uses your own free Telegram bot. Nothing on your PC is opened to the internet: the app only talks out to Telegram.

## Set it up (2 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
   BotFather replies with a **token** like `123456789:AA…`. Keep it private.
2. In Byditors Cut, open **Guard & settings → Phone alerts**, paste the token and press **Connect bot**.
3. The app shows a pairing code. Open your bot in Telegram and send it, for example `/start 123456`.
   The bot replies **Connected**. From now on only your Telegram chat can use it; anyone else is ignored.

Or put the token in `.env` as `BCUT_TELEGRAM_TOKEN=…` and restart the app, then do step 3.

## Commands

| Command | What it does |
|---|---|
| `/status` | Is the app running, what it's recording, the next upload |
| `/today` | Clips made, uploads done and scheduled today |
| `/review` | Sends the newest clips waiting for your OK, with Approve/Reject |
| `/restart` | Restarts the app (loads updates); the bot tells you when it's back |

## Good to know

- Clips arrive as small previews (about 2 MB) so they're quick on any connection. The full-quality file is what gets posted.
- **Approve** follows the same rules as the dashboard: a clip the Guard **blocked** can't be approved from the phone.
  Open the app and write why it's allowed.
- The bot only answers while your PC is on and Byditors Cut is running. Turn on **Start with Windows** in Settings.
- To stop alerts: Settings → Phone alerts → **Disconnect**. To move to a new phone or account, Disconnect and pair again.
