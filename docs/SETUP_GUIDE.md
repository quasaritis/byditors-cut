# Byditors Cut: the complete setup guide

Everything from unzipping to clips posting on their own, with alerts on your phone. Do the parts in order. Each one
says how long it takes and what you need. Only part 1 is required; everything else is optional and can be done later.

| Part | What you get | Time |
|---|---|---|
| 1. Install and start | The app running on your PC | 10 min (first time) |
| 2. Your first clips | Clips from any link | 5 min |
| 3. Free AI (Gemini) | Smarter picks, hooks and captions | 3 min |
| 4. Captions and music removal | Word-by-word captions, songs stripped | 5 min |
| 5. Connect YouTube | Shorts upload by themselves, on as many channels as you like | 15 min |
| 6. Connect Instagram | Reels post by themselves, on as many accounts as you like | 15 min |
| 7. Facebook and TikTok | More places to post | 15 min each |
| 8. Streamers | Lives and new uploads clipped automatically | 2 min each |
| 9. Daily autopilot | A set number of uploads a day, spread out | 1 min |
| 10. Phone alerts (Telegram) | Clips on your phone with Approve / Reject | 3 min |
| 11. Keep it running 24/7 | Never miss a live | 3 min |
| 12. Fixing problems | What each error means | |

**What you need:** a Windows 10/11 PC (Mac and Linux work too), internet, and about 5 GB of free space.
A graphics card is not required. An NVIDIA card makes captions faster.

---

## 1. Install and start

1. **Unzip** the download somewhere permanent, for example `Documents\byditors-cut`.
   Don't run it from inside the zip, and don't leave it in a temporary folder.
2. Open the folder and **double-click `start.bat`**.
   - If Windows says *"Windows protected your PC"*, click **More info → Run anyway**.
3. The first start installs everything by itself: Python, ffmpeg and the app. It takes **5–10 minutes**.
   A black window shows the progress. Later starts take a few seconds.
4. Your browser opens the app at **http://127.0.0.1:8777**.

**Keep the black window open** while you use it. You can minimise it. Closing it stops the app.
If you close the browser tab, open http://127.0.0.1:8777 again. The app keeps running in the black window.

**Mac / Linux:** open Terminal in the folder and run `./start.sh`. You need Python 3.10+ and ffmpeg
(`brew install ffmpeg` on Mac).

---

## 2. Your first clips

1. In the app, paste a video link (YouTube, Kick, Twitch or TikTok) in the box at the top. Or upload a file from your PC.
2. Next to the link box, choose whose video it is:
   - **It's my video**: your own content. Its clips can post automatically.
   - **I have permission**: the creator said yes, or you're in their clip program. Its clips can post automatically.
   - **Clip only**: anything else. Clips are made, but they wait for you and never post by themselves.
3. Press **Make clips**. A 20-minute video takes roughly 5–15 minutes, depending on your PC.
4. Finished clips appear on **Today** and in the **Clip inbox**. Each one has a verdict from the Guard:
   - **Ready to post**: passed every check.
   - **Needs a look**: something is unclear (no permission on record, music, a risky word). It waits for you.
   - **Blocked**: a clear problem (a copyrighted song, gambling promo, personal info). It only goes out if you write
     down why it's allowed.

Clips are saved in the app's `data\clips` folder as 1080 × 1920 MP4 files.

### What the editor does to every clip

- **Cuts dead air** between sentences. Screams, laughs and reactions are kept, and never more than a quarter of the
  clip is taken out.
- **Cold open**: the clip starts on its loudest 2 seconds, rewinds with a scratch, then plays from the setup.
- **Punch-in zooms** toward the face on the big moments, with a small shake on the biggest one.
- **Sound hits** under the zooms and the rewind: real recorded sounds (free CC0 sounds by Kenney). To use your own,
  put files called `boom`, `hit`, `whoosh`, `rewind` or `pop` (.mp3, .wav or .ogg) in the app's `data\sfx` folder.
- **Seamless loop**: the last quarter second dissolves into the first frame, so the replay doesn't jump.
- **Cover image**: the frame on the biggest reaction. It's used as the Instagram and TikTok cover.

Turn any of these off in **Guard & settings → Editing**.

### Fixing a clip by hand

Open a clip in the **Clip inbox**. Under the video, **Edit this clip** shows the original with 15 seconds either
side. Drag **Start** and **End** (or play it and press **Set to player**), press **Play the selection** to check,
change the hook or the layout if you like, then **Render again**. It takes about a minute.

