# Telegram report sharing

Telegram delivery uses the Bot API directly from the Django service. It does not require ContextForge or a Telegram webhook.

1. Create a dedicated bot using [@BotFather](https://t.me/BotFather) and save the token in the Django service's secret environment as `TELEGRAM_BOT_TOKEN`.
2. Ask each recipient to open that bot in a private chat and tap **Start**. Verify the user's identity before associating their chat with a BURO account. For a dedicated bot without a webhook, an administrator can run the following with the bot token already present in the environment. It prints only the chat IDs and Telegram usernames for private `/start` updates. Check the identity with the BURO user through an approved channel.

   ```bash
   DJANGO_SETTINGS_MODULE=buro_first.settings python - <<'PY'
   import os
   import django

   django.setup()
   from agentic_ai.telegram import bot_request

   for update in bot_request(os.environ['TELEGRAM_BOT_TOKEN'], 'getUpdates') or []:
       message = update.get('message', {})
       chat = message.get('chat', {})
       if chat.get('type') == 'private' and message.get('text', '').split()[:1] == ['/start']:
           print(chat['id'], chat.get('username', ''))
   PY
   ```

3. Set `TELEGRAM_USER_CHATS` to a JSON object mapping BURO user primary keys to verified Telegram chat IDs as strings. For example, `{"42":"123456789"}`. Each private chat can belong to one BURO account.
4. Set `TELEGRAM_REPORT_ORIGIN` to the HTTPS origin users use to sign in to BURO, such as `https://insight.example.org`. The Django service needs outbound HTTPS access to `api.telegram.org`.
5. Apply the Django migrations and restart the application with the new environment. In **Service access**, grant **Send through Telegram** to the intended users or roles.
6. Run a report in BURO Insight AI. In **Share this result**, choose **Telegram**, preview the message and confirm. Check that **Open full report** opens the saved run with the correct BURO account.

The message contains a Bangla summary and a sign-in link. Supported branch and zone reports include a chart of up to eight displayed rows. Member, employee and loan-level rows are excluded from Telegram messages. For those reports the message contains only a notification and a link. Full report snapshots follow the existing BURO retention rules. A successful response confirms Telegram accepted the message. The delivery flow does not retry automatically.
