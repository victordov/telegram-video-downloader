"""
Run the Telegram bot with proper error handling and setup instructions
"""

import os
import sys
from telegram_bot import TelegramVideoBot


def check_requirements():
    """Check if all requirements are met"""
    print("🔍 Checking requirements...")
    
    # Check if .env file exists
    if not os.path.exists('.env'):
        print("❌ .env file not found!")
        print("📝 Please create a .env file with your bot token:")
        print("   1. Copy .env.example to .env")
        print("   2. Edit .env and add your TELEGRAM_BOT_TOKEN")
        print("   3. Get a bot token from @BotFather on Telegram")
        return False
    
    # Check if token is set
    from dotenv import load_dotenv
    load_dotenv()
    
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token or token == 'your_bot_token_here':
        print("❌ TELEGRAM_BOT_TOKEN not set properly!")
        print("📝 Please edit .env file and set a valid bot token")
        return False
    
    print("✅ Requirements check passed!")
    return True


def main():
    """Main function with setup checks"""
    print("🤖 Telegram Video Downloader Bot")
    print("=" * 40)
    
    if not check_requirements():
        print("\n💡 Setup Instructions:")
        print("1. Create a bot with @BotFather on Telegram")
        print("2. Copy the bot token")
        print("3. Create .env file: cp .env.example .env")
        print("4. Edit .env and add your token")
        print("5. Run this script again")
        sys.exit(1)
    
    print("\n🚀 Starting bot...")
    
    try:
        from dotenv import load_dotenv
        load_dotenv()
        
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        bot = TelegramVideoBot(token)
        bot.run()
        
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Error starting bot: {e}")
        print("💡 Make sure your bot token is valid and you have internet connection")


if __name__ == "__main__":
    main()