---

## 3. Free AI with Gemini (recommended)

Without AI, clips are picked from loudness, laughs, scene cuts and chat. With AI, the app also reads what's said,
writes a hook and a caption for each platform, and flags risky moments. Google's Gemini key is free and needs no card.

1. Go to **https://aistudio.google.com/apikey** and sign in with your Google account.
2. Press **Create API key** (or copy the "Default Gemini API Key" if one is already there).
3. In Byditors Cut: **Guard & settings → AI that picks clips**.
4. **Service:** Google Gemini. **API key:** paste it.
5. Press **Find models** and pick a **flash** model, for example `gemini-3.8-flash`. Fast models are the right kind.
6. Press **Save and test**. You should see "Saved".

Other services work the same way: OpenAI, Groq, OpenRouter, Grok, Claude, or Ollama running on your PC.

**If the free Gemini key is slow.** At busy times Google's free tier can take two minutes or more to answer. The app
waits up to five minutes and then falls back to picking by loudness, so clips still come out, but without AI
hooks. If the Activity log keeps saying *"AI pick failed"*, switch to **Groq**: also free, and it answers in a few
seconds. Get a key at **https://console.groq.com/keys**, choose **Service: Groq**, paste it, press **Find models**,
pick the biggest model in the list (for example `llama-3.3-70b-versatile`) and press **Save and test**.

**Videos that already have subtitles.** If you clip an already-edited short or a video with its own subtitles,
Byditors Cut sees them and doesn't add a second set on top. It keeps the video's own subtitles and only adds the
hook, placed where it doesn't cover any text or face.

---

## 4. Captions and music removal

In **Guard & settings → Add-ons**:

- **Speech to text**: press **Install**, about 200 MB. It adds word-by-word captions and lets the AI read the
  transcript. Hindi, Hinglish and English all work. It runs on your PC, so the audio never leaves it.
- **Music removal, voice kept**: press **Install**, about 1–2 GB. It strips background songs from a clip and keeps
  the talking.
- **Song identification** (optional): a key from **https://dashboard.audd.io** names copyrighted songs so they're
  blocked for sure.
- **Moderation model** (optional): any OpenAI API key. It catches hate, sexual content and violence the word lists miss.

Installs run in the background and switch on by themselves. No restart is needed.

---

## 5. Connect YouTube (Shorts)

You make a free "app" in Google Cloud once. It's yours, and only your accounts use it. Google's pages change their
look from time to time, but the steps stay the same.

**A. Create the project and turn on YouTube**

1. Go to **https://console.cloud.google.com** and sign in with the Google account that owns your YouTube channel.
   If it asks you to accept the terms, choose your country and accept. You don't need the free trial or a card.
2. At the top, click the project picker → **New project** → name it `Byditors Cut` → **Create**.
   Make sure the new project is selected at the top.
3. Open **APIs & Services → Library**, search **YouTube Data API v3**, open it and press **Enable**.

**B. Set up the sign-in screen**

4. Open **Google Auth Platform** (search for it at the top). Press **Get started**.
5. **App information:** app name `Byditors Cut`, support email = your Gmail → **Next**.
6. **Audience:** choose **External** → **Next**.
7. **Contact information:** your Gmail → **Next**.
8. **Finish:** tick *I agree to the Google API Services: User Data Policy* → **Continue** → **Create**.

