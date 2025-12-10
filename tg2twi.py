import logging
import os
import time
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import pytwitter
from dotenv import load_dotenv
import traceback
import sys
from telegram.request import HTTPXRequest
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from telegram.error import NetworkError

# --- NOVOS IMPORTS PARA O SERVIDOR FALSO ---
import http.server
import socketserver
import threading
# -------------------------------------------

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)  # Set WARNING level for httpx

# Tokens from .env
TWITTER_CONSUMER_KEY = os.getenv('TWITTER_CONSUMER_KEY')
TWITTER_CONSUMER_SECRET = os.getenv('TWITTER_CONSUMER_SECRET')
TWITTER_ACCESS_TOKEN = os.getenv('TWITTER_ACCESS_TOKEN')
TWITTER_ACCESS_TOKEN_SECRET = os.getenv('TWITTER_ACCESS_TOKEN_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
MAX_TWEET_LENGTH = 280

# Initialize Twitter API
twitter_api = pytwitter.Api(
    consumer_key=TWITTER_CONSUMER_KEY,
    consumer_secret=TWITTER_CONSUMER_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_secret=TWITTER_ACCESS_TOKEN_SECRET,
)

# --- FUNÇÃO DO SERVIDOR DE HEALTH CHECK ---
def start_health_check():
    """Inicia um servidor web simples para satisfazer a verificação de porta do Render."""
    try:
        # O Render define a variável PORT automaticamente
        port = int(os.environ.get("PORT", 8080))
        
        class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"Bot is running!")
            
            # Remove logs de acesso do console para não poluir
            def log_message(self, format, *args):
                pass

        # Cria e inicia o servidor
        with socketserver.TCPServer(("0.0.0.0", port), HealthCheckHandler) as httpd:
            logger.info(f"Health check server listening on port {port}")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health check server: {e}")
# ------------------------------------------

# Function to post to Twitter
def post_to_twitter(text, post_id):
    try:
        # Create a link to the post on Telegram
        telegram_post_link = f"https://t.me/{TELEGRAM_CHANNEL_USERNAME}/{post_id}"
        link_length = len(telegram_post_link) + 5  # Adding buffer for "\n\n"
        allowed_text_length = MAX_TWEET_LENGTH - link_length

        # Remove @TELEGRAM_CHANNEL_USERNAME from the text
        clean_text = text.replace(f"@{TELEGRAM_CHANNEL_USERNAME}", "").strip()

        # Trim text if it exceeds the allowed length
        if len(clean_text) > allowed_text_length:
            clean_text = clean_text[:allowed_text_length - 1] + "…"  # Add ellipsis

        # Final tweet text
        tweet_text = f"{clean_text}\n\n{telegram_post_link}"
        twitter_api.create_tweet(text=tweet_text)

        logger.info(f"Posted to Twitter: {tweet_text}")
    except Exception as e:
        logger.error(f"Error posting to Twitter: {e}")
        raise


# Function to handle new messages in the Telegram channel
async def handle_new_message(update, context):
    try:
        # Check for channel_post and text
        if not update.channel_post or not update.channel_post.text:
            logger.warning(f"Received an update with no text in channel_post. {update}")
            return

        message = update.channel_post.text
        message_id = update.channel_post.message_id
        logger.info(f"Received message from channel: {message}")
        post_to_twitter(message, message_id)
    except Exception as e:
        logger.error(f"Error in handle_new_message: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)

# Retry logic for Telegram API calls
@retry(
    stop=stop_after_attempt(5),  # Stop after 5 attempts
    wait=wait_exponential(multiplier=1, min=2, max=10),  # Exponential backoff
    retry=retry_if_exception_type(NetworkError)  # Retry only for network errors
)
async def get_updates_with_retry(bot):
    return await bot.get_updates()

# Main bot logic with error handler
def main():
    # --- INICIA O SERVIDOR EM SEGUNDO PLANO ---
    # Isso impede que o Render mate o processo por falta de porta aberta
    health_thread = threading.Thread(target=start_health_check, daemon=True)
    health_thread.start()
    # ------------------------------------------

    while True:
        try:
            request = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0)
            application = Application.builder().token(TELEGRAM_TOKEN).request(request).build()
            # Handler for text messages
            message_handler = MessageHandler(filters.ChatType.CHANNEL, handle_new_message)
            application.add_handler(message_handler)
            application.add_error_handler(error_handler)

            # Replace polling loop to use the retry logic
            async def polling_loop():
                while True:
                    try:
                        updates = await get_updates_with_retry(application.bot)
                        for update in updates:
                            await application.process_update(update)
                    except Exception as e:
                        logger.error(f"Error during polling loop: {e}", exc_info=True)
                        time.sleep(5)  # Avoid spamming on repeated failures

            # Start the polling loop
            application.run_polling(poll_interval=1.0, close_loop=False)


        except Exception as e:
            logger.error(f"Critical error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(5)  # Delay before restarting

        except KeyboardInterrupt:
            application.stop_running()
            application.stop()
            logger.info("Bot stopped by user.")
            sys.exit(1)

if __name__ == '__main__':
    main()
