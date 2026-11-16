# Connecting your accounts

> The full click-by-click version, with what to expect at each step and fixes for common errors, is in [SETUP_GUIDE.md](SETUP_GUIDE.md) parts 5–7.

Byditors Cut posts through each platform's official API. You create a free developer app once per platform; it's yours, and only your accounts use it. Exact menu names move around; the platform docs linked below are the source of truth.

## Instagram Reels

Needs an Instagram **Professional** account (Creator or Business). Switch for free in the Instagram app: Settings → Account type and tools.

1. Go to [developers.facebook.com](https://developers.facebook.com/apps) → **Create app** → use case **Manage messaging & content on Instagram**.
2. In the app: **Instagram → API setup with Instagram login**. Add your Instagram account as a tester (accept the invite in Instagram → Settings → Website permissions → Apps and websites).
3. Press **Generate token** next to your account. Make sure the permissions include `instagram_business_basic` and `instagram_business_content_publish`.
4. Paste the token in Byditors Cut → **Accounts → Instagram Reels**.

Tokens last 60 days; Byditors Cut renews them automatically. Docs: [Content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing).

## YouTube Shorts

1. [console.cloud.google.com](https://console.cloud.google.com) → new project → **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **Google Auth Platform → Get started**: External. Then **Audience → Test users → Add users**: your Gmail, and check it saved.
3. **Credentials → Create credentials → OAuth client ID → Web application**. Authorised redirect URI: `http://127.0.0.1:8777/oauth/youtube`
4. Paste the client ID and secret in **Accounts → YouTube Shorts** and press **Connect YouTube** (or put them in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`).

Google keeps uploads from unverified API projects **private** until the project passes its audit. Request the audit from the API's quota page once things work. Videos up to 3 minutes in vertical format are shown as Shorts. Uploads now have their own quota: by default about 100 uploads a day per project (older guides that say 1,600 units, or 6 a day, are out of date).

## TikTok

1. [developers.tiktok.com](https://developers.tiktok.com) → **Manage apps → Connect an app**. Add **Login Kit** and **Content Posting API** (turn on Direct Post).
2. Set a redirect URI on a site you control (TikTok requires `https`), for example `https://byditorsstudio.shop/tiktok`. The page can be empty.
3. Put the client key, secret and that redirect URI in `.env` (`TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`) and restart.
4. **Accounts → TikTok → Open TikTok sign-in**. After you approve, copy the address you land on (it contains `?code=`) and paste it back.

Until TikTok audits your app, posts are visible **only to you**. Submit the app for review when you're ready to go public. Docs: [Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post).

## Facebook Reels (a Page)

1. In the same Meta app as Instagram, add **Facebook Login for Business** and request `pages_show_list`, `pages_read_engagement` and `pages_manage_posts`.
2. In the [Graph API Explorer](https://developers.facebook.com/tools/explorer/), pick your app, get a **User token** with those permissions, then call `GET /me/accounts`. Each Page in the list has an `id` and an `access_token`.
3. Paste the Page ID and the Page access token in **Accounts → Facebook Reels**.

Reels via the API must be 3–90 seconds. Docs: [Reels publishing](https://developers.facebook.com/docs/video-api/guides/reels-publishing).

## Kick and Twitch (optional, for watching streamers)

Live checks work without keys (through yt-dlp). Official keys make them faster and more reliable:

- **Kick:** [dev.kick.com](https://dev.kick.com) → create an app → `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`
- **Twitch:** [dev.twitch.tv/console](https://dev.twitch.tv/console) → register an app → `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`

Chat is read anonymously and needs no keys.