**C. Add yourself as a test user (don't skip this)**

9. In Google Auth Platform, open **Audience**. Under **Test users** press **Add users**.
10. Type the Gmail that owns your YouTube channel, press Enter, then **Save**.
11. **Check that it saved:** the list must now show your email and "1 user". If it says "0 users", add it again.
    Without this, Google shows *"Error 403: access_denied"* when you connect.

**D. Make the app's keys**

12. Open **Clients → Create client**. **Application type:** Web application. Name: `Byditors Cut (this PC)`.
13. Under **Authorised redirect URIs** press **Add URI** and paste exactly:
    `http://127.0.0.1:8777/oauth/youtube`
14. Press **Create**. A box shows your **Client ID** (ends in `.apps.googleusercontent.com`) and **Client secret**
    (starts with `GOCSPX-`). **Copy both now**, or press **Download JSON**. Google shows the secret only once.

**E. Connect**

15. In Byditors Cut: **Accounts → YouTube Shorts**. Paste the Client ID and Client secret → **Save**.
16. Press **Connect YouTube** and choose your channel's Google account.
17. Google warns *"Google hasn't verified this app"*. That's expected, because it's your own app. Click
    **Advanced → Go to Byditors Cut (unsafe)** → **Continue**, and allow access.
18. You're back in the app with your channel under YouTube Shorts.

**Important: make your uploads public.** Google keeps videos uploaded through a brand-new, unverified project
**private** until the project passes a free check called the *YouTube API Services audit*. Once uploads work, search
for **"YouTube API Services Audit and Quota Extension Form"**, fill it in for your project (say it uploads your own
clips to your own channel), and wait for Google's email. Until then, uploads arrive as private videos. You can still
make them public by hand in YouTube Studio.

**More channels:** press **Add another YouTube channel** in Accounts. Google shows its account picker: pick another
Google account, or another channel (brand account) on the same one. Each extra Google account must also be added
as a test user (step 9–11). One Google Cloud project serves all your channels.

Vertical videos up to 3 minutes count as Shorts. Each project can upload about 100 videos a day, shared by all
your channels.

---

## 6. Connect Instagram (Reels)

**Before you start:**

- Your Instagram must be a **Professional account** (Creator or Business). It's free to switch in the Instagram app:
  **Settings → Account type and tools → Switch to professional account**.
- You need a **Facebook account** for Meta's developer site. It doesn't have to be linked to your Instagram.
  **Brand-new Facebook accounts are limited for a few days.** If Meta says your account is too new, wait 3–7 days
  and try again.

**A. Become a Meta developer (one time)**

1. Go to **https://developers.facebook.com** and log in with Facebook. Press **Get started**.
2. Press **Continue** (this accepts Meta's developer terms), verify your phone number with the code Meta sends,
   confirm your email, and pick **Developer** or **Creator** as your role.

**B. Create the app**

3. Go to **https://developers.facebook.com/apps** → **Create app**.
4. **App name:** `Byditors Cut`. Contact email: yours → **Next**.
5. **Use cases:** filter by **Content management** and choose **Manage messaging & content on Instagram** → **Next**.
6. **Business:** choose a business portfolio, or *I don't want to connect a business portfolio yet* → **Next**.
7. **Requirements → Overview** → **Create app**. Re-enter your Facebook password if asked.

**C. Connect your Instagram and get a token**

8. In your new app open **Use cases → Manage messaging & content on Instagram → Customize**.
9. Open **API setup with Unified login** (sometimes called *API setup with Instagram login*).
   - If it asks for a **business portfolio**, press the portfolio button → **Create new account**, fill in a name
     (for example your brand), your name and email, and create it. Then select it.
   - If Meta says your Facebook account is too new to create one, wait a few days. Nothing else is needed.
10. Under **Generate access tokens**, press **Add account** and log in with your **Instagram** username and password
    in the window that opens. Allow the permissions it asks for.
11. Next to your Instagram account press **Generate token**, then copy the long token.
12. In Byditors Cut: **Accounts → Instagram Reels → Access token**: paste it → **Connect Instagram**.
    Your @username appears.

**More Instagram accounts:** in your Meta app press **Add account** again (step 10) for each Instagram account,
generate its token, and paste it in **Accounts → Add another Instagram account**. All of them must be Professional.

Tokens last 60 days. Byditors Cut renews them automatically while it runs. If the app is off for more than 60 days,
generate a new token the same way.

Reels must be 3 seconds to 15 minutes. Byditors Cut clips are 20–60 seconds.

---

## 7. Facebook Page and TikTok (optional)

**Facebook Reels (to a Page)**

1. In the same Meta app, add **Facebook Login for Business**, and request `pages_show_list`,
   `pages_read_engagement` and `pages_manage_posts`.
2. Open **https://developers.facebook.com/tools/explorer**, pick your app, get a **User token** with those
   permissions, then run `GET /me/accounts`. Each Page in the answer has an `id` and an `access_token`.
3. In Byditors Cut: **Accounts → Facebook Reels**: paste the **Page ID** and **Page access token**.

Facebook Reels made through the API must be 3–90 seconds.

**TikTok**

1. Go to **https://developers.tiktok.com → Manage apps → Connect an app**. Add **Login Kit** and
   **Content Posting API** (turn on **Direct Post**).
2. Set a redirect URI on a website you control, starting with `https://`. An empty page is fine.
3. Open the `.env` file in the Byditors Cut folder with Notepad and fill in `TIKTOK_CLIENT_KEY`,
   `TIKTOK_CLIENT_SECRET` and `TIKTOK_REDIRECT_URI`. Save, then restart the app.
4. **Accounts → TikTok → Open TikTok sign-in**. After you approve, copy the address you land on (it contains
   `?code=`) and paste it back in the app.

Until TikTok reviews your app, posts are visible only to you. Submit it for review when you're ready to go public.

---

## 8. Streamers: clip lives and new uploads automatically

1. Open **Streamers → Add a streamer**.
2. **Name**, and **Channels**: paste their links, one per line, for example
   `youtube.com/@name`, `kick.com/name`, `twitch.tv/name` or `tiktok.com/@name`.
3. **Rights**, the most important setting:
   - **Your content**: your own channel. Clips can post automatically.
   - **Permission** / **Clip program**: you're allowed to post their clips. Put the proof (a link to their
     clip-program rules, or your email thread) in **Proof of permission**.
   - **No permission**: clips are made and wait in the Clip inbox. They never post by themselves.
4. **Credit line**: how they're credited on every clip and caption, for example `@name on Kick`.
5. **Vertical layout**: *Decide per clip* is right for most people. It looks at every clip and picks one of these:
   - **Facecam on top, gameplay below**: gaming streams. The webcam box is found wherever it sits on screen; two
     webcams go side by side on top.
   - **Follow the speaker**: one person on camera. The frame follows them smoothly and never slices a face in half.
   - **Two people, one above the other**: two people sitting apart (podcasts, interviews).
   - **Two people, cut to whoever talks**: the same, full screen, cutting to whoever is talking like a podcast
     editor. Captions change colour with the speaker. *Decide per clip* uses this when both people talk.
   - **Whole frame on blurred fill**: nobody on camera.
   Pin one here if a streamer always streams the same way. Captions always move off faces, so they never cover a
   mouth. If one clip comes out in the wrong layout, open it in the Clip inbox, pick another under
   **Edit this clip → Layout** and press **Render again**.
6. Switches:
   - **Clip their lives**: records as soon as they go live and cuts the best moments while they stream.
   - **Clip new uploads**: new videos are downloaded and clipped. Their older videos are skipped.
   - **Auto-post clips that pass the Guard**, plus which accounts to **Post to**. This only works with Your content,
     Permission or Clip program; anything the Guard flags still waits for you.
7. **Save.** The app checks every streamer every 2 minutes.

During a live you can also press **Clip last 45 s** on Today to grab the last 45 seconds by hand.

**"Clip that"**: when the streamer says "clip that", "clip it" or "clip kar/karo", or chat spams "clip it" (Twitch
and Kick), the moment before it is clipped right away. Saying it needs Speech to text installed. To switch the
voice trigger off, set `BCUT_LIVE_VOICE_TRIGGER=0` in `.env`.

---

## 9. Daily autopilot: a set number of uploads a day

In **Accounts**, each connected account has a line: *Autopilot: upload [ ] of the best ready clips a day*.
With several channels, each one gets its own number, for example 3 a day on one channel and 5 on another.

- Type a number, for example **3**. Every day the app picks the best clips that are allowed to post (Ready to post
  and from your content or with permission, or approved by you) and spreads them through the day. With 3, they go
  out at roughly 11:20 am, 4 pm and 8:40 pm.
- **0** turns autopilot off. Allowed clips then post as soon as they're made, spaced by the minutes you set.
- **Every … min, at most … a day** on the same line sets the minimum gap between posts and a hard daily limit.
- Nothing posts between 1 am and 7 am. You can change that in `.env` (`BCUT_QUIET_HOURS`).

The **Queue** page shows everything scheduled. Each item has **Post now** and **Cancel**.

**It learns from your views.** Every 6 hours the views, likes and comments of your Instagram and YouTube posts are
read back. Clips from the streamers and hook styles that got the most views go out first, and the AI is shown your
best hooks when it writes new ones. Hooks are tested in five styles (question, shock, quote, stakes, POV): mostly the
winner so far, sometimes another. **Guard & settings → Editing** shows how each style is doing.

---

## 10. Phone alerts and control (Telegram)

Get every new clip on your phone as a video with **Approve** and **Reject** buttons, and a message when a streamer
goes live, a video posts or something fails. You can also check on the app and restart it from anywhere.

1. Install **Telegram** on your phone and open a chat with **@BotFather** (the one with the blue tick).
2. Send `/newbot`. Give it a name (anything) and a username that ends in `bot`, for example `myclipsalert_bot`.
3. BotFather replies with a **token** like `123456789:AAH…`. Copy it and keep it private.
4. In Byditors Cut: **Guard & settings → Phone alerts**: paste the token → **Connect bot**.
5. The app shows a line like **/start 123456**. Open your new bot in Telegram (tap the link in the app, or search its
   username), press **Start**, and send that exact line.
6. The bot replies **"Connected"**. Only your Telegram chat can use it from now on; anyone else is ignored.

| Command | What it does |
|---|---|
| `/status` | Is the app running, what's recording, when the next upload is |
| `/today` | Clips made today, uploads done and scheduled |
| `/review` | Sends the newest clips waiting for your OK, with Approve / Reject |
| `/restart` | Restarts the app; the bot tells you when it's back |

Good to know:

- Clips arrive as small previews (about 2 MB) so they're quick on any connection. The full-quality file is what
  gets posted.
- **Approve** follows the same rules as the app. A **Blocked** clip can't be approved from the phone: open the app and
  write why it's allowed.
- The bot only answers while your PC is on and Byditors Cut is running (see part 11).
- To stop alerts: **Phone alerts → Disconnect**. For a new phone or Telegram account, disconnect and pair again.

---

## 11. Keep it running 24/7

Byditors Cut runs on your PC, so **the PC has to be on** to catch lives and post. Chrome doesn't need to be open.

1. **Guard & settings → This PC → Start with Windows: on.** The app starts in the background when you sign in to
   Windows.
2. **Keep the PC awake while watching streamers: on.** Windows won't go to sleep while you have streamers to watch.
   The screen can still turn off.
3. In **Windows Settings → System → Power**, set *When plugged in, put my device to sleep after* to **Never**
   if your PC still sleeps.
4. If the app ever stops by itself, it restarts within 5 seconds. Problems are written to `data\logs\crash.log`.

When the PC is off, nothing is recorded and nothing posts. Scheduled uploads go out when it's back on.
Lives that happened while it was off can't be recorded, but their uploads (VODs) are still clipped.

---

## 12. Fixing problems

| What you see | What it means and what to do |
|---|---|
| "Windows protected your PC" | Click **More info → Run anyway**. It appears because the start file isn't signed. |
| The black window closes right away | Open it again. If it keeps happening, look in `data\logs\crash.log` and send it to us. |
| "already running" | The app is already open. Go to http://127.0.0.1:8777 |
| A job failed | Press **Try again** on it. If it fails again, the error text says why. |
| "Sign in to confirm you're not a bot" | YouTube is rate-limiting your connection. Wait an hour, or try another network. |
| YouTube: **Error 403: access_denied** | Your Gmail isn't a test user, or it didn't save. Do part 5, step 9–11 again. |
| YouTube: "Google hasn't verified this app" | Expected. **Advanced → Go to Byditors Cut**. |
| YouTube uploads are private | Your Google project needs the free audit. See the end of part 5. |
| Instagram: "account too new" / no portfolio | Meta limits new Facebook accounts for a few days. Wait, then continue part 6. |
| Instagram didn't accept the token | Make sure the account is Professional, then generate a new token (part 6, step 11). |
| A streamer is live but nothing records | Check that **Clip their lives** is on. The app checks every 2 minutes, and if a stream can't be opened it tries again after 15 minutes and says why in Today. |
| Clips stuck in the Clip inbox | They need your OK: no permission on record, or the Guard found something. Approve or Reject them, or use `/review` in Telegram. |
| Speech to text failed on the graphics card | It switches to the processor by itself. Slower, but it works. |
| Telegram bot doesn't answer | The PC is off or the app is closed. Or you haven't sent the `/start` code yet. |

**Updating to a new version:** download the newest zip from your account on byditorsstudio.shop, unzip it **over**
your current folder, and choose *Replace the files*. Your `data` folder and `.env` (your clips, accounts and keys)
are kept. Then start the app as usual.

**Your keys and accounts** stay in the app's folder on your PC (`.env` and `data`). Don't share those files.

Need help? Email the Byditors team from the contact page on byditorsstudio.shop.
